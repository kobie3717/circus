#!/bin/bash
# Mutation testing — verify every guard has a test that fails when the guard is disabled,
# AND that it's the RIGHT test(s) that fail — not just something.

set -e

ORCHESTRATOR="loop/orchestrator.mjs"
ORCHESTRATOR_BACKUP="${ORCHESTRATOR}.mutation-backup"

echo "=== Mutation Check ==="
echo "Testing that each guard's tests fail when the guard is disabled — and ONLY those tests"
echo

# Backup orchestrator
cp "$ORCHESTRATOR" "$ORCHESTRATOR_BACKUP"

function cleanup {
  echo "Restoring orchestrator..."
  mv "$ORCHESTRATOR_BACKUP" "$ORCHESTRATOR"
}
trap cleanup EXIT

# Define mutations as sed commands that disable each guard.
#
# Anchored to CONTENT, not line numbers. Line-numbered mutations went stale
# on the very first PR that touched orchestrator.mjs after they were written
# (see PR history: g6-role-isolation's target silently moved 405 -> 427).
# A no-op sed is caught by the apply-diagnostic below either way, but a
# content anchor means most edits to this file don't require re-anchoring
# every mutation as a routine chore — and routine chores are where a gamed
# mutation hides next time.
#
# Each pattern must be a UNIQUE substring in orchestrator.mjs (verified when
# written; re-verify with `grep -c` if a future edit could introduce a
# duplicate). Each mutation neutralizes one guard's condition/call to
# `false` or a comment, rather than commenting out a line range — this stays
# valid JS regardless of what's above/below the anchor.
declare -A MUTATIONS
MUTATIONS["g1-matrix-shape"]="s/JSON.stringify(actual) !== JSON.stringify(expectedSorted)/false \/* MUTATION: g1-matrix-shape disabled *\//"
MUTATIONS["g2-stop-is-terminal"]="s/process.exit(42);/\/* MUTATION: g2-stop-is-terminal disabled *\//"
MUTATIONS["g3-artifact-scrub"]="s/result.hits > 0/false \/* MUTATION: g3-artifact-scrub disabled *\//"
MUTATIONS["g4-harness-integrity"]="s/row.evidence.includes('SUBSTITUTED:')/false \/* MUTATION: g4-harness-integrity disabled *\//"
MUTATIONS["g5-stub-detection"]="s/flags.length > 0/false \/* MUTATION: g5-stub-detection disabled *\//"
MUTATIONS["g6-role-isolation"]="s/feedback = this.filterVerdictToFeedback(verdictJson);/\/* MUTATION: g6-role-isolation disabled *\//"
MUTATIONS["g2-resume-gate"]="s/this.checkResumeGate(resumeToken);/\/* MUTATION: g2-resume-gate disabled *\//"
MUTATIONS["applying-worktree-cwd"]="s/stdio: 'pipe', timeout: 30000, cwd: worktreePath/stdio: 'pipe', timeout: 30000 \/* MUTATION: applying-worktree-cwd disabled *\//"

# Expected red set per mutation — the fix for the g7-shape gap: a gate that
# only checks "did something go red" proves nothing about WHICH guard it's
# named after. cwd:worktreePath matched two call sites once, the whole suite
# went red, and the old gate reported it "effective" — true and meaningless.
#
# Each entry is a '|||'-delimited list of exact failure identifiers as they
# appear on TAP "not ok N - <name>" lines. Most mutations fail specific
# named tests. process.exit(42) inside handleStop() is genuinely shared
# infrastructure across G2/G5/APPLYING/harness-isolation — disabling it
# legitimately breaks everything that depends on STOP actually halting, so
# its expected set is wide ON PURPOSE, not noise; declaring it explicitly is
# what makes that a checked fact instead of an assumption.
#
# Some real STOP paths run in-process (not a subprocess) and process.exit
# there kills the whole node --test file before individual test results
# print — those show up as the file path itself, not a test name. Both
# granularities are valid entries; they're just whatever TAP actually names.
#
# Regenerate this by applying one mutation at a time and reading the actual
# `not ok` lines — don't hand-guess it. See PR history for the command.
declare -A EXPECTED_RED
EXPECTED_RED["g1-matrix-shape"]="G1 negative: verdict with wrong check names is rejected"
EXPECTED_RED["g2-stop-is-terminal"]="APPLYING negative: worktree creation failure causes STOP|||APPLYING negative: push failure causes STOP and preserves worktree|||APPLYING negative: gh auth failure causes STOP|||G2 negative: STOP signal exits with code 42 and writes resume token|||G2 BUILD 2: resume with hash stored on disk fails|||G2 resume gate: fresh run refuses to start when hash is present and no token supplied|||G2 resume gate: fresh run refuses to start when the supplied token does not match|||G2 resume gate: correct token clears the hash and lets run() proceed|||G5 BUILD 3: end-to-end orchestrator run with stub capitulation causes HALT|||harness isolation: a real STOP with an injected loopDir never touches the real repo .loop/"
EXPECTED_RED["g3-artifact-scrub"]="G3 negative: artifact with secret is rejected"
EXPECTED_RED["g4-harness-integrity"]="G4 negative: substituted command in verdict is rejected"
EXPECTED_RED["g5-stub-detection"]="G5 BUILD 3: end-to-end orchestrator run with stub capitulation causes HALT"
EXPECTED_RED["g6-role-isolation"]="G6 BUILD 4-B: feedback projection - only whitelisted fields leak to coder"
EXPECTED_RED["g2-resume-gate"]="loop/tests/g2-stop-terminal.test.mjs"
EXPECTED_RED["applying-worktree-cwd"]="APPLYING: checks run against worktree, not orchestrator directory"

