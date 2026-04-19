"""Federation admission pipeline — composes verifiers, owns persistence.

Single admission boundary for Memory Commons federation bundles. Orchestrates
the five verification stages in fixed order, persists quarantine and audit
rows, and returns typed AdmissionResult.

Key invariants (locked design, Sub-step 3.3):
- Fixed ordering: signature → passport structure → identity match → expiry
  → peer known → peer trusted
- Fail closed: any stage failure → quarantine or hard-reject, no further stages
- Atomic persistence: quarantine + audit in same transaction
- infra_error on DB failure — best-effort audit attempt, graceful return

Identity field name from passport.py analysis: passport["identity"]["name"]
(AI-IQ passports have "identity" dict with "name" field as the agent identifier)
"""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional
from uuid import uuid4

from circus.database import get_db
from circus.services.bundle_signing import canonicalize_for_signing
from circus.services.federation_verify import (
    verify_passport_expiry,
    verify_passport_structure,
    verify_peer_known,
    verify_peer_trusted,
    verify_signature,
)

logger = logging.getLogger(__name__)

Decision = Literal["admitted", "quarantined", "rejected", "infra_error", "skipped"]


@dataclass(frozen=True)
class AdmissionResult:
    """Result of bundle admission pipeline.

    Five terminal states:
    - admitted: all verifications passed, bundle ready for merge
    - quarantined: recoverable failure (passport expired, peer untrusted, etc)
      — wrote quarantine + audit rows for review
    - rejected: hard failure (signature invalid, peer mismatch) — audit only
    - infra_error: persistence failed — failed closed, best-effort audit attempt
    - skipped: bundle already processed (clean replay) — no further action

    admitted flag is convenience for `if result.admitted:`
    """
    admitted: bool
    decision: Decision
    reason: Optional[str] = None         # reason code (None on admit)
    stage_reached: Optional[str] = None  # which stage produced the decision
    peer_id: Optional[str] = None
    bundle_id: Optional[str] = None
    quarantine_id: Optional[str] = None  # federation_quarantine.id, if written
    audit_id: Optional[str] = None       # federation_audit.id, always written
    detail: Optional[str] = None         # human-readable
    metadata: dict = field(default_factory=dict)

    # Per-memory counters (Sub-step 3.4)
    memories_total: int = 0       # total memories in bundle
    memories_new: int = 0         # NEW memories (inserted into federation_seen)
    memories_skipped: int = 0     # SKIPPED memories (already in federation_seen or shared_memories)


