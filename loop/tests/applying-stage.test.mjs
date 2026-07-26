// APPLYING stage tests — proves the worktree contract and PR factory
import { test } from 'node:test';
import assert from 'node:assert';
import { Orchestrator } from '../orchestrator.mjs';
import { writeFileSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { mkdtempSync, rmSync } from 'node:fs';
import { tempLoopDir } from './helpers/loop-dir.mjs';
import { tempGitRepo } from './helpers/git-repo.mjs';

const orchestratorPath = fileURLToPath(new URL('../orchestrator.mjs', import.meta.url));

// TestOrchestrator that mocks only network operations, runs real git
class ApplyingTestOrchestrator extends Orchestrator {
  constructor(adapter, options = {}) {
    super(adapter, options);
    this.mockCalls = { pushBranch: [], createPR: [], checkGhAuth: [] };
  }
  pushBranch(worktreePath, branchName) {
    this.mockCalls.pushBranch.push({ worktreePath, branchName });
  }
  createPR(worktreePath, taskId, taskDescription, branchName, baseBranch) {
    this.mockCalls.createPR.push({ worktreePath, taskId, taskDescription, branchName, baseBranch });
    return `https://github.com/test/repo/pull/${Math.floor(Math.random() * 1000)}`;
  }
  checkGhAuth() {
    this.mockCalls.checkGhAuth.push({});
  }
}

test('APPLYING positive: full path with real git ops + mocked network', async (t) => {
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() {
      return { artifact: '# Plan\n## Checks\n- [test]: echo ok' };
    },
    async code({ plan, worktreePath }) {
      // Write a known file with known content
      writeFileSync(join(worktreePath, 'feature.txt'), 'new feature content\n');
      writeFileSync(join(worktreePath, 'other.txt'), 'another file\n');
      return { artifact: null };
    },
    async evaluate({ plan, diff }) {
      return { artifact: JSON.stringify({ rows: [{ name: 'test', pass: true, evidence: 'ok' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new ApplyingTestOrchestrator(adapter, {
    loopDir: tempLoopDir(t),
    repoRoot,
    baseBranch
  });

  const result = await orchestrator.run({ id: 'FEAT-123', task: 'add new feature', mandatory_checks: [] });

  // Assert diff artifact contains both the file path and its content
  assert.ok(result.diff, 'Should have diff artifact');
  assert.ok(result.diff.includes('feature.txt'), 'Diff should contain feature.txt path');
  assert.ok(result.diff.includes('new feature content'), 'Diff should contain feature.txt content');
  assert.ok(result.diff.includes('other.txt'), 'Diff should contain other.txt path');
  assert.ok(result.diff.includes('another file'), 'Diff should contain other.txt content');

  // Assert branch was created
  const branchName = 'loop/feat-123';
  const branches = execFileSync('git', ['-C', repoRoot, 'branch', '--list', branchName], { encoding: 'utf8' });
  assert.ok(branches.includes(branchName), 'Branch should be created in the git repo');

  // Assert commit exists with correct message shape
  const commitMsg = execFileSync('git', ['-C', repoRoot, 'log', branchName, '-1', '--pretty=%B'], { encoding: 'utf8' });
  assert.ok(commitMsg.includes('Ticket: FEAT-123'), 'Commit should include ticket ID');
  assert.ok(commitMsg.includes('Co-Authored-By: Circus Loop Factory'), 'Commit should include co-author trailer');

  // Assert mocked push and PR were called
  assert.strictEqual(orchestrator.mockCalls.pushBranch.length, 1, 'pushBranch should be called once');
  assert.strictEqual(orchestrator.mockCalls.pushBranch[0].branchName, branchName);
  assert.strictEqual(orchestrator.mockCalls.createPR.length, 1, 'createPR should be called once');
  assert.strictEqual(orchestrator.mockCalls.createPR[0].taskId, 'FEAT-123');
  assert.strictEqual(orchestrator.mockCalls.createPR[0].branchName, branchName);
  assert.ok(result.prUrl, 'Should have PR URL in artifacts');
});

test('APPLYING negative: missing task.id throws synchronously', async (t) => {
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { artifact: '{}' }; },
    async distill() { return { artifact: 'feedback' }; }
  };

  const orchestrator = new ApplyingTestOrchestrator(adapter, { loopDir: tempLoopDir(t), repoRoot, baseBranch });

  // String task
  await assert.rejects(
    () => orchestrator.run('a plain string'),
    /Task must be an object with a non-empty "id"/,
    'Should reject plain string task'
  );

  // Object without id
  await assert.rejects(
    () => orchestrator.run({ task: 'no id field', mandatory_checks: [] }),
    /Task must be an object with a non-empty "id"/,
    'Should reject task object without id field'
  );

  // Assert no worktree was created (failure is early, before worktree creation)
  const worktreeList = execFileSync('git', ['-C', repoRoot, 'worktree', 'list'], { encoding: 'utf8' });
  const worktreeCount = worktreeList.split('\n').filter(l => l.trim()).length;
  assert.strictEqual(worktreeCount, 1, 'Should have only the main worktree (repoRoot itself), no test worktrees created');
});

test('APPLYING negative: worktree creation failure causes STOP', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-applying-wtfail-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot } = tempGitRepo(t);

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  // Spawn subprocess to capture STOP exit code
  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    const adapter = {
      async plan() { return { artifact: '# plan' }; },
      async code() { return { artifact: 'diff' }; },
      async evaluate() { throw new Error('unreachable'); },
      async distill() { throw new Error('unreachable'); }
    };
    // Invalid baseBranch that doesn't exist in the repo -> worktree creation fails
    const orch = new Orchestrator(adapter, {
      resumeHashPath: '${resumeHashPath}',
      repoRoot: '${repoRoot}',
      baseBranch: 'nonexistent-base-branch'
    });
    await orch.run({ id: 'TEST-001', task: 'test', mandatory_checks: [] });
  `]);

  let stderr = '';
  childProcess.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42 on worktree creation failure');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash on STOP');
      assert.ok(stderr.includes('Failed to create worktree'), 'Error should mention worktree failure');
      resolve();
    });
  });
});

test('APPLYING negative: push failure causes STOP and preserves worktree', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-applying-pushfail-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot, baseBranch } = tempGitRepo(t);

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    import { writeFileSync } from 'node:fs';
    import { join } from 'node:path';

    class FailingPushOrchestrator extends Orchestrator {
      pushBranch() {
        throw new Error('Mock push failure');
      }
      createPR() { return 'unreachable'; }
      checkGhAuth() {}
    }

    const adapter = {
      async plan() { return { artifact: '# Plan\\n## Checks' }; },
      async code({ plan, worktreePath }) {
        writeFileSync(join(worktreePath, 'file.txt'), 'content');
        return { artifact: null };
      },
      async evaluate() { return { artifact: JSON.stringify({ rows: [] }) }; },
      async distill() { return { artifact: 'feedback' }; }
    };

    const orch = new FailingPushOrchestrator(adapter, {
      resumeHashPath: '${resumeHashPath}',
      loopDir: '${tempDir}',
      repoRoot: '${repoRoot}',
      baseBranch: '${baseBranch}'
    });
    await orch.run({ id: 'PUSH-FAIL', task: 'test', mandatory_checks: [] });
  `]);

  let stderr = '';
  childProcess.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42 on push failure');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash');
      assert.ok(stderr.includes('APPLYING failed'), 'Error should mention APPLYING failure');
      assert.ok(stderr.includes('Mock push failure'), 'Error should include the specific failure reason');

      // Assert worktree directory still exists (preserved for manual recovery)
      const worktreePath = join(tempDir, 'worktrees', 'PUSH-FAIL');
      assert.ok(existsSync(worktreePath), 'Worktree should be preserved after push failure');
      assert.ok(existsSync(join(worktreePath, 'file.txt')), 'Files should still exist in preserved worktree');

      resolve();
    });
  });
});

