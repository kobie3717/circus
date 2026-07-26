// G4: harness-integrity guard tests
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { tempLoopDir } from './helpers/loop-dir.mjs';
import { tempGitRepo } from './helpers/git-repo.mjs';
import { TestOrchestrator } from './helpers/mock-network.mjs';

test('G4 negative: substituted command in verdict is rejected', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const substitutedAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-substituted.json`, 'utf8') };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new TestOrchestrator(substitutedAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });

  await assert.rejects(
    async () => await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] }),
    /G4 violation.*substituted command/,
    'Should reject verdict with command substitution'
  );
});

test('G4 positive: verbatim command execution passes', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const verbatimAdapter = {
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

  const orchestrator = new TestOrchestrator(verbatimAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });
  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });

  assert.ok(result.verdict, 'Should complete with valid verdict');
  const verdict = JSON.parse(result.verdict);
  assert.ok(!verdict.rows.some(r => r.evidence.includes('SUBSTITUTED')));
});

test('G4 BUILD 2: evaluator false PASS claim is overridden by real exit code', async (t) => {
  const fixtureDir = fileURLToPath(new URL('../fixtures/', import.meta.url));
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const falsePassAdapter = {
    async plan() {
      return { artifact: readFileSync(`${fixtureDir}/plan-always-fails.md`, 'utf8') };
    },
    async code({ plan, worktreePath }) {
      const diffContent = readFileSync(`${fixtureDir}/diff`, 'utf8');
      writeFileSync(join(worktreePath, 'change.txt'), diffContent);
      return { artifact: null };
    },
    async evaluate() {
      return { artifact: readFileSync(`${fixtureDir}/verdict-false-pass.json`, 'utf8') };
    },
    async distill() {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new TestOrchestrator(falsePassAdapter, { fixtureDir, loopDir: tempLoopDir(t), repoRoot, baseBranch });
  const result = await orchestrator.run({ id: 'TEST-001', task: 'test task', mandatory_checks: [] });

  // The evaluator claimed PASS, but orchestrator should override with real FAIL
  const verdict = JSON.parse(result.verdict);
  const alwaysFailsRow = verdict.rows.find(r => r.name === 'always-fails');

  assert.ok(alwaysFailsRow, 'Should have always-fails check');
  assert.strictEqual(alwaysFailsRow.pass, false,
    'Orchestrator should override evaluator false PASS with real FAIL from exit code 1');
});