def admit_bundle(
    bundle: dict,
    *,
    now: Optional[datetime] = None,
) -> AdmissionResult:
    """Run verification pipeline and persist outcome.

    Fetches peer + public key from federation_peers.
    Composes verify_signature, verify_passport_structure, verify_passport_expiry,
    verify_peer_known, verify_peer_trusted in fixed order.
    Writes federation_quarantine + federation_audit rows.
    Never raises on verification failure. Returns AdmissionResult("infra_error", ...)
    if persistence fails.

    Args:
        bundle: Parsed bundle dict (must contain bundle_id, peer_id, memories,
                signature, passport)
        now: Current time (defaults to utcnow, injectable for tests)

    Returns:
        AdmissionResult with decision, reason, IDs, metadata

    Raises:
        TypeError: if bundle is not a dict (caller contract violation)
    """
    if not isinstance(bundle, dict):
        raise TypeError(f"bundle must be dict, got {type(bundle).__name__}")

    if now is None:
        now = datetime.utcnow()

    # Step -1: Extract peer_id (needed before bundle_id derivation)
    peer_id = bundle.get("peer_id")

    if not peer_id or not isinstance(peer_id, str):
        return _persist_and_return(
            decision="rejected",
            reason="malformed_bundle",
            stage="peer_lookup",
            peer_id=None,
            bundle_id=None,
            bundle=bundle,
            detail=f"peer_id missing or wrong type: {type(peer_id).__name__}",
            now=now,
        )

    # Step 0: Derive bundle_id and compute bundle_hash (NEW — Sub-step 3.4)
    try:
        bundle_id = _derive_bundle_id(bundle)
        bundle_hash = _compute_bundle_hash(bundle)
    except Exception as exc:
        # Canonicalization failure → malformed bundle
        return _persist_and_return(
            decision="rejected",
            reason="malformed_bundle",
            stage="bundle_id_derivation",
            peer_id=peer_id,
            bundle_id=None,
            bundle=bundle,
            detail=f"bundle canonicalization failed: {exc}",
            now=now,
        )

    # Step 1: Transport Dedup Check (NEW — Sub-step 3.4)
    # Check federation_bundles_seen before doing expensive verifications
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT bundle_hash, first_seen_at FROM federation_bundles_seen
                WHERE bundle_id = ?
            """, (bundle_id,))
            seen_row = cursor.fetchone()

            if seen_row:
                seen_hash = seen_row["bundle_hash"]
                first_seen = seen_row["first_seen_at"]

                if seen_hash == bundle_hash:
                    # Clean replay → skip (no writes)
                    logger.info("Bundle dedup: skipped replay", extra={
                        "bundle_id": bundle_id,
                        "source_instance": peer_id,
                        "first_seen_at": first_seen,
                    })
                    return AdmissionResult(
                        admitted=False,
                        decision="skipped",
                        reason="bundle_replay",
                        stage_reached="transport_dedup",
                        peer_id=peer_id,
                        bundle_id=bundle_id,
                        detail=f"bundle already processed with matching hash at {first_seen}",
                    )
                else:
                    # Same bundle_id but different hash → tampered
                    logger.warning("Bundle tampered", extra={
                        "bundle_id": bundle_id,
                        "expected_hash": seen_hash,
                        "actual_hash": bundle_hash,
                    })
                    # Hard reject with audit, but NO bundles_seen write
                    return _persist_and_return(
                        decision="rejected",
                        reason="bundle_tampered",
                        stage="transport_dedup",
                        peer_id=peer_id,
                        bundle_id=bundle_id,
                        bundle=bundle,
                        detail=f"bundle_id collision with different hash (expected {seen_hash[:8]}..., got {bundle_hash[:8]}...)",
                        now=now,
                        metadata={
                            "expected_hash": seen_hash,
                            "actual_hash": bundle_hash,
                        },
                    )
    except sqlite3.Error as exc:
        # DB error at transport dedup → infra_error
        logger.error("admit_bundle transport dedup check failed: %s", exc, extra={
            "bundle_id": bundle_id, "peer_id": peer_id
        })
        return _build_infra_error_result(
            stage="transport_dedup",
            decision_intent="admitted",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=f"db error at transport dedup: {exc}",
            now=now,
        )

    # Step 2: Peer lookup (needed for public key before signature check)
    # Not found or inactive → quarantine with passport_unknown
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT public_key, trust_score FROM federation_peers
                WHERE id = ? AND is_active = 1
            """, (peer_id,))
            peer_row = cursor.fetchone()
    except sqlite3.Error as exc:
        # DB error at peer lookup → infra_error
        logger.error("admit_bundle peer lookup failed: %s", exc, extra={
            "bundle_id": bundle_id, "peer_id": peer_id
        })
        return _build_infra_error_result(
            stage="peer_lookup",
            decision_intent="quarantined",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=f"db error at peer lookup: {exc}",
            now=now,
        )

    if not peer_row:
        return _persist_and_return(
            decision="quarantined",
            reason="passport_unknown",
            stage="peer_lookup",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail="peer not found in federation_peers or inactive",
            now=now,
        )

    public_key = peer_row["public_key"]
    trust_score = float(peer_row["trust_score"])

    # Step 3: Verify signature
    sig_result = verify_signature(bundle, public_key)
    if not sig_result.valid:
        return _persist_and_return(
            decision="rejected",
            reason=sig_result.reason,
            stage="verify_signature",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=sig_result.detail,
            now=now,
            metadata=sig_result.metadata,
        )

    # Step 4: Extract passport and verify structure
    passport = bundle.get("passport")
    if not passport:
        return _persist_and_return(
            decision="quarantined",
            reason="passport_malformed",
            stage="verify_passport_structure",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail="passport field missing from bundle",
            now=now,
        )

    struct_result = verify_passport_structure(passport)
    if not struct_result.valid:
        return _persist_and_return(
            decision="quarantined",
            reason=struct_result.reason,
            stage="verify_passport_structure",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=struct_result.detail,
            now=now,
            metadata=struct_result.metadata,
        )

    # Step 4b: Identity-match check (hard reject on mismatch)
    # passport["identity"]["name"] must match bundle["peer_id"]
    passport_identity = passport.get("identity", {}).get("name")
    if not passport_identity:
        # If identity field is missing after structure validation passed,
        # treat as passport_malformed (structure validator gap)
        return _persist_and_return(
            decision="quarantined",
            reason="passport_malformed",
            stage="verify_passport_structure",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail="passport identity.name field missing",
            now=now,
        )

    if passport_identity != peer_id:
        # Hard reject: someone brought a different agent's passport
        return _persist_and_return(
            decision="rejected",
            reason="passport_peer_mismatch",
            stage="verify_passport_structure",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=f"passport identity '{passport_identity}' != peer_id '{peer_id}'",
            now=now,
            metadata={"passport_identity": passport_identity},
        )

    # Step 5: Verify passport expiry
    expiry_result = verify_passport_expiry(passport, now=now)
    if not expiry_result.valid:
        return _persist_and_return(
            decision="quarantined",
            reason=expiry_result.reason,
            stage="verify_passport_expiry",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=expiry_result.detail,
            now=now,
            metadata=expiry_result.metadata,
            bundle_hash=bundle_hash,
        )

    # Step 6: Verify peer known (redundant check for safety)
    known_result = verify_peer_known(peer_id)
    if not known_result.valid:
        return _persist_and_return(
            decision="quarantined",
            reason=known_result.reason,
            stage="verify_peer_known",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=known_result.detail,
            now=now,
            metadata=known_result.metadata,
        )

    # Step 7: Verify peer trusted
    trusted_result = verify_peer_trusted(peer_id)
    if not trusted_result.valid:
        return _persist_and_return(
            decision="quarantined",
            reason=trusted_result.reason,
            stage="verify_peer_trusted",
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=trusted_result.detail,
            now=now,
            metadata=trusted_result.metadata,
            bundle_hash=bundle_hash,
        )

    # Step 7b: Verify hop_count boundary (bundle-level)
    # If ANY memory in bundle would cross max_hop_count after increment,
    # quarantine the ENTIRE bundle. Bundle-level not per-memory for MVP.
    from circus.config import settings
    memories = bundle.get("memories", [])
    for memory in memories:
        incoming_hop = memory.get("provenance", {}).get("hop_count", 1)
        if incoming_hop + 1 > settings.max_hop_count:
            return _persist_and_return(
                decision="quarantined",
                reason="hop_count_exceeded",
                stage="verify_hop_count",
                peer_id=peer_id,
                bundle_id=bundle_id,
                bundle=bundle,
                detail=f"memory {memory.get('id')} would exceed max_hop_count "
                       f"({incoming_hop} + 1 > {settings.max_hop_count})",
                now=now,
                metadata={
                    "violating_memory_id": memory.get("id"),
                    "incoming_hop_count": incoming_hop,
                    "max_hop_count": settings.max_hop_count,
                },
                bundle_hash=bundle_hash,
            )

    # Step 8: All verifications passed → prepare for admission with semantic dedup
    # Determine final decision (admitted vs quarantined based on previous checks)
    decision = "admitted"

    # Step 9: Per-memory semantic dedup (applies to both admitted and quarantined)
    # This step is integrated into _persist_and_return_with_dedup
    return _persist_and_return_with_dedup(
        decision=decision,
        reason=None,
        stage="admitted",
        peer_id=peer_id,
        bundle_id=bundle_id,
        bundle_hash=bundle_hash,
        bundle=bundle,
        detail="all verifications passed",
        now=now,
        metadata={
            "peer_trust_score": trust_score,
            "memory_ids": [m.get("id") for m in bundle.get("memories", [])],
            "hop_count": bundle.get("memories", [{}])[0].get("provenance", {}).get("hop_count"),
            "passport_hash": _compute_passport_hash(passport),
        },
    )


