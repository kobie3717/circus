// G4: harness-integrity guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync } from 'node:fs';

test('G4 negative: substituted command in verdict is rejected', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  const substitutedAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-substituted.json`, 'utf8') };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(substitutedAdapter, { fixtureDir });

  await assert.rejects(
    async () => await orchestrator.run('test task'),
    /G4 violation.*substituted command/,
    'Should reject verdict with command substitution'
  );
});

test('G4 positive: verbatim command execution passes', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  const verbatimAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
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

  const orchestrator = new Orchestrator(verbatimAdapter, { fixtureDir });
  const result = await orchestrator.run('test task');

  assert.ok(result.verdict, 'Should complete with valid verdict');
  const verdict = JSON.parse(result.verdict);
  assert.ok(!verdict.rows.some(r => r.evidence.includes('SUBSTITUTED')));
});

test('G4 BUILD 2: evaluator false PASS claim is overridden by real exit code', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  const falsePassAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan-always-fails.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-false-pass.json`, 'utf8') };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(falsePassAdapter, { fixtureDir });
  const result = await orchestrator.run('test task');

  // The evaluator claimed PASS, but orchestrator should override with real FAIL
  const verdict = JSON.parse(result.verdict);
  const alwaysFailsRow = verdict.rows.find(r => r.name === 'always-fails');

  assert.ok(alwaysFailsRow, 'Should have always-fails check');
  assert.strictEqual(alwaysFailsRow.pass, false,
    'Orchestrator should override evaluator false PASS with real FAIL from exit code 1');
});
