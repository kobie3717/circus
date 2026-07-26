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

## APPLYING Stage

After DISTILLING completes, the orchestrator commits changes to the worktree, pushes the branch, and creates a PR. This is the final stage where the loop's output becomes a real PR rather than just in-memory artifacts.

### Footgun Guard

`repoRoot` (the git repo all worktree/commit/push/PR operations run against) is a **required** constructor option — there is no default to `process.cwd()`. A bare `node loop/orchestrator.mjs` invocation used to default `repoRoot` to whatever directory it was run from; from the repo root, that created a real worktree and branch in `/root/circus` itself, with nothing to stop it. Two refusals close this:
- Missing `repoRoot` → constructor throws immediately.
- `repoRoot` pointing at a directory containing `loop/orchestrator.mjs` (i.e. looks like this codebase) → constructor throws unless `options.allowSelf: true` is explicitly passed.

Both are enforced at construction — before any git command runs — and both are mutation-tested (`footgun-reporoot-required`, `footgun-self-repo-refused`).

### Worktree Flow

**Worktree creation happens BEFORE coding, not after.** The sequence:

1. **PLANNING** produces `plan.md`
2. **Worktree creation** (`orchestrator.mjs:createWorktree`) creates a git worktree at `.loop/worktrees/<task-id>` branched from the base branch (usually `main` or `master`, resolved via `origin/HEAD` or passed explicitly as `options.baseBranch`)
3. **CODING** receives `worktreePath` as an artifact and works inside it. The coder adapter writes files directly into the worktree. The adapter's return value (`artifact` field) is **ignored** — the orchestrator never trusts a diff string from the coder.
4. **Diff extraction** (`orchestrator.mjs:extractDiffFromWorktree`) runs `git add -A` and `git diff --cached` in the worktree to derive the real diff. This is the **only** way a diff enters `artifacts.diff`.
5. **EVALUATING** runs check commands with `cwd: worktreePath` (`orchestrator.mjs:executeChecksAndCaptureExitCodes`). This ensures checks validate what will actually be PR'd, not the orchestrator's own directory.
6. **DISTILLING** produces feedback.
7. **APPLYING** commits (`git commit` with message containing `Ticket: <task.id>` and `Co-Authored-By: Circus Loop Factory` trailer), pushes (`git push origin <branch>`), creates a PR (`gh pr create --base <baseBranch> --head <branchName>`), and cleans up the worktree (`git worktree remove --force`).

### Failure Handling

- **Worktree creation fails** (e.g., invalid base branch): STOP, exit 42, resume hash written. No worktree to preserve.
- **APPLYING fails** (commit, push, or gh auth/pr-create fails): STOP, exit 42, resume hash written, **worktree is NOT cleaned up** — it remains on disk at `.loop/worktrees/<task-id>` for manual recovery. The STOP message includes the exact commands to finish the PR manually.

### Network Operations (Mocked in Tests)

Tests mock `pushBranch`, `createPR`, and `checkGhAuth` via a `TestOrchestrator` subclass. All git operations (worktree creation, commit, diff extraction) run against a real (but isolated, test-injected) git repo. This boundary is tested explicitly in `loop/tests/applying-stage.test.mjs`.

## State Machine

### States
1. **IDLE** — no active loop, awaiting task
2. **PLANNING** — planner role active
3. **CODING** — coder role active (now runs inside a git worktree)
4. **EVALUATING** — evaluator role active (checks run with cwd set to worktree)
5. **DISTILLING** — distiller role active
6. **APPLYING** — commit, push, PR creation
7. **STOPPED** — terminal state, requires resume token to continue

### Transitions

```
IDLE → PLANNING (on task receipt)
PLANNING → [create worktree] → CODING (plan.md complete, worktree ready)
CODING → [extract diff from worktree via git] → EVALUATING (diff extracted)
EVALUATING → STOPPED (if verdict signals STOP)
EVALUATING → DISTILLING (if all checks pass)
EVALUATING → STOPPED (if checks fail and max iterations reached)
DISTILLING → APPLYING (feedback complete, ready to commit/push/PR)
APPLYING → IDLE (PR created, worktree cleaned up)
APPLYING → STOPPED (if commit/push/PR creation fails; worktree preserved for manual recovery)
STOPPED → (no transition without resume token)
```

### STOP Signal
Any role can signal STOP by returning an object with `{ signal: 'STOP', reason: 'explanation' }` instead of its normal artifact.

When STOP is signaled:
1. Orchestrator exits with nonzero process exit code (exit code 42)
2. A resume token hash (SHA-256) is written to `.loop/resume-hash`
3. No further state transitions occur
4. **Not yet enforced**: `run()` does not check `.loop/resume-hash` on entry, so nothing currently requires a resume token before the next invocation proceeds — see G2 below.

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

**Enforcement — PARTIAL. The halt is real; the block on resuming is not.**
- STOP signal shape: `{ signal: 'STOP', reason: string }`
- On STOP: `handleStop()` (`orchestrator.mjs:113-126`) writes a resume-token hash to `.loop/resume-hash` and calls `process.exit(42)` — this part is real and tested.
- `verifyResumeToken()` (`orchestrator.mjs:129-137`) exists and correctly checks a supplied token against the stored hash — but **`run()` never calls it and never checks `resumeHashPath` on entry.** Nothing stops a fresh `orchestrator.run()` from simply starting again after a STOP; the hash file is written and then never read by the orchestrator itself. Tests cover "exits 42 and writes a hash," not "resume is blocked without the token" — which is the half that makes STOP *terminal* rather than just *loud*.
- Until `run()` gates on this, treat G2 as: halts reliably, does not yet prevent re-entry.

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
**What**: Each role receives ONLY the artifacts it's allowed to read, enforced by adapter call signature.

