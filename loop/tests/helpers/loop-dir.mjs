// loop/tests/helpers/loop-dir.mjs
//
// The Orchestrator's .loop/ state (resume-hash today; worktrees/checkpoints
// later) defaults to the repo root — correct in production, wrong in every
// test. A test that doesn't inject its own loopDir silently reads/writes the
// real repo's .loop/ directory: it inherits leftover state from a prior real
// STOP, or leaves its own state behind for the next real run to trip over.
// On CI that directory always starts clean, so this passes there and flakes
// or fails locally depending on unrelated history — exactly the profile that
// gets blamed on "environment" instead of the test.
//
// Same shape as the "no test resolves its git root to the project root"
// invariant from the item-4 work — write it once, reuse it wherever a test
// touches filesystem state that has a real, live counterpart in this repo.

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

/**
 * Create a fresh temp directory for a test's .loop/ state, registered for
 * cleanup via the test's context (t.after). Asserts it did not somehow
 * resolve to the project root before handing it back.
 */
export function tempLoopDir(t) {
  const dir = mkdtempSync(join(tmpdir(), 'circus-loop-'));
  assertNotProjectRoot(dir);
  if (t && typeof t.after === 'function') {
    t.after(() => rmSync(dir, { recursive: true, force: true }));
  }
  return dir;
}

/**
 * Throws if loopDir resolves to (or inside) the real project's .loop
 * directory. Call this on any path a test is about to hand to Orchestrator
 * as loopDir/resumeHashPath — it's the harness's own guardrail against the
 * defect class this file exists to prevent.
 */
export function assertNotProjectRoot(loopDir) {
  const resolved = resolve(loopDir);
  const projectLoop = resolve(process.cwd(), '.loop');
  if (resolved === projectLoop || resolved.startsWith(projectLoop + '/')) {
    throw new Error(
      `Test resolved .loop state to the project root (${resolved}) — ` +
      `inject an isolated loopDir/resumeHashPath (see tempLoopDir()) instead.`
    );
  }
}
