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
  assert.ok(flags[0].includes('file-exists'), 'Flag should mention the flipped check');
  assert.ok(flags[0].includes('FAIL→PASS'), 'Flag should indicate FAIL to PASS flip');
  assert.ok(flags[0].includes('possible stub'), 'Flag should warn about possible stub');
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
