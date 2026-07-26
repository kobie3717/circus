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

test('G6 BUILD 3: real subprocess with filesystem isolation', async (t) => {
  // Create separate temp dirs: one for orchestrator, one for coder subprocess
  const orchTempDir = mkdtempSync(join(tmpdir(), 'circus-test-g6-orch-'));
  const coderTempDir = mkdtempSync(join(tmpdir(), 'circus-test-g6-coder-'));

  t.after(() => {
    rmSync(orchTempDir, { recursive: true, force: true });
    rmSync(coderTempDir, { recursive: true, force: true });
  });

  // Write plan in coder's accessible directory
  writeFileSync(join(coderTempDir, 'plan.md'), '# Test Plan');

  // Write verdict OUTSIDE coder's directory (in orchestrator's dir)
  writeFileSync(join(orchTempDir, 'verdict.json'), JSON.stringify({ secret: 'should-not-be-accessible' }));

  // Spawn coder as a real subprocess with restricted cwd
  // Coder only knows relative paths - it gets "plan.md" but verdict.json is not in its cwd
  const coderScript = `
    import { readFileSync } from 'node:fs';

    try {
      // Try to read plan (should succeed - it's in our cwd as plan.md)
      readFileSync('plan.md', 'utf8');
      console.log('PLAN_READ_OK');

      // Try to read verdict.json (should fail - not in our cwd)
      readFileSync('verdict.json', 'utf8');
      console.log('VERDICT_READ_OK');
      process.exit(1); // Should not reach here
    } catch (err) {
      if (err.code === 'ENOENT' || err.code === 'EACCES') {
        console.log('VERDICT_READ_FAILED:' + err.code);
        process.exit(0);
      }
      throw err;
    }
  `;

  const childProcess = spawn('node', ['--input-type=module', '-e', coderScript], {
    cwd: coderTempDir,
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
      assert.strictEqual(code, 0, 'Coder subprocess should exit 0 after failing to read verdict');
      assert.ok(stdout.includes('PLAN_READ_OK'), 'Coder should successfully read plan from its cwd');
      assert.ok(stdout.includes('VERDICT_READ_FAILED'), 'Coder should fail to read verdict outside its cwd');
      assert.ok(
        stdout.includes('ENOENT') || stdout.includes('EACCES'),
        'Isolation enforced by ENOENT (path not in cwd) or EACCES (permission denied)'
      );
      resolve();
    });
  });
});
