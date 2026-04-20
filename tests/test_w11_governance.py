"""Tests for W11: Governance workflows (quarantine + audit)."""

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from circus.app import app
from circus.database import get_db, init_database
from circus.services import quarantine as quar_service
from circus.services.preference_admission import admit_preference
from circus.services.bundle_signing import canonicalize_for_signing


@dataclass
class MockVerifyResult:
    """Mock owner verification result."""
    valid: bool = True
    reason: str = "mock_success"


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    return db_path


@pytest.fixture
def client(test_db, monkeypatch):
    """Create FastAPI test client with test database."""
    from circus.config import settings
    monkeypatch.setattr(settings, "database_path", test_db)
    return TestClient(app)


@pytest.fixture
def cleanup_owner_keys():
    """Clean owner_keys table before and after each test."""
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()
    yield
    with get_db() as conn:
        conn.execute("DELETE FROM owner_keys")
        conn.commit()


@pytest.fixture
def mock_owner_verification(monkeypatch):
    """Mock owner verification to always pass."""
    from circus.services import owner_verification
    monkeypatch.setattr(owner_verification, "verify_owner_binding", lambda *args, **kwargs: MockVerifyResult())


@pytest.fixture
def set_owner_id(monkeypatch):
    """Set CIRCUS_OWNER_ID environment variable."""
    monkeypatch.setenv("CIRCUS_OWNER_ID", "kobus")


def create_test_agent(conn, agent_id=None):
    """Helper to create a test agent with unique ID."""
    if agent_id is None:
        agent_id = f"test-agent-{secrets.token_hex(4)}"

    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        INSERT INTO agents (id, name, role, capabilities, home_instance, passport_hash, token_hash, registered_at, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (agent_id, "Test Agent", "assistant", '["memory"]', "http://localhost:6200",
         secrets.token_hex(16), secrets.token_hex(16), now, now)
    )
    conn.commit()
    return agent_id


def create_test_room(conn, room_id, agent_id):
    """Helper to create a test room."""
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        INSERT INTO rooms (id, name, slug, created_by, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (room_id, "Test Room", f"#{room_id}", agent_id, now)
    )
    conn.commit()


