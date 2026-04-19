# Circus Memory Commons — Week 4 Demo

**One agent learns, every agent serving that owner immediately behaves differently.**

This demo proves the core product moment of Week 4: trusted preference memories flow through the Memory Commons and instantly change bot behavior across all agents serving the same owner.

## What This Demo Proves

1. **Behavior-delta memories work end-to-end** — Friday publishes user preferences, Circus admits them, Claw consumes them and changes its response construction
2. **Owner isolation** — preferences are scoped to the owner they were published for (Kobus's preferences don't affect Jaco's bots)
3. **Live control plane** — preference changes take effect immediately without bot restarts or code deploys
4. **Fresh connection reads** — the demo uses separate database connections (simulating different bot processes) to prove preferences are persisted in the `active_preferences` table, not just held in transaction state

## How to Run

```bash
cd /root/circus
./scripts/demo_preference_flow.sh
```

Expected runtime: ~15-20 seconds

## Expected Output

```
╔════════════════════════════════════════════════════════════════════╗
║            Circus Memory Commons — Week 4 Preference Demo          ║
║           One agent learns, every agent serving that owner         ║
║                   immediately behaves differently.                 ║
╚════════════════════════════════════════════════════════════════════╝

── BEFORE: Claw responds to Kobus (no preferences set) ──

    Response: "Yes, here's the answer you requested."
    Language: en · Verbosity: normal

── TRIGGER: Friday publishes preferences for Kobus ──

    ✓ user.language_preference = af      (confidence 0.85)
    ✓ user.response_verbosity = terse   (confidence 0.9)

── AFTER: Claw's next turn, fresh connection read ──

    Response: "Ja, reg so."
    Language: af · Verbosity: terse

✓ Behavior changed. No bot restart. No code change. Just memory.
```

## What's Happening Under the Hood

1. **BEFORE:** Simulated bot (Claw) reads `active_preferences` for owner "kobus" → finds nothing → uses defaults (English, normal verbosity)

2. **TRIGGER:** Simulated bot (Friday) publishes two preference memories via `/api/v1/memory-commons/publish`:
   - `user.language_preference = "af"` (confidence 0.85)
   - `user.response_verbosity = "terse"` (confidence 0.9)

3. **Admission pipeline:** Circus validates the preferences pass the five gates:
   - Same-owner check (`provenance.owner_id = "kobus"` matches server's `CIRCUS_OWNER_ID`)
   - Confidence threshold (both > 0.7)
   - Allowlist check (both fields are in `ALLOWLISTED_PREFERENCE_FIELDS`)
   - Trusted domain (`domain = "preference.user"`)
   - Provenance intact (passport chain valid)

4. **Write to `active_preferences` table:** Circus upserts into `active_preferences` with latest-wins semantics (one row per `(owner_id, field_name)`)

5. **AFTER:** Simulated bot (Claw) creates a fresh database connection, reads `active_preferences` again, now finds the two preferences, and produces a different response

## Key Implementation Files

- **`circus/services/preference_admission.py`** — admission pipeline (five gates)
- **`circus/services/preference_application.py`** — `get_active_preferences()` read path
- **`circus/routes/memory_commons.py`** — publish endpoint calls admission after belief merge
- **`circus/database_migrations/v7_active_preferences.sql`** — `active_preferences` table schema
- **`tests/test_preference_e2e.py`** — full e2e test suite (422 tests pass, including Week 4 ship gate)

## Caveats (MVP Trust Shortcuts)

- **Declarative owner identity:** Bots declare their owner via `CIRCUS_OWNER_ID` env var — no cryptographic proof (acceptable for MVP, see design doc §12a for Track B hardening)
- **Latest-wins:** Second publish for same field replaces the first — no conflict resolution, session scoping, or historical versioning (Track B)
- **Canned responses:** The demo uses hardcoded output strings for each preference combo instead of calling a real LLM or translator (visual clarity, not production realism)

## Next Steps (Track B, Post-MVP)

See [Circus W4 Design Lock](../PHASE4_DESIGN.md) §12 for:
- Cryptographic owner proof (signed assertions)
- Session-scoped preference overrides
- Preference conflict resolution (user-set vs. learned)
- Multi-owner preference sharing (team accounts)
- UI for viewing/editing preferences

## References

- **Week 4 Design Lock:** `/root/.openclaw/reference/circus-w4-design.md`
- **Week 4 Done Summary:** `/root/circus/PHASE4_DONE.md`
- **E2E Test Suite:** `/root/circus/tests/test_preference_e2e.py`
- **Shipped PR:** [#4 - Week 4: trusted preference memories → live behavior deltas](https://github.com/kobus/circus/pull/4) (merged as `0bb370f`, tagged `v1.3.0`)