def _persist_and_return(
    *,
    decision: str,
    reason: Optional[str],
    stage: str,
    peer_id: Optional[str],
    bundle_id: Optional[str],
    bundle: dict,
    detail: str,
    now: datetime,
    metadata: Optional[dict] = None,
    bundle_hash: Optional[str] = None,
) -> AdmissionResult:
    """Persist quarantine + audit rows, return AdmissionResult.

    Writes:
    - quarantine row if decision == "quarantined"
    - audit row always

    Both in same transaction. On DB error, return infra_error result.
    """
    if metadata is None:
        metadata = {}

    quarantine_id = None
    audit_id = f"feda-{uuid4().hex[:16]}"

    # Determine if we need a quarantine row
    needs_quarantine = decision == "quarantined"

    # Prepare audit metadata
    audit_metadata = {
        "stage_reached": stage,
        "detail": detail,
        **metadata,
    }

    if needs_quarantine:
        quarantine_id = f"fedq-{uuid4().hex[:16]}"
        audit_metadata["quarantine_id"] = quarantine_id

    # Attempt persistence in single transaction
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            if needs_quarantine:
                # Extract memory_id from first memory if parseable
                memory_id = None
                try:
                    memories = bundle.get("memories", [])
                    if memories and isinstance(memories, list) and len(memories) > 0:
                        memory_id = memories[0].get("id")
                except Exception:
                    pass

                # Compute passport hash if passport extractable
                passport_hash = None
                try:
                    passport = bundle.get("passport")
                    if passport:
                        passport_hash = _compute_passport_hash(passport)
                except Exception:
                    pass

                # Serialize full bundle (including signature)
                payload_json = json.dumps(bundle, sort_keys=True, default=str)

                # Quarantine expires in 7 days
                expires_at = (now + timedelta(days=7)).isoformat()

                cursor.execute("""
                    INSERT INTO federation_quarantine (
                        id, memory_id, source_instance, source_passport_hash,
                        reason, payload, received_at, expires_at,
                        reviewed_at, reviewed_by_passport, review_action, review_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """, (
                    quarantine_id,
                    memory_id,
                    peer_id,  # source_instance
                    passport_hash,
                    reason,
                    payload_json,
                    now.isoformat(),
                    expires_at,
                ))

            # Write federation_bundles_seen for quarantined bundles (if bundle_hash provided)
            if needs_quarantine and bundle_hash and bundle_id:
                memories_count = len(bundle.get("memories", []))
                cursor.execute("""
                    INSERT OR IGNORE INTO federation_bundles_seen
                    (bundle_id, first_seen_at, source_instance, bundle_hash, decision, memory_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (bundle_id, now.isoformat(), peer_id, bundle_hash, decision, memories_count))

            # Write audit row
            action = _decision_to_audit_action(decision)
            cursor.execute("""
                INSERT INTO federation_audit (
                    id, action, actor_passport, target_id, reason, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id,
                action,
                peer_id,  # actor_passport
                bundle_id,
                reason,
                json.dumps(audit_metadata, default=str),
                now.isoformat(),
            ))

            # Commit transaction explicitly (get_db doesn't auto-commit)
            conn.commit()

        # Success — return result
        return AdmissionResult(
            admitted=(decision == "admitted"),
            decision=decision,
            reason=reason,
            stage_reached=stage,
            peer_id=peer_id,
            bundle_id=bundle_id,
            quarantine_id=quarantine_id,
            audit_id=audit_id,
            detail=detail,
            metadata=audit_metadata,
        )

    except sqlite3.Error as exc:
        # Persistence failed — fail closed
        logger.error("admit_bundle persistence failed: %s", exc, extra={
            "bundle_id": bundle_id, "peer_id": peer_id, "stage": stage,
            "decision_intent": decision,
        })

        return _build_infra_error_result(
            stage=stage,
            decision_intent=decision,
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=f"db error during persist: {exc}",
            now=now,
            metadata=audit_metadata,
        )


