"""5-Agent Restaurant Demo — Circus bandit routing in 150 lines.

Story: a busy restaurant shift. 5 staff members, 5 task types, 200 tasks
arriving over the shift. The Circus bandit learns who's best at what — no
config, just outcomes.

Run:
    python -m examples.restaurant

Output: colourful transcript + final scoreboard.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from circus.services.bandit import ArmState, alpha_schedule, pick

FEATURE_DIM = 12  # smaller than prod 32 — keeps demo fast

# ---- Cast ----

@dataclass
class Staff:
    """A restaurant staff member with hidden true skill per task type."""
    id: str
    emoji: str
    role: str
    # Hidden truth: how good they are at each task (0..1). Unknown to bandit.
    true_skill: dict[str, float] = field(default_factory=dict)


CAST = [
    Staff("anna",   "👩‍🍳", "chef",       true_skill={"cook": 0.92, "plate": 0.78, "manage": 0.55}),
    Staff("bram",   "🧽", "dishwasher", true_skill={"clean": 0.95, "plate": 0.50}),
    Staff("cara",   "💰", "cashier",    true_skill={"checkout": 0.93, "greet": 0.82}),
    Staff("dries",  "🚪", "greeter",    true_skill={"greet": 0.96, "manage": 0.65}),
    Staff("eva",    "📋", "manager",    true_skill={"manage": 0.90, "checkout": 0.78, "cook": 0.55, "clean": 0.50, "greet": 0.78}),
]

TASK_TYPES = ("cook", "clean", "checkout", "greet", "plate", "manage")

# Optimal-by-truth lookup (used for regret tracking)
OPTIMAL = {
    tt: max(((s.id, s.true_skill.get(tt, 0.0)) for s in CAST), key=lambda p: p[1])
    for tt in TASK_TYPES
}


# ---- Simulation ----

def sample_reward(staff: Staff, task_type: str, rng: np.random.Generator) -> float:
    """Real-world outcome: skill ± noise. Untrained staff get 0.05 floor."""
    base = staff.true_skill.get(task_type, 0.05)
    return float(np.clip(base + rng.normal(0, 0.07), 0.0, 1.0))


def random_task(rng: np.random.Generator) -> tuple[str, np.ndarray]:
    """Pick a task type. Generate dummy 12-dim context (last dim = bias)."""
    tt = TASK_TYPES[int(rng.integers(0, len(TASK_TYPES)))]
    ctx = rng.normal(size=FEATURE_DIM - 1) * 0.3
    ctx = np.concatenate([ctx, [1.0]])
    return tt, ctx


def eligible(task_type: str) -> list[Staff]:
    """Staff who self-advertise the capability (true_skill > 0)."""
    return [s for s in CAST if task_type in s.true_skill]


# ---- Demo run ----

# ANSI colours for terminal joy
GREEN, YELLOW, RED, BLUE, GREY, BOLD, RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[94m", "\033[90m", "\033[1m", "\033[0m"
)


def colourise(reward: float) -> str:
    if reward >= 0.80:
        return f"{GREEN}{reward:.2f}{RESET}"
    if reward >= 0.50:
        return f"{YELLOW}{reward:.2f}{RESET}"
    return f"{RED}{reward:.2f}{RESET}"


def main(n_tasks: int = 200, sleep_s: float = 0.0):
    rng = np.random.default_rng(42)

    # One arm-state per (staff, task_type), lazy-init
    arms_by_tt: dict[str, dict[str, ArmState]] = {tt: {} for tt in TASK_TYPES}

    cumulative_regret = 0.0
    rewards_per_staff: dict[str, list[float]] = {s.id: [] for s in CAST}

    print(f"{BOLD}🍽️   THE CIRCUS — Restaurant Routing Demo{RESET}")
    print(f"{GREY}5 staff · 6 task types · {n_tasks} tasks · LinUCB online{RESET}\n")

    for t in range(n_tasks):
        tt, ctx = random_task(rng)
        elig = eligible(tt)

        # Init arms on demand
        for s in elig:
            if s.id not in arms_by_tt[tt]:
                arms_by_tt[tt][s.id] = ArmState.empty(FEATURE_DIM)

        arms = [(s.id, arms_by_tt[tt][s.id]) for s in elig]
        alpha = alpha_schedule(t, start=1.5, end=0.05, horizon=n_tasks)
        idx, mean, ucb, _ = pick(arms, ctx, alpha=alpha)
        chosen_id = arms[idx][0]
        chosen = next(s for s in CAST if s.id == chosen_id)

        # Observe reward
        r = sample_reward(chosen, tt, rng)
        arms[idx][1].update(ctx, r)
        rewards_per_staff[chosen_id].append(r)

        # Track regret vs oracle-optimal
        opt_id, opt_skill = OPTIMAL[tt]
        regret = max(0.0, opt_skill - r)
        cumulative_regret += regret

        # Print event
        if t < 30 or t % 20 == 0 or t == n_tasks - 1:
            opt_marker = "✓" if chosen_id == opt_id else " "
            print(
                f"{GREY}#{t:03d}{RESET} "
                f"{BLUE}{tt:8s}{RESET} → {chosen.emoji} {BOLD}{chosen.id:6s}{RESET} "
                f"reward {colourise(r)} {GREY}(α={alpha:.2f}){RESET} {opt_marker}"
            )
            if sleep_s:
                time.sleep(sleep_s)

    # ---- Final scoreboard ----
    print(f"\n{BOLD}── Final Scoreboard ──{RESET}")
    for s in CAST:
        rs = rewards_per_staff[s.id]
        n = len(rs)
        avg = float(np.mean(rs)) if rs else 0.0
        bar = "█" * int(avg * 20)
        print(f"  {s.emoji} {s.id:6s} {s.role:11s} {n:3d} tasks  avg {colourise(avg)} {GREY}{bar}{RESET}")

    print(f"\n{BOLD}── What the bandit learnt ──{RESET}")
    for tt in TASK_TYPES:
        scores = []
        for sid, arm in arms_by_tt[tt].items():
            if arm.n_samples == 0:
                continue
            scores.append((sid, arm.cumulative_reward / arm.n_samples, arm.n_samples))
        if not scores:
            continue
        scores.sort(key=lambda x: -x[1])
        winner_id, winner_score, winner_n = scores[0]
        opt_id, _ = OPTIMAL[tt]
        verdict = f"{GREEN}✓ correct{RESET}" if winner_id == opt_id else f"{RED}✗ should be {opt_id}{RESET}"
        print(f"  {tt:8s}: best by bandit → {winner_id} (avg {winner_score:.2f}, n={winner_n})  {verdict}")

    avg_regret = cumulative_regret / n_tasks
    print(f"\n{BOLD}Cumulative regret:{RESET} {cumulative_regret:.2f}  "
          f"({GREY}avg/task{RESET} {avg_regret:.3f})")

    # Convergence verdict
    correct = sum(
        1
        for tt in TASK_TYPES
        if (max(((sid, arm.cumulative_reward / arm.n_samples)
                 for sid, arm in arms_by_tt[tt].items() if arm.n_samples), key=lambda p: p[1])[0]
            == OPTIMAL[tt][0])
    )
    total = len(TASK_TYPES)
    print(f"{BOLD}Routing accuracy:{RESET} {correct}/{total} task types match oracle.")

    if correct == total:
        print(f"\n{GREEN}{BOLD}🎉  Bandit converged on optimal staffing — no config, just outcomes.{RESET}")
    else:
        print(f"\n{YELLOW}{BOLD}↻  Try more episodes (n_tasks > 200) for full convergence.{RESET}")


if __name__ == "__main__":
    # Default 500 tasks — bandit converges on all 6 task types.
    # Try 200 to see partial convergence, 1000 to see steady-state.
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    main(n_tasks=n)
