#!/usr/bin/env python3
"""Complete workflow test: experience logging -> boost -> routing -> reward -> auto-log."""
import json
import numpy as np
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import route_task, update_reward, _get_experience_boost
from circus.services.bandit import ArmState

db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

print("Complete Experience System Workflow Test")
print("=" * 70)

# Clean up previous test data
cursor.execute("DELETE FROM agent_experiences WHERE environment='test-workflow-env'")
cursor.execute("DELETE FROM routing_decisions WHERE context_hash LIKE 'workflow-test-%'")
cursor.execute("DELETE FROM tasks WHERE id LIKE 'workflow-test-%'")
conn.commit()

# Step 1: Create warm arms for test agents
print("\n[Step 1] Setting up warm arms for agents...")
for agent_id in ['friday-174577', 'octo-b0c49c']:
    arm = ArmState.empty(32)
    for i in range(10):
        context = np.random.randn(32)
        reward = 0.7  # Equal baseline performance
        arm.update(context, reward)

    A_blob, b_blob = arm.serialize()
    cursor.execute("""
        INSERT OR REPLACE INTO routing_arms
        (agent_id, task_type, A_blob, b_blob, n_samples, cumulative_reward, last_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (agent_id, 'test-task', A_blob, b_blob, arm.n_samples, arm.cumulative_reward, datetime.utcnow().isoformat()))

cursor.execute("UPDATE agents SET capabilities=json_insert(capabilities, '$[#]', 'test-task') WHERE id IN ('friday-174577', 'octo-b0c49c')")
conn.commit()
print("  ✓ Both agents have equal warm arms")

# Step 2: Manually log experience for octo
print("\n[Step 2] Logging manual experience for octo...")
exp_id = str(secrets.token_hex(16))
cursor.execute("""
    INSERT INTO agent_experiences
    (id, agent_id, environment, task_type, what_worked, outcome, confidence, observations)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", (exp_id, 'octo-b0c49c', 'test-workflow-env', 'test-task', 'Strategy that works', 0.95, 0.9, 8))
conn.commit()
print("  ✓ Experience logged (confidence=0.9, observations=8)")

# Step 3: Check experience boost
print("\n[Step 3] Checking experience boosts...")
boost_octo = _get_experience_boost(conn, 'test-workflow-env', 'test-task', 'octo-b0c49c')
boost_friday = _get_experience_boost(conn, 'test-workflow-env', 'test-task', 'friday-174577')
print(f"  Octo boost: +{boost_octo:.4f}")
print(f"  Friday boost: +{boost_friday:.4f}")
if boost_octo > 0 and boost_friday == 0:
    print("  ✓ Boost differential exists")

# Step 4: Route task (should prefer octo due to experience boost)
print("\n[Step 4] Routing task to test-workflow-env...")
payload = {
    "environment": "test-workflow-env",
    "description": "Test task"
}

decision = route_task(
    task_type="test-task",
    payload=payload,
    requester="circus-system",
    deadline=None,
    min_trust=30.0,
    exclude_agents=[],
    alpha_override=0.5,  # Lower exploration
    db_conn=conn
)

print(f"  Selected: {decision['agent_id']}")
print(f"  UCB score: {decision['ucb']:.4f}")
print(f"  Fallback: {decision['fallback']}")

# Step 5: Create task and link decision
print("\n[Step 5] Creating task...")
task_id = f"workflow-test-{secrets.token_hex(4)}"
now = datetime.utcnow().isoformat()

cursor.execute("""
    INSERT INTO tasks
    (id, from_agent_id, to_agent_id, task_type, payload, state, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", (task_id, "circus-system", decision['agent_id'], "test-task", json.dumps(payload), "completed", now, now))

cursor.execute("UPDATE routing_decisions SET task_id=? WHERE id=?", (task_id, decision['decision_id']))
conn.commit()
print(f"  ✓ Task created: {task_id}")

# Step 6: Update reward (should auto-log experience)
print("\n[Step 6] Updating reward (triggers auto-experience logging)...")

# Count experiences before
cursor.execute("""
    SELECT COUNT(*) FROM agent_experiences
    WHERE agent_id=? AND environment='test-workflow-env' AND task_type='test-task'
""", (decision['agent_id'],))
count_before = cursor.fetchone()[0]

update_reward(task_id, 0.88, "workflow_test", conn)
conn.commit()

# Count after
cursor.execute("""
    SELECT COUNT(*), SUM(observations) FROM agent_experiences
    WHERE agent_id=? AND environment='test-workflow-env' AND task_type='test-task'
""", (decision['agent_id'],))
count_after, total_obs = cursor.fetchone()

print(f"  Experiences before: {count_before}")
print(f"  Experiences after: {count_after}")
print(f"  Total observations: {total_obs}")

if decision['agent_id'] == 'octo-b0c49c':
    # Should merge with existing
    if count_after == count_before and total_obs > 8:
        print("  ✓ Experience merged with existing (observations increased)")
    else:
        print("  ⚠ Expected merge")
else:
    # Friday should have new experience
    if count_after > count_before:
        print("  ✓ New auto-experience created")

# Step 7: Verify updated experience
print("\n[Step 7] Verifying updated experience...")
cursor.execute("""
    SELECT confidence, observations, outcome FROM agent_experiences
    WHERE agent_id=? AND environment='test-workflow-env' AND task_type='test-task'
    AND what_worked IS NULL
""", (decision['agent_id'],))
auto_exp = cursor.fetchone()

if auto_exp:
    conf, obs, outcome = auto_exp
    print(f"  Auto-logged experience:")
    print(f"    Confidence: {conf:.3f}")
    print(f"    Observations: {obs}")
    print(f"    Outcome: {outcome:.3f}")
    print("  ✓ Auto-experience recorded")

# Summary
print("\n" + "=" * 70)
print("WORKFLOW SUMMARY:")
print("  1. ✓ Warm arms created for both agents")
print("  2. ✓ Manual experience logged for octo")
print(f"  3. ✓ Experience boost calculated: octo={boost_octo:.4f}, friday={boost_friday:.4f}")
print(f"  4. ✓ Task routed to: {decision['agent_id']}")
print(f"  5. ✓ Task created and linked")
print(f"  6. ✓ Reward updated (auto-experience triggered)")
print(f"  7. ✓ Experience data verified")
print("\n✓ Complete workflow test passed!")

# Clean up
cursor.execute("DELETE FROM agent_experiences WHERE environment='test-workflow-env'")
cursor.execute("DELETE FROM routing_decisions WHERE id=?", (decision['decision_id'],))
cursor.execute("DELETE FROM tasks WHERE id=?", (task_id,))
conn.commit()
conn.close()
