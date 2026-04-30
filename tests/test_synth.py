"""Smoke tests for synthetic dataset generator."""

import json
from pathlib import Path

import numpy as np
import pytest

from bench import synth


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    synth.generate(out)
    return synth.load_dataset(out)


def test_generates_expected_counts(dataset):
    agents, table, episodes = dataset
    assert len(agents) == synth.N_AGENTS
    assert len(episodes) == synth.N_EPISODES


def test_every_task_type_has_at_least_one_capable_agent(dataset):
    agents, _, _ = dataset
    covered = {c for a in agents for c in a.capabilities}
    for tt in synth.TASK_TYPES:
        assert tt in covered, f"no agent supports {tt}"


def test_episodes_have_valid_optimal_agent(dataset):
    agents, _, episodes = dataset
    agent_ids = {a.id for a in agents}
    for ep in episodes[:50]:
        assert ep["optimal_agent"] in agent_ids
        assert 0.0 <= ep["optimal_reward"] <= 1.0
        assert len(ep["context"]) == synth.FEATURE_DIM


def test_reward_model_clipped_to_unit_interval(dataset):
    _, table, _ = dataset
    rng = np.random.default_rng(99)
    for agent_id, tt_map in table.items():
        for tt, rm in tt_map.items():
            ctx = synth._sample_context(rng)
            r = rm.sample_reward(ctx, rng)
            assert 0.0 <= r <= 1.0


def test_optimal_agent_truly_optimal_in_expectation(dataset):
    """For each episode, no other capable agent should have higher expected reward."""
    agents, table, episodes = dataset
    cap_index = {a.id: set(a.capabilities) for a in agents}
    for ep in episodes[:100]:
        ctx = np.array(ep["context"])
        tt = ep["task_type"]
        opt_r = ep["optimal_reward"]
        for a_id, caps in cap_index.items():
            if tt not in caps:
                continue
            r = table[a_id][tt].expected_reward(ctx)
            assert r <= opt_r + 1e-9, f"{a_id} beats optimal on episode {ep['episode']}"


def test_reproducible_seed():
    """Re-generate same dataset → identical output."""
    out_a = Path("/tmp/synth_a")
    out_b = Path("/tmp/synth_b")
    synth.generate(out_a)
    synth.generate(out_b)
    a_agents = json.loads((out_a / "synth-agents.json").read_text())
    b_agents = json.loads((out_b / "synth-agents.json").read_text())
    assert a_agents == b_agents
