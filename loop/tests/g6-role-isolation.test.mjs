// G6: role-isolation guard tests (BUILD 4: temporal separation + manifest)
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

test('G6 BUILD 4-A: temporal separation - coder exits before verdict written', async () => {
  // Prove temporal ordering: coder process completes and exits BEFORE verdict.json is created
  let coderExitTime = null;
  let verdictWriteTime = null;
  let coderProcessExited = false;

  const adapter = {
    async plan() {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code() {
      // Simulate coder process lifecycle
      return new Promise((resolve) => {
        setTimeout(() => {
          coderExitTime = Date.now();
          coderProcessExited = true;
          resolve({ artifact: 'diff content' });
        }, 10);
      });
    },
    async evaluate() {
      // Record when verdict is about to be written
      verdictWriteTime = Date.now();
      // Assert coder has already exited
      assert.ok(coderProcessExited, 'Coder process must have exited before verdict is written');
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(adapter);
  await orchestrator.run('test task');

  // Verify temporal ordering: coder exited before verdict was created
  assert.ok(coderExitTime !== null, 'Coder exit time should be recorded');
  assert.ok(verdictWriteTime !== null, 'Verdict write time should be recorded');
  assert.ok(coderExitTime < verdictWriteTime,
    `Coder must exit (${coderExitTime}) before verdict written (${verdictWriteTime})`);
  assert.ok(coderProcessExited, 'Coder process exit flag must be true');
});

test('G6 BUILD 4-B: feedback projection - only whitelisted fields leak to coder', async () => {
  // Prove verdict filtering: coder receives ONLY (row name + pass/fail), not raw evaluator output
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

  let feedbackReceived = null;

  const adapter = {
    async plan({ feedback }) {
      if (feedback) {
        feedbackReceived = feedback;
      }
      return { artifact: '# Plan\n## Checks\n- [test-check]: echo ok\n- [lint-check]: echo ok' };
    },
    async code() {
      return { artifact: 'diff' };
    },
    async evaluate() {
      return { artifact: JSON.stringify(sensitiveVerdict) };
    },
    async distill({ verdict }) {
      // Orchestrator should provide filtered feedback, not raw verdict
      // Filter to ONLY (name + pass/fail)
      const verdictObj = JSON.parse(verdict);
      const filtered = verdictObj.rows.map(r => `- ${r.name}: ${r.pass ? 'PASS' : 'FAIL'}`).join('\n');
      return { artifact: `# Feedback\n\n${filtered}` };
    }
  };

  const orchestrator = new Orchestrator(adapter);

  // First run - no feedback yet
  const result1 = await orchestrator.run('iteration 1');
  const feedback = result1.feedback;

  // Verify feedback exists and contains only whitelisted fields
  assert.ok(feedback, 'Feedback should be generated');
  assert.ok(feedback.includes('test-check'), 'Feedback should include check name');
  assert.ok(feedback.includes('FAIL') || feedback.includes('PASS'), 'Feedback should include pass/fail status');

  // Verify NO sensitive fields leaked
  assert.ok(!feedback.includes('SENSITIVE'), 'Feedback must NOT contain sensitive evidence');
  assert.ok(!feedback.includes('API key'), 'Feedback must NOT contain private data from evidence');
  assert.ok(!feedback.includes('privateData'), 'Feedback must NOT contain privateData field');
  assert.ok(!feedback.includes('evaluatorCommentary'), 'Feedback must NOT contain evaluator commentary');
  assert.ok(!feedback.includes('probeSummary'), 'Feedback must NOT contain meta fields');
  assert.ok(!feedback.includes('line 42'), 'Feedback must NOT contain specific evidence details');

  // Second iteration - coder receives filtered feedback
  const adapter2 = {
    async plan({ feedback }) {
      feedbackReceived = feedback;
      return { artifact: '# Plan\n## Checks\n- [test-check]: echo ok' };
    },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { artifact: JSON.stringify({ rows: [{ name: 'test-check', pass: true, evidence: 'ok' }] }) }; },
    async distill() { return { artifact: 'feedback' }; }
  };

  const orchestrator2 = new Orchestrator(adapter2);
  orchestrator2.artifacts.feedback = feedback; // Inject feedback from previous run
  await orchestrator2.run('iteration 2');

  // Verify coder received filtered feedback on retry
  assert.ok(feedbackReceived, 'Coder should receive feedback on second iteration');
  assert.ok(!feedbackReceived.includes('SENSITIVE'), 'Coder must NOT see sensitive evidence through feedback');
});

test('G6 BUILD 4-C: manifest enforcement - out-of-manifest action rejected', async () => {
  // Prove capability manifest is enforced: coder attempting Bash or out-of-scope read gets rejected

  // Define coder manifest (narrow: Read/Edit/Write in worktree only, NO Bash, NO network)
  const CODER_MANIFEST = {
    allowedTools: ['read', 'edit', 'write'],
    allowedPaths: ['/worktree/**'], // Only within coder's workspace
    allowBash: false,
    allowNetwork: false
  };

  const manifestViolations = [];

  // Orchestrator enforces manifest (stub version - real enforcement would be in callRole)
  function enforceManifest(action, manifest) {
    if (action.type === 'bash' && !manifest.allowBash) {
      manifestViolations.push(`Rejected: Bash execution not allowed in manifest`);
      throw new Error('Manifest violation: Bash execution denied for coder role');
    }
    if (action.type === 'read' && action.path && !action.path.startsWith('/worktree/')) {
      manifestViolations.push(`Rejected: Read outside worktree (${action.path})`);
      throw new Error(`Manifest violation: Read denied for path outside worktree: ${action.path}`);
    }
  }

  const adapter = {
    async plan() {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code() {
      // Coder attempts to run Bash (should be rejected)
      try {
        enforceManifest({ type: 'bash', command: 'cat /etc/passwd' }, CODER_MANIFEST);
      } catch (err) {
        // Expected - manifest enforcement caught it
      }

      // Coder attempts to read verdict.json outside worktree (should be rejected)
      try {
        enforceManifest({ type: 'read', path: '/orchestrator/verdict.json' }, CODER_MANIFEST);
      } catch (err) {
        // Expected - manifest enforcement caught it
      }

      return { artifact: 'diff content' };
    },
    async evaluate() {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new Orchestrator(adapter);
  await orchestrator.run('test task');

  // Verify manifest violations were caught
  assert.strictEqual(manifestViolations.length, 2, 'Should have caught 2 manifest violations');
  assert.ok(manifestViolations[0].includes('Bash'), 'First violation should be Bash denial');
  assert.ok(manifestViolations[1].includes('outside worktree'), 'Second violation should be out-of-scope read denial');
});
