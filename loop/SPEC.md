# Circus Loop Specification

This document defines the Circus Loop state machine: a planner/coder/evaluator/distiller PR-factory orchestrator.

## Roles

### Planner
**Input**: Task description (from external trigger or previous feedback)  
**Output**: `plan.md` — a structured plan describing what to build/fix, acceptance criteria, and the eval strategy  
**Reads**: Task description, optional feedback from previous iteration  
**Writes**: `plan.md`

### Coder
**Input**: `plan.md`  
**Output**: `diff` — a unified diff or patch representing code changes  
**Reads**: ONLY `plan.md` (role isolation enforced by G6)  
**Writes**: `diff` file in unified diff format

### Evaluator
**Input**: `plan.md`, `diff`  
**Output**: `verdict.json` — structured pass/fail results for acceptance checks  
**Reads**: ONLY `plan.md` and `diff` (role isolation enforced by G6)  
**Writes**: `verdict.json`

### Distiller
**Input**: `plan.md`, `diff`, `verdict.json`  
**Output**: `feedback.md` — synthesized lessons and recommended next steps  
**Reads**: `plan.md`, `diff`, `verdict.json`  
**Writes**: `feedback.md`

## Artifact Contracts

### plan.md
Free-form markdown. Must include:
- Goal/objective
- Acceptance criteria (explicit list of checks)
- Eval strategy (how to verify success)

### diff
Unified diff format (standard `diff -u` output) or git patch format. Represents code changes to be applied.

### verdict.json
Schema:
```json
{
  "rows": [
    {
      "name": "check-name-1",
      "pass": true,
      "evidence": "command output or justification"
    },
    {
      "name": "check-name-2",
      "pass": false,
      "evidence": "error message or failed output"
    }
  ]
}
```

**Constraints**:
- `rows` is required and must be an array
- Each row must have `name` (string), `pass` (boolean), `evidence` (string)
- Row names must exactly match the set specified in `plan.md` (G1 matrix-shape guard)

### feedback.md
Free-form markdown. Synthesizes verdict results, extracts lessons, recommends iteration or completion.

## State Machine

### States
1. **IDLE** — no active loop, awaiting task
2. **PLANNING** — planner role active
3. **CODING** — coder role active
4. **EVALUATING** — evaluator role active
5. **DISTILLING** — distiller role active
6. **STOPPED** — terminal state, requires resume token to continue

### Transitions

```
IDLE → PLANNING (on task receipt)
PLANNING → CODING (plan.md complete)
CODING → EVALUATING (diff complete)
EVALUATING → STOPPED (if verdict signals STOP)
EVALUATING → DISTILLING (if all checks pass)
EVALUATING → STOPPED (if checks fail and max iterations reached)
DISTILLING → IDLE (feedback.md complete, ready for next task)
STOPPED → (no transition without resume token)
```

### STOP Signal
Any role can signal STOP by returning an object with `{ signal: 'STOP', reason: 'explanation' }` instead of its normal artifact.

When STOP is signaled:
1. Orchestrator exits with nonzero process exit code (exit code 42)
2. A resume token (SHA-256 hash of current state) is written to `.loop/resume-token`
3. No further state transitions occur
4. Next invocation must supply matching resume token via `--resume <token>` to continue

## Guards

These guards run in the orchestrator code, enforcing contracts that tests, roles, and integrators depend on.

### G1: matrix-shape
**What**: The evaluator's verdict.json `rows` array must exactly match the expected check names from plan.md.

**Enforcement**:
- Plan.md must declare expected checks (format: `## Checks` section with bullet list, one check per line)
- Verdict.json `rows[].name` set must match plan's check names exactly (same count, same names, order-independent)
- Any rename, merge, split, addition, or omission = invalid verdict
- Orchestrator aborts with error if mismatch detected

**Rationale**: Prevents evaluator from silently dropping hard checks or renaming them to dodge failures.

### G2: stop-is-terminal
**What**: When any role signals STOP, the orchestrator must halt immediately and require a resume token to continue.

**Enforcement**:
- STOP signal shape: `{ signal: 'STOP', reason: string }`
- On STOP: orchestrator exits with code 42, writes resume token to `.loop/resume-token`, proceeds no further
- Resume token = SHA-256 hash of: role name + reason + timestamp
- Next run must supply `--resume <token>` matching the token in `.loop/resume-token` to continue

**Rationale**: Prevents automatic retry loops when a role explicitly requests human review or intervention.

