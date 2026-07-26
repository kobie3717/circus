# Test Plan - Missing Mandatory Check

## Goal
Test that plan rejects when it omits a mandatory check.

## Checks
- [lint-check]: npm run lint
- [type-check]: npm run type-check

## Eval Strategy
This plan is missing the "test-greeting" mandatory check (intentionally).
