// G2: stop-is-terminal guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, existsSync, unlinkSync } from 'node:fs';
import { spawn } from 'node:child_process';

test('G2 negative: STOP signal exits with code 42 and writes resume token', async (t) => {
  const resumeHashPath = '.loop/resume-hash-test-g2-neg';

  // Clean up any existing hash
  if (existsSync(resumeHashPath)) {
    unlinkSync(resumeHashPath);
  }

  // Run in subprocess to capture exit code
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '/root/circus/loop/orchestrator.mjs';
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
      if (existsSync(resumeHashPath)) {
        unlinkSync(resumeHashPath);
      }
      resolve();
    });
  });
});

test('G2 positive: normal flow completes without STOP', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

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

test('G2 BUILD 2: resume with hash stored on disk fails', async () => {
  const resumeHashPath = '.loop/resume-hash-test-g2-build2';

  // Clean up
  if (existsSync(resumeHashPath)) {
    unlinkSync(resumeHashPath);
  }

  const fixtureDir = '/root/circus/loop/fixtures';

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
    import { Orchestrator } from '/root/circus/loop/orchestrator.mjs';
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

      // Clean up
      unlinkSync(resumeHashPath);
      resolve();
    });
  });
});
