# Token Pool System

Shared token pool management for Circus bots with automatic tier-based throttling.

## Overview

The token pool system provides centralized token budget management with automatic date-based resets, per-bot tracking, and tier-based throttling to prevent overuse.

## Database Tables

### `token_pool`
- `id`: Always 1 (singleton)
- `daily_budget`: Total tokens available per day (default: 5,000,000)
- `daily_used`: Tokens used today
- `current_date`: Current tracking date (ISO format)
- `updated_at`: Last update timestamp

### `token_pool_bots`
- `bot_id`: Unique bot identifier
- `daily_used`: Tokens used by this bot today
- `conversation_used`: Tokens used in current session
- `current_session`: Current session ID
- `tier`: Current tier (green/yellow/orange/red/exhausted/conv_limit)
- `updated_at`: Last update timestamp

## API Endpoints

### GET /api/v1/tokens/status

Get current pool status and per-bot breakdown.

**Response:**
```json
{
  "daily_budget": 5000000,
  "daily_used": 3605000,
  "pool_pct": 72.1,
  "current_date": "2026-05-24",
  "tier": "yellow",
  "bots": [
    {
      "bot_id": "whatsapp-bot",
      "daily_used": 1500000,
      "daily_pct": 30.0,
      "conversation_used": 45000,
      "current_session": "session-abc123",
      "tier": "yellow",
      "updated_at": "2026-05-24T10:30:00"
    }
  ]
}
```

### POST /api/v1/tokens/check

Check if bot can use tokens before making a request.

**Request:**
```json
{
  "bot_id": "whatsapp-bot",
  "session_id": "session-abc123",
  "estimated_tokens": 1000  // optional
}
```

**Response:**
```json
{
  "tier": "yellow",
  "delay_ms": 0,
  "pool_pct": 72.1,
  "bot_pct": 30.0,
  "message": "Pool at 72.1%, conserving..."
}
```

### POST /api/v1/tokens/record

Record actual token usage after a request.

**Request:**
```json
{
  "bot_id": "whatsapp-bot",
  "session_id": "session-abc123",
  "tokens_used": 5000
}
```

**Response:**
```json
{
  "tier": "yellow",
  "daily_used": 1505000,
  "conversation_used": 50000,
  "pool_daily_used": 3610000
}
```

### POST /api/v1/tokens/reset

Reset pool (owner-only, for testing/emergency).

**Headers:**
```
Authorization: Bearer <CIRCUS_SECRET_KEY>
```

**Response:**
```json
{
  "status": "reset",
  "current_date": "2026-05-24",
  "updated_at": "2026-05-24T12:00:00"
}
```

## Tier System

Tiers are calculated based on `pool_pct` (daily_used / daily_budget × 100):

| Tier | Pool % | Delay | Behavior |
|------|--------|-------|----------|
| **green** | 0-70% | 0ms | Normal operation |
| **yellow** | 70-85% | 0ms | Warning message, conserve tokens |
| **orange** | 85-95% | 2000ms | 2s delay per request |
| **red** | 95-100% | 5000ms | 5s delay per request |
| **exhausted** | 100%+ | 0ms | Refuse requests, resume tomorrow |
| **conv_limit** | - | 0ms | Conversation > 100k tokens, refuse |

## Auto Date Reset

The pool automatically resets to 0 used tokens at midnight UTC. All bot counters and conversation trackers are also reset.

## Per-Conversation Limit

If a bot's `conversation_used` exceeds 100,000 tokens for the current session, the tier becomes `conv_limit` and further requests should be refused. Starting a new session (different `session_id`) resets the conversation counter.

## Usage Example

```python
import requests

BASE_URL = "https://circus.whatshubb.co.za"

# Before making a request
response = requests.post(f"{BASE_URL}/api/v1/tokens/check", json={
    "bot_id": "my-bot",
    "session_id": "current-session-id"
})
check = response.json()

if check["tier"] == "exhausted":
    print("Pool exhausted, retry tomorrow")
    return

if check["tier"] == "conv_limit":
    print("Conversation limit exceeded, start new session")
    return

if check["delay_ms"] > 0:
    time.sleep(check["delay_ms"] / 1000)

# Make your API call...
actual_tokens = make_api_call()

# Record actual usage
requests.post(f"{BASE_URL}/api/v1/tokens/record", json={
    "bot_id": "my-bot",
    "session_id": "current-session-id",
    "tokens_used": actual_tokens
})
```

## Configuration

Default budget: 5,000,000 tokens/day. To change:

```sql
UPDATE token_pool SET daily_budget = 10000000 WHERE id = 1;
```

## Testing

Run the test suite:

```bash
cd /root/circus
python3 test_token_pool.py
```

## Implementation Files

- `/root/circus/circus/database.py` - Table definitions
- `/root/circus/circus/routes/tokens.py` - API endpoints
- `/root/circus/circus/app.py` - Route registration
- `/root/circus/test_token_pool.py` - Test suite
