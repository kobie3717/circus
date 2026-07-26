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

test('G2 positive: normal flow completes without STOP', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-positive-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

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

  // Isolated resumeHashPath — a stray .loop/resume-hash in the real repo
  // (left by a prior real STOP) must not gate an unrelated hermetic test.
  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath });
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

// ─── G2 resume gate: run() checks .loop/resume-hash on entry ────────────────
// Helper: run the STOP path in a subprocess (handleStop calls process.exit),
// capture the raw resume token it prints to stderr, and confirm the hash file
// it left behind. All three follow-on assertions then drive the real
// Orchestrator.run() in-process (no process.exit on these paths — they throw).
function runToStop(resumeHashPath) {
  return new Promise((resolve) => {
    const childProcess = spawn('node', ['-e', `
      import { Orchestrator } from '${orchestratorPath}';
      const adapter = {
        async plan() { return { artifact: '# plan' }; },
        async code() { return { artifact: 'diff' }; },
        async evaluate() { return { signal: 'STOP', reason: 'resume-gate test' }; },
        async distill() { throw new Error('unreachable'); }
      };
      const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}' });
      await orch.run('test');
    `]);

    let stderr = '';
    childProcess.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

    childProcess.on('exit', (code) => {
      const match = stderr.match(/Resume token: ([0-9a-f]+)/);
      resolve({ code, rawToken: match ? match[1] : null });
    });
  });
}

test('G2 resume gate: fresh run refuses to start when hash is present and no token supplied', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-notoken-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const { code } = await runToStop(resumeHashPath);
  assert.strictEqual(code, 42, 'setup: prior run should STOP and leave a hash');
  assert.ok(existsSync(resumeHashPath), 'setup: resume hash should exist');

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath });
  await assert.rejects(
    () => orchestrator.run('test task'),
    /G2 violation.*resume hash present.*no resume token supplied/s,
    'Fresh run() must refuse to start while a STOP resume hash is pending and no token is given'
  );
  assert.ok(existsSync(resumeHashPath), 'Hash must still be present after a refused start');
});

test('G2 resume gate: fresh run refuses to start when the supplied token does not match', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-wrongtoken-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const { code } = await runToStop(resumeHashPath);
  assert.strictEqual(code, 42);
  assert.ok(existsSync(resumeHashPath));

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath });
  await assert.rejects(
    () => orchestrator.run('test task', 'definitely-the-wrong-token'),
    /Resume token mismatch/,
    'Fresh run() must refuse to start when the supplied token does not hash to the stored value'
  );
  assert.ok(existsSync(resumeHashPath), 'Hash must still be present after a rejected token');
});

test('G2 resume gate: correct token clears the hash and lets run() proceed', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-goodtoken-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const { code, rawToken } = await runToStop(resumeHashPath);
  assert.strictEqual(code, 42);
  assert.ok(rawToken, 'setup: STOP must print a raw resume token to stderr');
  assert.ok(existsSync(resumeHashPath));

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath });
  const result = await orchestrator.run('test task', rawToken);

  assert.ok(result.feedback, 'run() should proceed and complete with the correct resume token');
  assert.ok(!existsSync(resumeHashPath), 'Resume hash must be cleared after a successful resume — it cannot gate a second time');
});

test('G2 resume gate: no hash on disk means normal start, token or not', async () => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-nohash-'));
  const resumeHashPath = join(tempDir, 'resume-hash');

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  assert.ok(!existsSync(resumeHashPath), 'setup: no prior STOP, no hash file');
  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath });
  const result = await orchestrator.run('test task');
  assert.ok(result.feedback, 'run() should proceed normally when there is no pending resume hash');

  rmSync(tempDir, { recursive: true, force: true });
});
