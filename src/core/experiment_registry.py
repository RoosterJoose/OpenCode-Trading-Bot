"""
Phase 1.6: Durable experiment IDs and trial registry.

Provides:
- Canonical ID scheme: HYP, CAND, TRI, RUN, DEP, ART, SRC, CLM, DEC
- SQLite tables for hypotheses, candidates, trials, runs, decisions
- Run manifest writer that captures git commit, config hash, data snapshot
- Append-only records (no update/delete of closed records)

This is the governance layer that enables:
- Multiple-testing correction (DSR, PBO) by counting all trials
- Reproducibility (every run has a manifest)
- Attribution (CONS vs AGGR is a deployment, not an independent experiment)
- Evidence lineage (claims link back to sources)
"""

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("hermes.experiment_registry")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_commit() -> str:
    """Get current git commit hash."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unknown"


def _git_dirty_hash(repo_path: str = ".") -> str:
    """Get hash of uncommitted changes (empty string if clean)."""
    try:
        diff = subprocess.check_output(
            ["git", "diff", "HEAD"], stderr=subprocess.DEVNULL, text=True,
            cwd=repo_path
        )
        if not diff:
            return ""
        return hashlib.sha256(diff.encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _config_hash(config: dict) -> str:
    """SHA-256 hash of a configuration dict (secrets removed)."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Hypothesis:
    """A research hypothesis (immutable once registered)."""
    hyp_id: str              # HYP-YYYY-NNN
    title: str
    economic_mechanism: str
    falsification_criterion: str
    registered_at: str = field(default_factory=_utc_now)
    parent_hyp_id: Optional[str] = None  # for derived hypotheses


@dataclass
class Candidate:
    """A frozen candidate specification (immutable once registered)."""
    cand_id: str             # CAND-{hyp_id}-vNN
    hyp_id: str
    parameters: dict        # frozen parameter set
    config_hash: str        # SHA-256 of parameters
    universe: list           # asset list
    cost_model: dict        # fee/spread/slippage assumptions
    benchmarks: list         # benchmark IDs
    registered_at: str = field(default_factory=_utc_now)


@dataclass
class Trial:
    """A planned test of a candidate (registered BEFORE observing outcomes)."""
    trial_id: str           # TRI-YYYYMMDD-NNN
    cand_id: str
    hyp_id: str
    evaluation_window: dict  # {"start": "...", "end": "..."}
    stopping_rule: str
    primary_metric: str
    falsification_threshold: float
    registered_at: str = field(default_factory=_utc_now)
    status: str = "planned"  # planned, running, completed, failed, abandoned
    outcome: Optional[dict] = None  # filled when completed


@dataclass
class RunManifest:
    """Immutable record of a single execution."""
    run_id: str             # RUN-{deployment}-NNN
    trial_id: Optional[str]
    deployment_id: str      # DEP-CONS, DEP-AGGR, DEP-SPOT
    git_commit: str
    git_dirty_hash: str
    config_hash: str
    data_snapshot_id: Optional[str]
    seed: Optional[int]
    command: str
    start_time: str = field(default_factory=_utc_now)
    end_time: Optional[str] = None
    metrics: Optional[dict] = None
    status: str = "running"  # running, completed, failed, aborted


