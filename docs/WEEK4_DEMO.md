# Circus Memory Commons — Preference Flow Demo

**One agent learns, every agent serving that owner immediately behaves differently.**

This demo proves the core product moment: trusted preference memories flow through the Memory Commons and instantly change bot behavior across all agents serving the same owner.

**W5 UPDATE (Signed Owner Binding):** As of Week 5, all preference memories must carry cryptographic owner signatures. This demo now includes the full signed-preference flow.

## What This Demo Proves

1. **Behavior-delta memories work end-to-end** — Friday publishes user preferences, Circus admits them, Claw consumes them and changes its response construction
2. **Owner isolation** — preferences are scoped to the owner they were published for (Kobus's preferences don't affect Jaco's bots)
3. **Live control plane** — preference changes take effect immediately without bot restarts or code deploys
4. **Fresh connection reads** — the demo uses separate database connections (simulating different bot processes) to prove preferences are persisted in the `active_preferences` table, not just held in transaction state

## How to Run

**W5 Prerequisite:** Generate an owner keypair first (one-time setup):

```bash
cd /root/circus
python -m circus.cli owner-keygen --owner demo-owner --output /tmp/demo-owner.key
export CIRCUS_OWNER_PRIVATE_KEY_PATH=/tmp/demo-owner.key
export CIRCUS_OWNER_ID=demo-owner
```

**Run the demo:**

```bash
./scripts/demo_preference_flow.sh
```

Expected runtime: ~15-20 seconds

**Note:** The demo will fail loudly if `CIRCUS_OWNER_PRIVATE_KEY_PATH` is not set — this is intentional W5 security design. Unsigned preferences are no longer accepted.

## Expected Output

```
╔════════════════════════════════════════════════════════════════════╗
║            Circus Memory Commons — Week 5 Preference Demo          ║
║           One agent learns, every agent serving that owner         ║
║                   immediately behaves differently.                 ║
║                (Now with cryptographic owner signatures)           ║
╚════════════════════════════════════════════════════════════════════╝

── BEFORE: Claw responds to Kobus (no preferences set) ──

    Response: "Yes, here's the answer you requested."
    Language: en · Verbosity: normal

── TRIGGER: Friday publishes preferences for Kobus (with signatures) ──

    ✓ user.language_preference = af      (confidence 0.85, signed)
    ✓ user.response_verbosity = terse   (confidence 0.9, signed)

── AFTER: Claw's next turn, fresh connection read ──

    Response: "Ja, reg so."
    Language: af · Verbosity: terse

✓ Behavior changed. No bot restart. No code change. Just memory.
  (With cryptographic proof of owner authorization)
```

## What's Happening Under the Hood

1. **SETUP:** Demo generates an owner keypair (or uses pre-generated from env var) and inserts the public key into the `owner_keys` table

2. **BEFORE:** Simulated bot (Claw) reads `active_preferences` for owner "kobus" → finds nothing → uses defaults (English, normal verbosity)

3. **TRIGGER:** Simulated bot (Friday) publishes two preference memories via `/api/v1/memory-commons/publish`:
   - Each preference carries a signed `owner_binding` with:
     - `agent_id`: Publishing agent identifier
     - `memory_id`: Unique memory identifier (binds signature to this specific preference)
     - `timestamp`: ISO8601 timestamp (prevents indefinite replay)
     - `signature`: Ed25519 signature over the canonical payload
   - `user.language_preference = "af"` (confidence 0.85, signed)
   - `user.response_verbosity = "terse"` (confidence 0.9, signed)

4. **Admission pipeline:** Circus validates the preferences pass the six gates:
   - Trusted domain (`domain = "preference.user"`)
   - Allowlist check (both fields are in `ALLOWLISTED_PREFERENCE_FIELDS`)
   - Owner declared (`provenance.owner_id` present)
   - Same-owner check (`provenance.owner_id = "kobus"` matches server's `CIRCUS_OWNER_ID`)
   - **NEW (W5): Owner signature valid** — cryptographic verification that the publishing agent is authorized
   - Confidence threshold (both > 0.7)

5. **Write to `active_preferences` table:** Circus upserts into `active_preferences` with latest-wins semantics (one row per `(owner_id, field_name)`)

6. **AFTER:** Simulated bot (Claw) creates a fresh database connection, reads `active_preferences` again, now finds the two preferences, and produces a different response

## Key Implementation Files

- **`circus/services/preference_admission.py`** — admission pipeline (six gates including W5 signature verification)
- **`circus/services/owner_verification.py`** — owner signature verification service (W5)
- **`circus/services/preference_application.py`** — `get_active_preferences()` read path
- **`circus/routes/memory_commons.py`** — publish endpoint calls admission after belief merge
- **`circus/database_migrations/v7_active_preferences.sql`** — `active_preferences` table schema
- **`circus/database_migrations/v8_owner_keys.sql`** — `owner_keys` table schema + migration (W5)
- **`tests/test_preference_e2e.py`** — full e2e test suite (449 tests pass, including W5 ship gate)

## Caveats (MVP Simplifications)

- **Owner identity via env var:** Bots still declare their owner via `CIRCUS_OWNER_ID` env var (W5 adds cryptographic proof of authorization, but identity resolution is still env-based)
- **Latest-wins:** Second publish for same field replaces the first — no conflict resolution, session scoping, or historical versioning (Track B)
- **Canned responses:** The demo uses hardcoded output strings for each preference combo instead of calling a real LLM or translator (visual clarity, not production realism)
- **No key rotation:** Once bound, owner keypair is permanent for MVP — no rotation, revocation, or recovery workflows (Track B)

## Next Steps (Track B, Post-MVP)

See W5 Design Lock for completed signed owner binding. Future Track B items:
- Key rotation and revocation workflows
- Session-scoped preference overrides
- Preference conflict resolution (user-set vs. learned)
- Multi-owner preference sharing (team accounts)
- UI for viewing/editing preferences
- HSM/TPM support for owner keys
- Federated owner key discovery

## References

- **Week 4 Design Lock:** `/root/.openclaw/reference/circus-w4-design.md`
- **Week 5 Design Lock:** `/root/.openclaw/reference/circus-w5-design.md`
- **W5 Migration Guide:** `/root/circus/docs/W5_MIGRATION.md`
- **E2E Test Suite:** `/root/circus/tests/test_preference_e2e.py`
- **Week 4 Shipped PR:** [#4 - Week 4: trusted preference memories → live behavior deltas](https://github.com/kobus/circus/pull/4) (merged as `0bb370f`, tagged `v1.3.0`)
