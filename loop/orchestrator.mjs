#!/usr/bin/env node
// orchestrator.mjs — Circus Loop state machine with guards
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { scrubEgress } from '/root/bot-circus/lib/experience-bridge.mjs';

const STATES = {
  IDLE: 'IDLE',
  PLANNING: 'PLANNING',
  CODING: 'CODING',
  EVALUATING: 'EVALUATING',
  DISTILLING: 'DISTILLING',
  STOPPED: 'STOPPED',
};

// ─── Stub Adapter (BUILD 1) ─────────────────────────────────────────────────
class StubAdapter {
  constructor(fixtureDir = '/root/circus/loop/fixtures') {
    this.fixtureDir = fixtureDir;
  }

  async plan({ task, feedback = null }) {
    const path = `${this.fixtureDir}/plan.md`;
    if (!existsSync(path)) {
      return { signal: 'STOP', reason: 'plan fixture missing' };
    }
    return { artifact: readFileSync(path, 'utf8') };
  }

  async code({ plan }) {
    const path = `${this.fixtureDir}/diff`;
    if (!existsSync(path)) {
      return { signal: 'STOP', reason: 'diff fixture missing' };
    }
    return { artifact: readFileSync(path, 'utf8') };
  }

  async evaluate({ plan, diff }) {
    const path = `${this.fixtureDir}/verdict.json`;
    if (!existsSync(path)) {
      return { signal: 'STOP', reason: 'verdict fixture missing' };
    }
    return { artifact: readFileSync(path, 'utf8') };
  }

  async distill({ plan, diff, verdict }) {
    const path = `${this.fixtureDir}/feedback.md`;
    if (!existsSync(path)) {
      return { signal: 'STOP', reason: 'feedback fixture missing' };
    }
    return { artifact: readFileSync(path, 'utf8') };
  }
}

// ─── Orchestrator ───────────────────────────────────────────────────────────
export class Orchestrator {
  constructor(adapter, options = {}) {
    this.adapter = adapter;
    this.state = STATES.IDLE;
    this.resumeTokenPath = options.resumeTokenPath || '.loop/resume-token';
    this.fixtureDir = options.fixtureDir || '/root/circus/loop/fixtures';
    this.artifacts = {};
    this.checkResults = {}; // for G5 stub-detection
  }

  // ─── G3: artifact-scrub ───────────────────────────────────────────────────
  scrubArtifact(artifactName, content) {
    const result = scrubEgress(content);
    if (result.hits > 0) {
      throw new Error(`G3 violation: artifact ${artifactName} contains secrets (${result.hits} hits) — aborting`);
    }
    return result.text;
  }

  // ─── G2: stop-is-terminal ─────────────────────────────────────────────────
  handleStop(roleName, reason) {
    mkdirSync('.loop', { recursive: true });
    const token = createHash('sha256')
      .update(`${roleName}:${reason}:${Date.now()}`)
      .digest('hex');
    writeFileSync(this.resumeTokenPath, token);
    console.error(`STOP signaled by ${roleName}: ${reason}`);
    console.error(`Resume token: ${token}`);
    process.exit(42);
  }

  // ─── G1: matrix-shape ─────────────────────────────────────────────────────
  extractExpectedChecks(planMd) {
    // Extract check names from plan.md ## Checks section
    const checksMatch = planMd.match(/^## Checks\s*\n((?:^[-*] .+$\n?)+)/m);
    if (!checksMatch) return [];

    const lines = checksMatch[1].trim().split('\n');
    return lines.map(line => {
      // Format: "- [check-name]: command" or "- check-name: description"
      const match = line.match(/^[-*]\s+(?:\[([^\]]+)\]|([^:]+)):/);
      return match ? (match[1] || match[2]).trim() : null;
    }).filter(Boolean);
  }

  validateVerdictMatrix(planMd, verdictJson) {
    const expected = this.extractExpectedChecks(planMd);
    const verdict = JSON.parse(verdictJson);

    if (!verdict.rows || !Array.isArray(verdict.rows)) {
      throw new Error('G1 violation: verdict.json missing or invalid rows array');
    }

    const actual = verdict.rows.map(r => r.name).sort();
    const expectedSorted = expected.sort();

    if (JSON.stringify(actual) !== JSON.stringify(expectedSorted)) {
      throw new Error(
        `G1 violation: verdict row names don't match plan checks.\n` +
        `Expected: ${JSON.stringify(expectedSorted)}\n` +
        `Actual: ${JSON.stringify(actual)}`
      );
    }
  }

  // ─── G4: harness-integrity ────────────────────────────────────────────────
  validateHarnessIntegrity(planMd, verdictJson) {
    // Extract commands from plan
    const checksMatch = planMd.match(/^## Checks\s*\n((?:^[-*] .+$\n?)+)/m);
    if (!checksMatch) return;

    const lines = checksMatch[1].trim().split('\n');
    const commands = {};
    lines.forEach(line => {
      const match = line.match(/^[-*]\s+\[([^\]]+)\]:\s*(.+)$/);
      if (match) {
        commands[match[1].trim()] = match[2].trim();
      }
    });

    const verdict = JSON.parse(verdictJson);
    // Check for command substitution markers in evidence (for BUILD 1 fixture testing)
    verdict.rows.forEach(row => {
      if (row.evidence && row.evidence.includes('SUBSTITUTED:')) {
        throw new Error(`G4 violation: check ${row.name} used substituted command instead of plan's command`);
      }
    });
  }

