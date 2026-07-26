// loop/tests/helpers/mock-network.mjs
//
// TestOrchestrator subclass that mocks network-leaving operations
// (pushBranch, createPR, checkGhAuth) so tests don't attempt real pushes
// or PR creations. All git operations (worktree creation, commit, diff
// extraction) run against the real (but test-injected) git repo — only
// the final push and gh pr create are mocked.

import { Orchestrator } from '../../orchestrator.mjs';

export class TestOrchestrator extends Orchestrator {
  constructor(adapter, options = {}) {
    super(adapter, options);
    // Track calls for assertion
    this.mockCalls = {
      pushBranch: [],
      createPR: [],
      checkGhAuth: []
    };
  }

  pushBranch(worktreePath, branchName) {
    // Mock: record call but don't actually push
    this.mockCalls.pushBranch.push({ worktreePath, branchName });
  }

  createPR(worktreePath, taskId, taskDescription, branchName, baseBranch) {
    // Mock: record call and return fake PR URL
    this.mockCalls.createPR.push({ worktreePath, taskId, taskDescription, branchName, baseBranch });
    return `https://github.com/test/repo/pull/${Math.floor(Math.random() * 1000)}`;
  }

  checkGhAuth() {
    // Mock: always succeed
    this.mockCalls.checkGhAuth.push({});
  }
}