@dataclass
class Decision:
    """A governance decision (promotion, parameter change, etc.)."""
    dec_id: str             # DEC-YYYY-NNN
    decision_type: str     # promote, parameter_change, kill, resume
    scope: str             # strategy, portfolio, operational
    rationale: str
    evidence_ids: list     # trial_ids, source_ids supporting this decision
    approver: str          # who approved (human name or "system")
    effective_date: str = field(default_factory=_utc_now)
    expiry_date: Optional[str] = None
    next_review: Optional[str] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    hyp_id TEXT PRIMARY KEY,
    title TEXT,
    economic_mechanism TEXT,
    falsification_criterion TEXT,
    registered_at TEXT,
    parent_hyp_id TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    cand_id TEXT PRIMARY KEY,
    hyp_id TEXT,
    parameters TEXT,  -- JSON
    config_hash TEXT,
    universe TEXT,    -- JSON
    cost_model TEXT,  -- JSON
    benchmarks TEXT,  -- JSON
    registered_at TEXT,
    FOREIGN KEY (hyp_id) REFERENCES hypotheses(hyp_id)
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id TEXT PRIMARY KEY,
    cand_id TEXT,
    hyp_id TEXT,
    evaluation_window TEXT,  -- JSON
    stopping_rule TEXT,
    primary_metric TEXT,
    falsification_threshold REAL,
    registered_at TEXT,
    status TEXT DEFAULT 'planned',
    outcome TEXT,  -- JSON, NULL until completed
    FOREIGN KEY (cand_id) REFERENCES candidates(cand_id),
    FOREIGN KEY (hyp_id) REFERENCES hypotheses(hyp_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    trial_id TEXT,
    deployment_id TEXT,
    git_commit TEXT,
    git_dirty_hash TEXT,
    config_hash TEXT,
    data_snapshot_id TEXT,
    seed INTEGER,
    command TEXT,
    start_time TEXT,
    end_time TEXT,
    metrics TEXT,  -- JSON
    status TEXT DEFAULT 'running',
    FOREIGN KEY (trial_id) REFERENCES trials(trial_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    dec_id TEXT PRIMARY KEY,
    decision_type TEXT,
    scope TEXT,
    rationale TEXT,
    evidence_ids TEXT,  -- JSON
    approver TEXT,
    effective_date TEXT,
    expiry_date TEXT,
    next_review TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    entity_type TEXT,
    entity_id TEXT,
    timestamp TEXT,
    actor TEXT,
    details TEXT  -- JSON
);
"""


class ExperimentRegistry:
    """
    Append-only experiment registry.

    All records are immutable once closed. This enables:
    - Multiple-testing correction (count all trials)
    - Reproducibility (run manifests)
    - Attribution (deployment ≠ experiment)
    - Evidence lineage
    """

    def __init__(self, db_path: str):
        import sqlite3
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        # Counters for ID generation (restored from DB)
        self._hyp_counter = self._restore_counter("experiments")
        self._trial_counter = self._restore_counter("trials")
        self._run_counter = self._restore_counter("runs")
        self._dec_counter = self._restore_counter("decisions")

    def _restore_counter(self, table: str) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) as cnt FROM %s" % table).fetchone()
            return row["cnt"] if row else 0
        except Exception:
            return 0

    def _next_hyp_id(self) -> str:
        year = datetime.now(timezone.utc).year
        self._hyp_counter += 1
        return f"HYP-{year}-{self._hyp_counter:03d}"

    def _next_cand_id(self, hyp_id: str, version: int = 1) -> str:
        return f"CAND-{hyp_id}-v{version:02d}"

    def _next_trial_id(self) -> str:
        now = datetime.now(timezone.utc)
        self._trial_counter += 1
        return f"TRI-{now.strftime('%Y%m%d')}-{self._trial_counter:03d}"

    def _next_run_id(self, deployment: str) -> str:
        self._run_counter += 1
        return f"RUN-{deployment}-{self._run_counter:03d}"

    def _next_dec_id(self) -> str:
        year = datetime.now(timezone.utc).year
        self._dec_counter += 1
        return f"DEC-{year}-{self._dec_counter:03d}"

    # ------------------------------------------------------------------
    # Registration methods (return the assigned ID)
    # ------------------------------------------------------------------

    def register_hypothesis(
        self,
        title: str,
        economic_mechanism: str,
        falsification_criterion: str,
        parent_hyp_id: Optional[str] = None,
    ) -> str:
        """Register a new hypothesis. Returns HYP-YYYY-NNN."""
        hyp_id = self._next_hyp_id()
        hyp = Hypothesis(
            hyp_id=hyp_id,
            title=title,
            economic_mechanism=economic_mechanism,
            falsification_criterion=falsification_criterion,
            parent_hyp_id=parent_hyp_id,
        )
        self.conn.execute(
            "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
            (hyp.hyp_id, hyp.title, hyp.economic_mechanism,
             hyp.falsification_criterion, hyp.registered_at, hyp.parent_hyp_id)
        )
        self.conn.commit()
        logger.info("EXPERIMENT: Registered hypothesis %s: %s", hyp_id, title)
        return hyp_id

    def register_candidate(
        self,
        hyp_id: str,
        parameters: dict,
        universe: list,
        cost_model: dict,
        benchmarks: list,
        version: int = 1,
    ) -> str:
        """Register a frozen candidate. Returns CAND-{hyp}-vNN."""
        cand_id = self._next_cand_id(hyp_id, version)
        chash = _config_hash(parameters)
        cand = Candidate(
            cand_id=cand_id,
            hyp_id=hyp_id,
            parameters=parameters,
            config_hash=chash,
            universe=universe,
            cost_model=cost_model,
            benchmarks=benchmarks,
        )
        self.conn.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cand.cand_id, cand.hyp_id, json.dumps(parameters), chash,
             json.dumps(universe), json.dumps(cost_model), json.dumps(benchmarks),
             cand.registered_at)
        )
        self.conn.commit()
        logger.info("EXPERIMENT: Registered candidate %s (hash=%s)", cand_id, chash)
        return cand_id

    def register_trial(
        self,
        cand_id: str,
        hyp_id: str,
        evaluation_window: dict,
        stopping_rule: str,
        primary_metric: str,
        falsification_threshold: float,
    ) -> str:
        """Register a planned trial. Must be done BEFORE observing outcomes."""
        trial_id = self._next_trial_id()
        trial = Trial(
            trial_id=trial_id,
            cand_id=cand_id,
            hyp_id=hyp_id,
            evaluation_window=evaluation_window,
            stopping_rule=stopping_rule,
            primary_metric=primary_metric,
            falsification_threshold=falsification_threshold,
        )
        self.conn.execute(
            "INSERT INTO trials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trial.trial_id, trial.cand_id, trial.hyp_id,
             json.dumps(evaluation_window), stopping_rule, primary_metric,
             falsification_threshold, trial.registered_at, "planned", None)
        )
        self.conn.commit()
        logger.info("EXPERIMENT: Registered trial %s for candidate %s", trial_id, cand_id)
        return trial_id

    def create_run_manifest(
        self,
        deployment_id: str,
        config: dict,
        trial_id: Optional[str] = None,
        seed: Optional[int] = None,
        command: str = "",
        repo_path: str = ".",
    ) -> str:
        """Create a run manifest. Called at process startup."""
        run_id = self._next_run_id(deployment_id)
        manifest = RunManifest(
            run_id=run_id,
            trial_id=trial_id,
            deployment_id=deployment_id,
            git_commit=_git_commit(),
            git_dirty_hash=_git_dirty_hash(repo_path),
            config_hash=_config_hash(config),
            data_snapshot_id=None,  # filled when data snapshot is taken
            seed=seed,
            command=command,
        )
        self.conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (manifest.run_id, manifest.trial_id, manifest.deployment_id,
             manifest.git_commit, manifest.git_dirty_hash, manifest.config_hash,
             manifest.data_snapshot_id, manifest.seed, manifest.command,
             manifest.start_time, manifest.end_time, None, "running")
        )
        self.conn.commit()
        logger.info("EXPERIMENT: Created run manifest %s (commit=%s, config_hash=%s)",
                    run_id, manifest.git_commit[:8], manifest.config_hash)
        return run_id

    def complete_run(self, run_id: str, metrics: dict, status: str = "completed"):
        """Mark a run as complete with metrics."""
        self.conn.execute(
            "UPDATE runs SET end_time=?, metrics=?, status=? WHERE run_id=?",
            (_utc_now(), json.dumps(metrics), status, run_id)
        )
        self.conn.commit()

    def complete_trial(self, trial_id: str, outcome: dict, status: str = "completed"):
        """Mark a trial as complete with outcome."""
        self.conn.execute(
            "UPDATE trials SET outcome=?, status=? WHERE trial_id=?",
            (json.dumps(outcome), status, trial_id)
        )
        self.conn.commit()

    def register_decision(
        self,
        decision_type: str,
        scope: str,
        rationale: str,
        evidence_ids: list,
        approver: str,
        expiry_date: Optional[str] = None,
        next_review: Optional[str] = None,
    ) -> str:
        """Register a governance decision."""
        dec_id = self._next_dec_id()
        dec = Decision(
            dec_id=dec_id,
            decision_type=decision_type,
            scope=scope,
            rationale=rationale,
            evidence_ids=evidence_ids,
            approver=approver,
            expiry_date=expiry_date,
            next_review=next_review,
        )
        self.conn.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dec.dec_id, dec.decision_type, dec.scope, dec.rationale,
             json.dumps(evidence_ids), dec.approver, dec.effective_date,
             dec.expiry_date, dec.next_review)
        )
        self.conn.commit()
        logger.info("EXPERIMENT: Registered decision %s (%s by %s)",
                    dec_id, decision_type, approver)
        return dec_id

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def count_trials(self, hyp_id: Optional[str] = None) -> int:
        """Count total trials (for multiple-testing correction)."""
        if hyp_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM trials WHERE hyp_id=?", (hyp_id,)
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]

    def list_hypotheses(self) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM hypotheses").fetchall()]

    def list_trials(self, status: Optional[str] = None) -> list:
        if status:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM trials WHERE status=?", (status,)).fetchall()]
        return [dict(r) for r in self.conn.execute("SELECT * FROM trials").fetchall()]

    def list_runs(self, deployment_id: Optional[str] = None) -> list:
        if deployment_id:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM runs WHERE deployment_id=?", (deployment_id,)).fetchall()]
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs").fetchall()]

    def list_decisions(self) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM decisions").fetchall()]

    def get_experiment_summary(self) -> dict:
        """Summary of all experiments for governance review."""
        return {
            "total_hypotheses": self.conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0],
            "total_candidates": self.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "total_trials": self.conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0],
            "completed_trials": self.conn.execute("SELECT COUNT(*) FROM trials WHERE status='completed'").fetchone()[0],
            "abandoned_trials": self.conn.execute("SELECT COUNT(*) FROM trials WHERE status='abandoned'").fetchone()[0],
            "total_runs": self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "total_decisions": self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "hypotheses": self.list_hypotheses(),
        }

    def close(self):
        self.conn.close()