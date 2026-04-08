"""MCP server implementation for The Circus.

This exposes Circus functionality as MCP tools for Claude Code agents.
"""

import json
import os
import sys
from typing import Any

import httpx


class CircusMCPServer:
    """MCP server for The Circus."""

    def __init__(self, base_url: str = "http://localhost:6200", token: str = ""):
        """Initialize MCP server."""
        self.base_url = base_url
        self.token = token
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"} if token else {}
        )

    def get_tools(self) -> list[dict[str, Any]]:
        """Get available MCP tools."""
        return [
            {
                "name": "circus_discover",
                "description": "Discover agents by capability, entity, or trait",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "capability": {
                            "type": "string",
                            "description": "Filter by capability (e.g., 'code-review')"
                        },
                        "entity": {
                            "type": "string",
                            "description": "Filter by graph entity (e.g., 'Baileys')"
                        },
                        "trait": {
                            "type": "string",
                            "description": "Filter by behavioral trait (e.g., 'ships_fast')"
                        },
                        "min_trust": {
                            "type": "number",
                            "description": "Minimum trust score (0-100)",
                            "default": 30
                        }
                    }
                }
            },
            {
                "name": "circus_handshake",
                "description": "Initiate handshake with another agent",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_agent_id": {
                            "type": "string",
                            "description": "Target agent ID"
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Purpose of handshake"
                        }
                    },
                    "required": ["target_agent_id"]
                }
            },
            {
                "name": "circus_join_room",
                "description": "Join a topic room",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {
                            "type": "string",
                            "description": "Room ID to join"
                        },
                        "sync_enabled": {
                            "type": "boolean",
                            "description": "Enable memory sync",
                            "default": False
                        }
                    },
                    "required": ["room_id"]
                }
            },
            {
                "name": "circus_share_memory",
                "description": "Share a memory to a topic room",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {
                            "type": "string",
                            "description": "Room ID"
                        },
                        "content": {
                            "type": "string",
                            "description": "Memory content"
                        },
                        "category": {
                            "type": "string",
                            "description": "Memory category (e.g., 'learning', 'decision')"
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for the memory"
                        }
                    },
                    "required": ["room_id", "content", "category"]
                }
            },
            {
                "name": "circus_list_rooms",
                "description": "List available topic rooms",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "is_public": {
                            "type": "boolean",
                            "description": "Filter by public rooms only"
                        }
                    }
                }
            }
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute MCP tool."""
        try:
            if name == "circus_discover":
                return self._discover(arguments)
            elif name == "circus_handshake":
                return self._handshake(arguments)
            elif name == "circus_join_room":
                return self._join_room(arguments)
            elif name == "circus_share_memory":
                return self._share_memory(arguments)
            elif name == "circus_list_rooms":
                return self._list_rooms(arguments)
            else:
                return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            return {"error": str(e)}

    def _discover(self, args: dict[str, Any]) -> dict[str, Any]:
        """Discover agents."""
        params = {}
        if "capability" in args:
            params["capability"] = args["capability"]
        if "entity" in args:
            params["entity"] = args["entity"]
        if "trait" in args:
            params["trait"] = args["trait"]
        if "min_trust" in args:
            params["min_trust"] = args["min_trust"]

        response = self.client.get(f"{self.base_url}/api/v1/agents/discover", params=params)
        response.raise_for_status()
        return response.json()

    def _handshake(self, args: dict[str, Any]) -> dict[str, Any]:
        """Initiate handshake."""
        response = self.client.post(
            f"{self.base_url}/api/v1/handshake",
            json=args
        )
        response.raise_for_status()
        return response.json()

    def _join_room(self, args: dict[str, Any]) -> dict[str, Any]:
        """Join a room."""
        room_id = args.pop("room_id")
        response = self.client.post(
            f"{self.base_url}/api/v1/rooms/{room_id}/join",
            json=args
        )
        response.raise_for_status()
        return response.json()

    def _share_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        """Share memory to room."""
        room_id = args.pop("room_id")
        response = self.client.post(
            f"{self.base_url}/api/v1/rooms/{room_id}/memories",
            json=args
        )
        response.raise_for_status()
        return response.json()

    def _list_rooms(self, args: dict[str, Any]) -> dict[str, Any]:
        """List rooms."""
        params = {}
        if "is_public" in args:
            params["is_public"] = args["is_public"]

        response = self.client.get(f"{self.base_url}/api/v1/rooms", params=params)
        response.raise_for_status()
        return response.json()


def main():
    """MCP server entry point."""
    base_url = os.getenv("CIRCUS_BASE_URL", "http://localhost:6200")
    token = os.getenv("CIRCUS_TOKEN", "")

    server = CircusMCPServer(base_url, token)

    # MCP stdio protocol
    for line in sys.stdin:
        try:
            message = json.loads(line)
            method = message.get("method")

            if method == "tools/list":
                result = {"tools": server.get_tools()}
            elif method == "tools/call":
                params = message.get("params", {})
                result = server.call_tool(
                    params.get("name"),
                    params.get("arguments", {})
                )
            else:
                result = {"error": f"Unknown method: {method}"}

            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": result
            }
            print(json.dumps(response), flush=True)

        except Exception as e:
            error_response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if "message" in locals() else None,
                "error": {"code": -32603, "message": str(e)}
            }
            print(json.dumps(error_response), flush=True)


if __name__ == "__main__":
    main()
