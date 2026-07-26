// G1: matrix-shape guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator, StubAdapter } from '../orchestrator.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { tempLoopDir } from './helpers/loop-dir.mjs';
import { tempGitRepo } from './helpers/git-repo.mjs';
import { TestOrchestrator } from './helpers/mock-network.mjs';

test('G1 negative: verdict with wrong check names is rejected', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  // Create a custom adapter that returns bad verdict
  const badAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-bad-matrix.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new TestOrchestrator(badAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });

  await assert.rejects(
    async () => await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] }),
    /G1 violation.*verdict row names don't match/,
    'Should reject verdict with mismatched check names'
  );
});

test('G1 positive: verdict with correct check names passes', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new TestOrchestrator(adapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });

  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });

  assert.ok(result.plan, 'Should have plan artifact');
  assert.ok(result.verdict, 'Should have verdict artifact');

  const verdict = JSON.parse(result.verdict);
  assert.strictEqual(verdict.rows.length, 3, 'Should have 3 check rows');
});

test('G1 BUILD 2: plan missing mandatory check is rejected', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan-missing-mandatory.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new TestOrchestrator(adapter, {
    fixtureDir,
    loopDir: tempLoopDir(t),
    repoRoot,
    baseBranch,
    mandatoryChecks: ['test-greeting', 'lint-check']
  });

  await assert.rejects(
    async () => await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: ['test-greeting', 'lint-check'] }),
    /G1 violation.*plan missing mandatory checks.*test-greeting/,
    'Should reject plan that omits mandatory check'
  );
});

test('G1 BUILD 2: plan with superset of mandatory checks passes', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict.json`, 'utf8') };
    },
    async distill() {
      return { artifact: readFileSync(`${fixtureDir}/feedback.md`, 'utf8') };
    }
  };

  const orchestrator = new TestOrchestrator(adapter, {
    fixtureDir,
    loopDir: tempLoopDir(t),
    repoRoot,
    baseBranch,
    mandatoryChecks: ['test-greeting', 'lint-check']
  });

  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: ['test-greeting', 'lint-check'] });

  assert.ok(result.plan, 'Should accept plan with superset of mandatory checks');
  const plan = result.plan;
  assert.ok(plan.includes('test-greeting'), 'Plan should include test-greeting');
  assert.ok(plan.includes('type-check'), 'Plan can include extra checks beyond mandatory');
});
