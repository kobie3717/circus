// G2: stop-is-terminal guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, existsSync, unlinkSync } from 'node:fs';
import { spawn } from 'node:child_process';

test('G2 negative: STOP signal exits with code 42 and writes resume token', async (t) => {
  const resumePath = '.loop/resume-token-test-g2';

  // Clean up any existing token
  if (existsSync(resumePath)) {
    unlinkSync(resumePath);
  }

  const fixtureDir = '/root/circus/loop/fixtures';

  // Create adapter that signals STOP at evaluator stage
  const stopAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code() {
      return { artifact: readFileSync(`${fixtureDir}/diff`, 'utf8') };
    },
    async evaluate() {
      return { signal: 'STOP', reason: 'test stop signal' };
    },
    async distill() {
      throw new Error('Should not reach distill after STOP');
    }
  };

  const orchestrator = new Orchestrator(stopAdapter, {
    fixtureDir,
    resumeTokenPath: resumePath
  });

  // Run in subprocess to capture exit code
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '/root/circus/loop/orchestrator.mjs';
    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code() { return { artifact: 'diff' }; },
      async evaluate() { return { signal: 'STOP', reason: 'test' }; },
      async distill() { throw new Error('unreachable'); }
    };
    const orch = new Orchestrator(adapter, { resumeTokenPath: '${resumePath}' });
    await orch.run('test');
  `]);

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42');
      assert.ok(existsSync(resumePath), 'Should write resume token');
      resolve();
    });
  });
});

test('G2 positive: normal flow completes without STOP', async () => {
  const fixtureDir = '/root/circus/loop/fixtures';

  const normalAdapter = {
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

  const orchestrator = new Orchestrator(normalAdapter, { fixtureDir });
  const result = await orchestrator.run('test task');

  assert.ok(result.feedback, 'Should complete and return feedback');
});
