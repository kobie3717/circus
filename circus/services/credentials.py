"""W3C Verifiable Credentials for trust portability."""

import json
import time
from .signing import sign_agent_card, verify_signature


def generate_credential(agent_id, agent_name, trust_score, ring, capabilities, vouched_by, private_key_pem):
    """Generate a W3C Verifiable Credential for trust attestation."""
    credential = {
        "@context": ["https://www.w3.org/2018/credentials/v1"],
        "type": ["VerifiableCredential", "TrustAttestation"],
        "issuer": f"circus:agent:{agent_id}",
        "issuanceDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "credentialSubject": {
            "id": f"circus:agent:{agent_id}",
            "name": agent_name,
            "trustScore": trust_score,
            "ring": ring,
            "capabilities": capabilities or [],
            "vouchedBy": vouched_by or []
        }
    }
    # Sign the credential
    payload = json.dumps(credential, sort_keys=True)
    signature = sign_agent_card(json.loads(payload), private_key_pem)
    credential["proof"] = {
        "type": "Ed25519Signature2020",
        "created": credential["issuanceDate"],
        "verificationMethod": f"circus:agent:{agent_id}#key-1",
        "proofPurpose": "assertionMethod",
        "proofValue": signature
    }
    return credential


def verify_credential(credential):
    """Verify a VC's structure is valid. Returns (valid: bool, reason: str)."""
    required = ["@context", "type", "issuer", "credentialSubject", "proof"]
    for field in required:
        if field not in credential:
            return False, f"Missing field: {field}"
    if "TrustAttestation" not in credential.get("type", []):
        return False, "Not a TrustAttestation credential"
    if "proofValue" not in credential.get("proof", {}):
        return False, "Missing proof signature"
    return True, "Valid credential structure"
