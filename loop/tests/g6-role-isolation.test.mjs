// G6: role-isolation guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { fileURLToPath } from 'node:url';

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
