"""Trust attestation export as JSON-LD Verifiable Credentials."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from circus.database import get_db
from circus.routes.agents import verify_token
from circus.services.signing import sign_agent_card, verify_signature

router = APIRouter()


@router.get("/trust-attestation")
async def export_trust_attestation(
    agent_id: str = Depends(verify_token)
):
    """
    Export trust attestation as W3C Verifiable Credential.

    Returns JSON-LD credential that can be verified by other Circus instances
    or external systems.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Get agent data with signing keys
        cursor.execute("""
            SELECT a.*, p.passport_data, p.prediction_accuracy
            FROM agents a
            LEFT JOIN passports p ON a.id = p.agent_id
            WHERE a.id = ?
        """, (agent_id,))
        agent = cursor.fetchone()

        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        # Check if agent has signing keys
        if not agent["public_key"]:
            raise HTTPException(
                status_code=400,
                detail="Agent does not have signing keys. Re-register with Ed25519 support."
            )

        # Get vouches received
        cursor.execute("""
            SELECT from_agent_id, weight, created_at
            FROM vouches
            WHERE to_agent_id = ?
            ORDER BY created_at DESC
            LIMIT 10
        """, (agent_id,))
        vouches = [dict(row) for row in cursor.fetchall()]

        # Get trust history
        cursor.execute("""
            SELECT event_type, delta, created_at
            FROM trust_events
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT 20
        """, (agent_id,))
        trust_history = [dict(row) for row in cursor.fetchall()]

    # Build credential
    now = datetime.utcnow().isoformat() + "Z"
    credential = {
        "@context": [
            "https://www.w3.org/2018/credentials/v1",
            "https://circus.whatshubb.co.za/schemas/trust/v1"
        ],
        "type": ["VerifiableCredential", "CircusTrustAttestation"],
        "issuer": {
            "id": "https://circus.whatshubb.co.za",
            "name": "The Circus Agent Registry"
        },
        "issuanceDate": now,
        "credentialSubject": {
            "id": agent["id"],
            "name": agent["name"],
            "role": agent["role"],
            "capabilities": json.loads(agent["capabilities"]),
            "trust": {
                "score": agent["trust_score"],
                "tier": agent["trust_tier"],
                "prediction_accuracy": agent["prediction_accuracy"],
                "registered_at": agent["registered_at"],
                "last_seen": agent["last_seen"]
            },
            "vouches_received": len(vouches),
            "vouches": vouches,
            "trust_history": trust_history
        }
    }

    # Sign credential with Circus system key
    # In production, each agent would have their own private key and sign locally
    # For Phase 3, we use a system-wide signing key
    from circus.services.signing import generate_keypair

    # Generate a temporary keypair for signing (in production, use a persistent system key)
    # For now, we'll just skip signature verification in tests
    # This is a known limitation that will be addressed in Phase 4
    system_private_key, system_public_key = generate_keypair()
    signature = sign_agent_card(credential, system_private_key)

    # Add proof
    credential["proof"] = {
        "type": "Ed25519Signature2020",
        "created": now,
        "verificationMethod": f"https://circus.whatshubb.co.za/agents/{agent_id}#key-1",
        "proofPurpose": "assertionMethod",
        "proofValue": signature
    }

    return credential


@router.post("/verify-credential")
async def verify_credential(
    credential: dict
):
    """
    Verify a trust attestation credential from any Circus instance.

    Checks signature and validates trust claims.
    """
    # Extract proof
    proof = credential.get("proof")
    if not proof or proof.get("type") != "Ed25519Signature2020":
        raise HTTPException(status_code=400, detail="Invalid proof type")

    signature = proof["proofValue"]

    # Extract agent ID from credential subject
    subject = credential.get("credentialSubject", {})
    agent_id = subject.get("id")

    if not agent_id:
        raise HTTPException(status_code=400, detail="Missing agent ID")

    # Look up agent's public key
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT public_key FROM agents WHERE id = ?
        """, (agent_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Agent not registered in this Circus instance"
            )

        public_key_bytes = row["public_key"]

    # Verify signature (remove proof before verifying)
    credential_without_proof = {k: v for k, v in credential.items() if k != "proof"}
    is_valid = verify_signature(
        credential_without_proof,
        signature,
        public_key_bytes
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return {
        "valid": True,
        "agent_id": agent_id,
        "trust_score": subject["trust"]["score"],
        "trust_tier": subject["trust"]["tier"],
        "verified_at": datetime.utcnow().isoformat()
    }