def create_test_memory(conn, memory_id, owner_id, content, category="preference", agent_id="test-agent", room_id="test-room"):
    """Helper to create a test memory."""
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() + "Z"

    cursor.execute(
        """
        INSERT INTO shared_memories (id, room_id, from_agent_id, content, category, shared_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (memory_id, room_id, agent_id, json.dumps(content), category, now)
    )
    conn.commit()


def _generate_test_keypair():
    """Generate Ed25519 keypair for testing."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    return private_key, private_bytes, public_bytes


def _insert_owner_key(conn, owner_id: str, public_key_b64: str):
    """Helper to insert owner key into DB."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
        (owner_id, public_key_b64, datetime.utcnow().isoformat())
    )
    conn.commit()


def _sign_owner_binding(private_key, owner_id: str, agent_id: str, memory_id: str, timestamp: str) -> str:
    """Sign owner binding payload and return base64 signature."""
    payload = {
        "agent_id": agent_id,
        "memory_id": memory_id,
        "owner_id": owner_id,
        "timestamp": timestamp,
    }
    canonical_bytes = canonicalize_for_signing(payload)
    signature = private_key.sign(canonical_bytes)
    return base64.b64encode(signature).decode('ascii')


def test_borderline_confidence_auto_quarantined(test_db, mock_owner_verification, set_owner_id, cleanup_owner_keys):
    """Memories with confidence 0.5-0.69 should be quarantined."""

    with get_db() as conn:
        cursor = conn.cursor()
        agent_id = create_test_agent(conn)
        room_id = f"test-room-{secrets.token_hex(4)}"
        create_test_room(conn, room_id, agent_id)

        # Create owner key with real Ed25519 keypair
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')
        _insert_owner_key(conn, "kobus", public_key_b64)

        # Create memory with valid signature
        memory_id = f"mem-{secrets.token_hex(8)}"
        now = datetime.utcnow()
        timestamp = now.isoformat() + "Z"

        signature = _sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id=agent_id,
            memory_id=memory_id,
            timestamp=timestamp
        )

        content = {
            "text": "User prefers Afrikaans",
            "provenance": {
                "owner_id": "kobus",
                "preference_field": "user.language_preference",
                "preference_value": "af",
                "owner_binding": {
                    "agent_id": agent_id,
                    "memory_id": memory_id,
                    "timestamp": timestamp,
                    "signature": signature,
                }
            }
        }

        create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

        # Admit with borderline confidence (0.6)
        result = admit_preference(
            conn,
            memory_id=memory_id,
            owner_id="kobus",
            preference_field="user.language_preference",
            preference_value="af",
            effective_confidence=0.6,
            now=now,
            agent_id=agent_id,
            shared_at=timestamp,
            owner_binding=content["provenance"]["owner_binding"],
        )

        assert not result.admitted
        assert result.reason == "confidence_borderline_quarantined"

        # Check quarantine created
        cursor.execute("SELECT COUNT(*) FROM quarantine WHERE memory_id = ?", (memory_id,))
        count = cursor.fetchone()[0]
        assert count == 1

        # Check audit event created
        cursor.execute("SELECT COUNT(*) FROM governance_audit WHERE event_type = 'quarantine_created'")
        audit_count = cursor.fetchone()[0]
        assert audit_count == 1


def test_below_borderline_not_quarantined(test_db, mock_owner_verification, set_owner_id, cleanup_owner_keys):
    """Memories with confidence < 0.5 should be skipped without quarantine."""
    with get_db() as conn:
        cursor = conn.cursor()
        agent_id = create_test_agent(conn)
        room_id = f"test-room-{secrets.token_hex(4)}"
        create_test_room(conn, room_id, agent_id)

        # Create owner key with real Ed25519 keypair
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')
        _insert_owner_key(conn, "kobus", public_key_b64)

        # Create memory with valid signature
        memory_id = f"mem-{secrets.token_hex(8)}"
        now = datetime.utcnow()
        timestamp = now.isoformat() + "Z"

        signature = _sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id=agent_id,
            memory_id=memory_id,
            timestamp=timestamp
        )

        content = {
            "text": "User prefers Afrikaans",
            "provenance": {
                "owner_id": "kobus",
                "preference_field": "user.language_preference",
                "preference_value": "af",
                "owner_binding": {
                    "agent_id": agent_id,
                    "memory_id": memory_id,
                    "timestamp": timestamp,
                    "signature": signature,
                }
            }
        }

        create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

        # Admit with low confidence (0.3)
        result = admit_preference(
            conn,
            memory_id=memory_id,
            owner_id="kobus",
            preference_field="user.language_preference",
            preference_value="af",
            effective_confidence=0.3,
            now=now,
            agent_id=agent_id,
            shared_at=timestamp,
            owner_binding=content["provenance"]["owner_binding"],
        )

        assert not result.admitted
        assert result.reason == "confidence_below_threshold"

        # Check NO quarantine created
        cursor.execute("SELECT COUNT(*) FROM quarantine WHERE memory_id = ?", (memory_id,))
        count = cursor.fetchone()[0]
        assert count == 0


def test_quarantine_list_api(test_db, client):
    """GET /governance/quarantine returns quarantined entries."""
    # Override dependency
    def mock_verify_token():
        return "test-agent-mock"

    from circus.routes.agents import verify_token
    app.dependency_overrides[verify_token] = mock_verify_token

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            agent_id = create_test_agent(conn)
            room_id = f"test-room-{secrets.token_hex(4)}"
            create_test_room(conn, room_id, agent_id)

            # Create test memory
            memory_id = f"mem-{secrets.token_hex(8)}"
            content = {"text": "Test memory"}
            create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

            # Quarantine it
            quar_service.quarantine_memory(conn, memory_id, "kobus", "confidence_borderline")
            conn.commit()

        response = client.get(
            "/api/v1/governance/quarantine",
            headers={"Authorization": "Bearer mock-token"}
        )
        assert response.status_code == 200

        data = response.json()
        assert data["count"] == 1
        assert len(data["quarantined"]) == 1
        assert data["quarantined"][0]["owner_id"] == "kobus"
        assert data["quarantined"][0]["reason"] == "confidence_borderline"
    finally:
        app.dependency_overrides.clear()


def test_quarantine_release_with_admit(test_db, client, mock_owner_verification, set_owner_id, cleanup_owner_keys):
    """POST /quarantine/{id}/release with admit=true activates preference."""
    # Override dependency
    def mock_verify_token():
        return "operator-mock"

    from circus.routes.agents import verify_token
    app.dependency_overrides[verify_token] = mock_verify_token

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            agent_id = create_test_agent(conn)
            room_id = f"test-room-{secrets.token_hex(4)}"
            create_test_room(conn, room_id, agent_id)

            # Create owner key with real Ed25519 keypair
            private_key, _, public_bytes = _generate_test_keypair()
            public_key_b64 = base64.b64encode(public_bytes).decode('ascii')
            _insert_owner_key(conn, "kobus", public_key_b64)

            # Create preference memory with valid signature
            memory_id = f"mem-{secrets.token_hex(8)}"
            now = datetime.utcnow()
            timestamp = now.isoformat() + "Z"

            signature = _sign_owner_binding(
                private_key,
                owner_id="kobus",
                agent_id=agent_id,
                memory_id=memory_id,
                timestamp=timestamp
            )

            content = {
                "text": "User prefers Afrikaans",
                "provenance": {
                    "owner_id": "kobus",
                    "preference_field": "user.language_preference",
                    "preference_value": "af",
                    "owner_binding": {
                        "agent_id": agent_id,
                        "memory_id": memory_id,
                        "timestamp": timestamp,
                        "signature": signature,
                    }
                }
            }
            create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

            # Quarantine it
            quar_id = quar_service.quarantine_memory(conn, memory_id, "kobus", "confidence_borderline")
            conn.commit()

        # Release with admit=true
        response = client.post(
            f"/api/v1/governance/quarantine/{quar_id}/release",
            json={"admit": True, "reason": "Manual review passed"},
            headers={"Authorization": "Bearer mock-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["released"] is True
        assert data["admitted"] is True

        # Check preference activated
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM active_preferences WHERE owner_id = 'kobus'")
            count = cursor.fetchone()[0]
            assert count == 1
    finally:
        app.dependency_overrides.clear()


def test_quarantine_release_without_admit(test_db, client):
    """POST /quarantine/{id}/release with admit=false just releases."""
    # Override dependency
    def mock_verify_token():
        return "operator-mock"

    from circus.routes.agents import verify_token
    app.dependency_overrides[verify_token] = mock_verify_token

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            agent_id = create_test_agent(conn)
            room_id = f"test-room-{secrets.token_hex(4)}"
            create_test_room(conn, room_id, agent_id)

            # Create test memory
            memory_id = f"mem-{secrets.token_hex(8)}"
            content = {"text": "Test memory"}
            create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

            # Quarantine it
            quar_id = quar_service.quarantine_memory(conn, memory_id, "kobus", "confidence_borderline")
            conn.commit()

        # Release with admit=false
        response = client.post(
            f"/api/v1/governance/quarantine/{quar_id}/release",
            json={"admit": False, "reason": "Released without activation"},
            headers={"Authorization": "Bearer mock-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["released"] is True
        assert data["admitted"] is False

        # Check preference NOT activated
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM active_preferences WHERE owner_id = 'kobus'")
            count = cursor.fetchone()[0]
            assert count == 0
    finally:
        app.dependency_overrides.clear()


def test_quarantine_discard(test_db, client):
    """POST /quarantine/{id}/discard marks entry as discarded."""
    # Override dependency
    def mock_verify_token():
        return "operator-mock"

    from circus.routes.agents import verify_token
    app.dependency_overrides[verify_token] = mock_verify_token

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            agent_id = create_test_agent(conn)
            room_id = f"test-room-{secrets.token_hex(4)}"
            create_test_room(conn, room_id, agent_id)

            # Create test memory
            memory_id = f"mem-{secrets.token_hex(8)}"
            content = {"text": "Test memory"}
            create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

            # Quarantine it
            quar_id = quar_service.quarantine_memory(conn, memory_id, "kobus", "confidence_borderline")
            conn.commit()

        # Discard
        response = client.post(
            f"/api/v1/governance/quarantine/{quar_id}/discard",
            headers={"Authorization": "Bearer mock-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["discarded"] is True

        # Check quarantine marked as released
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT released_at FROM quarantine WHERE id = ?", (quar_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] is not None  # released_at set
    finally:
        app.dependency_overrides.clear()


def test_audit_log_written_on_activation(test_db, mock_owner_verification, set_owner_id, cleanup_owner_keys):
    """When preference is activated, audit event is written."""
    with get_db() as conn:
        cursor = conn.cursor()
        agent_id = create_test_agent(conn)
        room_id = f"test-room-{secrets.token_hex(4)}"
        create_test_room(conn, room_id, agent_id)

        # Create owner key with real Ed25519 keypair
        private_key, _, public_bytes = _generate_test_keypair()
        public_key_b64 = base64.b64encode(public_bytes).decode('ascii')
        _insert_owner_key(conn, "kobus", public_key_b64)

        # Create memory with valid signature
        memory_id = f"mem-{secrets.token_hex(8)}"
        now = datetime.utcnow()
        timestamp = now.isoformat() + "Z"

        signature = _sign_owner_binding(
            private_key,
            owner_id="kobus",
            agent_id=agent_id,
            memory_id=memory_id,
            timestamp=timestamp
        )

        content = {
            "text": "User prefers Afrikaans",
            "provenance": {
                "owner_id": "kobus",
                "preference_field": "user.language_preference",
                "preference_value": "af",
                "owner_binding": {
                    "agent_id": agent_id,
                    "memory_id": memory_id,
                    "timestamp": timestamp,
                    "signature": signature,
                }
            }
        }

        create_test_memory(conn, memory_id, "kobus", content, agent_id=agent_id, room_id=room_id)

        # Admit with high confidence (activates)
        result = admit_preference(
            conn,
            memory_id=memory_id,
            owner_id="kobus",
            preference_field="user.language_preference",
            preference_value="af",
            effective_confidence=0.8,
            now=now,
            agent_id=agent_id,
            shared_at=timestamp,
            owner_binding=content["provenance"]["owner_binding"],
        )

        assert result.admitted

        # Check audit event written
        cursor.execute("SELECT COUNT(*) FROM governance_audit WHERE event_type = 'preference_activated'")
        count = cursor.fetchone()[0]
        assert count == 1

        # Check detail field
        cursor.execute("SELECT detail FROM governance_audit WHERE event_type = 'preference_activated'")
        row = cursor.fetchone()
        detail = json.loads(row[0])
        assert detail["field"] == "user.language_preference"
        assert detail["value"] == "af"


def test_audit_api(test_db, client):
    """GET /governance/audit returns merged audit events."""
    # Override dependency
    def mock_verify_token():
        return "operator"

    from circus.routes.agents import verify_token
    app.dependency_overrides[verify_token] = mock_verify_token

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Write test audit events
            quar_service.write_audit_event(
                conn,
                event_type="preference_activated",
                actor="test-agent",
                owner_id="kobus",
                detail=json.dumps({"field": "user.language_preference", "value": "af"})
            )

            quar_service.write_audit_event(
                conn,
                event_type="quarantine_created",
                actor="system",
                owner_id="kobus",
                detail=json.dumps({"memory_id": "mem-123", "reason": "confidence_borderline"})
            )

            conn.commit()

        response = client.get(
            "/api/v1/governance/audit",
            headers={"Authorization": "Bearer mock-token"}
        )
        assert response.status_code == 200

        data = response.json()
        assert data["count"] >= 2
        assert any(e["event_type"] == "preference_activated" for e in data["events"])
        assert any(e["event_type"] == "quarantine_created" for e in data["events"])
    finally:
        app.dependency_overrides.clear()
