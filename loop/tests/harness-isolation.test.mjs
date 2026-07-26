// harness-isolation: the .loop/ state directory must be injectable, and
// tests must never resolve it to the real project root. See
// loop/tests/helpers/loop-dir.mjs for the rationale — this is the reusable
// invariant, proven here once, that the PR-stage worktree work reuses next.

import { test } from 'node:test';
import assert from 'node:assert';
import { existsSync, rmSync } from 'node:fs';
import { Orchestrator } from '../orchestrator.mjs';
import { tempLoopDir, assertNotProjectRoot } from './helpers/loop-dir.mjs';

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
  const orchestrator = new Orchestrator(adapter);
  assert.strictEqual(orchestrator.loopDir, '.loop');
  assert.strictEqual(orchestrator.resumeHashPath, '.loop/resume-hash');
});

test('harness isolation: injecting loopDir moves resumeHashPath with it', (t) => {
  const dir = tempLoopDir(t);
  const adapter = { async plan() {}, async code() {}, async evaluate() {}, async distill() {} };
  const orchestrator = new Orchestrator(adapter, { loopDir: dir });
  assert.doesNotThrow(() => assertNotProjectRoot(orchestrator.resumeHashPath));
  assert.ok(orchestrator.resumeHashPath.startsWith(dir));
});

test('harness isolation: a real STOP with an injected loopDir never touches the real repo .loop/', async (t) => {
  const dir = tempLoopDir(t);
  const projectResumeHash = '.loop/resume-hash';
  const existedBefore = existsSync(projectResumeHash);

  const adapter = {
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { signal: 'STOP', reason: 'harness isolation proof' }; },
    async distill() { throw new Error('unreachable'); }
  };

  // handleStop calls process.exit(42) in production; here we only need to
  // prove the write target, so intercept process.exit for this assertion.
  const orchestrator = new Orchestrator(adapter, { loopDir: dir });
  const originalExit = process.exit;
  let exitCode = null;
  process.exit = (code) => { exitCode = code; throw new Error('__STOP_EXIT__'); };
  try {
    await orchestrator.run('test task');
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