MUTATION_COUNT=${#MUTATIONS[@]}
EXPECTED_GUARDS=8  # 6 original guards + g2-resume-gate + applying-worktree-cwd
                   # applying-worktree-cwd: disabling the cwd parameter in executeChecksAndCaptureExitCodes
                   # should turn the "checks run against worktree" test red, proving checks would
                   # run against the orchestrator's own directory instead of the coder's actual changes.
                   # Any future change to this number must arrive with the new mutation's
                   # red-run proof AND its declared expected-red-set attached.

if [ $MUTATION_COUNT -ne $EXPECTED_GUARDS ]; then
  echo "ERROR: Expected $EXPECTED_GUARDS mutations (G1-G6), found $MUTATION_COUNT"
  exit 1
fi

for GUARD_NAME in "${!MUTATIONS[@]}"; do
  if [ -z "${EXPECTED_RED[$GUARD_NAME]+set}" ]; then
    echo "ERROR: Mutation '$GUARD_NAME' has no declared EXPECTED_RED set."
    echo "  A mutation without a declared expected-red-set proves nothing —"
    echo "  it's exactly the gap this gate exists to close. Add one."
    exit 1
  fi
done

echo "Testing $MUTATION_COUNT mutations"
echo

FAILED_MUTATIONS=()

for GUARD_NAME in "${!MUTATIONS[@]}"; do
  SED_COMMAND="${MUTATIONS[$GUARD_NAME]}"
  echo "--- Testing mutation: $GUARD_NAME ---"

  # Restore clean orchestrator
  cp "$ORCHESTRATOR_BACKUP" "$ORCHESTRATOR"

  # Apply mutation via sed
  sed -i "$SED_COMMAND" "$ORCHESTRATOR"

  # Verify the sed actually changed the file — a no-op sed (stale line numbers,
  # non-matching pattern) must be distinguishable from "guard unenforced"
  if diff -q "$ORCHESTRATOR_BACKUP" "$ORCHESTRATOR" > /dev/null 2>&1; then
    echo "⚠️  MUTATION DID NOT APPLY: sed command for $GUARD_NAME made no change to $ORCHESTRATOR"
    echo "   Target lines/pattern are stale — this proves nothing about $GUARD_NAME's enforcement."
    FAILED_MUTATIONS+=("$GUARD_NAME (DID NOT APPLY)")
    echo
    continue
  fi

  echo "Applied mutation: $GUARD_NAME"

  # Run tests and capture full output — need the actual failing test names,
  # not just an aggregate pass/fail count.
  TEST_OUTPUT=$(npm test 2>&1 || true)

  # Actual red set: every "not ok N - <name>" line, name extracted, sorted+deduped.
  ACTUAL_RED=$(echo "$TEST_OUTPUT" | grep -E "^not ok [0-9]+ - " | sed -E 's/^not ok [0-9]+ - //' | sort -u)

  if [ -z "$ACTUAL_RED" ]; then
    echo "❌ MUTATION INEFFECTIVE: Tests still pass with $GUARD_NAME disabled!"
    echo "   This mutation does not actually disable the guard, or the guard has no tests."
    FAILED_MUTATIONS+=("$GUARD_NAME (INEFFECTIVE — nothing went red)")
    echo
    continue
  fi

  # Expected red set, sorted+deduped the same way for a clean set comparison.
  # Split on the literal '|||' separator (awk RS, not tr — tr only handles
  # single-char delimiters).
  EXPECTED_RED_SORTED=$(printf '%s' "${EXPECTED_RED[$GUARD_NAME]}" | awk -v RS='\\|\\|\\|' '{print}' | sort -u)

  MISSING=$(comm -23 <(echo "$EXPECTED_RED_SORTED") <(echo "$ACTUAL_RED"))
  EXTRA=$(comm -13 <(echo "$EXPECTED_RED_SORTED") <(echo "$ACTUAL_RED"))

  if [ -z "$MISSING" ] && [ -z "$EXTRA" ]; then
    echo "✓ Mutation effective AND precise: exactly the declared test(s) failed"
    echo "$ACTUAL_RED" | sed 's/^/    - /'
  else
    echo "❌ MUTATION IMPRECISE: red set does not match the declared expectation for $GUARD_NAME"
    if [ -n "$MISSING" ]; then
      echo "   Expected to fail but DIDN'T (guard may be weaker than believed):"
      echo "$MISSING" | sed 's/^/     - /'
    fi
    if [ -n "$EXTRA" ]; then
      echo "   Failed but wasn't expected to (over-broad mutation — this is the g7 shape:"
      echo "   a gate that's green/red for a reason unrelated to what it claims):"
      echo "$EXTRA" | sed 's/^/     - /'
    fi
    FAILED_MUTATIONS+=("$GUARD_NAME (IMPRECISE — see red-set mismatch above)")
  fi

  echo
done

# Restore orchestrator (cleanup trap will also do this, but be explicit)
cp "$ORCHESTRATOR_BACKUP" "$ORCHESTRATOR"

echo "=== Mutation Check Summary ==="
if [ ${#FAILED_MUTATIONS[@]} -eq 0 ]; then
  echo "✓ All $MUTATION_COUNT mutations are effective AND precise"
  echo "  Every guard causes exactly its declared test(s) to fail when disabled — nothing more, nothing less"
  exit 0
else
  echo "❌ ${#FAILED_MUTATIONS[@]} problem mutations:"
  for GUARD in "${FAILED_MUTATIONS[@]}"; do
    echo "  - $GUARD"
  done
  exit 1
fi
