"""
KellyRatchet: dynamic Fractional-Kelly monitoring + ratchet engine.

Implements the 4-gate design from NotebookLM + challenger redesign:

  G1. Trade counter: n<30 locked at phi=0.25; n>=30 -> 0.40; n>=100 -> 0.50.
      Evaluated only when n CROSSES an n-milestone (30/45/60/75/100/125/150/...)
      to avoid continuous interim-analysis (peeking) inflating the
      false-promotion rate. Crossing detection tolerates multi-trade gaps
      between evals (batch closes).
  G2. Net expectancy gate: avgR_net > 0 (R is already net of the 0.12%
      round-trip taker fee by construction).
  G3. Statistical significance gate: t-stat > 1.8 AND bootstrap 90% CI lower
      bound on mean R > 0 AND median R > 0, so a fat-tailed edge cannot pass
      on tail winners alone. Bootstrap is deterministically reseeded per eval
      so identical data yields an identical CI low.
  G4. Downward ratchet:
      - lower-CUSUM in sigma units (k=0.5 sigma, h=4.5 sigma, ARL0 ~= 500):
        a breach QUARANTINES phi back to 0.25 for the next 20 deduped
        entries, then re-evaluates (transient penalty, not permanent).
        CUSUM sigma is estimated from an in-control (wins-only) baseline so
        the losing run being detected cannot inflate its own detection sigma.
        The quarantine is anchored to the breach POSITION: a new breach at a
        different position re-triggers; the same old cluster does not.
      - structural halt: avgR_net <= 0 AND t-stat <= -1.5 at n>=30 sets
        phi=0.0 (HALTED). The halt is LATCHED — it persists until a manual
        reset (`reset_halt()`), so a single favorable eval cannot silently
        un-halt a proven loser.

Hysteresis: promotion requires 3 consecutive QUALIFYING milestone evals with
t > 1.8 margin (fast-up/slow-down asymmetry is intentional). Separate counters
keep 0.40 (qualifying evals at n>=30) and 0.50 (qualifying evals at n>=100)
both reachable; a non-qualifying milestone resets both.

Partial-close dedup is QUANTITY-WEIGHTED using the stored size column:
    R_entry = sum(r_i * qty_i) / sum(qty_i)
The dedup key is (strategy, asset, side, entry_time, entry_price). Trades are
loaded per-strategy with no global cap (store.strategy_trades).

Every evaluation is appended to an in-memory `history` list (persisted) so the
ratchet's own decisions can be backtested/audited later.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Optional

import numpy as np

from src.store.sqlite import Store

# Default tier thresholds (n-milestones). Evaluated when n crosses one.
DEFAULT_MILESTONES = [30, 45, 60, 75, 100, 125, 150, 175, 200]

# CUSUM parameters (in sigma units). k = allowance (detect shift ~= 1 sigma),
# h = decision interval. ARL0 ~= 500 at k=0.5, h=4.5 for Gaussian R.
DEFAULT_CUSUM_K = 0.5
DEFAULT_CUSUM_H = 4.5

DEFAULT_T_STAT = 1.8          # promotion t-stat (with margin)
DEFAULT_T_HALT = -1.5         # structural-halt t-stat (proven loser)
DEFAULT_QUARANTINE_ENTRIES = 20
DEFAULT_BOOTSTRAP_RESAMPLES = 2000
DEFAULT_CI_LEVEL = 0.90
DEFAULT_EPOCH = "2026-07-30"


class KellyRatchet:
    """Per-strategy Kelly multiplier monitor with statistical gates."""

    def __init__(
        self,
        store: Store,
        strategy: str,
        milestones: Optional[list] = None,
        cusum_k: float = DEFAULT_CUSUM_K,
        cusum_h: float = DEFAULT_CUSUM_H,
        t_stat: float = DEFAULT_T_STAT,
        t_halt: float = DEFAULT_T_HALT,
        quarantine_entries: int = DEFAULT_QUARANTINE_ENTRIES,
        bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
        ci_level: float = DEFAULT_CI_LEVEL,
        epoch: str = DEFAULT_EPOCH,
        rng_seed: int = 42,
    ):
        self.store = store
        self.strategy = strategy
        self.milestones = sorted(milestones or DEFAULT_MILESTONES)
        self.cusum_k = cusum_k
        self.cusum_h = cusum_h
        self.t_stat = t_stat
        self.t_halt = t_halt
        self.quarantine_entries = quarantine_entries
        self.bootstrap_resamples = bootstrap_resamples
        self.ci_level = ci_level
        self.epoch = epoch
        self._rng_seed = rng_seed
        self.state_key = f"kelly_ratchet_{strategy}"

    # ------------------------------------------------------------------ state

    def _default_state(self) -> dict:
        return {
            "phi": 0.25,
            "tier": "LOCKED",           # LOCKED | RATCHET_40 | RATCHET_50 | QUARANTINED | HALTED
            "n_deduped": 0,
            "n_raw": 0,
            "confirmations": 0,         # qualifying milestone evals at n>=30 (0.40 tier)
            "confirmations_50": 0,      # qualifying milestone evals at n>=100 (0.50 tier)
            "last_milestone_reached": 0,
            "quarantine_until_n": 0,    # deduped-entry count when quarantine lifts
            "baseline_n": 0,            # CUSUM window starts here (post-quarantine reset)
            "halted": False,            # latched structural halt (manual reset only)
            "fingerprint": "",          # sample fingerprint for evaluate_if_new
            "epoch": self.epoch,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "history": [],
        }

    def _load_state(self) -> dict:
        try:
            raw = self.store.get_state(self.state_key)
            if not raw:
                return self._default_state()
            state = json.loads(raw) if isinstance(raw, str) else dict(raw)
            defaults = self._default_state()
            defaults.update(state)
            return defaults
        except Exception:
            return self._default_state()

    def _save_state(self, state: dict) -> None:
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self.store.put_state(self.state_key, json.dumps(state))
        except Exception:
            pass

    # ------------------------------------------------------------- trade data

    def _load_deduped(self) -> list[dict]:
        """Load per-strategy trades (no global cap), filter to epoch, and
        dedup partial closes with quantity-weighted R.

        Dedup key: (asset, side, entry_time, entry_price). R_entry is the
        notional-weighted average of the per-unit R multiples.
        """
        rows = self.store.strategy_trades(self.strategy)
        self._last_n_raw = len(rows)

        groups: dict[tuple, list[dict]] = {}
        for t in rows:
            et = str(t.get("exit_time") or "")
            if et and et < self.epoch:
                continue
            key = (
                t.get("asset", ""),
                t.get("side", ""),
                t.get("entry_time", ""),
                t.get("entry_price", 0),
            )
            groups.setdefault(key, []).append(t)

        entries = []
        for g in groups.values():
            total_qty = sum(float(t.get("size", 1.0) or 0.0) for t in g)
            if total_qty <= 0:
                total_qty = float(len(g))
            wsum = 0.0
            for t in g:
                qty = float(t.get("size", 1.0) or 0.0) or 1.0
                wsum += float(t.get("r_multiple", 0.0) or 0.0) * qty
            entries.append({
                "asset": g[0].get("asset", ""),
                "side": g[0].get("side", ""),
                "entry_time": g[0].get("entry_time", ""),
                "entry_price": float(g[0].get("entry_price", 0) or 0),
                "r": wsum / total_qty,
            })
        # deterministic order by entry_time for CUSUM accumulation
        entries.sort(key=lambda e: (e["entry_time"], e["entry_price"]))
        return entries

    @staticmethod
    def _sample_fingerprint(entries: list[dict]) -> str:
        """Hash of (position, asset, side, rounded R). Changes when new entries
        arrive AND when a late partial-close leg revises an existing entry's R
        (same n, different R) — so evaluate_if_new re-fires on both."""
        payload = [
            [e["entry_time"], e["asset"], e["side"], round(e["r"], 4)]
            for e in entries
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _sample_std(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        return math.sqrt(var)

    @staticmethod
    def _median(values: list[float]) -> float:
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        if n % 2 == 1:
            return s[n // 2]
        return (s[n // 2 - 1] + s[n // 2]) / 2.0

    def _t_stat(self, avg_r: float, std_r: float, n: int) -> float:
        """t = avgR / (stdR / sqrt(n)). Degenerate cases:
        std=0 -> +inf if mean>0 (pass), 0 if mean==0, -inf if mean<0 (halt)."""
        if std_r <= 1e-12 or n < 2:
            if avg_r > 0:
                return float("inf")
            if avg_r < 0:
                return float("-inf")
            return 0.0
        return avg_r / (std_r / math.sqrt(n))

    def _bootstrap_ci_low(self, values: list[float]) -> float:
        """Percentile bootstrap lower bound of mean R.

        Deterministic per eval: the RNG is reseeded from (seed, n, sample
        fingerprint), so identical data ALWAYS yields the identical CI low.
        Two evals on the same sample can never flip a promotion gate by RNG
        advance (the prior design bug the challenger flagged)."""
        if len(values) < 3:
            return float(values[0]) if values else 0.0
        arr = np.asarray(values, dtype=float)
        n = len(arr)
        seed = int(
            hashlib.sha256(
                json.dumps([round(float(v), 6) for v in sorted(values)]).encode()
            ).hexdigest()[:8], 16
        ) ^ (self._rng_seed * 7919) ^ n
        rng = np.random.default_rng(seed)
        means = np.empty(self.bootstrap_resamples)
        for i in range(self.bootstrap_resamples):
            sample = arr[rng.integers(0, n, size=n)]
            means[i] = sample.mean()
        alpha = (1.0 - self.ci_level) / 2.0
        return float(np.quantile(means, alpha))

    def _in_control_std(self, rvals: list[float]) -> float:
        """CUSUM baseline sigma.

        Wins-only σ detects decay fast (the losing run can't inflate its own
        detection sigma), but pure wins-only σ is unrealistically tight — real
        R distributions have loss-side variance, and an ultra-tight σ makes
        every loss a 10-40σ event whose CUSUM never heals. Floor the baseline
        at half the full-sample σ so the chart retains realistic scale: fast
        enough to detect, loose enough to heal after the cluster ends."""
        full_std = self._sample_std(rvals)
        wins = [r for r in rvals if r > 0]
        wstd = self._sample_std(wins) if len(wins) >= 2 else 0.0
        baseline = max(wstd, 0.5 * full_std)
        return baseline if baseline > 1e-12 else full_std

    def _cusum_breach_index(self, entries: list[dict], baseline_n: int = 0) -> Optional[int]:
        """Lower CUSUM in sigma units on the R series. Detects sustained
        downward edge decay. S = max(0, S_prev - r_std - k), breach when > h.
        Returns the series index of the LAST crossing above h (None if the
        CUSUM never crossed h at any point)."""
        baseline_n = max(0, min(baseline_n, len(entries)))
        window = entries[baseline_n:]
        rvals = [e["r"] for e in window]
        std = self._in_control_std(rvals)
        if std <= 1e-12 or len(rvals) < 5:
            return None
        s = 0.0
        last_cross = None
        for i, r in enumerate(rvals):
            # Lower CUSUM: increment = -(r/std) - k. Shrinks on profits
            # (healthy), grows on losses (detects downward edge decay).
            s = max(0.0, s - (r / std) - self.cusum_k)
            if s > self.cusum_h:
                last_cross = baseline_n + i
        return last_cross

    # ------------------------------------------------------------ evaluation

    def evaluate(self) -> dict:
        """Run one evaluation for this strategy and persist the ratchet state."""
        entries = self._load_deduped()
        n = len(entries)
        state = self._load_state()
        prev_phi = state["phi"]
        prev_tier = state["tier"]

        rvals = [e["r"] for e in entries]
        avg_r = sum(rvals) / n if n else 0.0
        std_r = self._sample_std(rvals)
        median_r = self._median(rvals)
        t = self._t_stat(avg_r, std_r, n) if n else 0.0
        bootstrap_ci_low = self._bootstrap_ci_low(rvals) if n >= 3 else None
        wr20 = self._win_rate(rvals, window=20)

        gates = {
            "n_ge_30": n >= 30,
            "n_ge_100": n >= 100,
            "avgR_pos": avg_r > 0,
            "t_gt": t > self.t_stat,
            "ci_low_gt_0": (bootstrap_ci_low or 0) > 0 if bootstrap_ci_low is not None else False,
            "median_gt_0": median_r > 0,
        }

        # ---- G4a: structural halt (proven loser) — LATCHED until manual reset
        if state.get("halted"):
            phi, tier, event, reason = 0.0, "HALTED", "down", (
                "halt latched (manual reset_halt() required)")
        elif n >= 30 and avg_r <= 0 and t <= self.t_halt:
            state["halted"] = True
            state["confirmations"] = 0
            state["confirmations_50"] = 0
            phi, tier, event, reason = 0.0, "HALTED", "down", (
                f"avgR={avg_r:.3f}<=0 and t={t:.2f}<=-1.5 -> halt (latched)")
        else:
            # ---- G4b: quarantine handling (transient, expires after N entries)
            breach_idx = self._cusum_breach_index(
                entries, state.get("baseline_n", 0)
            ) if n >= 5 else None
            # Monotonic design: quarantine_until_n is set ONCE at trigger to
            # `n + quarantine_entries` (N NEW entries after the triggering
            # eval). While n < that count we stay quarantined; the count is
            # never re-anchored to a recomputed breach index, so quarantine
            # duration cannot drift with eval cadence or sample growth.
            if state["quarantine_until_n"] > n:
                phi, tier = 0.25, "QUARANTINED"
                event, reason = "down", (
                    f"CUSUM quarantine until entry {state['quarantine_until_n']}")
            else:
                # window passed (or never set): clear it and evaluate fresh.
                state["quarantine_until_n"] = 0
                # Fresh-breach rule: only quarantine when the last CUSUM cross
                # lies within the recent quarantine_entries trades. An OLD
                # cluster (idx 5 of 100) no longer triggers — the strategy
                # already out-served its penalty; a HEALED strategy's last
                # cross drifts far from the sample end and won't re-trigger.
                fresh = (
                    breach_idx is not None
                    and (n - breach_idx) <= self.quarantine_entries
                )
                if fresh:
                    # reset both hysteresis counters (breach = "edge decaying")
                    state["confirmations"] = 0
                    state["confirmations_50"] = 0
                    state["baseline_n"] = n
                    state["quarantine_until_n"] = n + self.quarantine_entries
                    phi, tier = 0.25, "QUARANTINED"
                    event, reason = "down", (
                        f"lower-CUSUM breached h={self.cusum_h} at idx {breach_idx} "
                        f"(n={n}) -> quarantine {self.quarantine_entries} new entries")
                else:
                    # ---- normal tier logic (milestone-crossing + hysteresis)
                    phi, tier, event, reason = self._tier_decision(
                        state, n, avg_r, t, median_r, bootstrap_ci_low, gates)

        # enforce tier phi
        phi = min(phi, self._tier_phi(tier))

        state.update({
            "phi": phi,
            "tier": tier,
            "n_deduped": n,
            "n_raw": self._last_n_raw,
            "confirmations": state.get("confirmations", 0),
            "confirmations_50": state.get("confirmations_50", 0),
            "last_milestone": max(state.get("last_milestone", 0), n),
            "fingerprint": self._sample_fingerprint(entries),
        })
        state.setdefault("history", []).append({
            "eval_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phi_prev": prev_phi, "phi_new": phi,
            "tier_prev": prev_tier, "tier_new": tier,
            "event": event, "reason": reason,
            "n_deduped": n, "n_raw": self._last_n_raw,
            "avgR_net": round(avg_r, 5),
            "stdR": round(std_r, 5),
            "t_stat": None if t in (float("inf"), float("-inf")) else round(t, 4),
            "median_R": round(median_r, 5),
            "bootstrap_ci_low": round(bootstrap_ci_low, 5) if bootstrap_ci_low is not None else None,
            "win_rate_20": round(wr20, 4),
            "cusum_h": self.cusum_h,
            "gates": gates,
        })
        # cap history so the persisted state doesn't grow unbounded
        state["history"] = state["history"][-500:]
        self._save_state(state)

        return {
            "phi": phi,
            "tier": tier,
            "event": event,
            "reason": reason,
            "n_deduped": n,
            "n_raw": self._last_n_raw,
            "avgR_net": avg_r,
            "stdR": std_r,
            "t_stat": t,
            "median_R": median_r,
            "bootstrap_ci_low": bootstrap_ci_low,
            "win_rate_20": wr20,
            "gates": gates,
        }

    def _tier_decision(self, state: dict, n: int, avg_r: float, t: float,
                       median_r: float, ci_low: Optional[float],
                       gates: dict) -> tuple[float, str, str, str]:
        """Milestone-crossing promotion with 3-consecutive hysteresis."""
        qualifying = (
            gates["avgR_pos"]
            and gates["t_gt"]
            and (ci_low or 0) > 0
            and gates["median_gt_0"]
        )

        # Milestone CROSSING detection (tolerates multi-trade gaps between
        # evals — exact-match `n in milestones` would silently skip when the
        # sample jumps 29 -> 57).
        last_milestone = state.get("last_milestone_reached", 0)
        crossed = [m for m in self.milestones if last_milestone < m <= n]
        # A batch jump may cross several thresholds, but it is one observed
        # sample update and therefore earns only one confirmation. Record the
        # highest crossed threshold so later evaluations cannot replay skipped
        # milestones using the same historical sample.
        next_ms = max(crossed) if crossed else None
        at_milestone = next_ms is not None

        # Determine the target tier from n (regardless of milestone gating)
        if n >= 100:
            target_phi, target_tier = 0.50, "RATCHET_50"
        elif n >= 30:
            target_phi, target_tier = 0.40, "RATCHET_40"
        else:
            target_phi, target_tier = 0.25, "LOCKED"

        current_phi = state.get("phi", 0.25)
        current_tier = state.get("tier", "LOCKED")
        # A persisted QUARANTINED/HALTED tier is a stale reference once the
        # window has passed; normal tier logic resumes from LOCKED semantics.
        if current_tier in ("QUARANTINED", "HALTED"):
            current_tier = "LOCKED"
            current_phi = 0.25

        # Proven-loser floor: negative edge never reaches the 0.40/0.50 tiers
        if not gates["avgR_pos"]:
            state["confirmations"] = 0
            state["confirmations_50"] = 0
            return 0.25, "LOCKED", "down", f"avgR={avg_r:.3f}<=0 -> lock at 0.25"

        if at_milestone:
            state["last_milestone_reached"] = next_ms
            if qualifying:
                if n >= 100:
                    # 0.50 tier: separate counter so it stays reachable after a
                    # normal 0.40 promotion (reset-on-promote made 0.50 dead).
                    state["confirmations_50"] = state.get("confirmations_50", 0) + 1
                else:
                    state["confirmations"] = state.get("confirmations", 0) + 1
                if target_phi > current_phi and (
                    (n >= 100 and state["confirmations_50"] >= 3)
                    or (n < 100 and state["confirmations"] >= 3)
                ):
                    phi = min(target_phi, 0.50)
                    if phi > current_phi:
                        tier = self._tier_name(phi)
                        return phi, tier, "up", (
                            f"3 consecutive qualifying evals -> phi={phi}")
                    return current_phi, current_tier, "hold", "already at tier"
                if n >= 100:
                    return current_phi, current_tier, "hold", (
                        f"confirming 0.50 ({state['confirmations_50']}/3) t={t:.2f}")
                return current_phi, current_tier, "hold", (
                    f"confirming 0.40 ({state['confirmations']}/3) t={t:.2f}")

            # A milestone that fails qualification resets both hysteresis counters
            state["confirmations"] = 0
            state["confirmations_50"] = 0
            return current_phi, current_tier, "hold", (
                f"milestone {next_ms} failed qualification -> reset confirmations")

        # Not crossing a milestone: keep current tier unless it's above what n
        # supports (e.g. trades were removed via epoch filter)
        if n < 30 and current_phi > 0.25:
            return 0.25, "LOCKED", "down", "n<30 -> reset to 0.25"
        if current_phi > target_phi:
            return target_phi, target_tier, "down", f"n={n} no longer supports tier"
        return current_phi, current_tier, "hold", "no qualifying milestone"

    @staticmethod
    def _tier_name(phi: float) -> str:
        if phi >= 0.50:
            return "RATCHET_50"
        if phi >= 0.40:
            return "RATCHET_40"
        return "LOCKED"

    @staticmethod
    def _tier_phi(tier: str) -> float:
        return {"RATCHET_50": 0.50, "RATCHET_40": 0.40,
                "QUARANTINED": 0.25, "HALTED": 0.0}.get(tier, 0.25)

    @staticmethod
    def _win_rate(values: list[float], window: int = 20) -> float:
        tail = values[-window:]
        if not tail:
            return 0.0
        return sum(1.0 for v in tail if v > 0) / len(tail)

    # ------------------------------------------------------------ public API

    def reset_halt(self) -> None:
        """Manual review reset for a latched structural halt."""
        state = self._load_state()
        state["halted"] = False
        state["phi"] = 0.25
        state["tier"] = "LOCKED"
        state["confirmations"] = 0
        state["confirmations_50"] = 0
        self._save_state(state)

    def evaluate_if_new(self) -> Optional[dict]:
        """Cheap guard: skip a full evaluate() when the sample fingerprint is
        unchanged. Re-fires on new entries AND on late partial-close legs that
        revise an existing entry's weighted R (same n, different R)."""
        entries = self._load_deduped()
        fp = self._sample_fingerprint(entries)
        state = self._load_state()
        if fp == state.get("fingerprint", ""):
            return None
        return self.evaluate()

    def kelly_fraction(self) -> float:
        """Current phi multiplier. Cheap read for the sizing path."""
        return self._load_state().get("phi", 0.25)
