# W5 Migration: Signed Owner Binding

## What Changed

W5 adds cryptographic owner signatures to preference memories, closing the unsigned `owner_id` trust hole from W4.

## Why active_preferences Is Cleared

Derived state must be rebuilt under signed-owner rules. `active_preferences` is control-plane cache, not source of truth. Pre-W5 preferences carry no cryptographic proof of ownership, so they cannot pass the new signature verification gate. Clearing forces conscious re-activation.

## What Is Preserved

Source memories in `shared_memories` remain intact for audit. Full history of every preference publish is preserved. Only the derived activation cache is reset.

## What Changes Operationally

Unsigned preferences will no longer activate. Any preference memory without a valid `owner_binding` signature will be skipped at admission with a structured INFO log. The memory stays in `shared_memories` but does not influence bot behavior.

## What Operators Must Do

### 1. Generate owner keys (on publishing nodes)
```bash
python -m circus.cli owner-keygen --owner <owner-id> --output /root/.circus/owner-<id>.key
```
This creates:
- `/root/.circus/owner-<id>.key` (private key, 600 perms — keep secure)
- `/root/.circus/owner-<id>.pub` (public key, 644 perms — share out-of-band)
- A row in the `owner_keys` table with the public key

### 2. Import owner keys (on consuming nodes)
On every node that needs to verify this owner's preferences:
```bash
python -m circus.cli owner-add --owner <owner-id> --public-key-file /tmp/owner-<id>.pub
```

### 3. Set the private key env var on publishing agents
```bash
export CIRCUS_OWNER_ID=<owner-id>
export CIRCUS_OWNER_PRIVATE_KEY_PATH=/root/.circus/owner-<owner-id>.key
```

### 4. Republish preferences or let agents re-learn
Agents that publish preferences based on inferred user behavior will naturally re-publish signed versions next time they observe the trigger. Operators wanting to pre-seed can manually publish via the demo script (see below).

## Verification

After migration and first signed republish:
```bash
sqlite3 circus.db "SELECT owner_id, field_name, value FROM active_preferences"
```

Should show freshly-activated signed preferences. Check logs for any `owner_signature_missing` / `owner_signature_invalid` skips — those indicate memories that need resigning.

## Demo

See `scripts/demo_preference_flow.sh` (updated in W5 5.5) for a minimal end-to-end flow: generate key → publish signed preference → observe behavior change.

## Rollback

Not recommended. Rolling back to pre-W5 means accepting unsigned preferences as trusted — that defeats the security upgrade. If you must roll back, restore from backup taken before v8 migration ran.
