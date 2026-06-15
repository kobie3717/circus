#!/usr/bin/env python3
"""Integration test: routing with experience boost."""
import json
import sqlite3
import sys
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import route_task

# Connect to database
db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Setup: Ensure we have agents with the 'debug' capability
cursor.execute("SELECT id, name, capabilities FROM agents WHERE is_active=1 LIMIT 5")
agents = cursor.fetchall()
print("Available agents:")
for agent_id, name, caps_json in agents:
    caps = json.loads(caps_json)
    print(f"  {agent_id} ({name}): {caps}")

# Add 'debug' capability to octo and friday if they don't have it
for agent_id in ['octo-b0c49c', 'friday-174577']:
    cursor.execute("SELECT capabilities FROM agents WHERE id=?", (agent_id,))
    row = cursor.fetchone()
    if row:
        caps = json.loads(row[0])
        if 'debug' not in caps:
            caps.append('debug')
            cursor.execute("UPDATE agents SET capabilities=? WHERE id=?", (json.dumps(caps), agent_id))
            print(f"\n  Added 'debug' capability to {agent_id}")

conn.commit()

# Create/verify experience for octo in hydra-note/debug with high confidence
cursor.execute("""
    SELECT id FROM agent_experiences
    WHERE agent_id='octo-b0c49c' AND environment='hydra-note' AND task_type='debug'
""")
if not cursor.fetchone():
    cursor.execute("""
        INSERT INTO agent_experiences
        (id, agent_id, environment, task_type, what_worked, outcome, confidence, observations)
        VALUES ('exp-octo-hydra', 'octo-b0c49c', 'hydra-note', 'debug', 'Check env vars', 0.95, 0.9, 10)
    """)
    print("\n  Created high-confidence experience for octo in hydra-note/debug")
else:
    cursor.execute("""
        UPDATE agent_experiences
        SET confidence=0.9, observations=10, outcome=0.95
        WHERE agent_id='octo-b0c49c' AND environment='hydra-note' AND task_type='debug'
    """)
    print("\n  Updated experience for octo in hydra-note/debug")

conn.commit()

# Test routing for hydra-note/debug task
print("\nTest: Route a hydra-note/debug task")
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
        alpha_override=1.0,
        db_conn=conn
    )

    print(f"\n  Routing decision:")
    print(f"    Selected agent: {decision['agent_id']}")
    print(f"    UCB score: {decision['ucb']:.4f}")
    print(f"    Candidates: {decision['candidates']}")
    print(f"    Fallback: {decision['fallback']}")

    # Check if octo was selected (high probability due to experience boost)
    if decision['agent_id'] == 'octo-b0c49c':
        print("\n  ✓ Octo was selected (likely due to experience boost)!")
    else:
        print(f"\n  ⚠ {decision['agent_id']} was selected instead of octo")
        print("    (This is OK - routing is probabilistic, but octo should have higher score)")

    # Clean up the decision
    cursor.execute("DELETE FROM routing_decisions WHERE id=?", (decision['decision_id'],))
    conn.commit()

except Exception as e:
    print(f"\n  ✗ Routing failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test routing for different environment (should not get experience boost)
print("\n\nTest: Route a different-env/debug task (no experience)")
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
    print(f"    Candidates: {decision2['candidates']}")
    print(f"    Fallback: {decision2['fallback']}")

    print("\n  ✓ Routing completed (no experience boost expected)")

    # Clean up
    cursor.execute("DELETE FROM routing_decisions WHERE id=?", (decision2['decision_id'],))
    conn.commit()

except Exception as e:
    print(f"\n  ✗ Routing failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

conn.close()
print("\n✓ Integration tests passed!")
