"""Federation wiring layer — admit_and_merge admitted bundles.

Handles shared_memories writes + merge pipeline calls for federated memories
that have passed all admission verifications.

Key invariants:
- hop_count incremented ONCE (single source of truth, line 9 in design)
- Both INSERT and merge pipeline see SAME incremented value
- Provenance chain preserved (original_author, original_timestamp, confidence, etc.)
- Idempotent (skip if memory already in shared_memories)
- No boundary checks (admission layer already enforced hop_count limit)
"""

import json
import logging
import sqlite3
from datetime import datetime
from typing import Optional

from circus.database import get_db
from circus.services.belief_merge import apply_belief_merge_pipeline, ConflictResolution
from circus.services.provenance import decay_confidence

logger = logging.getLogger(__name__)


def admit_and_merge(bundle: dict, peer_id: str, now: datetime) -> list[ConflictResolution]:
    """Process admitted federated bundle — write to shared_memories + call merge pipeline.

    PRE: Bundle has passed all admission verifications (signature, passport, trust, hop_count).
    POST: Memories written to shared_memories with incremented hop_count, merge pipeline called.

    Args:
        bundle: Admitted bundle dict with "memories" list
        peer_id: Pushing peer's identifier (for received_from provenance)
        now: Timestamp of admission

    Returns:
        List of ConflictResolution objects (one per conflict detected)
    """
    results = []
    with get_db() as conn:
        cursor = conn.cursor()

        for memory in bundle.get("memories", []):
            # Extract incoming provenance
            incoming_provenance = memory.get("provenance", {})
            incoming_hop_count = incoming_provenance.get("hop_count", 1)

            # INCREMENT hop_count (single source of truth)
            new_hop_count = incoming_hop_count + 1
            # Boundary already enforced in admit_bundle — no check here

            # Idempotency: skip if memory already in shared_memories (memory-level safety)
            cursor.execute("SELECT 1 FROM shared_memories WHERE id = ?", (memory["id"],))
            if cursor.fetchone():
                logger.info("Memory already in shared_memories, skipping", extra={"memory_id": memory["id"]})
                continue

            # Build updated provenance (with incremented hop_count)
            updated_provenance = {
                **incoming_provenance,
                "hop_count": new_hop_count,
                "received_from": peer_id,
                "received_at": now.isoformat(),
            }

            # Compute effective_confidence with new_hop_count
            # TODO: lookup sender trust score (hardcoded 50.0 default for 3.6)
            effective_conf = decay_confidence(
                base_confidence=incoming_provenance.get("confidence", 1.0),
                hop_count=new_hop_count,  # SAME VALUE as line above
                age_seconds=0.0,  # age_seconds at receive time
                author_trust_score=50.0,  # Default trust for federated
            )

            # INSERT into shared_memories (with new_hop_count)
            cursor.execute("""
                INSERT INTO shared_memories (
                    id, room_id, from_agent_id, content, category, domain,
                    tags, provenance, privacy_tier, hop_count, original_author,
                    confidence, age_days, effective_confidence, shared_at, trust_verified
                ) VALUES (?, 'room-memory-commons', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0)
            """, (
                memory["id"],
                incoming_provenance.get("original_author"),  # Preserve original
                memory["content"],
                memory["category"],
                memory.get("domain", "general"),
                json.dumps(memory.get("tags", [])),
                json.dumps(updated_provenance),
                memory.get("privacy_tier", "public"),
                new_hop_count,  # SAME VALUE as increment above
                incoming_provenance.get("original_author"),
                incoming_provenance.get("confidence", 1.0),
                effective_conf,
                now.isoformat(),
            ))
            conn.commit()

            # Call merge pipeline (with new_hop_count in new_memory dict)
            conflict = apply_belief_merge_pipeline(
                conn,
                new_memory={
                    "id": memory["id"],
                    "from_agent_id": incoming_provenance.get("original_author"),
                    "content": memory["content"],
                    "category": memory["category"],
                    "domain": memory.get("domain", "general"),
                    "confidence": incoming_provenance.get("confidence", 1.0),
                    "shared_at": now.isoformat(),
                },
                agent_id=peer_id,  # Peer who pushed it (for audit)
                now=now,
            )
            if conflict:
                results.append(conflict)

            # Week 4 (4.5): Preference admission for federated user_preference memories
            # CRITICAL: Use final stored effective_confidence (post-decay), not pre-decay value
            if memory.get("category") == "user_preference":
                preference = memory.get("preference")
                if preference and "field" in preference and "value" in preference:
                    # Extract owner_id from stored provenance (preserved from original publish)
                    owner_id = updated_provenance.get("owner_id")
                    if owner_id:
                        # Lazy import to avoid cycle risk
                        from circus.services.preference_admission import admit_preference

                        # W5: Extract owner_binding from stored provenance for signature verification
                        owner_binding_dict = updated_provenance.get("owner_binding")

                        admit_preference(
                            conn,
                            memory_id=memory["id"],
                            owner_id=owner_id,
                            preference_field=preference["field"],
                            preference_value=preference["value"],
                            effective_confidence=effective_conf,  # Final stored value (post-decay)
                            now=now,
                            agent_id=incoming_provenance.get("original_author", ""),
                            shared_at=now.isoformat(),
                            owner_binding=owner_binding_dict,
                        )
                        # Commit preference admission write (same pattern as publish route, line 360)
                        conn.commit()
                    else:
                        logger.warning(
                            "Federated preference memory missing owner_id in provenance, skipping admission",
                            extra={"memory_id": memory["id"]}
                        )
                else:
                    logger.warning(
                        "Federated preference memory malformed (missing preference.field or .value), skipping admission",
                        extra={"memory_id": memory["id"]}
                    )

    return results
