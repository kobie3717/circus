# Bandit Routing Spec — Phase 1 / Day 2

_Drafted: 2026-04-29. Owner: Kobus._

## Problem

Today: caller of `POST /tasks` must specify `to_agent_id` directly. Discovery via semantic similarity (`/agents/discover-semantic`) returns ranked candidates, but caller chooses. **No outcome-driven learning.** Same agent gets picked for same task even after repeated failures.

Goal: add **auto-routing** that learns from task outcomes which agent is best for which (context, task_type) combination, while respecting trust + capability constraints.

## Non-Goals

- Replace existing manual routing — additive endpoint, opt-in
- Solve assignment globally (no scheduler — one task at a time, online decisions)
- Learn cross-instance — single-Circus scope. Federation comes later.

## Approach

**Contextual multi-armed bandit (LinUCB).**

- **Arms** = candidate agents (filtered by task_type capability + minimum trust + active status)
- **Context** = feature vector per `(task, requester, time)` decision
- **Reward** = scalar from task outcome
- **Update** = whenever a routed task hits a terminal state, push reward to bandit

LinUCB chosen over Thompson sampling because:
- Linear model = explainable + cheap inference (single matrix multiply per arm)
- Confidence bounds give good cold-start exploration
- Single hyperparameter (`alpha`) controls explore/exploit trade-off
- Easy to retrain offline + redeploy weights

Fallback: when no arm has enough samples (`n < 5`), fall back to existing semantic discovery score (cosine similarity from goal_router).

## API

### `POST /tasks/auto-route`

Auto-pick best agent for task. Submits task on caller's behalf.

```
POST /tasks/auto-route
Authorization: Bearer <agent-token>
Content-Type: application/json

{
  "task_type": "summarize",
  "payload": {...},
  "deadline": "2026-04-30T12:00:00Z",
  "output_schema": {...},
  "min_trust": 50,            // optional, default 30
  "exclude_agents": ["a1"],   // optional
  "explore_factor": 1.0       // optional alpha override
}
```

Response (201):
```json
{
  "task_id": "task-abc123",
  "to_agent_id": "agent-xyz",
  "routing_decision": {
    "score": 0.82,
    "confidence_bound": 0.91,
    "candidates_considered": 7,
    "fallback": "bandit",  // or "semantic" if cold start
    "context_hash": "sha256:..."
  },
  ... // standard TaskResponse fields
}
```

### `GET /routing/decisions/:task_id`

Inspect routing decision for a completed task. Includes context vector + score per candidate.

### `POST /routing/feedback/:task_id`

Optional override reward signal. Default: derived automatically from task state transition (COMPLETED → +1, FAILED → 0, cancelled/expired → -0.5).

```json
{
  "reward": 0.8,        // 0-1 scale, override default
  "reason": "user_corrected_output"
}
```

## Context Features

Per decision, build a context vector `x ∈ R^d`. Initial dimension `d = 32`.

| Feature group | Dim | Source |
|---|---|---|
| Task type one-hot | 8  | top-8 task types from history (`task_type` enum) |
| Task embedding | 8  | first 8 PCA components of payload-summary embedding (sentence-transformers) |
| Requester trust bucket | 4  | one-hot of [low, mid, high, super] from caller agent |
| Time-of-day | 4  | one-hot of [night, morning, afternoon, evening] (UTC bucket) |
| Task urgency | 1  | min(1, 24h / hours_to_deadline) |
| Payload size bucket | 3  | one-hot of [small <1KB, mid 1-10KB, large >10KB] |
| Bias term | 1  | constant 1.0 |
| Reserved | 3  | future expansion |

Context vector standardized (z-score) using running mean/std persisted in `routing_feature_stats` table.

## Reward Function

Default reward derived from task state at terminal:

| State | Reward |
|---|---|
| COMPLETED + output_schema validated | +1.0 |
| COMPLETED + no schema | +0.8 |
| COMPLETED + schema validation failed | +0.4 |
| FAILED | 0.0 |
| EXPIRED / TIMED_OUT | 0.0 |
| CANCELLED by requester | 0.5 (no signal) |
| CANCELLED by assignee | 0.0 |

Latency bonus: `+0.1 * (1 - actual_seconds / deadline_seconds)`, clamped to `[0, 0.1]`.

User override via `POST /routing/feedback/:task_id` replaces default reward.

## LinUCB Algorithm

