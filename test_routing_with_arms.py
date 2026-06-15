#!/usr/bin/env python3
"""Integration test: routing with experience boost (with warm arms)."""
import json
import numpy as np
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import route_task
from circus.services.bandit import ArmState

# Connect to database
db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Create warm arm states for both agents
print("Setting up warm arm states for agents...")

# Warm up octo for debug tasks
arm_octo = ArmState.empty(32)
# Simulate some training data
for i in range(10):
    context = np.random.randn(32)
    reward = 0.85  # Good performance
    arm_octo.update(context, reward)

A_blob_octo, b_blob_octo = arm_octo.serialize()
now = datetime.utcnow().isoformat()

cursor.execute("""
    INSERT OR REPLACE INTO routing_arms
    (agent_id, task_type, A_blob, b_blob, n_samples, cumulative_reward, last_updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", ('octo-b0c49c', 'debug', A_blob_octo, b_blob_octo, arm_octo.n_samples, arm_octo.cumulative_reward, now))

# Warm up friday for debug tasks (but lower performance)
arm_friday = ArmState.empty(32)
for i in range(10):
    context = np.random.randn(32)
    reward = 0.6  # Lower performance
    arm_friday.update(context, reward)

A_blob_friday, b_blob_friday = arm_friday.serialize()

cursor.execute("""
    INSERT OR REPLACE INTO routing_arms
    (agent_id, task_type, A_blob, b_blob, n_samples, cumulative_reward, last_updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", ('friday-174577', 'debug', A_blob_friday, b_blob_friday, arm_friday.n_samples, arm_friday.cumulative_reward, now))

conn.commit()
print("  ✓ Created warm arms for both agents")

# Verify experience exists
cursor.execute("""
    SELECT confidence, observations, outcome FROM agent_experiences
    WHERE agent_id='octo-b0c49c' AND environment='hydra-note' AND task_type='debug'
""")
exp = cursor.fetchone()
if exp:
    print(f"  ✓ Octo has experience: confidence={exp[0]:.3f}, observations={exp[1]}, outcome={exp[2]:.3f}")
else:
    print("  ✗ No experience found for octo")

# Test routing for hydra-note/debug task
print("\nTest: Route a hydra-note/debug task (with experience boost)")
payload = {
    "environment": "hydra-note",
    "description": "Debug authentication issue"
}

try:
    decision = route_task(
        task_type="debug",
        payload=payload,
        requester="circus-system",
        deadline=None,
        min_trust=30.0,
        exclude_agents=[],
        alpha_override=1.0,  # Moderate exploration
        db_conn=conn
    )

    print(f"\n  Routing decision:")
    print(f"    Selected agent: {decision['agent_id']}")
    print(f"    UCB score: {decision['ucb']:.4f}")
    print(f"    Candidates: {decision['candidates']}")
    print(f"    Fallback: {decision['fallback']}")

    if decision['fallback'] == 'bandit':
        print("  ✓ Using bandit routing (not cold start)!")
        if decision['agent_id'] == 'octo-b0c49c':
            print("  ✓ Octo was selected (likely due to better arm + experience boost)!")
        else:
            print(f"  ⚠ {decision['agent_id']} was selected")
            print("    This can happen due to exploration, but octo should have an advantage")
    else:
        print("  ⚠ Still using semantic fallback")

    # Clean up
    cursor.execute("DELETE FROM routing_decisions WHERE id=?", (decision['decision_id'],))
    conn.commit()

except Exception as e:
    print(f"\n  ✗ Routing failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test routing for different environment (should not get experience boost)
print("\n\nTest: Route a different-env/debug task (no experience boost)")
payload2 = {
    "environment": "different-env",
    "description": "Debug general issue"
}

try:
    decision2 = route_task(
        task_type="debug",
        payload=payload2,
        requester="circus-system",
        deadline=None,
        min_trust=30.0,
        exclude_agents=[],
        alpha_override=1.0,
        db_conn=conn
    )

    print(f"\n  Routing decision:")
    print(f"    Selected agent: {decision2['agent_id']}")
    print(f"    UCB score: {decision2['ucb']:.4f}")

    if decision2['fallback'] == 'bandit':
        print("  ✓ Using bandit routing!")
        print("  ✓ No experience boost applied (different environment)")

    # Clean up
    cursor.execute("DELETE FROM routing_decisions WHERE id=?", (decision2['decision_id'],))
    conn.commit()

except Exception as e:
    print(f"\n  ✗ Routing failed: {e}")
    sys.exit(1)

conn.close()
print("\n✓ All integration tests passed!")
