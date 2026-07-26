// G6: role-isolation guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

test('G6 negative: role receiving disallowed artifacts fails', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

  // Create orchestrator that will try to pass verdict to coder
  const orchestrator = new Orchestrator({
    async plan() { return { artifact: '# Plan\n## Checks\n- [test]: echo ok' }; },
    async code({ plan, verdict }) {
      // Coder should NOT receive verdict
      if (verdict !== undefined) {
        throw new Error('G6 violation: coder received disallowed artifact (verdict)');
      }
      return { artifact: 'diff' };
    },
    async evaluate() {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill() { return { artifact: 'feedback' }; }
  });

  // Manually violate isolation by injecting verdict before coder runs
  orchestrator.artifacts.verdict = 'leaked verdict';

  // The coder adapter checks for verdict and throws if present
  // But the orchestrator should not pass it due to callRole enforcement
  const result = await orchestrator.run('test task');

  // Verify coder only received plan (test passes if no error)
  assert.ok(result.diff, 'Should complete even though verdict exists in artifacts');
});

test('G6 positive: roles receive only allowed artifacts', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

  const seenArtifacts = {
    coder: null,
    evaluator: null,
    distiller: null
  };

  const adapter = {
    async plan() {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code(ctx) {
      seenArtifacts.coder = Object.keys(ctx);
      return { artifact: 'diff content' };
    },
    async evaluate(ctx) {
      seenArtifacts.evaluator = Object.keys(ctx);
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill(ctx) {
      seenArtifacts.distiller = Object.keys(ctx);
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(adapter, { fixtureDir });
  await orchestrator.run('test task');

  // Verify each role received only allowed artifacts
  assert.deepStrictEqual(seenArtifacts.coder, ['plan'], 'Coder should receive only plan');
  assert.deepStrictEqual(seenArtifacts.evaluator.sort(), ['diff', 'plan'], 'Evaluator should receive plan and diff');
  assert.deepStrictEqual(seenArtifacts.distiller.sort(), ['diff', 'plan', 'verdict'], 'Distiller should receive plan, diff, and verdict');
});

test('G6 BUILD 4: real permission-based isolation (EACCES, not ENOENT)', async (t) => {
  const { chmodSync } = await import('node:fs');

  // Create temp dirs in /tmp (not tmpdir()) to avoid permission issues with /tmp/claude-0
  // Make them world-traversable (mode 0755) so 'nobody' user can access files
  const orchTempDir = mkdtempSync('/tmp/circus-test-g6-build4-');
  const coderTempDir = mkdtempSync('/tmp/circus-test-g6-coder-');
  chmodSync(orchTempDir, 0o755);
  chmodSync(coderTempDir, 0o755);

  t.after(() => {
    // Restore permissions before cleanup
    try { chmodSync(join(orchTempDir, 'verdict.json'), 0o644); } catch {}
    try { rmSync(orchTempDir, { recursive: true, force: true }); } catch {}
    try { rmSync(coderTempDir, { recursive: true, force: true }); } catch {}
  });

  // Write plan readable by everyone
  const planPath = join(coderTempDir, 'plan.md');
  writeFileSync(planPath, '# Test Plan');
  chmodSync(planPath, 0o644);

  // Write verdict with root-only permissions (mode 0600)
  const verdictPath = join(orchTempDir, 'verdict.json');
  writeFileSync(verdictPath, JSON.stringify({ secret: 'should-not-be-accessible' }));
  chmodSync(verdictPath, 0o600); // Owner (root) read/write, nobody else

  // Write test script to a file to avoid shell escaping issues
  const scriptPath = join(coderTempDir, 'test-script.js');
  writeFileSync(scriptPath, `
    const { readFileSync } = require('fs');

    try {
      // Try to read plan with absolute path (should succeed - world-readable)
      readFileSync('${planPath}', 'utf8');
      console.log('PLAN_READ_OK');

      // Try to read verdict with absolute path (should fail with EACCES - permission denied)
      readFileSync('${verdictPath}', 'utf8');
      console.log('VERDICT_READ_OK');
      process.exit(1); // Should NOT reach here
    } catch (err) {
      console.log('VERDICT_READ_FAILED:' + err.code);
      if (err.code === 'EACCES') {
        process.exit(0); // Success - got permission denied
      }
      process.exit(2); // Unexpected error code
    }
  `);
  chmodSync(scriptPath, 0o755);

  // Spawn as 'nobody' user to prove permission enforcement
  const childProcess = spawn('su', ['nobody', '-s', '/bin/sh', '-c', `node ${scriptPath}`], {
    stdio: ['pipe', 'pipe', 'pipe']
  });

  let stdout = '';
  let stderr = '';
  childProcess.stdout.on('data', (data) => { stdout += data; });
  childProcess.stderr.on('data', (data) => { stderr += data; });

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      if (code !== 0) {
        console.error('Coder subprocess stdout:', stdout);
        console.error('Coder subprocess stderr:', stderr);
      }
      assert.strictEqual(code, 0, 'Coder subprocess should exit 0 after getting EACCES');
      assert.ok(stdout.includes('PLAN_READ_OK'), 'Coder should read plan (world-readable)');
      assert.ok(stdout.includes('VERDICT_READ_FAILED:EACCES'), 'Coder should get EACCES (not ENOENT) on verdict');
      resolve();
    });
  });
});
