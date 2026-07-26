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
