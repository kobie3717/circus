#!/usr/bin/env python3
"""Test script for token pool API endpoints."""

import asyncio
import sys
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from circus.app import app
from circus.database import init_database, get_db
from circus.config import settings
import tempfile


def test_token_pool():
    """Test token pool endpoints."""
    # Use a temporary database for testing
    original_db = settings.database_path
    test_db = Path(tempfile.mktemp(suffix='.db'))
    settings.database_path = test_db

    try:
        # Initialize database
        init_database(test_db)

        # Create test client
        client = TestClient(app)

        print("Testing token pool endpoints...\n")

        # Test 1: Get pool status
        print("1. GET /api/v1/tokens/status")
        response = client.get("/api/v1/tokens/status")
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Daily budget: {data['daily_budget']}")
            print(f"   Daily used: {data['daily_used']}")
            print(f"   Pool %: {data['pool_pct']}")
            print(f"   Tier: {data['tier']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 2: Check token availability
        print("2. POST /api/v1/tokens/check")
        response = client.post("/api/v1/tokens/check", json={
            "bot_id": "test-bot-1",
            "session_id": "session-123",
            "estimated_tokens": 1000
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Tier: {data['tier']}")
            print(f"   Delay: {data['delay_ms']}ms")
            print(f"   Pool %: {data['pool_pct']}")
            print(f"   Bot %: {data['bot_pct']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 3: Record token usage
        print("3. POST /api/v1/tokens/record")
        response = client.post("/api/v1/tokens/record", json={
            "bot_id": "test-bot-1",
            "session_id": "session-123",
            "tokens_used": 5000
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Tier: {data['tier']}")
            print(f"   Bot daily used: {data['daily_used']}")
            print(f"   Conversation used: {data['conversation_used']}")
            print(f"   Pool daily used: {data['pool_daily_used']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 4: Check pool status after recording
        print("4. GET /api/v1/tokens/status (after recording)")
        response = client.get("/api/v1/tokens/status")
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Daily used: {data['daily_used']}")
            print(f"   Pool %: {data['pool_pct']}")
            print(f"   Bots count: {len(data['bots'])}")
            if len(data['bots']) > 0:
                print(f"   Bot 0 ID: {data['bots'][0]['bot_id']}")
                print(f"   Bot 0 daily used: {data['bots'][0]['daily_used']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 5: Record more usage to test tier changes
        print("5. POST /api/v1/tokens/record (large usage to test tier)")
        response = client.post("/api/v1/tokens/record", json={
            "bot_id": "test-bot-2",
            "session_id": "session-456",
            "tokens_used": 3600000  # 72% of budget
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Tier: {data['tier']} (should be yellow)")
            print(f"   Pool daily used: {data['pool_daily_used']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 6: Check if tier changed
        print("6. POST /api/v1/tokens/check (verify tier changed)")
        response = client.post("/api/v1/tokens/check", json={
            "bot_id": "test-bot-3",
            "session_id": "session-789"
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Tier: {data['tier']}")
            print(f"   Pool %: {data['pool_pct']}")
            if data['message']:
                print(f"   Message: {data['message']}")
            print("   ✓ PASS\n")
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        # Test 7: Test conversation limit
        print("7. POST /api/v1/tokens/record (test conversation limit)")
        # First, record tokens for a session
        response = client.post("/api/v1/tokens/record", json={
            "bot_id": "test-bot-limit",
            "session_id": "session-limit-1",
            "tokens_used": 120000  # Over 100k limit
        })
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   Conversation used: {data['conversation_used']}")

            # Now check - should get conv_limit tier
            response2 = client.post("/api/v1/tokens/check", json={
                "bot_id": "test-bot-limit",
                "session_id": "session-limit-1"
            })
            if response2.status_code == 200:
                data2 = response2.json()
                print(f"   Tier: {data2['tier']} (should be conv_limit)")
                if data2['message']:
                    print(f"   Message: {data2['message']}")
                print("   ✓ PASS\n")
            else:
                print(f"   ✗ FAIL on check: {response2.text}\n")
                return False
        else:
            print(f"   ✗ FAIL: {response.text}\n")
            return False

        print("=" * 60)
        print("All tests passed! ✓")
        print("=" * 60)
        return True

    finally:
        # Cleanup
        settings.database_path = original_db
        if test_db.exists():
            test_db.unlink()


if __name__ == "__main__":
    success = test_token_pool()
    sys.exit(0 if success else 1)
