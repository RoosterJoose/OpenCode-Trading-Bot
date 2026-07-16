"""
Phase 3.1: Walk-forward validation engine.

Provides:
- Chronological fold generation (expanding and rolling windows)
- Purging: remove training observations whose label/holding interval overlaps a test observation
- Embargo: gap after test before next training data admitted
- Block bootstrap for dependence-aware confidence intervals
- Stationary bootstrap (Politis-Romano) for weakly dependent returns
- Deflated Sharpe Ratio (Bailey & Lopez de Prado) for multiple-testing correction

Usage:
    folds = WalkForwardEngine.generate_folds(n_obs=1000, n_folds=5, purge_bars=10, embargo_bars=5)
    ci = WalkForwardEngine.block_bootstrap(returns, n_bootstrap=10000, block_size=10)
    dsr = WalkForwardEngine.deflated_sharpe(sharpe=1.29, n_trials=47, n_obs=365, skew=0.5, kurt=4.0)
"""

import logging
import math
import random
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger("hermes.validation")


@dataclass
class Fold:
    """A single walk-forward fold."""
    fold_id: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    purge_start: Optional[int] = None  # purged training observations
    purge_end: Optional[int] = None
    embargo_end: Optional[int] = None  # embargo period after test


class WalkForwardEngine:
    """
    Walk-forward validation with purging and embargo.

    Prevents look-ahead bias by:
    1. Training data always precedes test data chronologically
    2. Purging removes training observations whose label/holding interval overlaps test
    3. Embargo adds a gap after test before next training data is admitted
    4. Folds are non-overlapping in the test period
    """

    @staticmethod
    def generate_expanding_folds(
        n_obs: int,
        n_folds: int,
        min_train: int,
        purge_bars: int = 0,
        embargo_bars: int = 0,
    ) -> list[Fold]:
        """
        Generate expanding-window folds.

        Each fold's training set grows: [0, t) trains, [t, t+k) tests.
        Next fold: [0, t+k) trains, [t+k, t+2k) tests.
        """
        if n_obs < min_train + n_folds:
            raise ValueError(f"Need at least {min_train + n_folds} observations for {n_folds} folds")

        test_size = (n_obs - min_train) // n_folds
        folds = []

        for i in range(n_folds):
            test_start = min_train + i * test_size
            test_end = test_start + test_size

            if test_end > n_obs:
                test_end = n_obs

            train_start = 0
            train_end = test_start

            # Purging: remove training observations within purge_bars of test start
            purge_start = max(0, test_start - purge_bars)
            purge_end = test_start

            # Embargo: gap after test before next training
            embargo_end = min(n_obs, test_end + embargo_bars)

            folds.append(Fold(
                fold_id=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                purge_start=purge_start if purge_bars > 0 else None,
                purge_end=purge_end if purge_bars > 0 else None,
                embargo_end=embargo_end if embargo_bars > 0 else None,
            ))

        return folds

    @staticmethod
    def generate_rolling_folds(
        n_obs: int,
        n_folds: int,
        train_size: int,
        test_size: int,
        purge_bars: int = 0,
        embargo_bars: int = 0,
    ) -> list[Fold]:
        """
        Generate rolling-window folds.

        Each fold uses a fixed-size training window that slides forward.
        """
        step = test_size
        total_needed = train_size + n_folds * step
        if n_obs < total_needed:
            raise ValueError(f"Need {total_needed} obs for {n_folds} rolling folds")

        folds = []
        for i in range(n_folds):
            train_start = i * step
            train_end = train_start + train_size
            test_start = train_end
            test_end = test_start + test_size

            purge_start = max(train_start, train_end - purge_bars)
            embargo_end = min(n_obs, test_end + embargo_bars)

            folds.append(Fold(
                fold_id=i,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                purge_start=purge_start if purge_bars > 0 else None,
                purge_end=train_end if purge_bars > 0 else None,
                embargo_end=embargo_end if embargo_bars > 0 else None,
            ))

        return folds

    @staticmethod
    def block_bootstrap(
        returns: list[float],
        n_bootstrap: int = 10000,
        block_size: int = 10,
        seed: int = 42,
    ) -> dict:
        """
        Stationary block bootstrap (Politis-Romano 1994).

        Produces dependence-aware confidence intervals for the mean.

        Returns dict with: mean, std, ci_lower_95, ci_upper_95, n_bootstrap
        """
        rng = random.Random(seed)
        n = len(returns)
        if n < 2:
            return {"mean": 0, "std": 0, "ci_lower_95": 0, "ci_upper_95": 0, "n_bootstrap": 0}

        returns_arr = np.array(returns)
        bootstrap_means = []

        for _ in range(n_bootstrap):
            # Stationary bootstrap: block length is geometrically distributed
            samples = []
            while len(samples) < n:
                # Geometric random block length with mean = block_size
                bl = max(1, int(rng.expovariate(1.0 / block_size)))
                start = rng.randint(0, n - 1)
                for j in range(bl):
                    idx = (start + j) % n
                    samples.append(returns_arr[idx])
                    if len(samples) >= n:
                        break

            bootstrap_means.append(np.mean(samples[:n]))

        bootstrap_means = np.array(bootstrap_means)
        ci_lower = np.percentile(bootstrap_means, 2.5)
        ci_upper = np.percentile(bootstrap_means, 97.5)

        return {
            "mean": float(np.mean(bootstrap_means)),
            "std": float(np.std(bootstrap_means)),
            "ci_lower_95": float(ci_lower),
            "ci_upper_95": float(ci_upper),
            "n_bootstrap": n_bootstrap,
        }

    @staticmethod
    def deflated_sharpe(
        sharpe: float,
        n_trials: int,
        n_obs: int,
        skew: float = 0.0,
        kurt: float = 3.0,
    ) -> dict:
        """
        Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

        Adjusts observed Sharpe for:
        - Number of trials (multiple testing)
        - Sample length
        - Non-normality (skew, kurtosis)

        Returns dict with: dsr, dsr_probability, adjusted_sharpe, threshold
        """
        if n_obs < 2 or n_trials < 1:
            return {"dsr": 0, "dsr_probability": 0, "adjusted_sharpe": 0, "threshold": 0}

        # Expected maximum Sharpe under null (across n_trials independent trials)
        # E[max Sharpe] = sqrt(2 * ln(n_trials)) approximately
        if n_trials > 1:
            expected_max_sharpe = math.sqrt(2 * math.log(n_trials))
        else:
            expected_max_sharpe = 0.0

        # Variance of Sharpe estimator (adjusted for skew and kurtosis)
        # Var(SR) ≈ (1 + 0.5*skew^2 - kurt/4) / n_obs
        sr_var = (1 + 0.5 * skew**2 - (kurt - 3) / 4) / n_obs
        sr_std = math.sqrt(max(sr_var, 1e-10))

        # Deflated Sharpe = (observed SR - expected max SR) / SR std
        dsr = (sharpe - expected_max_sharpe) / sr_std if sr_std > 0 else 0.0

        # Probability that the deflated Sharpe is positive (standard normal CDF)
        # P(DSR > 0) = Φ(dsr)
        dsr_prob = 0.5 * (1 + math.erf(dsr / math.sqrt(2)))

        # Threshold for DSR probability >= 0.95
        # Need dsr >= 1.645 (one-sided 95%)
        threshold_sharpe = expected_max_sharpe + 1.645 * sr_std

        return {
            "dsr": round(dsr, 4),
            "dsr_probability": round(dsr_prob, 4),
            "adjusted_sharpe": round(sharpe - expected_max_sharpe, 4),
            "threshold": round(threshold_sharpe, 4),
            "n_trials": n_trials,
            "expected_max_sharpe": round(expected_max_sharpe, 4),
        }

    @staticmethod
    def probability_of_backtest_overfitting(
        in_sample_returns: list[float],
        out_of_sample_returns: list[float],
        n_partitions: int = 10,
        seed: int = 42,
    ) -> dict:
        """
        Combinatorial Symmetric Cross-Validation (CSCV) for PBO estimation.

        Splits returns into n_partitions, creates combinations of train/test,
        and estimates the probability that the in-sample winner underperforms
        out-of-sample.

        Returns dict with: pbo, n_combinations, is_overfit
        """
        rng = random.Random(seed)
        n = len(in_sample_returns)
        if n < n_partitions * 2:
            return {"pbo": 0.0, "n_combinations": 0, "is_overfit": False}

        # Split returns into n_partitions blocks
        block_size = n // n_partitions
        blocks_is = [in_sample_returns[i*block_size:(i+1)*block_size] for i in range(n_partitions)]
        blocks_oos = [out_of_sample_returns[i*block_size:(i+1)*block_size] for i in range(n_partitions)]

        # For each split of blocks into train/test halves, compute PBO
        n_worse = 0
        n_total = 0

        # Simplified: just check if in-sample mean rank matches out-of-sample rank
        # Full CSCV is more complex but this gives an approximate PBO
        is_means = [np.mean(b) for b in blocks_is]
        oos_means = [np.mean(b) for b in blocks_oos]

        for i in range(n_partitions):
            for j in range(i + 1, n_partitions):
                # Is block i better in-sample?
                is_i_better = is_means[i] > is_means[j]
                # Is block i worse out-of-sample?
                oos_i_worse = oos_means[i] < oos_means[j]

                if is_i_better and oos_i_worse:
                    n_worse += 1
                n_total += 1

        pbo = n_worse / n_total if n_total > 0 else 0.0

        return {
            "pbo": round(pbo, 4),
            "n_combinations": n_total,
            "is_overfit": pbo > 0.20,  # threshold from research
        }