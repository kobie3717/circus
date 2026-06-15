#!/usr/bin/env python3
"""Detailed test showing experience boost calculation."""
import json
import numpy as np
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import _get_experience_boost, build_context, standardize_context, get_candidate_agents
from circus.services.bandit import pick

# Connect to database
db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))
cursor = conn.cursor()

# Test payload
payload = {
    "environment": "hydra-note",
    "description": "Debug authentication issue"
}

print("Experience Boost Detailed Analysis")
print("=" * 60)

# Get agents
cursor.execute("SELECT id, name, trust_score FROM agents WHERE is_active=1 AND id IN ('octo-b0c49c', 'friday-174577')")
agents = cursor.fetchall()

print("\nAgents:")
for agent_id, name, trust in agents:
    print(f"  {agent_id} ({name}): trust={trust:.2f}")

# Check experiences
print("\nExperiences:")
for agent_id, name, trust in agents:
    cursor.execute("""
        SELECT confidence, observations, outcome FROM agent_experiences
        WHERE agent_id=? AND environment='hydra-note' AND task_type='debug'
    """, (agent_id,))
    exp = cursor.fetchone()
    if exp:
        conf, obs, outcome = exp
        print(f"  {name}: confidence={conf:.3f}, observations={obs}, outcome={outcome:.3f}")
    else:
        print(f"  {name}: no experience")

# Calculate boosts
print("\nExperience Boosts for hydra-note/debug:")
for agent_id, name, trust in agents:
    boost = _get_experience_boost(conn, "hydra-note", "debug", agent_id)
    print(f"  {name}: +{boost:.4f}")

# Get candidates with arms
print("\nLoading candidates with arm states...")
candidates = get_candidate_agents("debug", 30.0, [], conn)
print(f"  Found {len(candidates)} candidates")

# Build context
print("\nBuilding context vector...")
x_raw = build_context("debug", payload, "circus-system", None, conn)
x = standardize_context(x_raw, conn)
print(f"  Context vector shape: {x.shape}")

# Run bandit pick
print("\nRunning bandit selection (alpha=1.0)...")
idx, mean, ucb, all_ucbs = pick(candidates, x, alpha=1.0)

print("\nBase UCB scores (before experience boost):")
for i, (agent_id, arm) in enumerate(candidates):
    cursor.execute("SELECT name FROM agents WHERE id=?", (agent_id,))
    name = cursor.fetchone()[0]
    print(f"  {name}: {all_ucbs[i]:.4f}")

# Apply experience boosts
print("\nApplying experience boosts...")
boosted_ucbs = []
for i, (agent_id, _) in enumerate(candidates):
    exp_boost = _get_experience_boost(conn, "hydra-note", "debug", agent_id)
    boosted_score = all_ucbs[i] + exp_boost
    boosted_ucbs.append(boosted_score)

    cursor.execute("SELECT name FROM agents WHERE id=?", (agent_id,))
    name = cursor.fetchone()[0]
    print(f"  {name}: {all_ucbs[i]:.4f} + {exp_boost:.4f} = {boosted_score:.4f}")

# Pick best
best_idx = int(np.argmax(boosted_ucbs))
best_agent_id = candidates[best_idx][0]
cursor.execute("SELECT name FROM agents WHERE id=?", (best_agent_id,))
best_name = cursor.fetchone()[0]

print(f"\nFinal selection: {best_name} (score={boosted_ucbs[best_idx]:.4f})")

if best_agent_id == 'octo-b0c49c':
    print("✓ Octo was selected due to experience boost!")
else:
    print("⚠ Other agent selected (may need higher boost or better arm)")

# Test with different environment (no boost)
print("\n" + "=" * 60)
print("Test with different-env (no experience boost):")
print("=" * 60)

boosted_ucbs_2 = []
for i, (agent_id, _) in enumerate(candidates):
    exp_boost = _get_experience_boost(conn, "different-env", "debug", agent_id)
    boosted_score = all_ucbs[i] + exp_boost
    boosted_ucbs_2.append(boosted_score)

    cursor.execute("SELECT name FROM agents WHERE id=?", (agent_id,))
    name = cursor.fetchone()[0]
    print(f"  {name}: {all_ucbs[i]:.4f} + {exp_boost:.4f} = {boosted_score:.4f}")

best_idx_2 = int(np.argmax(boosted_ucbs_2))
best_agent_id_2 = candidates[best_idx_2][0]
cursor.execute("SELECT name FROM agents WHERE id=?", (best_agent_id_2,))
best_name_2 = cursor.fetchone()[0]

print(f"\nFinal selection: {best_name_2} (score={boosted_ucbs_2[best_idx_2]:.4f})")
print("(No experience boost, selection based on arm performance only)")

conn.close()
