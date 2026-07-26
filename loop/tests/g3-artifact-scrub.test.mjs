// G3: artifact-scrub guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync } from 'node:fs';

test('G3 negative: artifact with secret is rejected', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

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

  const orchestrator = new Orchestrator(secretAdapter, { fixtureDir });

  await assert.rejects(
    async () => await orchestrator.run('test task'),
    /G3 violation.*contains secrets/,
    'Should reject artifact containing secrets'
  );
});

test('G3 positive: clean artifact passes scrub', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  const cleanAdapter = {
    async plan() {
      return { artifact: 'Clean plan with no secrets, just regular text' };
    },
    async code() {
      return { artifact: 'diff content' };
    },
    async evaluate() {
      return { artifact: JSON.stringify({ rows: [] }) };
    },
    async distill() {
      return { artifact: 'Clean feedback' };
    }
  };

  const orchestrator = new Orchestrator(cleanAdapter, { fixtureDir });
  const result = await orchestrator.run('test task');

  assert.ok(result.plan, 'Should have plan artifact after scrub');
  assert.strictEqual(result.plan, 'Clean plan with no secrets, just regular text');
});

test('G3 BUILD 2: scrubber works without /root/bot-circus dependency', async () => {
  // Import the vendored scrubber directly to prove it's self-contained
  const { scrubEgress } = await import('../lib/scrub.mjs');

  // Test that it detects secrets correctly
  const textWithSecret = 'api_key: sk-1234567890abcdef';
  const result = scrubEgress(textWithSecret);

  assert.strictEqual(result.hits, 1, 'Should detect 1 secret');
  assert.ok(result.text.includes('[REDACTED]'), 'Should redact the secret');
  assert.ok(!result.text.includes('sk-1234567890abcdef'), 'Should not contain raw secret');

  // Verify the module doesn't import from /root/bot-circus
  const { readFileSync } = await import('node:fs');
  const scrubSource = readFileSync('/root/circus/loop/lib/scrub.mjs', 'utf8');
  // Check there's no import statement with /root/bot-circus path
  const hasImportFromBotCircus = /import\s+.*from\s+['"]\/root\/bot-circus/.test(scrubSource);
  assert.ok(!hasImportFromBotCircus,
    'Vendored scrub.mjs must not import from /root/bot-circus (provenance comments OK)');
});
