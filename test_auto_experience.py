#!/usr/bin/env python3
"""Test auto-experience logging on reward update."""
import json
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import update_reward

# Connect to database
db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Clean up any previous test data
cursor.execute("DELETE FROM agent_experiences WHERE environment='test-env'")
cursor.execute("DELETE FROM routing_decisions WHERE picked_agent_id='friday-174577' AND fallback='test'")
cursor.execute("DELETE FROM tasks WHERE id LIKE 'test-task-%'")
conn.commit()

# Create a test task with environment in payload
task_id = f"test-task-{secrets.token_hex(4)}"
payload = {"environment": "test-env", "description": "Test task for auto-experience"}
now = datetime.utcnow().isoformat()

cursor.execute("""
    INSERT INTO tasks (
        id, from_agent_id, to_agent_id, task_type, payload,
        state, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", (task_id, "circus-system", "friday-174577", "code-review", json.dumps(payload), "completed", now, now))

# Create a routing decision for this task
decision_id = f"decision-{secrets.token_hex(8)}"
cursor.execute("""
    INSERT INTO routing_decisions (
        id, task_id, picked_agent_id, context_hash, context_blob,
        candidates_considered, ucb_score, fallback, alpha, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (decision_id, task_id, "friday-174577", "test-hash", b"\x00" * 128, 3, 0.8, "bandit", 1.0, now))

conn.commit()

print("Test: Auto-logging experience on reward update")
print(f"  Task ID: {task_id}")
print(f"  Agent: friday-174577")
print(f"  Environment: test-env")
print(f"  Task type: code-review")

# Check experiences before reward
cursor.execute("""
    SELECT COUNT(*) FROM agent_experiences
    WHERE agent_id='friday-174577' AND environment='test-env' AND task_type='code-review'
""")
count_before = cursor.fetchone()[0]
print(f"\n  Experiences before reward: {count_before}")

# Update reward (this should auto-log an experience)
update_reward(task_id, 0.85, "test_reward", conn)
conn.commit()

# Check experiences after reward
cursor.execute("""
    SELECT id, confidence, observations, outcome FROM agent_experiences
    WHERE agent_id='friday-174577' AND environment='test-env' AND task_type='code-review'
""")
rows = cursor.fetchall()
count_after = len(rows)
print(f"  Experiences after reward: {count_after}")

if count_after > count_before:
    print("  ✓ Auto-experience was created!")
    exp_id, conf, obs, outcome = rows[0]
    print(f"    - Experience ID: {exp_id}")
    print(f"    - Confidence: {conf:.3f}")
    print(f"    - Observations: {obs}")
    print(f"    - Outcome: {outcome:.3f}")
else:
    print("  ✗ No auto-experience created")
    sys.exit(1)

# Test merging: call update_reward again with different reward
# First create another task
task_id2 = f"test-task-{secrets.token_hex(4)}"
cursor.execute("""
    INSERT INTO tasks (
        id, from_agent_id, to_agent_id, task_type, payload,
        state, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""", (task_id2, "circus-system", "friday-174577", "code-review", json.dumps(payload), "completed", now, now))

decision_id2 = f"decision-{secrets.token_hex(8)}"
cursor.execute("""
    INSERT INTO routing_decisions (
        id, task_id, picked_agent_id, context_hash, context_blob,
        candidates_considered, ucb_score, fallback, alpha, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (decision_id2, task_id2, "friday-174577", "test-hash2", b"\x00" * 128, 3, 0.8, "bandit", 1.0, now))
conn.commit()

print("\n  Testing experience merging with second reward...")
update_reward(task_id2, 0.90, "test_reward_2", conn)
conn.commit()

cursor.execute("""
    SELECT id, confidence, observations, outcome FROM agent_experiences
    WHERE agent_id='friday-174577' AND environment='test-env' AND task_type='code-review'
""")
rows = cursor.fetchall()
count_merged = len(rows)

if count_merged == count_after:
    print("  ✓ Experiences were merged (count unchanged)!")
    exp_id, conf, obs, outcome = rows[0]
    print(f"    - Updated confidence: {conf:.3f}")
    print(f"    - Updated observations: {obs}")
    print(f"    - Updated outcome: {outcome:.3f}")
    if obs == 2:
        print("  ✓ Observation count incremented correctly!")
else:
    print(f"  ✗ Expected 1 experience, got {count_merged}")
    sys.exit(1)

# Clean up
cursor.execute("DELETE FROM agent_experiences WHERE environment='test-env'")
cursor.execute("DELETE FROM routing_decisions WHERE picked_agent_id='friday-174577' AND context_hash LIKE 'test-%'")
cursor.execute("DELETE FROM tasks WHERE id LIKE 'test-task-%'")
conn.commit()
conn.close()

print("\n✓ All auto-experience tests passed!")
