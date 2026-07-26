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

# Define mutations as sed commands that disable each guard
declare -A MUTATIONS
MUTATIONS["g1-matrix-shape"]="175,181s/^/\/\/ MUTATION: /"
MUTATIONS["g2-stop-is-terminal"]="135,137s/^/\/\/ MUTATION: /"
MUTATIONS["g3-artifact-scrub"]="94,96s/^/\/\/ MUTATION: /"
MUTATIONS["g4-harness-integrity"]="213,223s/^/\/\/ MUTATION: /"
MUTATIONS["g5-stub-detection"]="264,268s/^/\/\/ MUTATION: /"
MUTATIONS["g6-role-isolation"]="405s/feedback = this.filterVerdictToFeedback(verdictJson);/\/\/ MUTATION: feedback = this.filterVerdictToFeedback(verdictJson);/"

MUTATION_COUNT=${#MUTATIONS[@]}
EXPECTED_GUARDS=6

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
