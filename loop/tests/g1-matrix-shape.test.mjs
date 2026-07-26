// G1: matrix-shape guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator, StubAdapter } from '../orchestrator.mjs';
import { readFileSync, writeFileSync } from 'node:fs';

test('G1 negative: verdict with wrong check names is rejected', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  // Create a custom adapter that returns bad verdict
  const badAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-bad-matrix.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new Orchestrator(badAdapter, { fixtureDir });

  await assert.rejects(
    async () => await orchestrator.run('test task'),
    /G1 violation.*verdict row names don't match/,
    'Should reject verdict with mismatched check names'
  );
});

test('G1 positive: verdict with correct check names passes', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';
  const adapter = new StubAdapter(fixtureDir);
  const orchestrator = new Orchestrator(adapter, { fixtureDir });

  const result = await orchestrator.run('test task');

  assert.ok(result.plan, 'Should have plan artifact');
  assert.ok(result.verdict, 'Should have verdict artifact');

  const verdict = JSON.parse(result.verdict);
  assert.strictEqual(verdict.rows.length, 3, 'Should have 3 check rows');
});
