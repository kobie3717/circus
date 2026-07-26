// G3: artifact-scrub guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { tempLoopDir } from './helpers/loop-dir.mjs';
import { tempGitRepo } from './helpers/git-repo.mjs';
import { TestOrchestrator } from './helpers/mock-network.mjs';

test('G3 negative: artifact with secret is rejected', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  // Adapter that returns artifact with secret
  const secretAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/artifact-with-secret.md`, 'utf8') };
    },
    async code() {
      return { artifact: 'diff' };
    },
    async evaluate() {
      return { artifact: '{}' };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new TestOrchestrator(secretAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });

  await assert.rejects(
    async () => await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] }),
    /G3 violation.*contains secrets/,
    'Should reject artifact containing secrets'
  );
});

test('G3 positive: clean artifact passes scrub', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const cleanAdapter = {
    async plan() {
      return { artifact: 'Clean plan with no secrets, just regular text' };
    },
    async code({ plan, worktreePath }) {
      writeFileSync(join(worktreePath, 'change.txt'), 'diff content');
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: JSON.stringify({ rows: [] }) };
    },
    async distill() {
      return { artifact: 'Clean feedback' };
    }
  };

  const orchestrator = new TestOrchestrator(cleanAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });
  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });

  assert.ok(result.plan, 'Should have plan artifact after scrub');
  assert.strictEqual(result.plan, 'Clean plan with no secrets, just regular text');
});

test('G3 BUILD 2: scrubber works as vendored copy without external dependency', async () => {
  // Import the vendored scrubber directly to prove it's self-contained
  const { scrubEgress } = await import('../lib/scrub.mjs');

  // Test that it detects secrets correctly
  const textWithSecret = 'api_key: sk-1234567890abcdef';
  const result = scrubEgress(textWithSecret);

  assert.strictEqual(result.hits, 1, 'Should detect 1 secret');
  assert.ok(result.text.includes('[REDACTED]'), 'Should redact the secret');
  assert.ok(!result.text.includes('sk-1234567890abcdef'), 'Should not contain raw secret');

  // Verify the vendored module doesn't import from external bot-circus path
  const { readFileSync } = await import('node:fs');
  const scrubPath = fileURLToPath(new URL('../lib/scrub.mjs', import.meta.url));
  const scrubSource = readFileSync(scrubPath, 'utf8');
  // Check there's no import statement with bot-circus path
  const hasImportFromBotCircus = /import\s+.*from\s+['"].*bot-circus/.test(scrubSource);
  assert.ok(!hasImportFromBotCircus,
    'Vendored scrub.mjs must not import from bot-circus (provenance comments OK)');
});
