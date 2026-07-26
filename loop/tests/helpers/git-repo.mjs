// loop/tests/helpers/git-repo.mjs
//
// The Orchestrator's worktree/PR operations (createWorktree, pushBranch,
// createPR) default to operating on the real repo at process.cwd() —
// production behavior where the codebase we're running in is the codebase
// we're changing. But a test running those operations against /root/circus
// itself will create real branches, real worktrees, and attempt real pushes
// to the live repo — exactly the defect that created ~22 junk branches
// earlier tonight when a test suite ran git ops directly.
//
// Every test that drives run() (which now creates worktrees) must inject its
// own isolated tmpGitRepo — a fresh git init with a base commit, so
// worktrees have a valid tree to branch from. Same pattern as tempLoopDir:
// mkdtemp, register cleanup via t.after, assert it didn't somehow resolve to
// the project root before handing it back.

import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

/**
 * Create a fresh isolated git repository for a test, with:
 * - git init
 * - user.email/name configured (required for commits)
 * - one committed file (so worktrees have a base tree to branch from)
 * - a 'main' branch (the baseBranch tests will pass explicitly)
 *
 * Returns { repoRoot, baseBranch }. Registers cleanup via t.after.
 */
export function tempGitRepo(t) {
  const repoRoot = mkdtempSync(join(tmpdir(), 'circus-git-'));
  assertNotProjectRoot(repoRoot);

  // Initialize git repo
  execFileSync('git', ['init'], { cwd: repoRoot, stdio: 'pipe' });
  execFileSync('git', ['config', 'user.email', 'test@circus.local'], { cwd: repoRoot, stdio: 'pipe' });
  execFileSync('git', ['config', 'user.name', 'Circus Test'], { cwd: repoRoot, stdio: 'pipe' });
  execFileSync('git', ['checkout', '-b', 'main'], { cwd: repoRoot, stdio: 'pipe' });

  // Create and commit a base file (worktrees need a non-empty tree)
  const baseFile = join(repoRoot, 'README.md');
  writeFileSync(baseFile, '# Test Repo\n');
  execFileSync('git', ['add', 'README.md'], { cwd: repoRoot, stdio: 'pipe' });
  execFileSync('git', ['commit', '-m', 'Initial commit'], { cwd: repoRoot, stdio: 'pipe' });

  if (t && typeof t.after === 'function') {
    t.after(() => rmSync(repoRoot, { recursive: true, force: true }));
  }

  return { repoRoot, baseBranch: 'main' };
}

/**
 * Throws if repoRoot resolves to (or inside) the real project directory.
 * Same guard as tempLoopDir's assertNotProjectRoot, but for the git root
 * rather than .loop state — tests must not run worktree/push operations
 * against the live /root/circus repo.
 */
export function assertNotProjectRoot(repoRoot) {
  const resolved = resolve(repoRoot);
  const projectRoot = resolve(process.cwd());
  if (resolved === projectRoot || resolved.startsWith(projectRoot + '/')) {
    throw new Error(
      `Test resolved git root to the project root (${resolved}) — ` +
      `inject an isolated tempGitRepo() instead.`
    );
  }
}
