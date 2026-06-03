#!/usr/bin/env python3
"""
Example: WhatsApp bot integration with Circus token pool.

This shows how a WhatsApp bot would use the token pool system to:
1. Check token availability before making Claude API calls
2. Apply delays based on pool tier
3. Record actual usage after requests
4. Handle exhaustion and conversation limits
"""

import time
import requests
from typing import Optional


class CircusTokenPool:
    """Client for Circus token pool API."""

    def __init__(self, base_url: str, bot_id: str):
        self.base_url = base_url.rstrip('/')
        self.bot_id = bot_id

    def check_availability(self, session_id: str, estimated_tokens: Optional[int] = None) -> dict:
        """Check if tokens are available before making a request."""
        response = requests.post(
            f"{self.base_url}/api/v1/tokens/check",
            json={
                "bot_id": self.bot_id,
                "session_id": session_id,
                "estimated_tokens": estimated_tokens
            }
        )
        response.raise_for_status()
        return response.json()

    def record_usage(self, session_id: str, tokens_used: int) -> dict:
        """Record actual token usage after a request."""
        response = requests.post(
            f"{self.base_url}/api/v1/tokens/record",
            json={
                "bot_id": self.bot_id,
                "session_id": session_id,
                "tokens_used": tokens_used
            }
        )
        response.raise_for_status()
        return response.json()

    def get_status(self) -> dict:
        """Get overall pool status."""
        response = requests.get(f"{self.base_url}/api/v1/tokens/status")
        response.raise_for_status()
        return response.json()


def make_claude_request_with_pool(
    pool: CircusTokenPool,
    session_id: str,
    message: str
) -> Optional[str]:
    """
    Make a Claude API request with token pool integration.

    Returns:
        Response text, or None if request was refused due to pool limits
    """
    # Step 1: Check token availability
    check = pool.check_availability(session_id)

    # Step 2: Handle exhaustion
    if check["tier"] == "exhausted":
        print(f"❌ Pool exhausted ({check['pool_pct']:.1f}%). Will resume tomorrow.")
        return None

    # Step 3: Handle conversation limit
    if check["tier"] == "conv_limit":
        print(f"❌ Conversation limit exceeded. Please start a new session.")
        return None

    # Step 4: Apply delay if needed
    if check["delay_ms"] > 0:
        print(f"⏱️  Pool at {check['pool_pct']:.1f}% ({check['tier']} tier), waiting {check['delay_ms']}ms...")
        time.sleep(check["delay_ms"] / 1000)

    # Step 5: Show warning message
    if check.get("message"):
        print(f"⚠️  {check['message']}")

    # Step 6: Make actual Claude API call
    print(f"🤖 Making Claude API call (pool: {check['pool_pct']:.1f}%, bot: {check['bot_pct']:.1f}%)...")

    # Simulate Claude API call (replace with actual API call)
    response_text = f"Hello! You said: {message}"
    actual_tokens_used = len(message) * 4  # Rough estimate: ~4 tokens per word

    # Step 7: Record actual usage
    result = pool.record_usage(session_id, actual_tokens_used)
    print(f"✅ Response received. Tokens used: {actual_tokens_used}")
    print(f"   Bot daily: {result['daily_used']}, conversation: {result['conversation_used']}")
    print(f"   New tier: {result['tier']}")

    return response_text


def main():
    """Example usage."""
    # Initialize token pool client
    pool = CircusTokenPool(
        base_url="https://circus.whatshubb.co.za",
        bot_id="whatsapp-bot-1"
    )

    # Session ID from WhatsApp conversation
    session_id = "chat-27123456789"

    print("WhatsApp Bot with Circus Token Pool")
    print("=" * 60)
    print()

    # Get initial pool status
    status = pool.get_status()
    print(f"📊 Pool status: {status['pool_pct']:.1f}% used ({status['tier']} tier)")
    print(f"   Daily budget: {status['daily_budget']:,} tokens")
    print(f"   Daily used: {status['daily_used']:,} tokens")
    print()

    # Example conversation
    messages = [
        "Hello, what's the weather today?",
        "Can you help me with a Python script?",
        "Tell me a joke"
    ]

    for i, message in enumerate(messages, 1):
        print(f"Message {i}: {message}")
        response = make_claude_request_with_pool(pool, session_id, message)

        if response:
            print(f"Response: {response}")
        else:
            print("Request refused due to pool limits")

        print()

        # Small delay between messages
        time.sleep(0.5)

    # Final status
    status = pool.get_status()
    print("=" * 60)
    print(f"📊 Final pool status: {status['pool_pct']:.1f}% used ({status['tier']} tier)")


if __name__ == "__main__":
    # Note: This is a local example. For production, replace base_url with actual Circus URL
    # and use real Claude API integration
    print("⚠️  This is an example. Update base_url and add real Claude API integration.")
    print()
    # Uncomment to run:
    # main()
