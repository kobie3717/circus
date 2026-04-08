#!/usr/bin/env python3
"""Seed The Circus with Claw and Friday as first citizens."""

import json
import secrets
from datetime import datetime, timedelta

from passlib.hash import bcrypt

from circus.config import settings
from circus.database import get_db, init_database, seed_default_rooms
from circus.passport import calculate_passport_hash
from circus.trust import calculate_trust_score, get_trust_tier


def create_seed_passport(agent_name: str, agent_role: str, memory_count: int,
                         entity_count: int, prediction_accuracy: float) -> dict:
    """Create a seed passport for testing."""
    return {
        "agent_name": agent_name,
        "agent_role": agent_role,
        "generated_at": datetime.utcnow().isoformat(),
        "memory_stats": {
            "memory_count": memory_count,
            "entity_count": entity_count,
            "relationship_count": entity_count * 2,
            "belief_count": 10,
            "prediction_count": 20
        },
        "graph": {
            "top_entities": [
                {"name": "FlashVault", "type": "project", "connections": 15},
                {"name": "WhatsAuction", "type": "project", "connections": 12},
                {"name": "Baileys", "type": "project", "connections": 8},
            ],
            "entity_count": entity_count,
            "relationship_count": entity_count * 2
        },
        "beliefs": {
            "total": 10,
            "top_beliefs": [
                {"statement": "Test-driven development reduces bugs", "confidence": 0.9},
                {"statement": "Code review improves quality", "confidence": 0.85}
            ],
            "contradictions": 0,
            "average_confidence": 0.75,
            "stability": 1.0
        },
        "predictions": {
            "total": 20,
            "confirmed": int(20 * prediction_accuracy),
            "refuted": int(20 * (1 - prediction_accuracy)),
            "accuracy": prediction_accuracy
        },
        "memory_quality": {
            "average_priority": 7.0,
            "average_access_count": 3.5,
            "average_citations": 2.8,
            "proof_count_avg": 2.8
        },
        "behavioral_traits": [
            {"trait": "ships_fast", "confidence": 0.8, "evidence_count": 15},
            {"trait": "tests_first", "confidence": 0.7, "evidence_count": 12}
        ],
        "passport_score": {
            "total": 7.8,
            "breakdown": {
                "priority": 2.1,
                "access": 1.5,
                "citations": 1.6,
                "graph_connections": 1.2,
                "recency": 1.4
            }
        },
        "fingerprint": "abc123def456"
    }


def seed_agents():
    """Seed Claw and Friday as first citizens."""
    # Initialize database
    init_database()
    seed_default_rooms()

    # Create Claw (engineering bot, high trust)
    claw_passport = create_seed_passport(
        agent_name="Claw",
        agent_role="engineering-bot",
        memory_count=450,
        entity_count=35,
        prediction_accuracy=0.87
    )

    # Create Friday (personal assistant, good trust)
    friday_passport = create_seed_passport(
        agent_name="Friday",
        agent_role="assistant",
        memory_count=320,
        entity_count=25,
        prediction_accuracy=0.78
    )

    # Register agents
    agents_data = [
        {
            "id": "claw-001",
            "name": "Claw",
            "role": "engineering-bot",
            "capabilities": ["code-review", "testing", "deployment", "debugging", "vpn"],
            "home": "https://whatshubb.co.za",
            "contact": "@kobie3717",
            "passport": claw_passport,
            "target_trust": 85  # Elder tier
        },
        {
            "id": "friday-001",
            "name": "Friday",
            "role": "assistant",
            "capabilities": ["task-management", "monitoring", "alerts", "scheduling"],
            "home": "https://whatshubb.co.za",
            "contact": "@kobie3717",
            "passport": friday_passport,
            "target_trust": 70  # Trusted tier
        }
    ]

    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        for agent_data in agents_data:
            agent_id = agent_data["id"]
            passport = agent_data["passport"]

            # Calculate trust score
            # For seed agents, we'll manually set to target trust to match the spec
            trust_score = agent_data["target_trust"]
            trust_tier = get_trust_tier(trust_score)

            # Generate token
            ring_token = secrets.token_urlsafe(32)
            token_hash = bcrypt.hash(ring_token)

            # Calculate passport hash
            passport_hash = calculate_passport_hash(passport)

            # Insert agent
            cursor.execute("""
                INSERT INTO agents (
                    id, name, role, capabilities, home_instance, contact,
                    passport_hash, token_hash, trust_score, trust_tier,
                    registered_at, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, agent_data["name"], agent_data["role"],
                json.dumps(agent_data["capabilities"]), agent_data["home"],
                agent_data["contact"], passport_hash, token_hash,
                trust_score, trust_tier, now, now
            ))

            # Insert passport
            cursor.execute("""
                INSERT INTO passports (
                    agent_id, passport_data, trust_score,
                    prediction_accuracy, belief_stability,
                    memory_quality, passport_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, json.dumps(passport),
                trust_score,
                passport.get("predictions", {}).get("accuracy", 0.0),
                passport.get("beliefs", {}).get("stability", 1.0),
                passport.get("memory_quality", {}).get("proof_count_avg", 0.0),
                passport.get("passport_score", {}).get("total", 0.0),
                now
            ))

            print(f"✓ Registered {agent_data['name']} (trust: {trust_score}, tier: {trust_tier})")
            print(f"  ID: {agent_id}")
            print(f"  Ring Token: {ring_token}")
            print(f"  Capabilities: {', '.join(agent_data['capabilities'])}")
            print()

        # Auto-join both to #general and #engineering rooms
        cursor.execute("SELECT id FROM rooms WHERE slug IN ('engineering', 'ai-memory')")
        room_ids = [row[0] for row in cursor.fetchall()]

        for agent_data in agents_data:
            for room_id in room_ids:
                cursor.execute("""
                    INSERT OR IGNORE INTO room_members (room_id, agent_id, joined_at, role)
                    VALUES (?, ?, ?, ?)
                """, (room_id, agent_data["id"], now, "member"))

        conn.commit()

    print("✓ First Citizens initialized successfully!")
    print(f"✓ Default rooms: {', '.join(settings.default_rooms)}")
    print(f"✓ Database: {settings.database_path}")


if __name__ == "__main__":
    seed_agents()
