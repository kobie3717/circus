// G2: stop-is-terminal guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, writeFileSync, existsSync, unlinkSync, mkdtempSync, rmSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { tempGitRepo } from './helpers/git-repo.mjs';
import { TestOrchestrator } from './helpers/mock-network.mjs';

const orchestratorPath = fileURLToPath(new URL('../orchestrator.mjs', import.meta.url));

test('G2 negative: STOP signal exits with code 42 and writes resume token', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-neg-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot } = tempGitRepo(t);

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  // Run in subprocess to capture exit code
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code({ worktreePath }) { return { artifact: null }; },
      async evaluate() { return { signal: 'STOP', reason: 'test' }; },
      async distill() { throw new Error('unreachable'); }
    };
    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}', loopDir: '${tempDir}', baseBranch: 'main', repoRoot: '${repoRoot}' });
    await orch.run({ id: 'TEST-001', task: 'test', mandatory_checks: [] });
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
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const normalAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code({ worktreePath }) {
      // Write real content into the worktree — the orchestrator derives the
      // diff via `git diff`, it never reads a diff string off this return value.
      writeFileSync(join(worktreePath, 'greeting.js'), 'export function greet(name) { return `Hello, ${name}!`; }\n');
      return { artifact: null };
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
  // Isolated repoRoot — worktree/push/PR operations must not touch /root/circus.
  const orchestrator = new TestOrchestrator(normalAdapter, { fixtureDir, resumeHashPath, loopDir: tempDir, baseBranch, repoRoot });
  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });

  assert.ok(result.feedback, 'Should complete and return feedback');
});

test('G2 BUILD 2: resume with hash stored on disk fails', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-build2-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot } = tempGitRepo(t);

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
      async code({ worktreePath }) { return { artifact: null }; },
      async evaluate() { return { signal: 'STOP', reason: 'test' }; },
      async distill() { throw new Error('unreachable'); }
    };
    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}', loopDir: '${tempDir}', baseBranch: 'main', repoRoot: '${repoRoot}' });
    await orch.run({ id: 'TEST-001', task: 'test', mandatory_checks: [] });
  `]);

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash');

      // Read the hash that was stored
      const storedHash = readFileSync(resumeHashPath, 'utf8').trim();

      // Try to resume using the hash itself (not the raw token) - should FAIL
      const resumeOrch = new Orchestrator(stopAdapter, { resumeHashPath, loopDir: tempDir, baseBranch: 'main', repoRoot });
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
function runToStop(resumeHashPath, repoRoot, loopDir) {
  return new Promise((resolve) => {
    const childProcess = spawn('node', ['-e', `
      import { Orchestrator } from '${orchestratorPath}';
      const adapter = {
        async plan() { return { artifact: '# plan' }; },
        async code({ worktreePath }) { return { artifact: null }; },
        async evaluate() { return { signal: 'STOP', reason: 'resume-gate test' }; },
        async distill() { throw new Error('unreachable'); }
      };
      const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}', loopDir: '${loopDir}', baseBranch: 'main', repoRoot: '${repoRoot}' });
      await orch.run({ id: 'TEST-001', task: 'test', mandatory_checks: [] });
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
  const { repoRoot } = tempGitRepo(t);

  const { code } = await runToStop(resumeHashPath, repoRoot, tempDir);
  assert.strictEqual(code, 42, 'setup: prior run should STOP and leave a hash');
  assert.ok(existsSync(resumeHashPath), 'setup: resume hash should exist');

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  // Gate check happens before task validation, so this refusal must trip
  // before any git operation — repoRoot is injected anyway for defense-in-depth.
  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath, loopDir: tempDir, baseBranch: 'main', repoRoot });
  await assert.rejects(
    () => orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] }),
    /G2 violation.*resume hash present.*no resume token supplied/s,
    'Fresh run() must refuse to start while a STOP resume hash is pending and no token is given'
  );
  assert.ok(existsSync(resumeHashPath), 'Hash must still be present after a refused start');
});

test('G2 resume gate: fresh run refuses to start when the supplied token does not match', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-wrongtoken-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));
  const { repoRoot } = tempGitRepo(t);

  const { code } = await runToStop(resumeHashPath, repoRoot, tempDir);
  assert.strictEqual(code, 42);
  assert.ok(existsSync(resumeHashPath));

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code() { return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') }; },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir, resumeHashPath, loopDir: tempDir, baseBranch: 'main', repoRoot });
  await assert.rejects(
    () => orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] }, 'definitely-the-wrong-token'),
    /Resume token mismatch/,
    'Fresh run() must refuse to start when the supplied token does not hash to the stored value'
  );
  assert.ok(existsSync(resumeHashPath), 'Hash must still be present after a rejected token');
});

test('G2 resume gate: correct token clears the hash and lets run() proceed', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-goodtoken-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  t.after(() => rmSync(tempDir, { recursive: true, force: true }));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const { code, rawToken } = await runToStop(resumeHashPath, repoRoot, tempDir);
  assert.strictEqual(code, 42);
  assert.ok(rawToken, 'setup: STOP must print a raw resume token to stderr');
  assert.ok(existsSync(resumeHashPath));

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code({ worktreePath }) {
      writeFileSync(join(worktreePath, 'greeting.js'), 'export function greet(name) { return `Hello, ${name}!`; }\n');
      return { artifact: null };
    },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  const orchestrator = new TestOrchestrator(normalAdapter, { fixtureDir, resumeHashPath, loopDir: tempDir, baseBranch, repoRoot });
  // Distinct task id — the resume gate only clears the hash and lets a new
  // run start; it does not resume execution into the STOPped run's own
  // worktree/branch (that's checkpoint/resume-state work, out of scope
  // here). Reusing 'TEST-001' would collide with the branch the STOPped
  // subprocess already created and left in place by design.
  const result = await orchestrator.run({ id: 'TEST-002', task: 'test task', mandatory_checks: [] }, rawToken);

  assert.ok(result.feedback, 'run() should proceed and complete with the correct resume token');
  assert.ok(!existsSync(resumeHashPath), 'Resume hash must be cleared after a successful resume — it cannot gate a second time');
});

test('G2 resume gate: no hash on disk means normal start, token or not', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g2-gate-nohash-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const normalAdapter = {
    async plan() { return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') }; },
    async code({ worktreePath }) {
      writeFileSync(join(worktreePath, 'greeting.js'), 'export function greet(name) { return `Hello, ${name}!`; }\n');
      return { artifact: null };
    },
    async evaluate() { return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') }; },
    async distill() { return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') }; }
  };

  assert.ok(!existsSync(resumeHashPath), 'setup: no prior STOP, no hash file');
  const orchestrator = new TestOrchestrator(normalAdapter, { fixtureDir, resumeHashPath, loopDir: tempDir, baseBranch, repoRoot });
  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });
  assert.ok(result.feedback, 'run() should proceed normally when there is no pending resume hash');

  rmSync(tempDir, { recursive: true, force: true });
});
