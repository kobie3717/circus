"""Synthetic dataset generator + reward sampler for routing bench.

10 agents, 5 task types, 1000 episodes. Each (agent, task_type) has a hidden
linear reward model with base mean + sparse context weights + Gaussian noise.

Reproducible — seeded everywhere. Day 6 bench loads from /root/circus/data/.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np


# ---- Constants ----

SEED = 1337
N_AGENTS = 10
N_EPISODES = 1000
FEATURE_DIM = 32

TASK_TYPES = ("summarize", "classify", "translate", "extract", "search")

# Each agent gets a random subset of task types (3-5 capabilities)
MIN_CAPS_PER_AGENT = 3
MAX_CAPS_PER_AGENT = 5

# Each (agent, task_type) reward model:
#   reward = clip(base_mean + weights · context + noise, 0, 1)
#   noise ~ N(0, NOISE_STDDEV)
NOISE_STDDEV = 0.08
N_NONZERO_WEIGHTS = 4
WEIGHT_RANGE = 0.2  # weights uniform in [-WEIGHT_RANGE, +WEIGHT_RANGE]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---- Data classes ----

@dataclass
class RewardModel:
    """Hidden reward model for one (agent, task_type) pair."""
    base_mean: float
    weights: list[float]      # length FEATURE_DIM, mostly zero
    noise_stddev: float

    def expected_reward(self, context: np.ndarray) -> float:
        """Expected reward (no noise) for a context vector."""
        w = np.asarray(self.weights, dtype=np.float64)
        return float(np.clip(self.base_mean + w @ context, 0.0, 1.0))

    def sample_reward(self, context: np.ndarray, rng: np.random.Generator) -> float:
        noise = rng.normal(0.0, self.noise_stddev)
        return float(np.clip(self.base_mean + np.asarray(self.weights) @ context + noise, 0.0, 1.0))


@dataclass
class SynthAgent:
    id: str
    name: str
    capabilities: list[str]


# ---- Generation ----

def _make_agents(rng: np.random.Generator) -> list[SynthAgent]:
    agents: list[SynthAgent] = []
    for i in range(N_AGENTS):
        n_caps = int(rng.integers(MIN_CAPS_PER_AGENT, MAX_CAPS_PER_AGENT + 1))
        caps = list(rng.choice(TASK_TYPES, size=n_caps, replace=False))
        agents.append(SynthAgent(
            id=f"agent_{i:02d}",
            name=f"Synth Agent {i}",
            capabilities=caps,
        ))
    # Guarantee every task_type has at least one agent
    covered = {c for a in agents for c in a.capabilities}
    missing = set(TASK_TYPES) - covered
    for tt in missing:
        # Add the missing cap to a random agent
        idx = int(rng.integers(0, N_AGENTS))
        if tt not in agents[idx].capabilities:
            agents[idx].capabilities.append(tt)
    return agents


def _make_reward_model(rng: np.random.Generator) -> RewardModel:
    base_mean = float(rng.beta(2.0, 2.0))  # roughly U(0,1) but bell-shaped
    weights = np.zeros(FEATURE_DIM, dtype=np.float64)
    nonzero_idx = rng.choice(FEATURE_DIM, size=N_NONZERO_WEIGHTS, replace=False)
    weights[nonzero_idx] = rng.uniform(-WEIGHT_RANGE, WEIGHT_RANGE, size=N_NONZERO_WEIGHTS)
    return RewardModel(
        base_mean=base_mean,
        weights=weights.tolist(),
        noise_stddev=NOISE_STDDEV,
    )


def _make_reward_table(agents: list[SynthAgent], rng: np.random.Generator) -> dict[str, dict[str, RewardModel]]:
    """One RewardModel per (agent_id, task_type) the agent supports."""
    table: dict[str, dict[str, RewardModel]] = {}
    for agent in agents:
        table[agent.id] = {}
        for tt in agent.capabilities:
            table[agent.id][tt] = _make_reward_model(rng)
    return table


def _sample_context(rng: np.random.Generator) -> np.ndarray:
    """Random standardized context with bias term in last dim."""
    x = rng.normal(size=FEATURE_DIM - 1)
    x = x / (np.linalg.norm(x) + 1e-9)
    return np.concatenate([x, [1.0]])


def _optimal_agent(
    task_type: str,
    context: np.ndarray,
    agents: list[SynthAgent],
    table: dict[str, dict[str, RewardModel]],
) -> tuple[str, float]:
    best_id = ""
    best_r = -1.0
    for agent in agents:
        if task_type not in agent.capabilities:
            continue
        r = table[agent.id][task_type].expected_reward(context)
        if r > best_r:
            best_r = r
            best_id = agent.id
    return best_id, best_r


def generate(out_dir: Optional[Path] = None) -> tuple[Path, Path]:
    """Generate the dataset. Returns (agents_path, episodes_path)."""
    out_dir = out_dir or DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(SEED)
    agents = _make_agents(rng)
    table = _make_reward_table(agents, rng)

    # Save agents + reward model
    agents_path = out_dir / "synth-agents.json"
    agents_payload = {
        "seed": SEED,
        "feature_dim": FEATURE_DIM,
        "task_types": list(TASK_TYPES),
        "agents": [asdict(a) for a in agents],
        "reward_model": {
            agent_id: {tt: asdict(rm) for tt, rm in tt_map.items()}
            for agent_id, tt_map in table.items()
        },
    }
    with agents_path.open("w") as f:
        json.dump(agents_payload, f, indent=2)

    # Save episodes
    ep_path = out_dir / "synth-routing.jsonl"
    with ep_path.open("w") as f:
        for ep in range(N_EPISODES):
            tt = TASK_TYPES[ep % len(TASK_TYPES)] if ep < len(TASK_TYPES) else \
                 str(rng.choice(TASK_TYPES))
            ctx = _sample_context(rng)
            opt_id, opt_r = _optimal_agent(tt, ctx, agents, table)
            row = {
                "episode": ep,
                "task_type": tt,
                "context": ctx.tolist(),
                "optimal_agent": opt_id,
                "optimal_reward": opt_r,
            }
            f.write(json.dumps(row) + "\n")

    return agents_path, ep_path


# ---- Loaders for bench/test use ----

def load_dataset(data_dir: Optional[Path] = None) -> tuple[list[SynthAgent], dict, list[dict]]:
    """Load (agents, reward_table, episodes). reward_table is dict[agent_id][task_type] -> RewardModel."""
    data_dir = data_dir or DATA_DIR
    with (data_dir / "synth-agents.json").open() as f:
        bundle = json.load(f)
    agents = [SynthAgent(**a) for a in bundle["agents"]]
    table: dict[str, dict[str, RewardModel]] = {}
    for agent_id, tt_map in bundle["reward_model"].items():
        table[agent_id] = {tt: RewardModel(**rm) for tt, rm in tt_map.items()}

    episodes: list[dict] = []
    with (data_dir / "synth-routing.jsonl").open() as f:
        for line in f:
            episodes.append(json.loads(line))
    return agents, table, episodes


def sample_reward(
    agent_id: str,
    task_type: str,
    context: np.ndarray,
    table: dict,
    rng: np.random.Generator,
) -> float:
    """Sample a reward for a (agent, task_type) pick under context. Returns 0 if no model."""
    if agent_id not in table or task_type not in table[agent_id]:
        return 0.0
    return table[agent_id][task_type].sample_reward(context, rng)


if __name__ == "__main__":
    agents_path, ep_path = generate()
    agents, table, episodes = load_dataset()
    print(f"Generated {len(agents)} agents, {len(episodes)} episodes")
    print(f"  {agents_path}")
    print(f"  {ep_path}")
    # Sanity: per-task best agent
    rng = np.random.default_rng(0)
    for tt in TASK_TYPES:
        episodes_tt = [e for e in episodes if e["task_type"] == tt]
        avg_opt = float(np.mean([e["optimal_reward"] for e in episodes_tt]))
        n = len(episodes_tt)
        agents_with_cap = [a.id for a in agents if tt in a.capabilities]
        print(f"  task={tt:10s} n={n:4d} avg_optimal_reward={avg_opt:.3f} eligible_agents={len(agents_with_cap)}")
