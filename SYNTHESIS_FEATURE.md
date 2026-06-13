# Backpressure-Triggered Memory Synthesis (Round 3 Gap 2)

## Overview

When the task queue gets too deep with deferrable work, Circus automatically halts new deferrable auctions and mines the queue for duplicates/similar tasks — compressing multiple tasks into synthesized ones. More load = better consolidation = antifragile.

## Features

### Two-Tier Queue

- **realtime** — auction tasks, user-facing, never paused, never synthesized
- **deferrable** — analytics, background, AI-IQ sync, research — synthesized under pressure

### Automatic Synthesis

- Threshold: >10 pending deferrable tasks → trigger synthesis
- Groups tasks by `task_type` and consolidates groups of 3+ into single synthesized tasks
- Original tasks marked as 'canceled' with error='synthesized'
- Synthesized task payload includes all original payloads and metadata

### Database Schema

#### Tasks table additions:
```sql
ALTER TABLE tasks ADD COLUMN priority_tier TEXT DEFAULT 'deferrable';
CREATE INDEX idx_tasks_tier ON tasks(priority_tier, state);
```

#### Synthesis tracking:
```sql
CREATE TABLE task_synthesis_log (
    id TEXT PRIMARY KEY,
    triggered_at TEXT NOT NULL,
    queue_depth_before INTEGER NOT NULL,
    tasks_consumed INTEGER NOT NULL DEFAULT 0,
    tasks_created INTEGER NOT NULL DEFAULT 0,
    compression_ratio REAL DEFAULT 1.0,
    synthesis_groups TEXT,   -- JSON: [{original_ids: [...], synthesized_id: "..."}]
    completed_at TEXT
);
```

## API Endpoints

### 1. Get Queue Depth
```http
GET /api/v1/tasks/queue-depth
Authorization: Bearer <token>
```

Response:
```json
{
  "realtime_pending": 0,
  "deferrable_pending": 13,
  "synthesis_threshold": 10,
  "synthesis_recommended": true
}
```

### 2. Manual Synthesis Trigger
```http
POST /api/v1/tasks/synthesize
Authorization: Bearer <token>
```

Response:
```json
{
  "synthesis_id": "124aab3fab08",
  "tasks_consumed": 13,
  "tasks_created": 1,
  "compression_ratio": 13.0,
  "groups": [
    {
      "original_ids": ["task-abc", "task-def", ...],
      "synthesized_id": "1ba075d266fc",
      "task_type": "analytics"
    }
  ],
  "triggered_at": "2026-06-13T14:06:24.718168"
}
```

### 3. Synthesis Log
```http
GET /api/v1/tasks/synthesis-log?limit=10
Authorization: Bearer <token>
```

Response:
```json
{
  "events": [
    {
      "id": "124aab3fab08",
      "triggered_at": "2026-06-13T14:06:24.718168",
      "queue_depth_before": 14,
      "tasks_consumed": 13,
      "tasks_created": 1,
      "compression_ratio": 13.0,
      "completed_at": "2026-06-13T14:06:24.718168"
    }
  ]
}
```

## Task Submission with Priority Tier

When submitting tasks, you can now specify the priority tier:

```json
POST /api/v1/tasks
{
  "to_agent_id": "agent-123",
  "task_type": "analytics",
  "payload": {"data": "..."},
  "priority_tier": "deferrable"  // or "realtime"
}
```

## Auto-Synthesis Behavior

When a deferrable task is submitted:
1. Task is inserted into the tasks table
2. System checks deferrable queue depth
3. If depth > 10, synthesis is automatically triggered
4. Synthesis runs in the background and consolidates tasks

## Synthesized Task Payload

When tasks are synthesized, the payload looks like:
```json
{
  "synthesized": true,
  "source_count": 13,
  "source_ids": ["task-abc", "task-def", ...],
  "merged_payloads": [
    {"original": "payload1"},
    {"original": "payload2"},
    ...
  ]
}
```

## Testing

Run the test script:
```bash
cd /root/circus
python3 test_synthesis.py
```

This will:
1. Register a test agent
2. Submit 12 deferrable tasks
3. Verify auto-synthesis triggers
4. Manually trigger synthesis
5. Check synthesis logs

## Migration

The v23 migration is automatically applied on startup. To manually run:
```python
from circus.database import init_database
init_database()
```

## Production Notes

- Realtime tasks (notify, auction, bid, alert) are never synthesized
- Synthesis only consolidates tasks of the same type
- Minimum group size for synthesis: 3 tasks
- Original tasks preserve audit trail (state='canceled', error='synthesized')
- Compression ratios typically range from 3.0 to 13.0+
