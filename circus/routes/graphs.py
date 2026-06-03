"""Graph orchestration routes."""

import json
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from circus.database import get_db
from circus.routes.agents import verify_token

router = APIRouter()


def dict_factory(cursor, row):
    """Convert row to dict."""
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}


@router.post("/define")
async def define_graph(
    request: dict,
    agent_id: str = Depends(verify_token)
):
    """
    Define a new versioned graph.

    Body:
    {
      "name": "my-workflow",
      "version": 1,
      "definition": { ...graph JSON... }
    }
    """
    name = request.get("name")
    version = request.get("version", 1)
    definition = request.get("definition")

    if not name or not definition:
        raise HTTPException(status_code=400, detail="name and definition required")

    # Validate definition has required fields
    if not isinstance(definition, dict):
        raise HTTPException(status_code=400, detail="definition must be a JSON object")

    if "nodes" not in definition or "edges" not in definition:
        raise HTTPException(status_code=400, detail="definition must have nodes and edges")

    # Generate graph ID
    graph_id = f"graph-{secrets.token_hex(16)}"
    now = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.cursor()

        # Check for duplicate name+version
        cursor.execute(
            "SELECT id FROM graph_definitions WHERE name = ? AND version = ?",
            (name, version)
        )
        if cursor.fetchone():
            raise HTTPException(
                status_code=409,
                detail=f"Graph {name} version {version} already exists"
            )

        # Inject name into definition so JS deserializer can reconstruct it
        definition['name'] = name

        # Insert graph definition
        cursor.execute("""
            INSERT INTO graph_definitions (id, name, version, created_by, definition, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (graph_id, name, version, agent_id, json.dumps(definition), now))

        conn.commit()

    return {
        "graph_id": graph_id,
        "name": name,
        "version": version,
        "created_at": now
    }


@router.post("/run/{graph_id_or_name}")
async def run_graph(
    graph_id_or_name: str,
    request: dict,
    agent_id: str = Depends(verify_token)
):
    """
    Start a graph execution.

    Body:
    {
      "input": { ...input data... }
    }
    """
    input_data = request.get("input", {})

    # Resolve graph ID from name if needed
    resolved_graph_id = graph_id_or_name
    graph_version = None

    with get_db() as conn:
        cursor = conn.cursor()

        if not graph_id_or_name.startswith("graph-"):
            # Lookup by name (latest version)
            cursor.execute(
                "SELECT id, version FROM graph_definitions WHERE name = ? ORDER BY version DESC LIMIT 1",
                (graph_id_or_name,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Graph not found: {graph_id_or_name}")
            resolved_graph_id = row["id"]
            graph_version = row["version"]
        else:
            # Lookup by ID
            cursor.execute(
                "SELECT version FROM graph_definitions WHERE id = ?",
                (graph_id_or_name,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Graph not found: {graph_id_or_name}")
            graph_version = row["version"]

        # Create execution record
        execution_id = f"exec-{secrets.token_hex(16)}"
        now = datetime.utcnow().isoformat()

        cursor.execute("""
            INSERT INTO graph_executions (
                id, graph_id, graph_version, started_by, state,
                input_data, execution_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', ?, '[]', ?, ?)
        """, (execution_id, resolved_graph_id, graph_version, agent_id, json.dumps(input_data), now, now))

        conn.commit()

    # Spawn graph execution as detached background process
    import subprocess
    import os
    import shutil

    node_bin = shutil.which('node') or 'node'
    # Support GRAPH_ENGINE_DIR env var; fall back to hydrabot path
    graph_engine_dir = os.environ.get(
        'GRAPH_ENGINE_DIR',
        os.path.join(os.path.expanduser('~'), 'hydrabot', 'graph-engine')
    )
    graph_engine_script = os.path.join(graph_engine_dir, 'run-execution.mjs')

    env = os.environ.copy()
    env['GRAPH_AGENT_ID'] = agent_id
    env['GRAPH_RING_TOKEN'] = ''  # runner uses Circus DB directly

    try:
        subprocess.Popen(
            [node_bin, graph_engine_script, execution_id, resolved_graph_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent
            env=env
        )
    except Exception as e:
        # Non-fatal — execution record created, can be manually triggered
        import sys
        print(f"[graphs] Warning: Failed to spawn execution: {e}", file=sys.stderr)

    return {
        "execution_id": execution_id,
        "graph_id": resolved_graph_id,
        "state": "running",
        "created_at": now
    }


@router.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    agent_id: str = Depends(verify_token)
):
    """Get execution status."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.row_factory = dict_factory

        cursor.execute(
            "SELECT * FROM graph_executions WHERE id = ?",
            (execution_id,)
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Check authorization
    if row["started_by"] != agent_id:
        raise HTTPException(status_code=403, detail="Not authorized to view this execution")

    return row


@router.get("/executions")
async def list_executions(
    state: Optional[str] = Query(None, pattern="^(running|paused|completed|failed|canceled)$"),
    limit: int = Query(50, ge=1, le=500),
    agent_id: str = Depends(verify_token)
):
    """List graph executions for the calling agent."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.row_factory = dict_factory

        if state:
            cursor.execute(
                "SELECT * FROM graph_executions WHERE started_by = ? AND state = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, state, limit)
            )
        else:
            cursor.execute(
                "SELECT * FROM graph_executions WHERE started_by = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit)
            )

        rows = cursor.fetchall()

    return {"executions": rows, "count": len(rows)}


@router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: str,
    request: dict,
    agent_id: str = Depends(verify_token)
):
    """
    Resume a paused execution (respond to human approval).

    Body:
    {
      "approval_id": "approval-...",
      "response": "approve" | "reject" | "..."
    }
    """
    approval_id = request.get("approval_id")
    response = request.get("response")

    if not approval_id or not response:
        raise HTTPException(status_code=400, detail="approval_id and response required")

    with get_db() as conn:
        cursor = conn.cursor()

        # Verify execution belongs to agent
        cursor.execute(
            "SELECT started_by FROM graph_executions WHERE id = ?",
            (execution_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Execution not found")
        if row["started_by"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Update approval
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            UPDATE graph_human_approvals
            SET response = ?, responded_by = ?, responded_at = ?
            WHERE id = ? AND execution_id = ?
        """, (response, agent_id, now, approval_id, execution_id))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Approval not found")

        conn.commit()

    return {"status": "resumed", "approval_id": approval_id}


@router.delete("/executions/{execution_id}")
async def cancel_execution(
    execution_id: str,
    agent_id: str = Depends(verify_token)
):
    """Cancel a running or paused execution."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Verify execution belongs to agent
        cursor.execute(
            "SELECT started_by, state FROM graph_executions WHERE id = ?",
            (execution_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Execution not found")
        if row["started_by"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        if row["state"] not in ("running", "paused"):
            raise HTTPException(status_code=400, detail=f"Cannot cancel execution in state: {row['state']}")

        # Update state
        now = datetime.utcnow().isoformat()
        cursor.execute("""
            UPDATE graph_executions
            SET state = 'canceled', updated_at = ?, completed_at = ?
            WHERE id = ?
        """, (now, now, execution_id))

        conn.commit()

    return {"status": "canceled", "execution_id": execution_id}


@router.get("/executions/{execution_id}/nodes")
async def list_node_executions(
    execution_id: str,
    agent_id: str = Depends(verify_token)
):
    """List node executions for an execution."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.row_factory = dict_factory

        # Verify ownership
        cursor.execute(
            "SELECT started_by FROM graph_executions WHERE id = ?",
            (execution_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Execution not found")
        if row["started_by"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Fetch node executions
        cursor.execute(
            "SELECT * FROM node_executions WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id,)
        )
        nodes = cursor.fetchall()

    return {"nodes": nodes, "count": len(nodes)}


@router.get("/executions/{execution_id}/approvals")
async def list_approvals(
    execution_id: str,
    agent_id: str = Depends(verify_token)
):
    """List human approvals for an execution."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.row_factory = dict_factory

        # Verify ownership
        cursor.execute(
            "SELECT started_by FROM graph_executions WHERE id = ?",
            (execution_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Execution not found")
        if row["started_by"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Fetch approvals
        cursor.execute(
            "SELECT * FROM graph_human_approvals WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id,)
        )
        approvals = cursor.fetchall()

    return {"approvals": approvals, "count": len(approvals)}


@router.get("/executions/{execution_id}/audit")
async def get_audit_log(
    execution_id: str,
    agent_id: str = Depends(verify_token)
):
    """Get audit log for a graph execution."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.row_factory = dict_factory

        # Verify access
        cursor.execute("SELECT started_by FROM graph_executions WHERE id = ?", (execution_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Execution not found")
        if row["started_by"] != agent_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        cursor.execute(
            "SELECT * FROM graph_audit_log WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id,)
        )
        return {"audit_log": cursor.fetchall()}
