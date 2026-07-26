# Test Plan - Always Fails Check

## Goal
Test that orchestrator executes check commands and overrides false passes.

## Checks
- [always-fails]: exit 1

## Eval Strategy
The command "exit 1" always fails. Evaluator will claim pass, orchestrator must override.
