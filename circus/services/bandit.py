"""LinUCB contextual multi-armed bandit for agent routing.

Pure numpy. No I/O. Persistence handled by routing service.

Reference: Li et al. 2010, "A Contextual-Bandit Approach to Personalized News Article Recommendation."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass
class ArmState:
    """LinUCB arm state. One per (agent_id, task_type) pair."""

    A: np.ndarray  # (d, d) covariance — starts as I_d
    b: np.ndarray  # (d,) reward-weighted feature sum — starts as 0
    n_samples: int = 0
    cumulative_reward: float = 0.0

    @classmethod
    def empty(cls, d: int) -> "ArmState":
        """Initialise unseen arm. A = I_d, b = 0."""
        return cls(A=np.eye(d, dtype=np.float64), b=np.zeros(d, dtype=np.float64))

    def theta(self) -> np.ndarray:
        """Estimated weights: A^-1 · b."""
        return np.linalg.solve(self.A, self.b)

    def ucb_score(self, x: np.ndarray, alpha: float) -> tuple[float, float]:
        """Compute UCB score for context x.

        Returns (mean_estimate, upper_bound). Upper bound is what we maximise.
        """
        # Solve A^-1 · x once for both terms.
        A_inv_x = np.linalg.solve(self.A, x)
        mean = float(self.b @ A_inv_x)  # theta · x = b · A^-1 · x
        variance = float(x @ A_inv_x)
        if variance < 0:
            variance = 0.0  # numerical guard
        ucb = mean + alpha * np.sqrt(variance)
        return mean, ucb

    def update(self, x: np.ndarray, reward: float) -> None:
        """Online update after observing reward for context x.

        A ← A + x x^T
        b ← b + r x
        """
        self.A = self.A + np.outer(x, x)
        self.b = self.b + reward * x
        self.n_samples += 1
        self.cumulative_reward += float(reward)

    def serialize(self) -> tuple[bytes, bytes]:
        """Pack A, b as float32 little-endian for SQLite BLOB storage."""
        return (
            self.A.astype(np.float32).tobytes(order="C"),
            self.b.astype(np.float32).tobytes(order="C"),
        )

    @classmethod
    def deserialize(cls, A_blob: bytes, b_blob: bytes, d: int, n: int = 0, cum_r: float = 0.0) -> "ArmState":
        """Rebuild from BLOB pair. d = feature dim."""
        A = np.frombuffer(A_blob, dtype=np.float32).reshape(d, d).astype(np.float64)
        b = np.frombuffer(b_blob, dtype=np.float32).astype(np.float64)
        return cls(A=A.copy(), b=b.copy(), n_samples=n, cumulative_reward=cum_r)


def pick(
    arms: Sequence[tuple[str, ArmState]],
    x: np.ndarray,
    alpha: float = 1.0,
    cold_start_threshold: int = 5,
) -> tuple[int, float, float, list[float]]:
    """Pick arm with highest UCB score.

    Returns (chosen_index, mean, ucb, all_ucbs).

    If every arm has < cold_start_threshold samples, caller should use a fallback policy
    (e.g., semantic similarity). This function still picks the highest-UCB arm regardless.
    Caller decides what to do with low-confidence picks.
    """
    if not arms:
        raise ValueError("pick() requires at least one arm")

    scores: list[tuple[float, float]] = []  # (mean, ucb) per arm
    for _, arm in arms:
        scores.append(arm.ucb_score(x, alpha))

    ucbs = [s[1] for s in scores]
    best_idx = int(np.argmax(ucbs))
    return best_idx, scores[best_idx][0], scores[best_idx][1], ucbs


def is_cold_start(arms: Sequence[tuple[str, ArmState]], threshold: int = 5) -> bool:
    """True if every arm has fewer than `threshold` samples."""
    return all(arm.n_samples < threshold for _, arm in arms)


def alpha_schedule(global_step: int, start: float = 1.0, end: float = 0.1, horizon: int = 10_000) -> float:
    """Linearly decay exploration weight from `start` to `end` across `horizon` steps."""
    if global_step >= horizon:
        return end
    frac = global_step / horizon
    return start + frac * (end - start)
