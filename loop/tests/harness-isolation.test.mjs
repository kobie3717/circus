// harness-isolation: the .loop/ state directory must be injectable, and
// tests must never resolve it to the real project root. See
// loop/tests/helpers/loop-dir.mjs for the rationale — this is the reusable
// invariant, proven here once, that the PR-stage worktree work reuses next.

import { test } from 'node:test';
import assert from 'node:assert';
import { existsSync, rmSync } from 'node:fs';
import { Orchestrator } from '../orchestrator.mjs';
import { tempLoopDir, assertNotProjectRoot } from './helpers/loop-dir.mjs';
import { tempGitRepo } from './helpers/git-repo.mjs';

test('harness isolation: assertNotProjectRoot rejects the real .loop directory', () => {
  assert.throws(
    () => assertNotProjectRoot('.loop'),
    /project root/,
    'A relative .loop path must be rejected as the project root'
  );
  assert.throws(
    () => assertNotProjectRoot(`${process.cwd()}/.loop`),
    /project root/,
    'An absolute path to the project .loop directory must be rejected'
  );
  assert.throws(
    () => assertNotProjectRoot(`${process.cwd()}/.loop/nested/deeper`),
    /project root/,
    'A path nested under the project .loop directory must also be rejected'
  );
});

test('harness isolation: assertNotProjectRoot accepts an isolated tmp path', (t) => {
  const dir = tempLoopDir(t);
  assert.doesNotThrow(() => assertNotProjectRoot(dir));
});

test('harness isolation: Orchestrator defaults loopDir to the project root — production behavior, which is exactly why tests must override it', () => {
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  // repoRoot is required (footgun guard, see below) but doesn't need to be a
  // real repo for this assertion — only loopDir's default is under test here.
  const orchestrator = new Orchestrator(adapter, { repoRoot: '/tmp/circus-harness-isolation-placeholder' });
  assert.strictEqual(orchestrator.loopDir, '.loop');
  assert.strictEqual(orchestrator.resumeHashPath, '.loop/resume-hash');
});

test('harness isolation: injecting loopDir moves resumeHashPath with it', (t) => {
  const dir = tempLoopDir(t);
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  const orchestrator = new Orchestrator(adapter, { loopDir: dir, repoRoot: '/tmp/circus-harness-isolation-placeholder' });
  assert.doesNotThrow(() => assertNotProjectRoot(orchestrator.resumeHashPath));
  assert.ok(orchestrator.resumeHashPath.startsWith(dir));
});

// ─── Footgun guard: repoRoot required, self-repo refused ───────────────────
// A bare `node loop/orchestrator.mjs` from the repo root used to default
// repoRoot to process.cwd() and just proceed — creating a real worktree and
// branch in /root/circus with nothing to stop it (found by the 2026-07-26
// survey). Both refusals below close that.

test('footgun guard: constructor throws when repoRoot is not provided', () => {
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  assert.throws(
    () => new Orchestrator(adapter),
    /repoRoot is required/,
    'Orchestrator must refuse to construct without an explicit repoRoot — no process.cwd() fallback'
  );
  assert.throws(
    () => new Orchestrator(adapter, { loopDir: '/tmp/whatever' }),
    /repoRoot is required/,
    'Other options being present must not paper over a missing repoRoot'
  );
});

test('footgun guard: constructor throws when repoRoot looks like the Circus repo itself, without allowSelf', () => {
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  // The real /root/circus repo (process.cwd() when tests run) contains
  // loop/orchestrator.mjs — exactly the marker this guard checks for.
  assert.throws(
    () => new Orchestrator(adapter, { repoRoot: process.cwd() }),
    /Refusing to operate on repoRoot.*looks like the Circus repo itself/s,
    'Must refuse to construct against a repo containing loop/orchestrator.mjs'
  );
});

test('footgun guard: allowSelf: true overrides the self-repo refusal', () => {
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  assert.doesNotThrow(
    () => new Orchestrator(adapter, { repoRoot: process.cwd(), allowSelf: true }),
    'allowSelf: true must let construction proceed against the real repo when explicitly requested'
  );
});

test('footgun guard: a repo without loop/orchestrator.mjs is never treated as self, regardless of allowSelf', (t) => {
  const { repoRoot } = tempGitRepo(t);
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  assert.doesNotThrow(
    () => new Orchestrator(adapter, { repoRoot }),
    'An isolated tmp repo with no loop/orchestrator.mjs must construct fine without allowSelf'
  );
});

test('harness isolation: a real STOP with an injected loopDir never touches the real repo .loop/', async (t) => {
  const dir = tempLoopDir(t);
  const projectResumeHash = '.loop/resume-hash';
  const existedBefore = existsSync(projectResumeHash);
  // Real repoRoot required now (footgun guard) — isolated, so worktree
  // creation (which now happens before evaluate()'s STOP is even reached)
  // runs against a throwaway repo, never /root/circus.
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { signal: 'STOP', reason: 'harness isolation proof' }; },
    async distill() { throw new Error('unreachable'); }
  };

  // handleStop calls process.exit(42) in production; here we only need to
  // prove the write target, so intercept process.exit for this assertion.
  const orchestrator = new Orchestrator(adapter, { loopDir: dir, baseBranch, repoRoot });
  const originalExit = process.exit;
  let exitCode = null;
  process.exit = (code) => { exitCode = code; throw new Error('__STOP_EXIT__'); };
  try {
    await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });
  } catch (err) {
    if (err.message !== '__STOP_EXIT__') throw err;
  } finally {
    process.exit = originalExit;
  }

  assert.strictEqual(exitCode, 42);
  assert.ok(existsSync(orchestrator.resumeHashPath), 'Hash should be written to the injected loopDir');
  assert.strictEqual(
    existsSync(projectResumeHash), existedBefore,
    'The real project .loop/resume-hash must be unaffected by this test'
  );
});
