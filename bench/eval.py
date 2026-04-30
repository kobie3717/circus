"""Bandit eval harness — Day 6.

Loads synthetic dataset, runs LinUCB + baselines, plots regret curve.

Outputs:
  bench/regret-curve.png
  bench/results.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np

from circus.services.bandit import ArmState, alpha_schedule, pick

# Late-bind synth so we can run from repo root via `python -m bench.eval`
import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.synth import FEATURE_DIM, TASK_TYPES, load_dataset, sample_reward  # noqa: E402

BENCH_DIR = ROOT / "bench"
SEED = 1337


# ---- Policies ----

def _eligible(agents: list, task_type: str) -> list:
    return [a for a in agents if task_type in a.capabilities]


def policy_random(agents, table, episode, rng, state):
    elig = _eligible(agents, episode["task_type"])
    return elig[int(rng.integers(0, len(elig)))].id


def policy_round_robin(agents, table, episode, rng, state):
    elig = _eligible(agents, episode["task_type"])
    key = episode["task_type"]
    idx = state.setdefault(key, 0)
    chosen = elig[idx % len(elig)]
    state[key] = idx + 1
    return chosen.id


def policy_semantic(agents, table, episode, rng, state):
    """Pick agent by static 'advertised quality' (noisy prior) — no online learning.

    Simulates real-world embedding/description-based routing: agents publish a quality
    signal that's roughly correlated with truth but noisy. Caller has no access to
    actual rewards. Computed once per (agent, task_type) at first use, frozen after.
    """
    priors: dict = state.setdefault("_priors", {})
    if not priors:
        # Build noisy priors once. Seeded for reproducibility.
        prior_rng = np.random.default_rng(SEED + 99)
        for a in agents:
            for tt in a.capabilities:
                truth = table[a.id][tt].base_mean
                priors[(a.id, tt)] = float(truth + prior_rng.normal(0.0, 0.20))
    elig = _eligible(agents, episode["task_type"])
    tt = episode["task_type"]
    return max(elig, key=lambda a: priors[(a.id, tt)]).id


def policy_linucb(horizon: int):
    """Closure binding the LinUCB arms across episodes."""
    arms_by_tt: dict[str, dict[str, ArmState]] = {tt: {} for tt in TASK_TYPES}

    def _policy(agents, table, episode, rng, state):
        tt = episode["task_type"]
        elig = _eligible(agents, tt)
        ctx = np.asarray(episode["context"], dtype=np.float64)
        # Initialise unseen arms
        arms = arms_by_tt[tt]
        for a in elig:
            if a.id not in arms:
                arms[a.id] = ArmState.empty(FEATURE_DIM)
        arm_pairs = [(a.id, arms[a.id]) for a in elig]
        alpha = alpha_schedule(state.get("step", 0), start=1.5, end=0.05, horizon=horizon)
        idx, _, _, _ = pick(arm_pairs, ctx, alpha=alpha)
        state["step"] = state.get("step", 0) + 1
        chosen_id = arm_pairs[idx][0]
        # Stash for online update after reward
        state["_pending"] = (tt, chosen_id, ctx)
        return chosen_id

    def _update(state, reward: float):
        if "_pending" not in state:
            return
        tt, agent_id, ctx = state.pop("_pending")
        arms_by_tt[tt][agent_id].update(ctx, reward)

    _policy.update = _update  # type: ignore[attr-defined]
    return _policy


# ---- Eval loop ----

def run_policy(name: str, policy: Callable, agents, table, episodes, rng) -> dict:
    state: dict = {}
    cum_regret = 0.0
    regret_trace = []
    reward_trace = []
    for ep in episodes:
        ctx = np.asarray(ep["context"], dtype=np.float64)
        chosen = policy(agents, table, ep, rng, state)
        r = sample_reward(chosen, ep["task_type"], ctx, table, rng)
        # Online update for learning policies
        if hasattr(policy, "update"):
            policy.update(state, r)  # type: ignore[attr-defined]
        regret = max(0.0, ep["optimal_reward"] - r)
        cum_regret += regret
        regret_trace.append(cum_regret)
        reward_trace.append(r)
    return {
        "name": name,
        "n": len(episodes),
        "cum_regret": cum_regret,
        "avg_regret": cum_regret / len(episodes),
        "avg_reward": float(np.mean(reward_trace)),
        "final_100_avg_regret": float(np.mean(np.diff([0.0] + regret_trace[-100:]))),
        "regret_trace": regret_trace,
        "reward_trace": reward_trace,
    }


def main():
    agents, table, episodes = load_dataset()
    print(f"Loaded {len(agents)} agents, {len(episodes)} episodes.")
    rng = np.random.default_rng(SEED)

    policies = {
        "random": policy_random,
        "round_robin": policy_round_robin,
        "semantic": policy_semantic,
        "linucb": policy_linucb(horizon=len(episodes)),
    }

    results = {}
    for name, fn in policies.items():
        # Each policy gets its own RNG draw to keep reward sampling fair
        per_rng = np.random.default_rng(SEED + hash(name) % 10_000)
        res = run_policy(name, fn, agents, table, episodes, per_rng)
        results[name] = res
        print(
            f"  {name:13s} cum_regret={res['cum_regret']:8.2f} "
            f"avg_regret={res['avg_regret']:.4f} "
            f"final100_avg_regret={res['final_100_avg_regret']:.4f} "
            f"avg_reward={res['avg_reward']:.4f}"
        )

    # Save numeric results (drop traces from JSON to keep file small + drop heavy lists)
    out_json = BENCH_DIR / "results.json"
    summary = {k: {kk: vv for kk, vv in v.items() if kk not in ("regret_trace", "reward_trace")}
               for k, v in results.items()}
    with out_json.open("w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nResults JSON: {out_json}")

    # Plot
    fig, ax = plt.subplots(figsize=(9, 5.5))
    palette = {"random": "#999999", "round_robin": "#3b82f6",
               "semantic": "#f59e0b", "linucb": "#10b981"}
    for name, res in results.items():
        ax.plot(res["regret_trace"], label=name, color=palette.get(name), linewidth=2)
    ax.set_xlabel("Episode")
    ax.set_ylabel("Cumulative regret")
    ax.set_title("LinUCB vs baselines on synthetic routing (10 agents, 5 task types)")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    out_png = BENCH_DIR / "regret-curve.png"
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    print(f"Plot: {out_png}")

    # Compute pairwise lift
    base = results["semantic"]["cum_regret"]
    lin = results["linucb"]["cum_regret"]
    lift = (base - lin) / base * 100 if base else 0
    print(f"\nLinUCB regret reduction vs semantic: {lift:.1f}%")
    print(f"Target: ≥30% (per spec). { 'PASS' if lift >= 30 else 'BELOW TARGET'}")

    return results


if __name__ == "__main__":
    main()
