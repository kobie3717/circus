"""Tests for LinUCB bandit (circus/services/bandit.py)."""

import numpy as np
import pytest

from circus.services.bandit import ArmState, alpha_schedule, is_cold_start, pick


D = 8  # small feature dim for tests


def _ctx(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=D)
    return x / (np.linalg.norm(x) + 1e-9)


def test_empty_arm_is_identity():
    arm = ArmState.empty(D)
    assert arm.A.shape == (D, D)
    assert np.allclose(arm.A, np.eye(D))
    assert np.allclose(arm.b, np.zeros(D))
    assert arm.n_samples == 0
    assert arm.cumulative_reward == 0.0


def test_theta_zero_for_empty_arm():
    arm = ArmState.empty(D)
    theta = arm.theta()
    assert np.allclose(theta, np.zeros(D))


def test_ucb_score_returns_finite_for_empty():
    arm = ArmState.empty(D)
    x = _ctx(0)
    mean, ucb = arm.ucb_score(x, alpha=1.0)
    assert np.isfinite(mean)
    assert np.isfinite(ucb)
    assert ucb >= mean  # exploration bonus non-negative


def test_update_increments_counters():
    arm = ArmState.empty(D)
    x = _ctx(1)
    arm.update(x, reward=1.0)
    assert arm.n_samples == 1
    assert arm.cumulative_reward == 1.0


def test_update_changes_theta_toward_reward_direction():
    arm = ArmState.empty(D)
    x = np.zeros(D)
    x[0] = 1.0
    arm.update(x, reward=1.0)
    arm.update(x, reward=1.0)
    arm.update(x, reward=1.0)
    theta = arm.theta()
    assert theta[0] > 0  # learnt positive weight on dim 0
    # other dims untouched
    assert np.allclose(theta[1:], 0)


def test_serialize_roundtrip():
    arm = ArmState.empty(D)
    x = _ctx(2)
    arm.update(x, reward=0.7)
    arm.update(x * 0.5, reward=0.3)

    A_blob, b_blob = arm.serialize()
    rebuilt = ArmState.deserialize(A_blob, b_blob, d=D, n=arm.n_samples, cum_r=arm.cumulative_reward)

    # float32 round-trip is lossy; allow tight tolerance
    assert np.allclose(rebuilt.A, arm.A, atol=1e-5)
    assert np.allclose(rebuilt.b, arm.b, atol=1e-5)
    assert rebuilt.n_samples == arm.n_samples
    assert rebuilt.cumulative_reward == arm.cumulative_reward


def test_pick_requires_arms():
    with pytest.raises(ValueError):
        pick([], _ctx(0))


def test_pick_returns_best_ucb():
    arm_good = ArmState.empty(D)
    arm_bad = ArmState.empty(D)
    # Train good arm to predict +1 on x_pref, bad arm to predict 0
    x_pref = np.zeros(D)
    x_pref[0] = 1.0
    for _ in range(20):
        arm_good.update(x_pref, reward=1.0)
        arm_bad.update(x_pref, reward=0.0)

    arms = [("good", arm_good), ("bad", arm_bad)]
    idx, mean, ucb, all_ucbs = pick(arms, x_pref, alpha=0.1)

    assert arms[idx][0] == "good"
    assert mean > 0.5  # learnt positive reward
    assert all_ucbs[0] > all_ucbs[1]


def test_pick_explores_unseen_arm():
    # Trained arm with low mean reward + fresh arm. UCB should explore.
    trained = ArmState.empty(D)
    fresh = ArmState.empty(D)
    x = _ctx(3)
    for _ in range(50):
        trained.update(x, reward=0.1)

    arms = [("trained", trained), ("fresh", fresh)]
    # High alpha → exploration wins
    idx, _, _, all_ucbs = pick(arms, x, alpha=5.0)
    assert arms[idx][0] == "fresh"


def test_is_cold_start_true_for_empty_arms():
    arms = [("a", ArmState.empty(D)), ("b", ArmState.empty(D))]
    assert is_cold_start(arms, threshold=5)


def test_is_cold_start_false_after_enough_samples():
    arm = ArmState.empty(D)
    x = _ctx(4)
    for _ in range(10):
        arm.update(x, reward=0.5)
    arms = [("a", arm)]
    assert not is_cold_start(arms, threshold=5)


def test_alpha_schedule_decay():
    assert alpha_schedule(0, start=1.0, end=0.1, horizon=100) == 1.0
    assert alpha_schedule(100, start=1.0, end=0.1, horizon=100) == 0.1
    assert alpha_schedule(200, start=1.0, end=0.1, horizon=100) == 0.1  # clamps
    mid = alpha_schedule(50, start=1.0, end=0.1, horizon=100)
    assert 0.5 < mid < 0.6


def test_regret_decreases_with_training():
    """Sanity: bandit converges to best arm on stationary 2-arm problem.

    Context has bias dim (last entry = 1.0) so a constant-reward arm is learnable.
    """
    rng = np.random.default_rng(42)
    arm_a = ArmState.empty(D)
    arm_b = ArmState.empty(D)

    # Hidden truth: arm A wins for all contexts (mean reward 0.8 vs 0.2)
    cumulative_regret = 0.0
    n_episodes = 400
    last_100_regret = 0.0
    for t in range(n_episodes):
        x = rng.normal(size=D - 1)
        x = x / (np.linalg.norm(x) + 1e-9)
        x = np.concatenate([x, [1.0]])  # bias term
        arms = [("A", arm_a), ("B", arm_b)]
        idx, _, _, _ = pick(arms, x, alpha=alpha_schedule(t, horizon=n_episodes))
        chosen = arms[idx][0]
        if chosen == "A":
            r = 0.8 + rng.normal(scale=0.05)
        else:
            r = 0.2 + rng.normal(scale=0.05)
            cumulative_regret += 0.6
            if t >= n_episodes - 100:
                last_100_regret += 0.6
        arms[idx][1].update(x, reward=float(np.clip(r, 0, 1)))

    # Final 100 episodes — bandit should rarely pick B
    avg_recent_regret = last_100_regret / 100
    assert avg_recent_regret < 0.10, f"recent avg regret too high: {avg_recent_regret}"
    # Arm A should dominate samples
    assert arm_a.n_samples > arm_b.n_samples * 2
