# Niche Tier Classification Implementation (Round 2 Gap 3)

## Overview

Implemented a 3-tier classification system for task types to prevent routing safety-critical tasks to low-trust agents.

## What Was Built

### 1. Database Migration (v21)

**File**: `/root/circus/circus/database.py`

- Created `task_niche_registry` table with columns:
  - `task_type` (PRIMARY KEY)
  - `tier` (SANDBOX | PRODUCTION | SAFETY_CRITICAL)
  - `min_trust` (minimum trust score required)
  - `description`
  - `requires_human_approval` (1 for SAFETY_CRITICAL)
  - `completion_count` (auto-incremented on task completion)
  - `created_by`, `created_at`, `updated_at`

- Added `niche_tier` column to `tasks` table (default: 'SANDBOX')

- Seeded 5 default task types:
  - `build` → PRODUCTION (min_trust=40)
  - `code-review` → PRODUCTION (min_trust=40)
  - `research` → SANDBOX (min_trust=0)
  - `notify` → SANDBOX (min_trust=0)
  - `test-task` → SANDBOX (min_trust=0)

### 2. Models

**File**: `/root/circus/circus/models.py`

Added:
- `NicheTier` enum (SANDBOX, PRODUCTION, SAFETY_CRITICAL)
- `NicheRegistryEntry` request model
- `NicheRegistryResponse` response model

### 3. Broadcast Endpoint Enhancement

**File**: `/root/circus/circus/routes/tasks.py`

Updated `/tasks/broadcast` endpoint to:
1. Look up task type in `task_niche_registry`
2. Check if `requires_human_approval` is set → return 403 if true
3. Filter candidates by `min_trust >= min_trust_required`
4. Return 404 if no agents meet the minimum trust requirement

### 4. New Endpoints

All three endpoints placed BEFORE `GET /{task_id}` to avoid route collision:

#### GET /api/v1/tasks/niches
List all registered task type niches (ordered by tier, then task_type).

#### POST /api/v1/tasks/niches
Register or update a task type niche tier.

**Request**:
```json
{
  "task_type": "security-audit",
  "tier": "SAFETY_CRITICAL",
  "min_trust": 80.0,
  "description": "Security auditing tasks",
  "requires_human_approval": true
}
```

**Response**:
```json
{
  "task_type": "security-audit",
  "tier": "SAFETY_CRITICAL",
  "registered_at": "2026-06-13T12:03:56.788489"
}
```

#### GET /api/v1/tasks/niches/{task_type}
Get tier info for a specific task type. Returns default SANDBOX tier if unregistered.

## Verification

### Database Check
```bash
sqlite3 /root/.circus/circus.db "SELECT task_type, tier FROM task_niche_registry"
```

Output:
```
build|PRODUCTION
code-review|PRODUCTION
research|SANDBOX
notify|SANDBOX
test-task|SANDBOX
security-audit|SAFETY_CRITICAL
```

### Test Suite
All 5 tests pass:
- ✓ List niches
- ✓ Get specific niche
- ✓ Get unregistered niche (returns SANDBOX default)
- ✓ Register new niche
- ✓ Broadcast with tier enforcement (SANDBOX accepted, PRODUCTION accepted, SAFETY_CRITICAL blocked)

### API Health
```bash
curl http://localhost:6200/health
```
Returns: `{"status":"healthy","version":"1.0.0","agents_count":6,"rooms_count":6,...}`

## Key Features

1. **Safety Guardrails**: SAFETY_CRITICAL tasks require human approval, cannot be auto-routed
2. **Trust Filtering**: Agents below min_trust threshold are excluded from auction
3. **Graceful Defaults**: Unregistered task types default to SANDBOX tier (min_trust=0)
4. **Audit Trail**: `created_by`, `created_at`, `updated_at` tracked for all registry entries
5. **Completion Tracking**: `completion_count` field for future metrics

## Files Modified

1. `/root/circus/circus/database.py` - Added `run_v21_migration()`, called from `init_database()`
2. `/root/circus/circus/models.py` - Added `NicheTier`, `NicheRegistryEntry`, `NicheRegistryResponse`
3. `/root/circus/circus/routes/tasks.py` - Updated `/broadcast`, added 3 new endpoints

## Status

**DONE** - All requirements met, tests passing, existing endpoints unbroken.
