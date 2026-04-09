"""Agent discovery service."""

import json
import sqlite3
from typing import Optional

from circus.database import get_db


def discover_agents(
    capability: Optional[str] = None,
    entity: Optional[str] = None,
    trait: Optional[str] = None,
    min_trust: float = 30.0,
    limit: int = 50
) -> list[dict]:
    """
    Discover agents by filters.

    Args:
        capability: Filter by capability tag
        entity: Filter by graph entity
        trait: Filter by behavioral trait
        min_trust: Minimum trust score
        limit: Maximum results

    Returns:
        List of agent records
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build query based on filters
        if capability or entity or trait:
            # Use FTS search for capability
            if capability:
                agents = search_agents_fts(capability, min_trust, limit)
            else:
                # For entity/trait filtering, search in passport data
                query = """
                    SELECT a.*, p.passport_data, p.prediction_accuracy
                    FROM agents a
                    LEFT JOIN passports p ON a.id = p.agent_id
                    WHERE a.trust_score >= ? AND a.is_active = 1
                    ORDER BY a.trust_score DESC
                    LIMIT ?
                """
                cursor.execute(query, (min_trust, limit))
                rows = cursor.fetchall()

                agents = []
                for row in rows:
                    agent = dict(row)
                    passport_data = json.loads(agent.get("passport_data", "{}"))

                    # Filter by entity
                    if entity:
                        entities = passport_data.get("graph_summary", {}).get("entities", [])
                        entity_names = [e.get("name", "") for e in entities]
                        if entity not in entity_names:
                            continue

                    # Filter by trait
                    if trait:
                        traits = passport_data.get("traits", {})
                        if trait not in traits or traits[trait].get("confidence", 0) < 0.7:
                            continue

                    agents.append(agent)
        else:
            # No specific filters, just return by trust score
            query = """
                SELECT a.*, p.prediction_accuracy
                FROM agents a
                LEFT JOIN passports p ON a.id = p.agent_id
                WHERE a.trust_score >= ? AND a.is_active = 1
                ORDER BY a.trust_score DESC
                LIMIT ?
            """
            cursor.execute(query, (min_trust, limit))
            agents = [dict(row) for row in cursor.fetchall()]

        return agents


def search_agents_fts(
    search_query: str,
    min_trust: float = 30.0,
    limit: int = 50
) -> list[dict]:
    """
    Search agents using FTS5 full-text search.

    Args:
        search_query: Search query string
        min_trust: Minimum trust score
        limit: Maximum results

    Returns:
        List of agent records
    """
    with get_db() as conn:
        cursor = conn.cursor()

        query = """
            SELECT a.*, p.prediction_accuracy
            FROM agents a
            LEFT JOIN passports p ON a.id = p.agent_id
            WHERE a.id IN (
                SELECT agent_id FROM agents_fts WHERE agents_fts MATCH ?
            )
            AND a.trust_score >= ?
            AND a.is_active = 1
            ORDER BY a.trust_score DESC
            LIMIT ?
        """

        cursor.execute(query, (search_query, min_trust, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_agent_by_id(agent_id: str) -> Optional[dict]:
    """Get agent by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT a.*, p.passport_data, p.prediction_accuracy
            FROM agents a
            LEFT JOIN passports p ON a.id = p.agent_id
            WHERE a.id = ? AND a.is_active = 1
        """, (agent_id,))

        row = cursor.fetchone()
        return dict(row) if row else None


def find_shared_entities(agent_a_id: str, agent_b_id: str) -> list[str]:
    """Find shared graph entities between two agents."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get both agents' passports
        cursor.execute("""
            SELECT agent_id, passport_data
            FROM passports
            WHERE agent_id IN (?, ?)
            ORDER BY created_at DESC
        """, (agent_a_id, agent_b_id))

        rows = cursor.fetchall()
        if len(rows) < 2:
            return []

        passports = {}
        for row in rows:
            agent_id = row["agent_id"]
            passport_data = json.loads(row["passport_data"])
            passports[agent_id] = passport_data

        # Extract entities
        entities_a = set()
        entities_b = set()

        if agent_a_id in passports:
            graph = passports[agent_a_id].get("graph_summary", {})
            entities_a = {e.get("name") for e in graph.get("entities", [])}

        if agent_b_id in passports:
            graph = passports[agent_b_id].get("graph_summary", {})
            entities_b = {e.get("name") for e in graph.get("entities", [])}

        # Find intersection
        shared = entities_a & entities_b
        return sorted(list(shared))
