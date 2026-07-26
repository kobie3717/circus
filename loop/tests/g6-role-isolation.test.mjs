// G6: role-isolation guard tests (BUILD 4: real orchestrator enforcement)
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { tempLoopDir } from './helpers/loop-dir.mjs';

test('G6 negative: role receiving disallowed artifacts fails', async (t) => {
  // Test that coder cannot receive verdict even if we try to inject it
  const adapter = {
    async plan({ task, feedback }) {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code({ plan, verdict }) {
      // If verdict is passed (shouldn't be), throw to indicate violation
      if (verdict !== undefined) {
        throw new Error('G6 violation: coder received disallowed artifact (verdict)');
      }
      return { artifact: 'diff content' };
    },
    async evaluate({ plan, diff }) {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(adapter, { loopDir: tempLoopDir(t) });

  // Manually inject verdict into artifacts before coder runs (simulating leak attempt)
  orchestrator.artifacts.verdict = 'leaked verdict';

  // Run should complete — callRole enforces isolation by only passing allowed artifacts
  const result = await orchestrator.run('test task');

  // Verify run completed (code adapter didn't receive verdict, so didn't throw)
  assert.ok(result.diff, 'Should complete without coder receiving verdict');
});

test('G6 positive: roles receive only allowed artifacts', async (t) => {
  const seenArtifacts = {
    coder: null,
    evaluator: null,
    distiller: null
  };

  const adapter = {
    async plan({ task, feedback }) {
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

  const orchestrator = new Orchestrator(adapter, { loopDir: tempLoopDir(t) });
  await orchestrator.run('test task');

  // Verify each role received only allowed artifacts (adapter interface enforcement)
  assert.deepStrictEqual(seenArtifacts.coder, ['plan'], 'Coder should receive only plan');
  assert.deepStrictEqual(seenArtifacts.evaluator.sort(), ['diff', 'plan'], 'Evaluator should receive plan and diff');
  assert.deepStrictEqual(seenArtifacts.distiller.sort(), ['diff', 'plan', 'verdict'], 'Distiller should receive plan, diff, and verdict');
});

test('G6 BUILD 4-A: temporal separation - coder exits before verdict written', async (t) => {
  // Drive REAL orchestrator and inspect its runState timestamps
  const adapter = {
    async plan({ task, feedback }) {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code({ plan }) {
      // Simulate some async work
      await new Promise(resolve => setTimeout(resolve, 10));
      return { artifact: 'diff content' };
    },
    async evaluate({ plan, diff }) {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(adapter, { loopDir: tempLoopDir(t) });
  await orchestrator.run('test task');

  // Verify orchestrator recorded real timestamps in runState
  assert.ok(orchestrator.runState.coderExitedAt, 'Orchestrator should record coder exit timestamp');
  assert.ok(orchestrator.runState.verdictWrittenAt, 'Orchestrator should record verdict write timestamp');

  // Verify temporal ordering: coder exited BEFORE verdict was written
  assert.ok(
    orchestrator.runState.coderExitedAt < orchestrator.runState.verdictWrittenAt,
    `Coder must exit (${orchestrator.runState.coderExitedAt}) before verdict written (${orchestrator.runState.verdictWrittenAt})`
  );
});

test('G6 BUILD 4-B: feedback projection - only whitelisted fields leak to coder', async (t) => {
  // Feed REAL orchestrator a verdict with sensitive fields, inspect REAL feedback output
  const sensitiveVerdict = {
    rows: [
      {
        name: 'test-check',
        pass: false,
        evidence: 'SENSITIVE: API key validation failed at line 42',
        privateData: 'internal probe result: XYZ',
        evaluatorCommentary: 'This suggests a deeper auth issue'
      },
      {
        name: 'lint-check',
        pass: true,
        evidence: 'linter output with file paths',
        internalNote: 'checked 47 files'
      }
    ],
    meta: {
      evaluatorVersion: '2.1',
      probeSummary: 'SENSITIVE INTERNAL DATA'
    }
  };

  const adapter = {
    async plan({ task, feedback }) {
      return { artifact: '# Plan\n## Checks\n- [test-check]: echo test\n- [lint-check]: echo lint' };
    },
    async code({ plan }) {
      return { artifact: 'diff content' };
    },
    async evaluate({ plan, diff }) {
      // Return verdict with extra sensitive fields
      return { artifact: JSON.stringify(sensitiveVerdict) };
    },
    async distill({ plan, diff, verdict }) {
      // Distiller returns whatever it wants — orchestrator should override with filtered version
      return { artifact: '# Distiller output\n\nThis should be replaced by orchestrator filtering.' };
    }
  };

  const orchestrator = new Orchestrator(adapter, { loopDir: tempLoopDir(t) });
  await orchestrator.run('test task');

  // Inspect REAL feedback that orchestrator produced
  const feedback = orchestrator.artifacts.feedback;

  // Verify feedback exists and contains only whitelisted fields
  assert.ok(feedback, 'Feedback should be generated');
  assert.ok(feedback.includes('test-check'), 'Feedback should include check name');
  assert.ok(feedback.includes('lint-check'), 'Feedback should include check name');
  assert.ok(feedback.includes('FAIL') || feedback.includes('PASS'), 'Feedback should include pass/fail status');

  // Verify NO sensitive fields leaked (orchestrator must filter these out)
  assert.ok(!feedback.includes('SENSITIVE'), 'Feedback must NOT contain sensitive evidence');
  assert.ok(!feedback.includes('API key'), 'Feedback must NOT contain private data from evidence');
  assert.ok(!feedback.includes('privateData'), 'Feedback must NOT contain privateData field');
  assert.ok(!feedback.includes('evaluatorCommentary'), 'Feedback must NOT contain evaluator commentary');
  assert.ok(!feedback.includes('probeSummary'), 'Feedback must NOT contain meta fields');
  assert.ok(!feedback.includes('line 42'), 'Feedback must NOT contain specific evidence details');
  assert.ok(!feedback.includes('Distiller output'), 'Feedback must be orchestrator-filtered, not raw distiller output');
});

test('G6 BUILD 4-C: manifest enforcement - coder invoked without manifest fails', async (t) => {
  // Test that REAL orchestrator enforces manifest presence/shape
  const adapter = {
    async plan({ task, feedback }) {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code({ plan }) {
      return { artifact: 'diff' };
    },
    async evaluate({ plan, diff }) {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  // Attempt to construct orchestrator without manifest (should fail)
  assert.throws(
    () => {
      new Orchestrator(adapter, { coderManifest: null });
    },
    /G6 violation: coder manifest missing or malformed/,
    'Orchestrator should refuse to construct without coder manifest'
  );

  // Attempt to construct with malformed manifest (should fail)
  assert.throws(
    () => {
      new Orchestrator(adapter, { coderManifest: { allowedTools: 'not-an-array' } });
    },
    /G6 violation: coder manifest.allowedTools must be an array/,
    'Orchestrator should refuse malformed manifest'
  );
});

test('G6 BUILD 4-D: manifest enforcement - Bash and network must be denied', async (t) => {
  // Test that REAL orchestrator enforces Bash/network denial in manifest
  const adapter = {
    async plan({ task, feedback }) {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code({ plan }) {
      return { artifact: 'diff' };
    },
    async evaluate({ plan, diff }) {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  // Attempt to construct with Bash allowed (should fail when coder is invoked)
  const orchestratorWithBash = new Orchestrator(adapter, {
    loopDir: tempLoopDir(t),
    coderManifest: {
      allowedTools: ['read', 'edit', 'write'],
      allowedPaths: ['/worktree/**'],
      allowBash: true, // VIOLATION
      allowNetwork: false
    }
  });

  await assert.rejects(
    async () => {
      await orchestratorWithBash.run('test task');
    },
    /G6 violation: coder manifest must not allow Bash execution/,
    'Orchestrator should reject coder invocation with Bash allowed'
  );

  // Attempt to construct with network allowed (should fail when coder is invoked)
  const orchestratorWithNetwork = new Orchestrator(adapter, {
    loopDir: tempLoopDir(t),
    coderManifest: {
      allowedTools: ['read', 'edit', 'write'],
      allowedPaths: ['/worktree/**'],
      allowBash: false,
      allowNetwork: true // VIOLATION
    }
  });

  await assert.rejects(
    async () => {
      await orchestratorWithNetwork.run('test task');
    },
    /G6 violation: coder manifest must not allow network access/,
    'Orchestrator should reject coder invocation with network allowed'
  );
});
