// G2: stop-is-terminal guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, existsSync, unlinkSync, mkdtempSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const orchestratorPath = fileURLToPath(new URL('../orchestrator.mjs', import.meta.url));

test('G2 negative: STOP signal exits with code 42 and writes resume token', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-neg-'));
  const resumeHashPath = join(tempDir, 'resume-hash');

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  // Run in subprocess to capture exit code
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code() { return { artifact: 'diff' }; },
      async evaluate() { return { signal: 'STOP', reason: 'test' }; },
      async distill() { throw new Error('unreachable'); }
    };
    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}' });
    await orch.run('test');
  `]);

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash');
      resolve();
    });
  });
});

test('G2 positive: normal flow completes without STOP', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

  const normalAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir });
  const result = await orchestrator.run('test task');

  assert.ok(result.feedback, 'Should complete and return feedback');
});

test('G2 BUILD 2: resume with hash stored on disk fails', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-build2-'));
  const resumeHashPath = join(tempDir, 'resume-hash');

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

  const stopAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { signal: 'STOP', reason: 'test hash storage' };
    },
    async distill() {
      throw new Error('unreachable');
    }
  };

  // Run in subprocess to trigger STOP
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code() { return { artifact: 'diff' }; },
      async evaluate() { return { signal: 'STOP', reason: 'test' }; },
      async distill() { throw new Error('unreachable'); }
    };
    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}' });
    await orch.run('test');
  `]);

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash');

      // Read the hash that was stored
      const storedHash = readFileSync(resumeHashPath, 'utf8').trim();

      // Try to resume using the hash itself (not the raw token) - should FAIL
      const resumeOrch = new Orchestrator(stopAdapter, { resumeHashPath });
      assert.throws(
        () => resumeOrch.verifyResumeToken(storedHash),
        /Resume token mismatch/,
        'Should reject resume attempt using the hash itself (proof that hash alone is useless)'
      );

      resolve();
    });
  });
});
