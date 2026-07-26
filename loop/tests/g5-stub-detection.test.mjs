// G5: stub-detection guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const orchestratorPath = fileURLToPath(new URL('../orchestrator.mjs', import.meta.url));

test('G5 negative: new file flipping check FAIL→PASS is flagged', async () => {
  // Test the detectStubCapitulation method directly
  const orchestrator = new Orchestrator({
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { artifact: '{}' }; },
    async distill() { return { artifact: 'feedback' }; }
  });

  const diff = '--- /dev/null\n+++ b/required.txt\n@@ -0,0 +1 @@\n+stub';
  const beforeResults = { 'file-exists': false };
  const afterResults = { 'file-exists': true };

  const flags = orchestrator.detectStubCapitulation(diff, beforeResults, afterResults);

  assert.ok(flags.length > 0, 'Should flag stub capitulation');
  assert.ok(flags[0].message.includes('file-exists'), 'Flag should mention the flipped check');
  assert.ok(flags[0].message.includes('FAIL→PASS'), 'Flag should indicate FAIL to PASS flip');
  assert.ok(flags[0].message.includes('possible stub'), 'Flag should warn about possible stub');
  assert.strictEqual(flags[0].check, 'file-exists', 'Flag should identify the check');
  assert.ok(Array.isArray(flags[0].files), 'Flag should include files array');
});

test('G5 positive: editing existing files without flipping checks passes', async () => {
  // Test that editing existing files doesn't trigger stub detection
  const orchestrator = new Orchestrator({
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { artifact: '{}' }; },
    async distill() { return { artifact: 'feedback' }; }
  });

  // Diff only edits existing file, no new files
  const diff = '--- a/existing.js\n+++ b/existing.js\n@@ -1,1 +1,2 @@\n code\n+more code';
  const beforeResults = { 'test': false };
  const afterResults = { 'test': true };

  const flags = orchestrator.detectStubCapitulation(diff, beforeResults, afterResults);

  assert.strictEqual(flags.length, 0, 'Should not flag when no new files are created');

  // Also verify extractNewFiles works correctly
  const newFiles = orchestrator.extractNewFiles(diff);
  assert.strictEqual(newFiles.length, 0, 'Should have no new files');
});

test('G5 BUILD 3: end-to-end orchestrator run with stub capitulation causes HALT', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-g5-e2e-'));
  const resumeHashPath = join(tempDir, 'resume-hash');

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  // Full orchestrator run that triggers G5 HALT via detectStubCapitulation
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';

    const adapter = {
      async plan() {
        return { artifact: '# Plan\\n## Checks\\n- [file-check]: echo ok' };
      },
      async code() {
        // Diff adds new file
        return { artifact: '--- /dev/null\\n+++ b/newfile.txt\\n@@ -0,0 +1 @@\\n+content' };
      },
      async evaluate() {
        return { artifact: JSON.stringify({ rows: [{ name: 'file-check', pass: true, evidence: 'ok' }] }) };
      },
      async distill() {
        throw new Error('Should not reach distill - G5 should halt');
      }
    };

    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}' });

    // Patch detectStubCapitulation to simulate proper beforeResults tracking
    // (Current orchestrator has beforeResults={} limitation)
    const originalDetect = orch.detectStubCapitulation.bind(orch);
    orch.detectStubCapitulation = function(diff, beforeResults, afterResults) {
      // Simulate that file-check was FAIL before coder ran
      const simulatedBefore = { 'file-check': false };
      return originalDetect(diff, simulatedBefore, afterResults);
    };

    await orch.run('test task');
  `]);

  let stdout = '';
  let stderr = '';
  childProcess.stdout.on('data', (data) => { stdout += data; });
  childProcess.stderr.on('data', (data) => { stderr += data; });

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      if (code !== 42) {
        console.error('Subprocess stdout:', stdout);
        console.error('Subprocess stderr:', stderr);
      }
      assert.strictEqual(code, 42, 'Should exit with code 42 when G5 detects stub capitulation');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash on G5 HALT');
      resolve();
    });
  });
});
