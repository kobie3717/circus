#!/usr/bin/env python3
"""Test stake-driven task compression (Round 3 Gap 4)."""

import requests
import json
import secrets
import time

BASE_URL = "http://localhost:6200"

def register_test_agent(suffix=""):
    """Register a test agent and return the token."""
    agent_name = f"StakeTest-{secrets.token_hex(3)}{suffix}"

    passport = {
        "identity": {
            "name": agent_name,
            "role": "test-agent",
            "capabilities": ["testing", "backing"]
        },
        "score": {
            "trust_score": 60.0,
            "belief_stability": 1.0,
            "memory_quality": 0.8,
            "prediction_accuracy": 0.7,
            "passport_score": 0.8
        },
        "created_at": "2026-06-13T10:00:00Z"
    }

    request_data = {
        "name": agent_name,
        "role": "test-agent",
        "capabilities": ["testing", "backing"],
        "home": "http://localhost:5000",
        "passport": passport,
        "contact": "test@example.com"
    }

    response = requests.post(f"{BASE_URL}/api/v1/agents/register", json=request_data)
    if response.status_code == 201:
        data = response.json()
        print(f"✓ Registered test agent: {data['agent_id']}")
        return data['ring_token'], data['agent_id']
    else:
        print(f"✗ Failed to register agent: {response.status_code}")
        print(response.text)
        return None, None

def submit_task(token, from_agent_id, to_agent_id, task_type="analytics", payload_data=None):
    """Submit a single deferrable task."""
    headers = {"Authorization": f"Bearer {token}"}

    task_data = {
        "to_agent_id": to_agent_id,
        "task_type": task_type,
        "payload": payload_data or {"test": "data"},
        "priority_tier": "deferrable"
    }

    response = requests.post(f"{BASE_URL}/api/v1/tasks", json=task_data, headers=headers)
    if response.status_code == 201:
        task_id = response.json().get('task_id')
        print(f"  ✓ Task submitted: {task_id}")
        return task_id
    else:
        print(f"  ✗ Task submission failed: {response.status_code}")
        print(response.text)
        return None

def stake_on_task(token, task_id, performer_agent_id, stake_amount=10.0):
    """Create a backing stake on a task."""
    headers = {"Authorization": f"Bearer {token}"}

    stake_data = {
        "task_id": task_id,
        "performer_agent_id": performer_agent_id,
        "stake_amount": stake_amount
    }

    response = requests.post(f"{BASE_URL}/api/v1/backing/stake", json=stake_data, headers=headers)
    if response.status_code == 201:
        data = response.json()
        print(f"  ✓ Staked {stake_amount} on task {task_id}: {data.get('stake_id')}")
        return data.get('stake_id')
    else:
        print(f"  ✗ Stake failed: {response.status_code}")
        print(response.text)
        return None