**Currently implemented (adapter interface enforcement)**:
- **Coder** receives: `plan.md` only (and `feedback.md` on retry, if present)
- **Evaluator** receives: `plan.md`, `diff` only
- **Distiller** receives: `plan.md`, `diff`, `verdict.json` only
- **Planner** receives: task description, optional `feedback.md` from prior iteration
- Orchestrator does not pass disallowed artifacts to role adapters (they are not in scope)
- Tests verify: adapter functions are called with correct arguments (negative test: coder throws if verdict passed; positive test: each role receives only allowed artifacts)

**Status as of BUILD 4 PHASE 1 (this PR)** — two of three implemented against the real `Orchestrator`, one observed-not-enforced:

- **Temporal separation — OBSERVED, NOT ENFORCED.** `orchestrator.mjs:346` records `runState.coderExitedAt = Date.now()` after the coder adapter's promise resolves; `:379` records `runState.verdictWrittenAt` after the verdict is captured. `loop/tests/g6-role-isolation.test.mjs` (BUILD 4-A) drives the real `Orchestrator`, calls `.run()`, and asserts `coderExitedAt < verdictWrittenAt` off the live instance — that part is real. But there is no comparison or gate anywhere in `orchestrator.mjs` itself; the two timestamps are recorded and never checked against each other by the orchestrator. The ordering the test observes is guaranteed by the surrounding `await` sequencing, not by this mechanism. `coderExitedAt` is also misnamed: it marks when the adapter's promise resolved, not when a coder *process* exited — for a stub adapter those are identical, but a real spawned coder process can resolve its promise (e.g. stdout closes) before the OS process has actually exited, and this mechanism cannot tell the difference. **Real process-exit confirmation (not promise-resolution timestamping) is required before this can gate a real coder.**

- **Feedback projection — IMPLEMENTED.** `orchestrator.mjs:100-109` defines `filterVerdictToFeedback()`; `:403` calls it to override the distiller's own output: `feedback = this.filterVerdictToFeedback(verdictJson)`. `loop/tests/g6-role-isolation.test.mjs` (BUILD 4-B) drives the real `Orchestrator`, feeds a verdict containing sensitive fields (`privateData`, `evaluatorCommentary`, `probeSummary`) plus a distiller that returns unrelated raw text, then asserts on `orchestrator.artifacts.feedback` that none of it leaked and the raw distiller output was discarded. One-line disable: comment out line 403.

- **Capability manifest — VALIDATED, NOT WIRED.** The orchestrator validates the manifest's *shape* at construction (`orchestrator.mjs:100-105`: throws if missing/malformed or `allowedTools` isn't an array) and refuses to invoke the coder role if `allowBash`/`allowNetwork` are `true` (`callRole`, `:408-412`). Tests (BUILD 4-C/D) construct the real `Orchestrator` with bad or permissive manifests and `assert.throws` — that part is real. **What's missing:** the manifest is never translated into anything that actually constrains a coder process. It only gates whether the orchestrator *starts* the coder role — it does not configure the coder's own tool access. A real coder (e.g. a spawned Claude Code process) reads nothing from `coderManifest`; absent separate wiring, it has full tool access regardless of what the manifest object says, the same way a linter that only checks a config file exists doesn't enforce the rules inside it. **Translating `coderManifest` into actual CLI permission flags on the coder invocation is required before a real coder runs** — this is the last place a validated intention and an enforced one still get conflated in this codebase, exactly the gap #22/#23 existed to correct for the other two mechanisms.

**Why not permission bits or per-user isolation?**
- All production processes run as root (uid=0), making permission bits ineffective
- Container-per-role adds complexity without solving the root-bypass constraint
- Temporal separation is simpler and more reliable: if the artifact doesn't exist yet, permission bits are moot

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

## Before the Real Coder

Everything above describes a state machine that has been proven against stub and mocked adapters — never against a real spawned process. Three things are required before that changes, because the class of risk changes with it: every prior build produced tests and code that could only be wrong in a file; a real coder produces actions — real branches, real pushes, real PRs — from a process whose output isn't read before it acts.

1. **Manifest wiring.** `coderManifest` (see G6 above) validates shape and gates whether the coder role starts — it does not configure anything. A real coder process needs `allowBash`/`allowNetwork`/`allowedPaths`/`allowedTools` translated into actual CLI permission flags on the invocation itself. Until that exists, the manifest is a validated intention, not an enforced one, and a real coder has full tool access regardless of what the object claims.
2. **Temporal separation on real process exit.** `runState.coderExitedAt` currently records adapter-promise resolution, not OS process exit (see G6 above). Those are identical for a stub and can diverge for a real spawned process. This can't be honestly fixed or even tested until there's a real process to observe — but it must land before temporal separation is trusted for anything real.
3. **Dispatcher checkpointing.** Observed three times in one session: a dispatched build dies mid-work with no persisted state, and recovery depends entirely on git history plus a human reading the diff by hand. The loop has no equivalent of G2's resume-hash for its own dispatcher. Needed before real-coder runs are long enough (and costly enough) for a mid-run death to be expensive rather than merely annoying.

First real-coder run, when it happens: scratch repo, one trivial task, manifest actually wired into the invocation. Success criterion is a PR a human reads end to end — not a green test suite.
