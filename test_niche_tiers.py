#!/usr/bin/env python3
"""Test niche tier classification endpoints."""

import requests
import json
import sys

BASE_URL = "http://localhost:6200"

# Get a valid token from an existing agent
# For testing, we'll use the circus-system agent or check what's available
def get_test_token():
    """Get test token by registering a test agent."""
    register_url = f"{BASE_URL}/api/v1/agents/register"
    payload = {
        "name": "NicheTester",
        "role": "tester",
        "capabilities": ["testing"],
        "home": "http://localhost:6200",
        "passport": {
            "identity": {"name": "NicheTester", "role": "tester"},
            "capabilities": ["testing"],
            "predictions": {"confirmed": 0, "refuted": 0},
            "beliefs": {"total": 0, "contradictions": 0},
            "memory_stats": {"proof_count_avg": 0, "graph_connections": 0},
            "score": {"total": 5},
            "graph_summary": {"entities": []},
            "traits": {}
        }
    }

    try:
        resp = requests.post(register_url, json=payload)
        if resp.status_code == 201:
            return resp.json()["ring_token"]
        else:
            print(f"Registration failed: {resp.status_code} - {resp.text}")
            # Try to use an existing agent's path
            return None
    except Exception as e:
        print(f"Error getting token: {e}")
        return None

def test_list_niches(token):
    """Test GET /api/v1/tasks/niches"""
    print("\n" + "="*60)
    print("TEST: List all niches")
    print("="*60)

    url = f"{BASE_URL}/api/v1/tasks/niches"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, headers=headers)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\nFound {len(data['niches'])} registered niches:")
        for niche in data['niches']:
            print(f"  - {niche['task_type']}: {niche['tier']} (min_trust={niche['min_trust']})")
        return True
    else:
        print(f"Error: {resp.text}")
        return False

def test_get_niche(token, task_type):
    """Test GET /api/v1/tasks/niches/{task_type}"""
    print("\n" + "="*60)
    print(f"TEST: Get niche for '{task_type}'")
    print("="*60)

    url = f"{BASE_URL}/api/v1/tasks/niches/{task_type}"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, headers=headers)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\nNiche details:")
        print(json.dumps(data, indent=2))
        return True
    else:
        print(f"Error: {resp.text}")
        return False

def test_register_niche(token):
    """Test POST /api/v1/tasks/niches"""
    print("\n" + "="*60)
    print("TEST: Register new niche")
    print("="*60)

    url = f"{BASE_URL}/api/v1/tasks/niches"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "task_type": "security-audit",
        "tier": "SAFETY_CRITICAL",
        "min_trust": 80.0,
        "description": "Security auditing tasks",
        "requires_human_approval": True
    }

    print(f"\nRequest: {json.dumps(payload, indent=2)}")

    resp = requests.post(url, json=payload, headers=headers)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 201:
        data = resp.json()
        print(f"\nResponse:")
        print(json.dumps(data, indent=2))
        return True
    else:
        print(f"Error: {resp.text}")
        return False

def test_broadcast_with_tier_enforcement(token):
    """Test broadcast task with tier enforcement"""
    print("\n" + "="*60)
    print("TEST: Broadcast task with tier enforcement")
    print("="*60)

    all_passed = True

    # Test 1: SANDBOX task (should work with low trust)
    url = f"{BASE_URL}/api/v1/tasks/broadcast"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "task_type": "research",
        "payload": {"query": "test query"},
        "domain": "research"
    }

    print(f"\nTest 1: SANDBOX task (research)")
    resp = requests.post(url, json=payload, headers=headers)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 201:
        print("✓ SANDBOX task accepted")
    else:
        print(f"✗ Failed: {resp.text}")
        all_passed = False

    # Test 2: PRODUCTION task (requires min_trust=40)
    payload2 = {
        "task_type": "build",
        "payload": {"repo": "test"},
        "domain": "coding"
    }

    print(f"\nTest 2: PRODUCTION task (build, min_trust=40)")
    resp2 = requests.post(url, json=payload2, headers=headers)
    print(f"Status: {resp2.status_code}")
    if resp2.status_code == 201:
        print("✓ PRODUCTION task accepted")
    elif resp2.status_code == 404:
        print("✓ Correctly rejected (no agents meet min_trust requirement)")
    else:
        print(f"Response: {resp2.text}")
        all_passed = False

    # Test 3: SAFETY_CRITICAL task (should be blocked)
    payload3 = {
        "task_type": "security-audit",
        "payload": {"target": "system"},
    }

    print(f"\nTest 3: SAFETY_CRITICAL task (should require approval)")
    resp3 = requests.post(url, json=payload3, headers=headers)
    print(f"Status: {resp3.status_code}")
    if resp3.status_code == 403:
        print("✓ Correctly blocked SAFETY_CRITICAL task")
        print(f"   Message: {resp3.json()['detail']}")
    else:
        print(f"✗ Unexpected response: {resp3.text}")
        all_passed = False

    return all_passed

def main():
    print("Niche Tier Classification Test Suite")
    print("="*60)

    # Get token
    print("\nGetting authentication token...")
    token = get_test_token()
    if not token:
        print("Failed to get token. Exiting.")
        sys.exit(1)

    print(f"✓ Got token: {token[:20]}...")

    # Run tests
    results = []

    results.append(("List niches", test_list_niches(token)))
    results.append(("Get specific niche", test_get_niche(token, "build")))
    results.append(("Get unregistered niche", test_get_niche(token, "unknown-task")))
    results.append(("Register new niche", test_register_niche(token)))
    results.append(("Broadcast with tier enforcement", test_broadcast_with_tier_enforcement(token)))

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    passed = sum(1 for _, result in results if result)
    total = len(results)
    print(f"\nPassed: {passed}/{total}")

    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {test_name}")

    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
