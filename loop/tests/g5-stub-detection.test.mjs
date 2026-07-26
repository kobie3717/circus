// G5: stub-detection guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';

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

test('G5 BUILD 2: handleStubDetectionFlags causes HALT with exit code 42', async () => {
  // Test that handleStubDetectionFlags actually halts the process
  const { spawn } = await import('node:child_process');
  const { existsSync, unlinkSync } = await import('node:fs');

  const resumeHashPath = '.loop/resume-hash-test-g5-build2';
  if (existsSync(resumeHashPath)) {
    unlinkSync(resumeHashPath);
  }

  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '/root/circus/loop/orchestrator.mjs';

    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code() { return { artifact: 'diff' }; },
      async evaluate() { return { artifact: '{}' }; },
      async distill() { return { artifact: 'feedback' }; }
    };

    const orch = new Orchestrator(adapter, { resumeHashPath: '${resumeHashPath}' });

    // Call handleStubDetectionFlags with a flag - should cause HALT
    const flags = [{
      check: 'test-check',
      files: ['new-file.txt'],
      message: 'Check test-check flipped FAIL→PASS via new file(s): new-file.txt — possible stub'
    }];

    orch.handleStubDetectionFlags(flags);

    // Should not reach here
    console.log('ERROR: Did not halt');
    process.exit(1);
  `]);

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42 when stub flags are handled');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash on G5 HALT');

      // Clean up
      if (existsSync(resumeHashPath)) {
        unlinkSync(resumeHashPath);
      }
      resolve();
    });
  });
});
