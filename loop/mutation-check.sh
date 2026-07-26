#!/bin/bash
# Mutation testing — verify every guard has a test that fails when the guard is disabled

set -e

ORCHESTRATOR="loop/orchestrator.mjs"
ORCHESTRATOR_BACKUP="${ORCHESTRATOR}.mutation-backup"

echo "=== Mutation Check ==="
echo "Testing that each guard's tests fail when the guard is disabled"
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

MUTATION_COUNT=${#MUTATIONS[@]}
EXPECTED_GUARDS=8  # 6 original guards + g2-resume-gate + applying-worktree-cwd
                   # applying-worktree-cwd: disabling the cwd parameter in executeChecksAndCaptureExitCodes
                   # should turn the "checks run against worktree" test red, proving checks would
                   # run against the orchestrator's own directory instead of the coder's actual changes.

if [ $MUTATION_COUNT -ne $EXPECTED_GUARDS ]; then
  echo "ERROR: Expected $EXPECTED_GUARDS mutations (G1-G6), found $MUTATION_COUNT"
  exit 1
fi

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

  # Run tests (should FAIL with mutation applied)
  if npm test > /dev/null 2>&1; then
    echo "❌ MUTATION INEFFECTIVE: Tests still pass with $GUARD_NAME disabled!"
    echo "   This mutation does not actually disable the guard, or the guard has no tests."
    FAILED_MUTATIONS+=("$GUARD_NAME")
  else
    echo "✓ Mutation effective: Tests failed as expected with $GUARD_NAME disabled"
  fi

  echo
done

# Restore orchestrator (cleanup trap will also do this, but be explicit)
cp "$ORCHESTRATOR_BACKUP" "$ORCHESTRATOR"

echo "=== Mutation Check Summary ==="
if [ ${#FAILED_MUTATIONS[@]} -eq 0 ]; then
  echo "✓ All $MUTATION_COUNT mutations are effective"
  echo "  Every guard causes tests to fail when disabled"
  exit 0
else
  echo "❌ ${#FAILED_MUTATIONS[@]} ineffective mutations:"
  for GUARD in "${FAILED_MUTATIONS[@]}"; do
    echo "  - $GUARD"
  done
  exit 1
fi