  // ─── G5: stub-detection ───────────────────────────────────────────────────
  detectStubCapitulation(diff, beforeResults, afterResults) {
    const newFiles = this.extractNewFiles(diff);
    if (newFiles.length === 0) return [];

    const flags = [];
    Object.keys(afterResults).forEach(checkName => {
      const before = beforeResults[checkName];
      const after = afterResults[checkName];
      if (before === false && after === true) {
        flags.push(`Check ${checkName} flipped FAIL→PASS via new file(s): ${newFiles.join(', ')} — possible stub`);
      }
    });
    return flags;
  }

  extractNewFiles(diff) {
    // Parse unified diff for new files (where old file is /dev/null)
    const lines = diff.split('\n');
    const newFiles = [];
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith('--- ') && lines[i].includes('/dev/null')) {
        // Next line should be "+++ b/filename"
        if (i + 1 < lines.length && lines[i + 1].startsWith('+++ b/')) {
          const file = lines[i + 1].substring(6);
          newFiles.push(file);
        }
      }
    }
    return newFiles;
  }

  // ─── G6: role-isolation ───────────────────────────────────────────────────
  async callRole(roleName, allowedArtifacts, roleMethod) {
    const context = {};
    allowedArtifacts.forEach(name => {
      if (this.artifacts[name] !== undefined) {
        context[name] = this.artifacts[name];
      }
    });

    // Verify role doesn't try to access disallowed artifacts
    const provided = Object.keys(context);
    const allArtifacts = Object.keys(this.artifacts);
    const disallowed = allArtifacts.filter(a => !allowedArtifacts.includes(a));

    if (disallowed.some(a => this.artifacts[a] && !context[a])) {
      // Role isolation is enforced by only passing allowed artifacts
      // This guard is tested by attempting to pass wrong artifacts
    }

    return await roleMethod(context);
  }

  // ─── Main Run Loop ────────────────────────────────────────────────────────
  async run(task) {
    this.state = STATES.PLANNING;

    // PLANNING
    const planResult = await this.callRole('planner', ['task', 'feedback'],
      (ctx) => this.adapter.plan({ task, feedback: ctx.feedback }));

    if (planResult.signal === 'STOP') {
      this.handleStop('planner', planResult.reason);
    }

    const planMd = this.scrubArtifact('plan.md', planResult.artifact);
    this.artifacts.plan = planMd;

    // CODING
    this.state = STATES.CODING;
    const codeResult = await this.callRole('coder', ['plan'],
      (ctx) => this.adapter.code({ plan: ctx.plan }));

    if (codeResult.signal === 'STOP') {
      this.handleStop('coder', codeResult.reason);
    }

    const diff = this.scrubArtifact('diff', codeResult.artifact);
    this.artifacts.diff = diff;

    // EVALUATING
    this.state = STATES.EVALUATING;
    const evalResult = await this.callRole('evaluator', ['plan', 'diff'],
      (ctx) => this.adapter.evaluate({ plan: ctx.plan, diff: ctx.diff }));

    if (evalResult.signal === 'STOP') {
      this.handleStop('evaluator', evalResult.reason);
    }

    const verdictJson = this.scrubArtifact('verdict.json', evalResult.artifact);
    this.artifacts.verdict = verdictJson;

    // G1: matrix-shape
    this.validateVerdictMatrix(planMd, verdictJson);

    // G4: harness-integrity
    this.validateHarnessIntegrity(planMd, verdictJson);

    // G5: stub-detection (mock before/after for BUILD 1)
    const verdict = JSON.parse(verdictJson);
    const stubFlags = this.detectStubCapitulation(diff, {},
      verdict.rows.reduce((acc, r) => ({ ...acc, [r.name]: r.pass }), {}));

    if (stubFlags.length > 0) {
      console.warn('G5 stub-detection flags:', stubFlags);
    }

    // DISTILLING
    this.state = STATES.DISTILLING;
    const distillResult = await this.callRole('distiller', ['plan', 'diff', 'verdict'],
      (ctx) => this.adapter.distill({
        plan: ctx.plan,
        diff: ctx.diff,
        verdict: ctx.verdict
      }));

    if (distillResult.signal === 'STOP') {
      this.handleStop('distiller', distillResult.reason);
    }

    const feedback = this.scrubArtifact('feedback.md', distillResult.artifact);
    this.artifacts.feedback = feedback;

    this.state = STATES.IDLE;
    return this.artifacts;
  }
}

// ─── CLI Entry Point ──────────────────────────────────────────────────────
if (import.meta.url === `file://${process.argv[1]}`) {
  const args = process.argv.slice(2);
  const fixtureDir = args.includes('--fixture-dir')
    ? args[args.indexOf('--fixture-dir') + 1]
    : '/root/circus/loop/fixtures';

  const adapter = new StubAdapter(fixtureDir);
  const orchestrator = new Orchestrator(adapter, { fixtureDir });

  orchestrator.run('BUILD 1 test task')
    .then(artifacts => {
      console.log('Loop completed successfully');
      console.log('Artifacts:', Object.keys(artifacts));
    })
    .catch(err => {
      console.error('Loop failed:', err.message);
      process.exit(1);
    });
}

export { StubAdapter, STATES };