For each arm `a` (candidate agent):

```
A_a    ∈ R^(d×d)   # cumulative covariance
b_a    ∈ R^d       # cumulative reward-weighted features
theta_a = A_a^-1 · b_a              # estimated weights
p_a     = theta_a · x + alpha · sqrt(x^T · A_a^-1 · x)   # UCB score
```

Pick `a* = argmax_a p_a`. After observing reward `r`:

```
A_a*  ← A_a* + x · x^T
b_a*  ← b_a* + r · x
```

Hyperparameter `alpha` (exploration weight): start `1.0`, decay to `0.1` over time.

## Cold Start

If `arm.n_samples < 5`:
- Fallback: use existing semantic similarity from `goal_router.find_matching_goals` semantics extended to agent capabilities
- Mark `routing_decision.fallback = "semantic"`
- Still update arm with reward — accumulates fast on early traffic

If no candidate at all → 503 with hint to seed agents.

## Persistence

New tables:

```sql
CREATE TABLE routing_arms (
  agent_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  A_blob BLOB NOT NULL,        -- d×d matrix as float32 bytes
  b_blob BLOB NOT NULL,        -- d-vector
  n_samples INTEGER DEFAULT 0,
  cumulative_reward REAL DEFAULT 0,
  last_updated_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, task_type)
);

CREATE TABLE routing_decisions (
  id TEXT PRIMARY KEY,           -- decision UUID
  task_id TEXT NOT NULL,
  picked_agent_id TEXT NOT NULL,
  context_hash TEXT NOT NULL,
  context_blob BLOB NOT NULL,    -- raw feature vector
  candidates_considered INTEGER,
  ucb_score REAL,
  fallback TEXT,                 -- "bandit" | "semantic"
  alpha REAL,
  created_at TEXT NOT NULL,
  reward REAL,                   -- nullable, set on terminal
  reward_reason TEXT,
  FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE routing_feature_stats (
  feature_idx INTEGER PRIMARY KEY,
  running_mean REAL,
  running_std REAL,
  n_samples INTEGER
);
```

## Trust + Safety Hooks

- Min trust gate respected (default 30, override per call)
- Quarantined agents excluded
- Capability mismatch → excluded (require `task_type` ∈ agent.capabilities)
- Sybil mitigation: agents with `n_samples > 1000` and `cumulative_reward / n < 0.1` auto-flagged for review
- Trust score itself used as a feature (bucketed) so bandit naturally avoids low-trust unless context demands

## Bench Plan

Synthetic dataset `data/synth-routing.jsonl`:
- 10 agents with hidden true reward distributions per task type
- 1000 episodes
- Compare: random / round-robin / semantic-only / LinUCB
- Metric: cumulative regret + final-100-episode mean reward

Target on synthetic:
- LinUCB ≥ 30% lower regret than semantic-only by episode 500
- LinUCB ≥ 50% lower regret than random by episode 100

## Module Layout

```
circus/services/
  bandit.py              # LinUCB implementation (numpy only)
  routing.py             # Context-builder + decision + persistence
circus/routes/
  routing.py             # /tasks/auto-route, /routing/decisions, /routing/feedback
circus/db/migrations/
  003_bandit_routing.py  # New tables
tests/
  test_bandit.py
  test_routing_e2e.py
bench/
  routing_bench.py       # Synthetic + report
data/
  synth-routing.jsonl
```

## Risks + Mitigations

| Risk | Mitigation |
|---|---|
| Sparse rewards (most tasks unfinished) | Decision row created on submit, reward filled async on terminal. Bandit only updates after reward arrives. |
| Reward delay biases recent picks | Acceptable — bandit handles delayed rewards naturally. |
| Adversarial agent inflates reward via own tasks | Reward source = `from_agent_id`. Self-routing tasks (`from == to`) rejected from bandit update. |
| Feature drift over time | Feature stats updated incrementally + drift dashboard in Phase 3. |
| Cold start exploration too aggressive → bad UX | Cap exploration in early days via dynamic alpha schedule. |

## Out of Scope (this sprint)

- Multi-armed bandit per-room (Phase 2 follow-up)
- Cross-instance federated bandit (Phase 3)
- Causal effect estimation (just associational learning)
- Off-policy evaluation harness (add later)

## Day 2 — DONE

Next: Day 3 — implement LinUCB bandit in `circus/services/bandit.py` (~100 lines, pure numpy).
