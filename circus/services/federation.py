"""Federation service for cross-Circus discovery."""

import httpx
import asyncio
from typing import List, Dict, Any, Optional


async def federated_discover(query: str, peers: List[Dict], timeout: float = 5.0) -> List[Dict]:
    """Search across federated Circus peers."""
    results = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = []
        for peer in peers:
            url = f"{peer['url'].rstrip('/')}/api/v1/agents/discover?q={query}"
            tasks.append(_query_peer(client, peer['name'], url))

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        seen_names = set()
        for resp in responses:
            if isinstance(resp, Exception):
                continue
            for agent in resp:
                name = agent.get("name", "")
                if name not in seen_names:
                    seen_names.add(name)
                    results.append(agent)

    return results


async def _query_peer(client: httpx.AsyncClient, peer_name: str, url: str) -> List[Dict]:
    """Query a single peer, return empty list on failure."""
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            # Tag results with source peer
            agents = data if isinstance(data, list) else data.get("agents", [])
            for a in agents:
                a["_source_peer"] = peer_name
            return agents
    except Exception:
        pass
    return []
