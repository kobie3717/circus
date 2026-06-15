# Agent Experiences Integration into Circus Routing

## Summary

Successfully integrated the `agent_experiences` system into the Circus LinUCB router. The router now:
1. Queries experiences before routing to boost UCB scores for agents with relevant experience
2. Auto-logs experiences after task completion to build knowledge over time

## Changes Made

### File: `/root/circus/circus/services/routing.py`

#### Added Imports
```python
import math
import uuid
```

#### New Helper Function: `_get_experience_boost()`
- **Location**: After line 24 (after FEATURE_DIM constant)
- **Purpose**: Query agent_experiences table and calculate UCB boost
- **Logic**:
  - Queries for experiences matching (agent_id, environment, task_type)
  - Requires confidence >= 0.5
  - Calculates boost as: `0.25 × confidence × trust_factor × obs_factor`
  - Max boost: +0.25
  - Returns 0.0 if no relevant experience found
- **Non-fatal**: Wrapped in try/except, returns 0.0 on any error

#### New Helper Function: `_auto_log_experience()`
- **Location**: After `_get_experience_boost()`
- **Purpose**: Auto-log experience after task completion
- **Logic**:
  - Checks for existing auto-logged experience (no what_worked/what_failed)
  - If exists: merges using Bayesian update on confidence
  - If new: creates experience with confidence = max(0.3, outcome)
  - Increments observation count
- **Non-fatal**: Wrapped in try/except with debug logging

#### Modified: `route_task()` function
- **Lines changed**: ~326-348 (cold start and bandit selection block)
- **Changes**:
  1. Extract `environment` from payload: `payload.get("environment", "general")`
  2. After bandit UCB calculation, compute experience boost for each candidate
  3. Apply boost to all UCB scores: `boosted_score = ucb + experience_boost`
  4. Re-pick agent with highest boosted score
  5. Log boost values at debug level when boost > 0
- **Preserves**: Original behavior when no experiences exist (boost = 0.0)

#### Modified: `update_reward()` function
- **Lines changed**: ~507-519 (after arm update, before function end)
- **Changes**:
  1. After marking decision as rewarded, query task payload
  2. Extract environment from payload
  3. Call `_auto_log_experience()` with (agent_id, environment, task_type, reward)
- **Non-fatal**: Wrapped in try/except, logs debug message on failure

## How It Works

### Before Routing (Experience Boost)
```
Task arrives with payload: {"environment": "hydra-note", ...}
↓
Build context vector (32 dims)
↓
Load candidate agents with arms
↓
Calculate base UCB scores for all candidates
↓
For each candidate:
  - Query agent_experiences for (agent_id, "hydra-note", task_type)
  - Calculate boost (0 to +0.25)
  - Add boost to UCB score
↓
Pick agent with highest boosted UCB score
```

### After Task Completion (Auto-Experience Logging)
```
Task completes, reward calculated
↓
update_reward() called
↓
Update LinUCB arm (existing logic)
↓
Extract environment from task payload
↓
Check for existing auto-logged experience:
  - If exists: merge (Bayesian update confidence, increment observations)
  - If new: create (confidence = max(0.3, reward))
↓
Experience ready for next routing decision
```

## Boost Calculation Formula

```python
# Scale factors
obs_factor = min(1.0, log(observations + 1) / log(11))  # 0.0 to 1.0
trust_factor = agent_trust_score / 100.0                # 0.0 to 1.0

# Final boost
boost = 0.25 × confidence × trust_factor × obs_factor
```

**Example**:
- Agent: octo-b0c49c (trust=45)
- Experience: confidence=0.9, observations=10, outcome=0.95
- Boost = 0.25 × 0.9 × 0.45 × 1.0 = **0.1013**

## Test Results

### Test 1: Experience Boost Calculation
```
✓ Boost for octo in hydra-note/debug: +0.0757
✓ Boost for different environment: 0.0000
✓ Boost for different agent: 0.0000
```

### Test 2: Auto-Experience Logging
```
✓ First reward creates new experience (conf=0.85, obs=1)
✓ Second reward merges (conf=0.875, obs=2)
✓ Observation count incremented correctly
```

### Test 3: Complete Workflow
```
✓ Experience boost calculated correctly
✓ UCB scores boosted appropriately
✓ Task routed using bandit (not cold start)
✓ Reward update auto-logs experience
✓ Experience data persists for next routing
```

## Impact

### Positive
- Agents with proven track record in specific environments get routing advantage
- System learns from every completed task (auto-experience)
- Non-breaking: zero boost when no experiences exist
- Scales with observation count (more observations = higher boost ceiling)
- Weighted by agent trust (low-trust agents get lower boost)

### Trade-offs
- Experience boost can override arm performance differences (up to 0.25 UCB points)
- Auto-logged experiences have no narrative (what_worked/what_failed are NULL)
- Requires environment field in task payload (defaults to "general")

## Future Enhancements

1. **Decay old experiences**: Weight recent experiences higher than old ones
2. **Cross-environment transfer**: Allow partial boost for similar environments
3. **Confidence calibration**: Auto-adjust confidence based on outcome variance
4. **Manual experience priority**: Weight manually logged experiences higher than auto-logged

## Files Changed

- `/root/circus/circus/services/routing.py` (main integration)

## Test Files Created

- `/root/circus/test_experience_boost.py` (boost calculation)
- `/root/circus/test_auto_experience.py` (auto-logging)
- `/root/circus/test_experience_boost_detailed.py` (detailed analysis)
- `/root/circus/test_complete_workflow.py` (end-to-end)

## Deployment

```bash
pm2 restart circus-api
# No database migrations needed (agent_experiences table already exists)
```

## Verification

```bash
# Check API health
curl http://localhost:6200/health

# Query experiences
curl "http://localhost:6200/api/v1/experiences/query?environment=hydra-note&task_type=debug"

# Run tests
cd /root/circus
python3 test_complete_workflow.py
```
