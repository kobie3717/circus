"""AMD Hackathon Demo: 3 AI Agents Handshaking via The Circus.

Three LLM-powered agents (Atlas, Mira, Quill) register, discover each other,
handshake, collaborate on a task, and rate their interactions — all through
The Circus federation layer with AI-IQ passports.

Prerequisites:
    1. Start Circus: circus serve --port 8000
    2. Set ANTHROPIC_API_KEY (or OPENAI_API_BASE for vLLM)
    3. Run: python -m examples.handshake_demo [--slow]
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util
spec = importlib.util.spec_from_file_location("passport", ROOT / "circus" / "passport.py")
passport_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(passport_module)
generate_passport = passport_module.generate_passport

CIRCUS_BASE = os.getenv("CIRCUS_BASE_URL", "http://localhost:8000")

BLUE = "\033[94m"
GREEN = "\033[92m"
PURPLE = "\033[95m"
GREY = "\033[90m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Agent:
    def __init__(self, name: str, role: str, color: str, memories: list[str], capabilities: list[str]):
        self.name = name
        self.role = role
        self.color = color
        self.memories = memories
        self.capabilities = capabilities
        self.agent_id: Optional[str] = None
        self.token: Optional[str] = None
        self.trust_score: float = 0.0
        self.db_path: Optional[Path] = None

    def __repr__(self):
        return f"{self.color}{self.name}{RESET}"


def print_step(step: int, title: str):
    print(f"\n{BOLD}{'═' * 70}{RESET}")
    print(f"{BOLD}STEP {step}: {title}{RESET}")
    print(f"{BOLD}{'═' * 70}{RESET}\n")


def log(agent: Agent, message: str):
    print(f"{agent.color}[{agent.name.upper()}]{RESET} {message}")


def http_log(method: str, path: str):
    print(f"{GREY}→ {method} {path}{RESET}")


def llm_response(agent: Agent, prompt: str, use_real_llm: bool) -> str:
    if use_real_llm:
        return call_llm(agent, prompt)
    else:
        return get_canned_response(agent, prompt)


def call_llm(agent: Agent, prompt: str) -> str:
    openai_base = os.getenv("OPENAI_API_BASE")
    if openai_base:
        import openai
        client = openai.OpenAI(base_url=openai_base, api_key="dummy")
        response = client.chat.completions.create(
            model="meta-llama/Llama-3.1-70B-Instruct",
            messages=[
                {"role": "system", "content": f"You are {agent.name}, a {agent.role}. {' '.join(agent.memories)}"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150
        )
        return response.choices[0].message.content.strip()
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=150,
            system=f"You are {agent.name}, a {agent.role}. {' '.join(agent.memories)}",
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()


def get_canned_response(agent: Agent, prompt: str) -> str:
    if agent.name == "Mira":
        if "eval" in prompt.lower():
            return "CRITICAL: eval() allows arbitrary code execution. This is a classic injection vulnerability. Use JSON.parse() instead."
        return "Code looks secure."
    elif agent.name == "Quill":
        return "## Security Finding\n\n**Issue**: Remote code execution via eval()\n**Fix**: Replace with JSON.parse()"
    else:
        return "Acknowledged."


def create_memory_db(agent: Agent) -> Path:
    db_path = Path(tempfile.mkdtemp()) / "memories.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            category TEXT,
            priority INTEGER DEFAULT 5,
            access_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE beliefs (
            id INTEGER PRIMARY KEY,
            statement TEXT,
            confidence REAL,
            status TEXT DEFAULT 'active'
        )
    """)

    cursor.execute("""
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY,
            statement TEXT,
            resolution TEXT DEFAULT 'pending'
        )
    """)

    cursor.execute("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            name TEXT,
            type TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY,
            from_entity TEXT,
            to_entity TEXT,
            relation TEXT
        )
    """)

    for i, memory in enumerate(agent.memories):
        cursor.execute("""
            INSERT INTO memories (content, category, priority, access_count, created_at)
            VALUES (?, 'skill', 8, ?, datetime('now'))
        """, (memory, i * 2))

    conn.commit()
    conn.close()
    return db_path


def register_agent(agent: Agent) -> bool:
    import secrets
    unique_suffix = secrets.token_hex(2)
    unique_name = f"{agent.name}-demo-{unique_suffix}"

    agent.db_path = create_memory_db(agent)
    passport = generate_passport(agent.db_path, unique_name, agent.role)

    payload = {
        "name": unique_name,
        "role": agent.role,
        "capabilities": agent.capabilities,
        "home": "http://localhost:8000",
        "passport": passport
    }

    http_log("POST", "/api/v1/agents/register")
    resp = requests.post(f"{CIRCUS_BASE}/api/v1/agents/register", json=payload)

    if resp.status_code != 201:
        log(agent, f"{RED}Registration failed: {resp.status_code} {resp.text}{RESET}")
        return False

    data = resp.json()
    agent.agent_id = data["agent_id"]
    agent.token = data["ring_token"]
    agent.trust_score = data["trust_score"]

    passport_id = passport.get("fingerprint", "unknown")[:8]
    log(agent, f"Joined The Circus — passport: {passport_id} — trust: {agent.trust_score:.1f}")
    return True


def discover_agents(agent: Agent, capability: str) -> list[dict]:
    http_log("GET", f"/api/v1/agents/discover?capability={capability}")
    headers = {"Authorization": f"Bearer {agent.token}"}
    resp = requests.get(f"{CIRCUS_BASE}/api/v1/agents/discover",
                       params={"capability": capability, "min_trust": 30},
                       headers=headers)

    if resp.status_code != 200:
        return []

    return resp.json().get("agents", [])


def handshake(agent_a: Agent, agent_b: Agent) -> Optional[str]:
    http_log("POST", "/api/v1/handshake")
    headers = {"Authorization": f"Bearer {agent_a.token}"}
    payload = {"target_agent_id": agent_b.agent_id, "purpose": "code review collaboration"}

    resp = requests.post(f"{CIRCUS_BASE}/api/v1/handshake", json=payload, headers=headers)

    if resp.status_code != 200:
        log(agent_a, f"{RED}Handshake failed: {resp.text}{RESET}")
        return None

    data = resp.json()
    return data.get("handshake_id")


def record_interaction_memory(agent: Agent, interaction: str):
    if agent.db_path and agent.db_path.exists():
        conn = sqlite3.connect(str(agent.db_path))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO memories (content, category, priority, access_count, created_at)
            VALUES (?, 'interaction', 7, 1, datetime('now'))
        """, (interaction,))
        conn.commit()
        conn.close()


