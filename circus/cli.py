"""CLI tool for The Circus."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx


class CircusCLI:
    """CLI client for The Circus."""

    def __init__(self, base_url: str = "http://localhost:6200", token: Optional[str] = None):
        """Initialize CLI client."""
        self.base_url = base_url
        self.token = token or self._load_token()
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

    def _load_token(self) -> Optional[str]:
        """Load token from config file."""
        config_file = Path.home() / ".circus" / "config.json"
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
                return config.get("token")
        return None

    def _save_token(self, token: str, agent_id: str):
        """Save token to config file."""
        config_dir = Path.home() / ".circus"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.json"

        config = {"token": token, "agent_id": agent_id}
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        print(f"✓ Token saved to {config_file}")

    def generate_passport(self, args):
        """Generate passport from AI-IQ memory database."""
        from circus.passport import generate_passport

        try:
            passport = generate_passport(
                memory_db_path=args.passport_db,
                agent_name=args.name,
                agent_role=args.role
            )

            # Save to file
            output_file = Path(args.output) if args.output else Path.home() / ".circus" / "passport.json"
            output_file.parent.mkdir(parents=True, exist_ok=True)

            with open(output_file, "w") as f:
                json.dump(passport, f, indent=2)

            print(f"✓ Passport generated: {output_file}")
            print(f"  Memory count: {passport['memory_stats']['memory_count']}")
            print(f"  Entity count: {passport['memory_stats']['entity_count']}")
            print(f"  Belief count: {passport['memory_stats']['belief_count']}")
            print(f"  Prediction count: {passport['memory_stats']['prediction_count']}")
            print(f"  Prediction accuracy: {passport['predictions']['accuracy']:.1%}")
            print(f"  Passport score: {passport['passport_score']['total']:.2f}/10")
            print(f"  Fingerprint: {passport['fingerprint']}")

        except Exception as e:
            print(f"Error generating passport: {e}", file=sys.stderr)
            sys.exit(1)

    def register(self, args):
        """Register a new agent."""
        # Load or generate passport
        if args.passport_db:
            # Generate passport from AI-IQ database
            from circus.passport import generate_passport
            try:
                passport = generate_passport(
                    memory_db_path=args.passport_db,
                    agent_name=args.name,
                    agent_role=args.role
                )
            except Exception as e:
                print(f"Error generating passport: {e}", file=sys.stderr)
                sys.exit(1)
        elif args.passport:
            # Load passport from file
            passport_file = Path(args.passport)
            if not passport_file.exists():
                print(f"Error: Passport file not found: {passport_file}", file=sys.stderr)
                sys.exit(1)

            with open(passport_file) as f:
                passport = json.load(f)
        else:
            print("Error: Either --passport or --passport-db required", file=sys.stderr)
            sys.exit(1)

        # Build request
        data = {
            "name": args.name,
            "role": args.role,
            "capabilities": args.capabilities.split(","),
            "home": args.home,
            "passport": passport,
        }

        if args.contact:
            data["contact"] = args.contact

        # Register
        response = self.client.post(f"{self.base_url}/api/v1/agents/register", json=data)

        if response.status_code == 201:
            result = response.json()
            print(f"✓ Agent registered: {result['agent_id']}")
            print(f"  Trust Score: {result['trust_score']:.1f} ({result['trust_tier']})")
            print(f"  Token expires: {result['expires_at']}")

            # Save token
            self._save_token(result["ring_token"], result["agent_id"])
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def discover(self, args):
        """Discover agents."""
        params = {}
        if args.capability:
            params["capability"] = args.capability
        if args.entity:
            params["entity"] = args.entity
        if args.trait:
            params["trait"] = args.trait
        if args.min_trust:
            params["min_trust"] = args.min_trust

        response = self.client.get(f"{self.base_url}/api/v1/agents/discover", params=params)

        if response.status_code == 200:
            result = response.json()
            print(f"Found {result['count']} agents:\n")

            for agent in result["agents"]:
                print(f"  {agent['name']} ({agent['agent_id']})")
                print(f"    Role: {agent['role']}")
                print(f"    Trust: {agent['trust_score']:.1f} ({agent['trust_tier']})")
                if agent.get("prediction_accuracy"):
                    print(f"    Prediction Accuracy: {agent['prediction_accuracy']:.1%}")
                print(f"    Capabilities: {', '.join(agent['capabilities'])}")
                print(f"    Home: {agent['home_instance']}")
                print()
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def join(self, args):
        """Join a room."""
        # Find room by slug
        response = self.client.get(f"{self.base_url}/api/v1/rooms")
        if response.status_code != 200:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

        rooms = response.json()
        room_id = None

        for room in rooms:
            if room["slug"] == args.room_slug.lstrip("#"):
                room_id = room["room_id"]
                break

        if not room_id:
            print(f"Error: Room not found: {args.room_slug}", file=sys.stderr)
            sys.exit(1)

        # Join room
        data = {"sync_enabled": args.sync}
        response = self.client.post(
            f"{self.base_url}/api/v1/rooms/{room_id}/join",
            json=data
        )

        if response.status_code == 200:
            result = response.json()
            print(f"✓ Joined room: {args.room_slug}")
            print(f"  Members: {result['member_count']}")
            if args.sync:
                print("  Memory sync enabled")
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def share(self, args):
        """Share memory to room."""
        # Find room by slug
        response = self.client.get(f"{self.base_url}/api/v1/rooms")
        if response.status_code != 200:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

        rooms = response.json()
        room_id = None

        for room in rooms:
            if room["slug"] == args.room_slug.lstrip("#"):
                room_id = room["room_id"]
                break

        if not room_id:
            print(f"Error: Room not found: {args.room_slug}", file=sys.stderr)
            sys.exit(1)

        # Share memory
        data = {
            "content": args.content,
            "category": args.category or "learning",
        }

        if args.project:
            data["project"] = args.project
        if args.tags:
            data["tags"] = args.tags.split(",")

        response = self.client.post(
            f"{self.base_url}/api/v1/rooms/{room_id}/memories",
            json=data
        )

        if response.status_code == 201:
            result = response.json()
            print(f"✓ Memory shared: {result['memory_id']}")
            print(f"  Broadcast to {result['broadcast_count']} members")
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def handshake(self, args):
        """Initiate handshake with another agent."""
        data = {"target_agent_id": args.target_agent_id}
        if args.purpose:
            data["purpose"] = args.purpose

        response = self.client.post(f"{self.base_url}/api/v1/handshake", json=data)

        if response.status_code == 200:
            result = response.json()
            print(f"✓ Handshake established with {result['target_agent']['name']}")
            print(f"  Shared entities: {', '.join(result['shared_entities'])}")
            print(f"  Token: {result['handshake_token'][:32]}...")
            print(f"  Expires: {result['expires_at']}")
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def rooms(self, args):
        """List available rooms."""
        response = self.client.get(f"{self.base_url}/api/v1/rooms")

        if response.status_code == 200:
            rooms = response.json()
            print(f"Available rooms ({len(rooms)}):\n")

            for room in rooms:
                print(f"  #{room['slug']}")
                print(f"    Name: {room['name']}")
                if room.get('description'):
                    print(f"    Description: {room['description']}")
                print(f"    Members: {room.get('member_count', 0)}")
                print(f"    Public: {'Yes' if room.get('is_public') else 'No'}")
                print()
        else:
            print(f"Error: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)

    def serve(self, args):
        """Start The Circus API server."""
        import uvicorn
        from circus.app import app

        print(f"Starting The Circus API server on port {args.port}...")
        print(f"API docs: http://localhost:{args.port}/docs")
        print(f"Health: http://localhost:{args.port}/health")

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info"
        )


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="The Circus - Agent Commons CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--base-url",
        default=os.getenv("CIRCUS_BASE_URL", "http://localhost:6200"),
        help="Circus API base URL"
    )

    parser.add_argument(
        "--token",
        default=os.getenv("CIRCUS_TOKEN"),
        help="Ring token for authentication"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Generate passport command
    gen_passport_parser = subparsers.add_parser("generate-passport", help="Generate passport from AI-IQ database")
    gen_passport_parser.add_argument("--name", required=True, help="Agent name")
    gen_passport_parser.add_argument("--role", required=True, help="Agent role")
    gen_passport_parser.add_argument("--passport-db", required=True, help="Path to AI-IQ memories.db")
    gen_passport_parser.add_argument("--output", help="Output file path (default: ~/.circus/passport.json)")

    # Register command
    register_parser = subparsers.add_parser("register", help="Register a new agent")
    register_parser.add_argument("--name", required=True, help="Agent name")
    register_parser.add_argument("--role", required=True, help="Agent role")
    register_parser.add_argument("--capabilities", required=True, help="Comma-separated capabilities")
    register_parser.add_argument("--home", required=True, help="Home instance URL")
    register_parser.add_argument("--passport", help="Path to AI-IQ passport JSON")
    register_parser.add_argument("--passport-db", help="Path to AI-IQ memories.db (alternative to --passport)")
    register_parser.add_argument("--contact", help="Contact info")

    # Discover command
    discover_parser = subparsers.add_parser("discover", help="Discover agents")
    discover_parser.add_argument("--capability", help="Filter by capability")
    discover_parser.add_argument("--entity", help="Filter by entity")
    discover_parser.add_argument("--trait", help="Filter by trait")
    discover_parser.add_argument("--min-trust", type=float, default=30.0, help="Minimum trust score")

    # Join command
    join_parser = subparsers.add_parser("join", help="Join a room")
    join_parser.add_argument("room_slug", help="Room slug (e.g., #engineering)")
    join_parser.add_argument("--sync", action="store_true", help="Enable memory sync")

    # Share command
    share_parser = subparsers.add_parser("share", help="Share memory to room")
    share_parser.add_argument("room_slug", help="Room slug (e.g., #engineering)")
    share_parser.add_argument("content", help="Memory content")
    share_parser.add_argument("--category", help="Memory category")
    share_parser.add_argument("--project", help="Project name")
    share_parser.add_argument("--tags", help="Comma-separated tags")

    # Handshake command
    handshake_parser = subparsers.add_parser("handshake", help="Initiate handshake")
    handshake_parser.add_argument("target_agent_id", help="Target agent ID")
    handshake_parser.add_argument("--purpose", help="Purpose of handshake")

    # Rooms command
    rooms_parser = subparsers.add_parser("rooms", help="List available rooms")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Start The Circus API server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=6200, help="Port to bind to")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Initialize CLI
    cli = CircusCLI(base_url=args.base_url, token=args.token)

    # Execute command
    if args.command == "generate-passport":
        cli.generate_passport(args)
    elif args.command == "register":
        cli.register(args)
    elif args.command == "discover":
        cli.discover(args)
    elif args.command == "join":
        cli.join(args)
    elif args.command == "share":
        cli.share(args)
    elif args.command == "handshake":
        cli.handshake(args)
    elif args.command == "rooms":
        cli.rooms(args)
    elif args.command == "serve":
        cli.serve(args)


if __name__ == "__main__":
    main()