def _persist_and_return_with_dedup(
    *,
    decision: str,
    reason: Optional[str],
    stage: str,
    peer_id: Optional[str],
    bundle_id: str,
    bundle_hash: str,
    bundle: dict,
    detail: str,
    now: datetime,
    metadata: Optional[dict] = None,
) -> AdmissionResult:
    """Persist with semantic dedup + bundle tracking.

    Writes (in single transaction):
    - federation_bundles_seen row (for admitted + quarantined)
    - federation_seen rows for NEW memories (admitted only)
    - quarantine row if decision == "quarantined"
    - audit row always

    Performs per-memory dedup against federation_seen + shared_memories.
    Boomerang backfill: if memory in shared_memories but not federation_seen,
    INSERT into federation_seen (heals pre-3.4 gaps).
    """
    if metadata is None:
        metadata = {}

    quarantine_id = None
    audit_id = f"feda-{uuid4().hex[:16]}"

    # Determine if we need a quarantine row
    needs_quarantine = decision == "quarantined"

    # Prepare audit metadata
    audit_metadata = {
        "stage_reached": stage,
        "detail": detail,
        **metadata,
    }

    if needs_quarantine:
        quarantine_id = f"fedq-{uuid4().hex[:16]}"
        audit_metadata["quarantine_id"] = quarantine_id

    # Per-memory semantic dedup
    memories_total = len(bundle.get("memories", []))
    memories_new = 0
    memories_skipped = 0
    new_memory_ids = []

    # Attempt persistence in single transaction
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Step 1: Per-memory semantic dedup (for admitted + quarantined)
            if decision in ("admitted", "quarantined"):
                for memory in bundle.get("memories", []):
                    memory_id = memory.get("id")
                    if not memory_id:
                        continue

                    # Check federation_seen
                    cursor.execute("SELECT 1 FROM federation_seen WHERE memory_id = ?", (memory_id,))
                    if cursor.fetchone():
                        memories_skipped += 1
                        continue

                    # Check shared_memories (boomerang detection)
                    cursor.execute("SELECT 1 FROM shared_memories WHERE id = ?", (memory_id,))
                    if cursor.fetchone():
                        memories_skipped += 1
                        # Boomerang backfill (heal federation_seen gap)
                        cursor.execute("""
                            INSERT OR IGNORE INTO federation_seen (memory_id, first_seen_at, source_instance)
                            VALUES (?, ?, ?)
                        """, (memory_id, now.isoformat(), peer_id))
                        continue

                    # NEW memory
                    memories_new += 1
                    new_memory_ids.append(memory_id)

            # Step 2: Write federation_bundles_seen (for admitted + quarantined)
            if decision in ("admitted", "quarantined"):
                cursor.execute("""
                    INSERT OR IGNORE INTO federation_bundles_seen
                    (bundle_id, first_seen_at, source_instance, bundle_hash, decision, memory_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (bundle_id, now.isoformat(), peer_id, bundle_hash, decision, memories_total))

            # Step 3: Write federation_seen for NEW memories (admitted only)
            if decision == "admitted":
                for memory_id in new_memory_ids:
                    cursor.execute("""
                        INSERT OR IGNORE INTO federation_seen (memory_id, first_seen_at, source_instance)
                        VALUES (?, ?, ?)
                    """, (memory_id, now.isoformat(), peer_id))

            # Step 4: Write quarantine row if needed
            if needs_quarantine:
                # Extract memory_id from first memory if parseable
                memory_id = None
                try:
                    memories = bundle.get("memories", [])
                    if memories and isinstance(memories, list) and len(memories) > 0:
                        memory_id = memories[0].get("id")
                except Exception:
                    pass

                # Compute passport hash if passport extractable
                passport_hash = None
                try:
                    passport = bundle.get("passport")
                    if passport:
                        passport_hash = _compute_passport_hash(passport)
                except Exception:
                    pass

                # Serialize full bundle (including signature)
                payload_json = json.dumps(bundle, sort_keys=True, default=str)

                # Quarantine expires in 7 days
                expires_at = (now + timedelta(days=7)).isoformat()

                cursor.execute("""
                    INSERT INTO federation_quarantine (
                        id, memory_id, source_instance, source_passport_hash,
                        reason, payload, received_at, expires_at,
                        reviewed_at, reviewed_by_passport, review_action, review_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """, (
                    quarantine_id,
                    memory_id,
                    peer_id,
                    passport_hash,
                    reason,
                    payload_json,
                    now.isoformat(),
                    expires_at,
                ))

            # Step 5: Write audit row
            action = _decision_to_audit_action(decision)
            cursor.execute("""
                INSERT INTO federation_audit (
                    id, action, actor_passport, target_id, reason, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id,
                action,
                peer_id,
                bundle_id,
                reason,
                json.dumps({**audit_metadata, "memories_new": memories_new, "memories_skipped": memories_skipped}, default=str),
                now.isoformat(),
            ))

            # Commit transaction explicitly (get_db doesn't auto-commit)
            conn.commit()

        # Log mixed bundle info
        if memories_new > 0 and memories_skipped > 0:
            logger.info("Bundle admitted with mixed memories", extra={
                "bundle_id": bundle_id,
                "memories_total": memories_total,
                "memories_new": memories_new,
                "memories_skipped": memories_skipped,
            })

        # Success — return result
        return AdmissionResult(
            admitted=(decision == "admitted"),
            decision=decision,
            reason=reason,
            stage_reached=stage,
            peer_id=peer_id,
            bundle_id=bundle_id,
            quarantine_id=quarantine_id,
            audit_id=audit_id,
            detail=detail,
            metadata=audit_metadata,
            memories_total=memories_total,
            memories_new=memories_new,
            memories_skipped=memories_skipped,
        )

    except sqlite3.Error as exc:
        # Persistence failed — fail closed
        logger.error("admit_bundle persistence with dedup failed: %s", exc, extra={
            "bundle_id": bundle_id, "peer_id": peer_id, "stage": stage,
            "decision_intent": decision,
        })

        return _build_infra_error_result(
            stage=stage,
            decision_intent=decision,
            peer_id=peer_id,
            bundle_id=bundle_id,
            bundle=bundle,
            detail=f"db error during persist with dedup: {exc}",
            now=now,
            metadata=audit_metadata,
        )


def _build_infra_error_result(
    *,
    stage: str,
    decision_intent: str,
    peer_id: Optional[str],
    bundle_id: Optional[str],
    bundle: dict,
    detail: str,
    now: datetime,
    metadata: Optional[dict] = None,
) -> AdmissionResult:
    """Return infra_error result, attempt best-effort audit write.

    If the best-effort audit also fails, swallow silently — logger has it.
    """
    if metadata is None:
        metadata = {}

    audit_id = f"feda-{uuid4().hex[:16]}"

    # Best-effort second transaction — write infra_error audit row
    try:
        with get_db() as conn2:
            cursor = conn2.cursor()
            cursor.execute("""
                INSERT INTO federation_audit (
                    id, action, actor_passport, target_id, reason, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                audit_id,
                "bundle_infra_error",
                peer_id,
                bundle_id,
                "infra_error",
                json.dumps({
                    "stage_reached": stage,
                    "original_decision_intent": decision_intent,
                    "db_error": detail,
                    **metadata,
                }, default=str),
                now.isoformat(),
            ))
            conn2.commit()
        # Best-effort write succeeded
    except Exception:
        # Best-effort write also failed — swallow, logger already has everything
        pass

    return AdmissionResult(
        admitted=False,
        decision="infra_error",
        reason="infra_error",
        stage_reached=stage,
        peer_id=peer_id,
        bundle_id=bundle_id,
        quarantine_id=None,
        audit_id=audit_id,
        detail=detail,
        metadata={
            "original_decision_intent": decision_intent,
            "stage_reached": stage,
            **metadata,
        },
    )


def _derive_bundle_id(bundle: dict) -> str:
    """Derive bundle_id from bundle dict.

    Sender-provided bundle_id takes precedence, else derive from
    SHA256[:16] of canonical bundle (signature stripped).
    """
    bundle_id = bundle.get("bundle_id")
    if bundle_id and isinstance(bundle_id, str):
        return bundle_id

    # Auto-generate deterministic ID from canonical bytes
    canonical_bytes = canonicalize_for_signing(bundle)
    return hashlib.sha256(canonical_bytes).hexdigest()[:16]


def _compute_bundle_hash(bundle: dict) -> str:
    """Compute SHA256 hash of canonical bundle for tamper detection."""
    canonical_bytes = canonicalize_for_signing(bundle)
    return hashlib.sha256(canonical_bytes).hexdigest()  # full 64 hex chars


def _compute_passport_hash(passport: dict) -> str:
    """Compute SHA256 hash of passport for fingerprinting."""
    passport_json = json.dumps(passport, sort_keys=True, default=str)
    return hashlib.sha256(passport_json.encode()).hexdigest()


def _decision_to_audit_action(decision: str) -> str:
    """Map decision to audit action string."""
    if decision == "admitted":
        return "bundle_admitted"
    elif decision == "quarantined":
        return "bundle_quarantined"
    elif decision == "rejected":
        return "bundle_rejected"
    elif decision == "skipped":
        return "bundle_skipped"
    else:
        return "bundle_infra_error"