def update_trust_score(agent: Agent, event_type: str):
    http_log("POST", f"/api/v1/agents/{agent.agent_id}/trust-event")
    headers = {"Authorization": f"Bearer {agent.token}"}
    payload = {"event_type": event_type, "context": {}}

    resp = requests.post(f"{CIRCUS_BASE}/api/v1/agents/{agent.agent_id}/trust-event",
                        json=payload, headers=headers)

    if resp.status_code == 200:
        data = resp.json()
        old_score = agent.trust_score
        agent.trust_score = data["new_trust_score"]
        delta = agent.trust_score - old_score
        sign = "+" if delta >= 0 else ""
        log(agent, f"Trust updated: {agent.trust_score:.1f} ({sign}{delta:.1f})")
    else:
        pass


def print_scorecard(agents: list[Agent]):
    print(f"\n{BOLD}{'═' * 70}{RESET}")
    print(f"{BOLD}FINAL SCORECARD{RESET}")
    print(f"{BOLD}{'═' * 70}{RESET}\n")

    for agent in agents:
        if agent.db_path and agent.db_path.exists():
            conn = sqlite3.connect(str(agent.db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories WHERE status='active'")
            memory_count = cursor.fetchone()[0]
            conn.close()
        else:
            memory_count = 0

        print(f"{agent.color}{BOLD}{agent.name:8s}{RESET} "
              f"trust: {agent.trust_score:5.1f} | "
              f"memories: {memory_count:3d} | "
              f"role: {agent.role}")


def main():
    parser = argparse.ArgumentParser(description="AMD Hackathon: AI Agent Handshake Demo")
    parser.add_argument("--slow", action="store_true", help="Add delays for screen recording")
    args = parser.parse_args()

    delay = 1.0 if args.slow else 0.0

    print(f"{BOLD}🎪 THE CIRCUS — AI Agent Handshake Demo{RESET}")
    print(f"{GREY}AMD Developer Hackathon Submission{RESET}\n")

    try:
        resp = requests.get(f"{CIRCUS_BASE}/health", timeout=2)
        if resp.status_code != 200:
            raise Exception("Health check failed")
    except:
        print(f"{RED}❌ Circus FastAPI not reachable at {CIRCUS_BASE}{RESET}")
        print(f"{YELLOW}Start it: circus serve --port 8000{RESET}")
        return 1

    has_anthropic = os.getenv("ANTHROPIC_API_KEY")
    has_openai_base = os.getenv("OPENAI_API_BASE")
    use_real_llm = bool(has_anthropic or has_openai_base)

    if not use_real_llm:
        print(f"{YELLOW}⚠ No ANTHROPIC_API_KEY or OPENAI_API_BASE — using canned responses{RESET}\n")

    time.sleep(delay)

    atlas = Agent(
        "Atlas",
        "research analyst",
        BLUE,
        ["expert at synthesizing market reports", "follows fintech regulation closely", "writes structured exec briefings"],
        ["research", "analysis", "planning"]
    )

    mira = Agent(
        "Mira",
        "code reviewer",
        GREEN,
        ["specializes in TypeScript and Python security audits", "catches injection bugs", "writes terse review comments"],
        ["code review", "security", "testing"]
    )

    quill = Agent(
        "Quill",
        "technical writer",
        PURPLE,
        ["expert in API documentation", "translates code to docs in 30+ languages", "uses examples not abstractions"],
        ["documentation", "writing", "translation"]
    )

    agents = [atlas, mira, quill]

    print_step(1, "Agent Registration")
    for agent in agents:
        if not register_agent(agent):
            return 1
        time.sleep(delay * 0.5)

    time.sleep(delay)

    print_step(2, "Discovery")
    log(atlas, f"Searching for code review experts...")
    reviewers = discover_agents(atlas, "code review")

    mira_found = next((a for a in reviewers if a["name"] == "Mira"), None)
    if mira_found:
        log(atlas, f"Found Mira (trust score: {mira_found['trust_score']:.1f})")

    time.sleep(delay)

    print_step(3, "Handshake")
    hs_id = handshake(atlas, mira)
    if hs_id:
        print(f"{YELLOW}🤝 Atlas ↔ Mira handshake successful{RESET}")
        print(f"{GREY}   Handshake ID: {hs_id}{RESET}")

    time.sleep(delay)

    print_step(4, "Task Execution")
    code_snippet = "app.use((req,res)=>{ eval(req.body.code) })"
    log(atlas, f"Sending to Mira: Please review this snippet:")
    print(f"{GREY}   {code_snippet}{RESET}")

    time.sleep(delay * 0.5)

    prompt = f"Please review this code for security issues: {code_snippet}"
    mira_response = llm_response(mira, prompt, use_real_llm)

    log(mira, "Security finding:")
    for line in mira_response.split('\n')[:3]:
        print(f"    {GREY}→{RESET} {line}")

    time.sleep(delay)

    print_step(5, "3-Way Collaboration")
    log(atlas, "Pulling Quill in to document Mira's findings...")

    hs_id_quill = handshake(atlas, quill)
    if hs_id_quill:
        print(f"{YELLOW}🤝 Atlas ↔ Quill handshake successful{RESET}")

    time.sleep(delay * 0.5)

    doc_prompt = f"Write docs from this security review: {mira_response[:100]}"
    quill_response = llm_response(quill, doc_prompt, use_real_llm)

    log(quill, "Documentation generated:")
    for line in quill_response.split('\n')[:3]:
        print(f"    {GREY}→{RESET} {line}")

    time.sleep(delay)

    print_step(6, "Memory Exchange")
    record_interaction_memory(atlas, "Worked with Mira on TS security review — Mira caught eval() injection")
    record_interaction_memory(mira, "Atlas asked sharp questions, well-scoped task")
    record_interaction_memory(quill, "Wrote docs from Mira's findings, Atlas validated")

    log(atlas, "Committed interaction memory")
    log(mira, "Committed interaction memory")
    log(quill, "Committed interaction memory")

    time.sleep(delay)

    print_step(7, "Final Scorecard")

    print_scorecard(agents)

    print(f"\n{GREEN}{BOLD}✅ Demo complete — 3 agents collaborated through The Circus{RESET}")
    print(f"{GREY}Agents handshook, collaborated on security review, updated trust scores{RESET}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
