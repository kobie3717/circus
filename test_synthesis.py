#!/usr/bin/env python3
"""Test backpressure synthesis endpoints (Round 3 Gap 2)."""

import requests
import json
import secrets
import time

BASE_URL = "http://localhost:6200"

def register_test_agent():
    """Register a test agent and return the token."""
    agent_name = f"SynthTest-{secrets.token_hex(3)}"

    passport = {
        "identity": {
            "name": agent_name,
            "role": "test-agent",
            "capabilities": ["testing", "synthesis"]
        },
        "score": {
            "trust_score": 50.0,
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
        "capabilities": ["testing", "synthesis"],
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

def test_queue_depth(token):
    """Test /queue-depth endpoint."""
    print("\n--- Testing /queue-depth ---")
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.get(f"{BASE_URL}/api/v1/tasks/queue-depth", headers=headers)
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Queue depth: realtime={data['realtime_pending']}, deferrable={data['deferrable_pending']}")
        print(f"  Synthesis threshold: {data['synthesis_threshold']}")
        print(f"  Synthesis recommended: {data['synthesis_recommended']}")
        return data
    else:
        print(f"✗ Failed: {response.status_code}")
        print(response.text)
        return None

def submit_test_tasks(token, agent_id, count=12, task_type="analytics"):
    """Submit multiple deferrable tasks to trigger synthesis."""
    print(f"\n--- Submitting {count} deferrable tasks ---")
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(count):
        task_data = {
            "to_agent_id": agent_id,
            "task_type": task_type,
            "payload": {"test_id": i, "data": f"test data {i}"},
            "priority_tier": "deferrable"
        }

        response = requests.post(f"{BASE_URL}/api/v1/tasks", json=task_data, headers=headers)
        if response.status_code == 201:
            print(f"  ✓ Task {i+1} submitted")
        else:
            print(f"  ✗ Task {i+1} failed: {response.status_code}")

    print(f"✓ Submitted {count} tasks")

def test_synthesis(token):
    """Test manual /synthesize endpoint."""
    print("\n--- Testing manual synthesis ---")
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.post(f"{BASE_URL}/api/v1/tasks/synthesize", headers=headers)
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Synthesis completed:")
        print(f"  - Tasks consumed: {data['tasks_consumed']}")
        print(f"  - Tasks created: {data['tasks_created']}")
        print(f"  - Compression ratio: {data['compression_ratio']}")
        print(f"  - Groups: {len(data['groups'])}")
        return data
    else:
        print(f"✗ Failed: {response.status_code}")
        print(response.text)
        return None

def test_synthesis_log(token):
    """Test /synthesis-log endpoint."""
    print("\n--- Testing synthesis log ---")
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.get(f"{BASE_URL}/api/v1/tasks/synthesis-log", headers=headers)
    if response.status_code == 200:
        data = response.json()
        events = data.get('events', [])
        print(f"✓ Found {len(events)} synthesis events:")
        for event in events[:3]:  # Show first 3
            print(f"  - {event['id']}: {event['tasks_consumed']} → {event['tasks_created']} (ratio: {event['compression_ratio']})")
        return data
    else:
        print(f"✗ Failed: {response.status_code}")
        print(response.text)
        return None

def main():
    print("=" * 60)
    print("Backpressure Synthesis Test (Round 3 Gap 2)")
    print("=" * 60)

    # Register test agent
    token, agent_id = register_test_agent()
    if not token:
        print("✗ Cannot proceed without valid token")
        return

    # Test 1: Check initial queue depth
    initial_depth = test_queue_depth(token)

    # Test 2: Submit tasks to trigger synthesis
    submit_test_tasks(token, agent_id, count=12, task_type="analytics")

    # Wait a moment
    time.sleep(1)

    # Test 3: Check queue depth after submission
    print("\n--- Queue depth after submission ---")
    after_depth = test_queue_depth(token)

    # Test 4: Manual synthesis
    synthesis_result = test_synthesis(token)

    # Wait a moment
    time.sleep(1)

    # Test 5: Check queue depth after synthesis
    print("\n--- Queue depth after synthesis ---")
    final_depth = test_queue_depth(token)

    # Test 6: Check synthesis log
    test_synthesis_log(token)

    print("\n" + "=" * 60)
    print("Test Summary:")
    print("=" * 60)
    if initial_depth and after_depth and final_depth:
        print(f"Initial deferrable tasks: {initial_depth['deferrable_pending']}")
        print(f"After submission: {after_depth['deferrable_pending']}")
        print(f"After synthesis: {final_depth['deferrable_pending']}")
        if synthesis_result:
            print(f"Compression ratio: {synthesis_result['compression_ratio']}")
            print("✓ All tests passed!")
        else:
            print("⚠ Synthesis test incomplete")
    else:
        print("✗ Some tests failed")
    print("=" * 60)

if __name__ == "__main__":
    main()
