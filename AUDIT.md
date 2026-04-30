# Circus Repo Audit — Day 1

_Audited: 2026-04-29. Owner: Kobus._

## Summary

Circus is **far more mature than the roadmap assumed**. Currently `v1.9.0` with 73+ endpoints across 10 routers, 48 test files, full W3C VC + trust + commons + federation + MCP server already shipped. Roadmap revised based on actual state.

## What Already Exists

### Routes (10 routers, ~73 handlers)

- `agents.py` (32KB, 15 handlers) — register, refresh passport, discover, audit log, vouch, trust events, semantic discovery, competence
- `rooms.py` (10KB, 6 handlers) — create, join, share memory, list, briefing
- `tasks.py` (14KB, 7 handlers) — submit, update state, inbox, outbox, history, stream progress
- `memory_commons.py` (40KB, 15 handlers) — goals, publish, stream, domain stewards, preferences, conflicts, shared search, auto-resolve
- `credentials.py` (5.5KB, 2 handlers) — export trust attestation, verify credential
- `federation.py` (20KB, 12 handlers) — register peer, federated discovery, pull/push bundles, outbox, metrics
- `governance.py` (8KB, 5 handlers) — quarantine list/release/discard, audit log
- `handshake.py` (4KB) — initiate handshake
- `key_lifecycle.py` (7KB, 8 handlers) — discover/rotate/revoke owner key
- `sse.py` (5KB) — room event stream

### Services (40+ modules)

- **Trust** — formula-based (prediction accuracy, belief stability, memory quality, passport, days-active). Decay applied. Tiers + vouching + cost gates.
- **W3C VC** — full issuance + verification with did:web-style identifiers
- **Passport** — AI-IQ passport import + validation
- **Goal router** — semantic similarity matching (cosine via sentence-transformers)
- **Memory commons** — goal subscriptions, publish, share, conflict detection, auto-resolve
- **Federation** — multi-instance peer registration, signed bundle push/pull, dedup, admission control
- **Belief merge** — cross-agent belief reconciliation
- **Conflict detection** — semantic + negation
- **Provenance** — memory chain of custody
- **Hull integrity** — anti-tampering checks
- **Quarantine** — bad-actor isolation
- **Owner verification** — keypair-based ownership proofs
- **MCP server** — exposes Circus as MCP

### SDK + Tooling

- `circus_sdk/` — Python client (signing, models, async client)
- `cli.py` — CLI tool
- `aiiq_bridge.py` — AI-IQ integration

### Tests

- 48 test files. Federation, trust, passport, MCP, governance, key lifecycle, conflict, preferences, ownership, SDK — all covered.

### Docs / Marketing

- README — clear pitch, comparison matrix vs CrewAI/AutoGen/LangGraph
- ARCHITECTURE.md (31KB)
- CHANGELOG.md
- PHASE1/2/3 summaries
- DEPLOYMENT.md, Dockerfile

## What's Missing (Real Gaps)

### Routing / Learning

- ❌ **No contextual bandit routing.** Goal router uses cosine similarity only — picks best-matching agent, doesn't learn from outcomes. Adding LinUCB or Thompson sampling on top would give online learning.
- ❌ **No GNN trust model.** Trust is formula-based with hand-tuned weights. Graph-learned trust would resist sybil + collusion better.
- ❌ **No drift detection.** No automated metric monitoring. Trust formula could rot silently as agent populations shift.
- ❌ **No A/B framework.** No champion/challenger for routing or trust upgrades.

### Marketing / Demo Surface

- ❌ **No demo video.** README pitch is text only.
- ❌ **No landing page.** github.io or custom domain.
- ❌ **No public bench numbers.** No "Circus vs CrewAI" benchmark with reproducible script.
- ❌ **No HN/X launch artifact.** No top-of-funnel push.
- ❌ **No newsletter coverage.** TLDR AI, Ben's Bites, The Batch — never approached.

### Interop

- ✅ MCP server present BUT
- ❓ **MCP Registry submission status unknown** — check Anthropic's registry.
- ❌ **A2A protocol** — not implemented. Google A2A interop missing.
- ❌ **CrewAI / AutoGen interop tests** — none.

### Enterprise

- ✅ Dockerfile present BUT
- ❌ **No Helm chart** for Kubernetes.
- ❌ **No SOC2-flavored audit log compliance pack.**
- ❌ **No multi-tenant RBAC story.**
- ❌ **No SSO (SAML/OIDC).**
- ❌ **No on-prem install guide.**
- ❌ **No pilot agreement template.**

### Online Learning

- ❌ **No retrain cron.**
- ❌ **No active learning loop.**
- ❌ **No model versioning / champion-challenger.**
- ❌ **No ops runbook for ML.**

## Updated Mental Model

Original roadmap split Phase 2 across days 15-42 building trust + VC + commons. **Most of that already shipped.** Real opportunity is to:

1. Add **learnable routing** on top of existing semantic goal router (bandit layer)
2. Ship **public bench + demo + landing page** (tell the world what already works)
3. Add **GNN trust upgrade** as opt-in alongside existing formula
4. Build **interop bridges** (A2A, MCP registry, CrewAI/AutoGen tests)
5. Build **enterprise pilot kit** (Helm, RBAC, SSO, audit pack)

## Recommended Roadmap Revision

See `/root/.openclaw/reference/circus-roadmap.md` for revised version.

Skip Phase 2 trust/VC/commons build days (already done).
Phase 1 still valid (bandit + bench + demo).
Phase 3 reordered — A2A + bench artifacts before GNN.

## Day 1 — DONE

Next: Day 2 — define routing problem + spec.
