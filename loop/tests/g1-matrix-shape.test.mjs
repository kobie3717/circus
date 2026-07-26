// G1: matrix-shape guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator, StubAdapter } from '../orchestrator.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

test('G1 negative: verdict with wrong check names is rejected', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

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
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const adapter = new StubAdapter(fixtureDir);
  const orchestrator = new Orchestrator(adapter, { fixtureDir });

  const result = await orchestrator.run('test task');

  assert.ok(result.plan, 'Should have plan artifact');
  assert.ok(result.verdict, 'Should have verdict artifact');

  const verdict = JSON.parse(result.verdict);
  assert.strictEqual(verdict.rows.length, 3, 'Should have 3 check rows');
});

test('G1 BUILD 2: plan missing mandatory check is rejected', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));

  const adapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan-missing-mandatory.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new Orchestrator(adapter, {
    fixtureDir,
    mandatoryChecks: ['test-greeting', 'lint-check']
  });

  await assert.rejects(
    async () => await orchestrator.run('test task'),
    /G1 violation.*plan missing mandatory checks.*test-greeting/,
    'Should reject plan that omits mandatory check'
  );
});

test('G1 BUILD 2: plan with superset of mandatory checks passes', async () => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const adapter = new StubAdapter(fixtureDir);
  const orchestrator = new Orchestrator(adapter, {
    fixtureDir,
    mandatoryChecks: ['test-greeting', 'lint-check']
  });

  const result = await orchestrator.run('test task');

  assert.ok(result.plan, 'Should accept plan with superset of mandatory checks');
  const plan = result.plan;
  assert.ok(plan.includes('test-greeting'), 'Plan should include test-greeting');
  assert.ok(plan.includes('type-check'), 'Plan can include extra checks beyond mandatory');
});