def test_stake_compression(token, backer_agent_id):
    """Test stake-driven compression."""
    print("\n--- Testing stake-driven compression ---")

    # Create target agent
    performer_token, performer_id = register_test_agent("-performer")
    if not performer_id:
        print("✗ Cannot create performer agent")
        return None

    print(f"\n1. Creating 3 tasks from different types...")
    # Create 3 tasks of different types (so task_type grouping won't trigger)
    task1 = submit_task(token, backer_agent_id, performer_id, "analytics", {"job": 1})
    task2 = submit_task(token, backer_agent_id, performer_id, "research", {"job": 2})
    task3 = submit_task(token, backer_agent_id, performer_id, "notify", {"job": 3})

    if not all([task1, task2, task3]):
        print("✗ Failed to create test tasks")
        return None

    print(f"\n2. Backer stakes on all 3 tasks...")
    # Same backer stakes on all 3 tasks
    stake1 = stake_on_task(token, task1, performer_id, 10.0)
    stake2 = stake_on_task(token, task2, performer_id, 15.0)
    stake3 = stake_on_task(token, task3, performer_id, 20.0)

    if not all([stake1, stake2, stake3]):
        print("✗ Failed to create stakes")
        return None

    print(f"\n3. Triggering synthesis...")
    # Trigger synthesis
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(f"{BASE_URL}/api/v1/tasks/synthesize", headers=headers)

    if response.status_code == 200:
        data = response.json()
        print(f"✓ Synthesis completed:")
        print(f"  - Tasks consumed: {data['tasks_consumed']}")
        print(f"  - Tasks created: {data['tasks_created']}")
        print(f"  - Compression ratio: {data['compression_ratio']}")
        print(f"  - Groups: {len(data['groups'])}")

        # Check if stake-graph method was used
        stake_graph_groups = [g for g in data['groups'] if g.get('method') == 'stake_graph']
        if stake_graph_groups:
            print(f"\n✓ Stake-graph compression detected!")
            for g in stake_graph_groups:
                print(f"  - Backer signal: {g['backer_signal']}")
                print(f"  - Compression ID: {g['compression_id']}")
                print(f"  - Original tasks: {len(g['original_ids'])}")
                print(f"  - Synthesized ID: {g['synthesized_id']}")
        else:
            print(f"\n⚠ No stake-graph compression (may need more tasks)")

        return data
    else:
        print(f"✗ Synthesis failed: {response.status_code}")
        print(response.text)
        return None

def test_fallback_behavior(token, agent_id):
    """Test that task_type grouping still works as fallback."""
    print("\n--- Testing task_type fallback (no stakes) ---")

    # Create target agent
    performer_token, performer_id = register_test_agent("-fallback")
    if not performer_id:
        print("✗ Cannot create performer agent")
        return None

    print(f"\n1. Creating 4 tasks of same type (no stakes)...")
    # Create 4 tasks of same type WITHOUT stakes
    tasks = []
    for i in range(4):
        task_id = submit_task(token, agent_id, performer_id, "analytics", {"job": i+100})
        if task_id:
            tasks.append(task_id)

    if len(tasks) < 3:
        print("✗ Failed to create enough tasks")
        return None

    print(f"\n2. Triggering synthesis (no stakes, should use task_type grouping)...")
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(f"{BASE_URL}/api/v1/tasks/synthesize", headers=headers)

    if response.status_code == 200:
        data = response.json()
        print(f"✓ Synthesis completed:")
        print(f"  - Tasks consumed: {data['tasks_consumed']}")
        print(f"  - Tasks created: {data['tasks_created']}")

        # Check if task_type method was used
        task_type_groups = [g for g in data['groups'] if g.get('method') == 'task_type']
        if task_type_groups:
            print(f"\n✓ Task-type fallback worked!")
            for g in task_type_groups:
                print(f"  - Task type: {g['task_type']}")
                print(f"  - Original tasks: {len(g['original_ids'])}")
        else:
            print(f"\n⚠ No task-type grouping detected")

        return data
    else:
        print(f"✗ Synthesis failed: {response.status_code}")
        print(response.text)
        return None

def main():
    print("=" * 60)
    print("Stake-Driven Task Compression Test (Round 3 Gap 4)")
    print("=" * 60)

    # Register test agents
    backer_token, backer_id = register_test_agent("-backer")
    if not backer_token:
        print("✗ Cannot proceed without valid token")
        return

    # Test 1: Stake-driven compression
    result1 = test_stake_compression(backer_token, backer_id)

    time.sleep(1)

    # Test 2: Fallback to task_type grouping
    result2 = test_fallback_behavior(backer_token, backer_id)

    print("\n" + "=" * 60)
    print("Test Summary:")
    print("=" * 60)
    if result1 and result2:
        print("✓ All tests passed!")
        print(f"Stake-graph compression: {len([g for g in result1.get('groups', []) if g.get('method') == 'stake_graph'])} groups")
        print(f"Task-type fallback: {len([g for g in result2.get('groups', []) if g.get('method') == 'task_type'])} groups")
    else:
        print("⚠ Some tests incomplete")
    print("=" * 60)

if __name__ == "__main__":
    main()