test('APPLYING negative: gh auth failure causes STOP', async (t) => {
  const tempDir = mkdtempSync(join(tmpdir(), 'circus-test-applying-authfail-'));
  const resumeHashPath = join(tempDir, 'resume-hash');
  const { repoRoot, baseBranch } = tempGitRepo(t);

  t.after(() => rmSync(tempDir, { recursive: true, force: true }));

  const childProcess = spawn('node', ['-e', `
    import { Orchestrator } from '${orchestratorPath}';
    import { writeFileSync } from 'node:fs';
    import { join } from 'node:path';

    class FailingAuthOrchestrator extends Orchestrator {
      pushBranch() {}
      checkGhAuth() {
        throw new Error('gh auth status failed');
      }
      // NOT overriding createPR — the real createPR() calls this.checkGhAuth()
      // as its first line. Overriding createPR would bypass that call entirely
      // and the mocked checkGhAuth() throw would never fire.
    }

    const adapter = {
      async plan() { return { artifact: '# Plan\\n## Checks' }; },
      async code({ plan, worktreePath }) {
        writeFileSync(join(worktreePath, 'file.txt'), 'content');
        return { artifact: null };
      },
      async evaluate() { return { artifact: JSON.stringify({ rows: [] }) }; },
      async distill() { return { artifact: 'feedback' }; }
    };

    const orch = new FailingAuthOrchestrator(adapter, {
      resumeHashPath: '${resumeHashPath}',
      loopDir: '${tempDir}',
      repoRoot: '${repoRoot}',
      baseBranch: '${baseBranch}'
    });
    await orch.run({ id: 'AUTH-FAIL', task: 'test', mandatory_checks: [] });
  `]);

  let stderr = '';
  childProcess.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

  await new Promise((resolve) => {
    childProcess.on('exit', (code) => {
      assert.strictEqual(code, 42, 'Should exit with code 42 on auth failure');
      assert.ok(existsSync(resumeHashPath), 'Should write resume hash');
      resolve();
    });
  });
});