### G3: artifact-scrub
**What**: All artifacts pass through `scrubEgress` (from `loop/lib/scrub.mjs`) before being written or passed to next role.

**Enforcement**:
- Every artifact (plan.md, diff, verdict.json, feedback.md) is scrubbed via `scrubEgress(text)`
- If `scrubEgress` returns `hits > 0`, orchestrator aborts the run immediately
- Report artifact name/path in abort message
- NEVER log or print the matched secret value anywhere (not in logs, errors, test output)

**Rationale**: Prevents credentials, tokens, or PEM keys from leaking into loop artifacts or PR bodies.

### G4: harness-integrity
**What**: Acceptance check commands specified in plan.md must be executed verbatim — no substitution, narrowing, or modification to dodge failures.

**Enforcement**:
- Plan.md declares commands in `## Checks` section (format: `- [check-name]: command`)
- Evaluator role must execute commands exactly as written
- Orchestrator validates that verdict evidence matches command output (not a different/narrower command)
- In BUILD 1 (stub mode), this is tested via fixture: a plan specifies `npm test`, evaluator tries to substitute `npm test -- --grep easy`, orchestrator rejects it

**Rationale**: Prevents gaming the eval by running a subset of tests or a looser command.

### Harness Independence Principle

**The evaluator's harness/acceptance-check infrastructure must be verified independently of the evaluator's own verdict.** Twice in this project's history the defect was in the thing measuring (a same-host clone that couldn't detect non-portability; a diff-scanner that only checked added lines and missed a secret in deleted lines) rather than in the thing being measured (the scrubEgress detector itself was fine both times). A harness cannot certify its own soundness—something outside the loop must.

### G5: stub-detection
**What**: Flag if a file created within the same run flips a previously-failing check from fail to pass.

**Enforcement**:
- Track check results before and after coder role's file writes
- If a check was FAIL before coder ran, and PASS after, but the diff includes a new file (not just edits to existing files), flag as potential stub capitulation
- Report: "Check X flipped FAIL→PASS via new file Y — possible stub"
- Orchestrator continues but logs the flag in verdict output

**Rationale**: Detects the pattern "create an empty file to make `test -f foo.txt` pass" instead of implementing real functionality.

### G6: role-isolation
**What**: Each role receives ONLY the artifacts it's allowed to read, enforced by the adapter call signature.

**Enforcement**:
- **Coder** receives: `plan.md` only
- **Evaluator** receives: `plan.md`, `diff` only
- **Distiller** receives: `plan.md`, `diff`, `verdict.json` only
- **Planner** receives: task description, optional `feedback.md` from prior iteration
- Adapter interface throws error if a role attempts to access an artifact outside its allowed set
- Orchestrator does not pass disallowed artifacts to role adapters (they are not in scope)

**Rationale**: Prevents coder from reading prior verdicts and adjusting code to game specific checks; prevents evaluator from seeing distiller output that might bias its verdict.

## Adapter Interface

The orchestrator interacts with roles via a pluggable adapter. BUILD 1 ships a **stub adapter** that returns canned artifacts from `loop/fixtures/`.

### Adapter Shape
```javascript
const adapter = {
  async plan({ task, feedback = null }) {
    // returns: { artifact: "plan.md content" } or { signal: 'STOP', reason: '...' }
  },
  
  async code({ plan }) {
    // returns: { artifact: "diff content" } or { signal: 'STOP', reason: '...' }
  },
  
  async evaluate({ plan, diff }) {
    // returns: { artifact: verdictJson } or { signal: 'STOP', reason: '...' }
  },
  
  async distill({ plan, diff, verdict }) {
    // returns: { artifact: "feedback.md content" } or { signal: 'STOP', reason: '...' }
  }
};
```

### Stub Adapter (BUILD 1)
The stub adapter reads from `loop/fixtures/`:
- `plan()` → reads `loop/fixtures/plan.md`
- `code()` → reads `loop/fixtures/diff`
- `evaluate()` → reads `loop/fixtures/verdict.json`
- `distill()` → reads `loop/fixtures/feedback.md`

For guard tests, specialized fixtures (e.g., `verdict-bad-matrix.json`, `plan-stop-signal.json`) are used to trigger violations.

## Testing Requirements

Each guard (G1-G6) requires exactly two tests:
1. **Negative test**: Fixture/scenario that violates the guard, proving orchestrator rejects it
2. **Positive test**: Fixture/scenario that satisfies the guard, proving clean run passes it

No guard is considered implemented without both tests.

Test framework: Node's built-in `node --test` runner. No external test libraries.
