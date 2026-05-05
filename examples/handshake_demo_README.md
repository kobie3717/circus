# Handshake Demo — AMD Developer Hackathon Submission

**3 AI agents handshaking + collaborating through The Circus federation layer.**

This demo showcases:
- AI-IQ passport-based identity (memory-derived trust scores)
- Agent discovery via capability search
- P2P handshake protocol with trust verification
- LLM-powered task execution (security code review)
- Cross-agent memory exchange and trust scoring

## Prerequisites

### 1. Start The Circus FastAPI Server

```bash
# In one terminal
circus serve --port 8000
```

The demo expects Circus at `http://localhost:8000` by default. Override with:
```bash
export CIRCUS_BASE_URL=http://localhost:6200
```

### 2. LLM Backend (Optional)

**Option A: Anthropic Claude** (recommended for demo quality)
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Option B: AMD ROCm + vLLM (Llama 3.1 70B)**
```bash
export OPENAI_API_BASE=http://your-amd-cloud-instance:8000/v1
```

**Option C: No LLM (canned responses)**
If neither env var is set, the demo uses pre-written responses and still runs end-to-end.

### 3. Python Dependencies

```bash
pip install anthropic  # if using Anthropic
# OR
pip install openai     # if using vLLM/OPENAI_API_BASE
```

All other deps (requests, etc.) are already in `circus` requirements.

## Run the Demo

### Normal Speed (default)

```bash
python -m examples.handshake_demo
```

### Slow Mode (for screen recording)

```bash
python -m examples.handshake_demo --slow
```

Adds 1-second pauses between steps and 50ms typing effect for watchable video.

## Expected Output

```
🎪 THE CIRCUS — AI Agent Handshake Demo
AMD Developer Hackathon Submission

══════════════════════════════════════════════════════════════════════
STEP 1: Agent Registration
══════════════════════════════════════════════════════════════════════

→ POST /api/v1/agents/register
[ATLAS] Joined The Circus — passport: a3f7b2c1 — trust: 52.3
→ POST /api/v1/agents/register
[MIRA] Joined The Circus — passport: d8e9f1a2 — trust: 51.8
→ POST /api/v1/agents/register
[QUILL] Joined The Circus — passport: 9c8d7e6f — trust: 50.9

══════════════════════════════════════════════════════════════════════
STEP 2: Discovery
══════════════════════════════════════════════════════════════════════

[ATLAS] Searching for code review experts...
→ GET /api/v1/agents/discover?capability=code review
[ATLAS] Found Mira (trust score: 51.8)

══════════════════════════════════════════════════════════════════════
STEP 3: Handshake
══════════════════════════════════════════════════════════════════════

→ POST /api/v1/handshake
🤝 Atlas ↔ Mira handshake successful
   Handshake ID: hs-a7f3d2e1b9c4

══════════════════════════════════════════════════════════════════════
STEP 4: Task Execution
══════════════════════════════════════════════════════════════════════

[ATLAS] Sending to Mira: Please review this snippet:
   app.use((req,res)=>{ eval(req.body.code) })
[MIRA] Security finding:
    → CRITICAL: eval() allows arbitrary code execution. This is a classic injection vulnerability.
    → Use JSON.parse() instead.

══════════════════════════════════════════════════════════════════════
STEP 5: 3-Way Collaboration
══════════════════════════════════════════════════════════════════════

[ATLAS] Pulling Quill in to document Mira's findings...
→ POST /api/v1/handshake
🤝 Atlas ↔ Quill handshake successful
[QUILL] Documentation generated:
    → ## Security Finding
    → **Issue**: Remote code execution via eval()
    → **Fix**: Replace with JSON.parse()

══════════════════════════════════════════════════════════════════════
STEP 6: Memory Exchange
══════════════════════════════════════════════════════════════════════

[ATLAS] Committed interaction memory
[MIRA] Committed interaction memory
[QUILL] Committed interaction memory

══════════════════════════════════════════════════════════════════════
STEP 7: Final Scorecard
══════════════════════════════════════════════════════════════════════


══════════════════════════════════════════════════════════════════════
FINAL SCORECARD
══════════════════════════════════════════════════════════════════════

Atlas    trust:  44.4 | memories:   4 | role: research analyst
Mira     trust:  44.4 | memories:   4 | role: code reviewer
Quill    trust:  44.4 | memories:   4 | role: technical writer

✅ Demo complete — 3 agents collaborated through The Circus
Agents handshook, collaborated on security review, updated trust scores
```

## Recording Tips

For a 5-minute screen recording:

1. **Terminal setup**: 100 columns width, dark theme (e.g., Solarized Dark)
2. **Run with --slow**: `python -m examples.handshake_demo --slow`
3. **Recording tool**: `asciinema` for terminal, or OBS for full screen
4. **Framerate**: 30fps
5. **Post-edit**: Add title card + background music (optional)

### Asciinema

```bash
asciinema rec handshake_demo.cast
python -m examples.handshake_demo --slow
# Ctrl-D to stop
asciinema play handshake_demo.cast
```

Convert to GIF:
```bash
agg handshake_demo.cast handshake_demo.gif
```

## Architecture Highlights

- **AI-IQ Passports**: Each agent's trust score is derived from their memory database (skill memories, belief stability, prediction accuracy)
- **P2P Handshake**: Trust verification happens before collaboration (min trust 30)
- **LLM Integration**: Real LLM calls (Claude/Llama) for task execution, with fallback canned responses
- **Memory Commons**: Agents commit interaction outcomes back to their AI-IQ stores
- **Trust Dynamics**: Successful tasks increase trust scores (enabling higher-trust operations later)

## What This Demonstrates

1. **Identity without centralized auth**: Each agent's passport is cryptographically derived from their knowledge graph
2. **Trust-based routing**: Atlas only finds Mira after she passes the trust threshold
3. **Emergent collaboration**: 3-way handshake (Atlas → Mira → Quill) without pre-configured relationships
4. **Memory portability**: Agents carry their interaction history across sessions
5. **LLM flexibility**: Same demo works with Anthropic Claude, AMD ROCm vLLM, or no LLM

## Next Steps

- **Scale to 10+ agents**: Add domain stewardship, conflict resolution
- **Federation**: Connect multiple Circus instances (peer-to-peer memory sharing)
- **MCP integration**: Expose agents via Model Context Protocol
- **Observability**: OpenTelemetry traces for all handshakes + tasks

---

**AMD Developer Hackathon 2026** | Built on The Circus v1.9.0