test('APPLYING: checks run against worktree, not orchestrator directory', async (t) => {
  const { repoRoot, baseBranch } = tempGitRepo(t);

  const adapter = {
    async plan() {
      return { artifact: '# Plan\n## Checks\n- [file-exists]: test -f marker.txt' };
    },
    async code({ plan, worktreePath }) {
      // Create marker file in worktree
      writeFileSync(join(worktreePath, 'marker.txt'), 'exists in worktree');
      return { artifact: null };
    },
    async evaluate({ plan, diff }) {
      // Evaluator will claim whatever; orchestrator overwrites with real exit codes
      return { artifact: JSON.stringify({ rows: [{ name: 'file-exists', pass: false, evidence: 'evaluator guess' }] }) };
    },
    async distill({ plan, diff, verdict }) {
      return { artifact: 'feedback' };
    }
  };

  const orchestrator = new ApplyingTestOrchestrator(adapter, {
    loopDir: tempLoopDir(t),
    repoRoot,
    baseBranch
  });

  const result = await orchestrator.run({ id: 'CHECK-CWD', task: 'test cwd', mandatory_checks: [] });

  // The check should PASS because marker.txt exists in the worktree
  const verdict = JSON.parse(result.verdict);
  const fileExistsRow = verdict.rows.find(r => r.name === 'file-exists');
  assert.ok(fileExistsRow, 'Should have file-exists check');
  assert.strictEqual(fileExistsRow.pass, true, 'Check should PASS because it ran in worktree where marker.txt exists');

  // Also verify marker.txt does NOT exist in repoRoot (proving the check didn't run there)
  assert.ok(!existsSync(join(repoRoot, 'marker.txt')), 'marker.txt should NOT exist in repoRoot');
});

test('APPLYING: base branch resolution with no origin fails with specific error', async (t) => {
  const { repoRoot } = tempGitRepo(t);

  const adapter = {
    async plan() { return { artifact: '# plan' }; },
    async code() { return { artifact: 'diff' }; },
    async evaluate() { return { artifact: '{}' }; },
    async distill() { return { artifact: 'feedback' }; }
  };

  // Don't pass baseBranch option, and repoRoot has no origin remote
  const orchestrator = new ApplyingTestOrchestrator(adapter, { loopDir: tempLoopDir(t), repoRoot });

  await assert.rejects(
    () => orchestrator.run({ id: 'NO-ORIGIN', task: 'test', mandatory_checks: [] }),
    // 's' flag: the real error message embeds git's own stderr (with newlines)
    // between the clauses this regex checks for.
    /Cannot resolve base branch.*origin\/HEAD unavailable.*pass options\.baseBranch explicitly/s,
    'Should fail with specific error about origin/HEAD and needing explicit baseBranch'
  );
});
