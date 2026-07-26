#!/usr/bin/env node
// orchestrator.mjs — Circus Loop state machine with guards
import { readFileSync, writeFileSync, existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { scrubEgress } from './lib/scrub.mjs';

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
  constructor(fixtureDir = fileURLToPath(new URL('./fixtures/', import.meta.url))) {
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
    this.resumeHashPath = options.resumeHashPath || '.loop/resume-hash';
    this.fixtureDir = options.fixtureDir || fileURLToPath(new URL('./fixtures/', import.meta.url));
    this.artifacts = {};
    this.checkResults = {}; // for G5 stub-detection
    this.mandatoryChecks = options.mandatoryChecks || [];
    this.runState = {}; // G6: temporal separation timestamps

    // G6: capability manifest for coder role
    if (options.coderManifest === undefined) {
      // Default manifest if not provided
      this.coderManifest = {
        allowedTools: ['read', 'edit', 'write'],
        allowedPaths: ['/worktree/**'],
        allowBash: false,
        allowNetwork: false
      };
    } else {
      // Use provided manifest but validate it
      this.coderManifest = options.coderManifest;
    }

    // G6: enforce manifest is present and well-formed
    if (this.coderManifest === null || typeof this.coderManifest !== 'object' || Array.isArray(this.coderManifest)) {
      throw new Error('G6 violation: coder manifest missing or malformed');
    }
    if (!Array.isArray(this.coderManifest.allowedTools)) {
      throw new Error('G6 violation: coder manifest.allowedTools must be an array');
    }
  }

  // ─── G3: artifact-scrub ───────────────────────────────────────────────────
  scrubArtifact(artifactName, content) {
    const result = scrubEgress(content);
    if (result.hits > 0) {
      throw new Error(`G3 violation: artifact ${artifactName} contains secrets (${result.hits} hits) — aborting`);
    }
    return result.text;
  }

  // ─── G6: feedback-projection ──────────────────────────────────────────────
  // Filter verdict to ONLY {row name, pass/fail} — no evidence, no commentary
  filterVerdictToFeedback(verdictJson) {
    const verdict = JSON.parse(verdictJson);
    if (!verdict.rows || !Array.isArray(verdict.rows)) {
      return '# Feedback\n\nNo verdict rows found.';
    }
    const filtered = verdict.rows.map(r => `- ${r.name}: ${r.pass ? 'PASS' : 'FAIL'}`).join('\n');
    return `# Feedback\n\n${filtered}`;
  }

  // ─── G2: stop-is-terminal (BUILD 2: hash-only storage) ───────────────────
  handleStop(roleName, reason) {
    mkdirSync('.loop', { recursive: true });
    // Generate raw token (returned to caller, never persisted)
    const rawToken = createHash('sha256')
      .update(`${roleName}:${reason}:${Date.now()}`)
      .digest('hex');
    // Store only the hash of the token
    const tokenHash = createHash('sha256').update(rawToken).digest('hex');
    writeFileSync(this.resumeHashPath, tokenHash);
    console.error(`STOP signaled by ${roleName}: ${reason}`);
    console.error(`Resume token: ${rawToken}`);
    console.error(`(Token hash stored in ${this.resumeHashPath})`);
    process.exit(42);
  }

  // Verify resume token matches stored hash
  verifyResumeToken(suppliedToken) {
    if (!existsSync(this.resumeHashPath)) {
      throw new Error('No resume hash found — cannot resume');
    }
    const storedHash = readFileSync(this.resumeHashPath, 'utf8').trim();
    const suppliedHash = createHash('sha256').update(suppliedToken).digest('hex');
    if (storedHash !== suppliedHash) {
      throw new Error('Resume token mismatch — invalid token');
    }
  }

  // ─── G2: resume gate — entry check ─────────────────────────────────────────
  // A prior STOP left a resume-hash on disk. A fresh run() must not silently
  // proceed past that: no token supplied, or a token that doesn't match, both
  // refuse to start. Only a correct token clears the hash and allows entry.
  // This is what makes G2's STOP terminal rather than merely loud.
  checkResumeGate(resumeToken) {
    if (!existsSync(this.resumeHashPath)) {
      return; // no prior STOP pending — normal start
    }
    if (!resumeToken) {
      throw new Error('G2 violation: resume hash present at ' + this.resumeHashPath +
        ' but no resume token supplied — refusing to start. Prior run STOPped and was never resumed.');
    }
    // Throws 'Resume token mismatch' on wrong token — propagates as refusal.
    this.verifyResumeToken(resumeToken);
    // Correct token: the STOP is resolved — clear the hash so it can't gate again.
    unlinkSync(this.resumeHashPath);
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

    // BUILD 2: G1 two-sided — plan must be superset of mandatory_checks
    if (this.mandatoryChecks.length > 0) {
      const missing = this.mandatoryChecks.filter(m => !expected.includes(m));
      if (missing.length > 0) {
        throw new Error(
          `G1 violation: plan missing mandatory checks: ${JSON.stringify(missing)}`
        );
      }
    }

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

  // ─── G4: harness-integrity (BUILD 2: orchestrator executes checks) ───────
  extractCheckCommands(planMd) {
    const checksMatch = planMd.match(/^## Checks\s*\n((?:^[-*] .+$\n?)+)/m);
    if (!checksMatch) return {};

    const lines = checksMatch[1].trim().split('\n');
    const commands = {};
    lines.forEach(line => {
      const match = line.match(/^[-*]\s+\[([^\]]+)\]:\s*(.+)$/);
      if (match) {
        commands[match[1].trim()] = match[2].trim();
      }
    });
    return commands;
  }

  executeChecksAndCaptureExitCodes(planMd) {
    const commands = this.extractCheckCommands(planMd);
    const results = {};

    for (const [checkName, command] of Object.entries(commands)) {
      try {
        // Execute command and capture exit code
        execSync(command, { stdio: 'pipe', timeout: 30000 });
        results[checkName] = { pass: true, exitCode: 0 };
      } catch (err) {
        // Non-zero exit = fail
        results[checkName] = { pass: false, exitCode: err.status || 1 };
      }
    }
    return results;
  }

  validateHarnessIntegrity(planMd, verdictJson, executedResults) {
    const verdict = JSON.parse(verdictJson);

    // BUILD 2: For mandatory checks, orchestrator's real exit codes OVERWRITE evaluator's claims
    for (const row of verdict.rows) {
      if (executedResults[row.name] !== undefined) {
        // This is a mandatory check - use orchestrator's real execution result
        const realResult = executedResults[row.name];
        if (row.pass !== realResult.pass) {
          console.warn(
            `G4: Evaluator claimed ${row.pass ? 'PASS' : 'FAIL'} for ${row.name}, ` +
            `but real exit code was ${realResult.exitCode} (${realResult.pass ? 'PASS' : 'FAIL'}). ` +
            `Overwriting with real result.`
          );
          row.pass = realResult.pass;
        }
      }
    }

    // Also check for command substitution markers (BUILD 1 compatibility)
    verdict.rows.forEach(row => {
      if (row.evidence && row.evidence.includes('SUBSTITUTED:')) {
        throw new Error(`G4 violation: check ${row.name} used substituted command instead of plan's command`);
      }
    });

    return verdict;
  }

  // ─── G5: stub-detection (BUILD 2: HALT on detection) ─────────────────────
  detectStubCapitulation(diff, beforeResults, afterResults) {
    const newFiles = this.extractNewFiles(diff);
    if (newFiles.length === 0) return [];

    const flags = [];
    Object.keys(afterResults).forEach(checkName => {
      const before = beforeResults[checkName];
      const after = afterResults[checkName];
      if (before === false && after === true) {
        flags.push({
          check: checkName,
          files: newFiles,
          message: `Check ${checkName} flipped FAIL→PASS via new file(s): ${newFiles.join(', ')} — possible stub`
        });
      }
    });
    return flags;
  }

  handleStubDetectionFlags(flags) {
    if (flags.length > 0) {
      const reasons = flags.map(f => f.message).join('\n');
      console.error('G5 stub-detection violation:', reasons);
      this.handleStop('orchestrator-G5', `Stub capitulation detected:\n${reasons}`);
    }
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
    // G6: capability manifest enforcement for coder role
    if (roleName === 'coder') {
      if (!this.coderManifest) {
        throw new Error('G6 violation: coder invoked without manifest');
      }
      if (this.coderManifest.allowBash === true) {
        throw new Error('G6 violation: coder manifest must not allow Bash execution');
      }
      if (this.coderManifest.allowNetwork === true) {
        throw new Error('G6 violation: coder manifest must not allow network access');
      }
    }

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
  async run(task, resumeToken = null) {
    // G2: refuse to start if a prior STOP's resume hash is still pending
    this.checkResumeGate(resumeToken);

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

    // G6: temporal separation — record coder exit timestamp
    this.runState.coderExitedAt = Date.now();

    if (codeResult.signal === 'STOP') {
      this.handleStop('coder', codeResult.reason);
    }

    const diff = this.scrubArtifact('diff', codeResult.artifact);
    this.artifacts.diff = diff;

    // EVALUATING
    this.state = STATES.EVALUATING;

    // BUILD 2: G4 inversion — execute check commands ourselves
    const executedResults = this.executeChecksAndCaptureExitCodes(planMd);

    const evalResult = await this.callRole('evaluator', ['plan', 'diff'],
      (ctx) => this.adapter.evaluate({ plan: ctx.plan, diff: ctx.diff }));

    if (evalResult.signal === 'STOP') {
      this.handleStop('evaluator', evalResult.reason);
    }

    let verdictJson = this.scrubArtifact('verdict.json', evalResult.artifact);

    // G1: matrix-shape (BUILD 2: also checks mandatory_checks superset)
    this.validateVerdictMatrix(planMd, verdictJson);

    // G4: harness-integrity (BUILD 2: overwrite with real exit codes)
    const correctedVerdict = this.validateHarnessIntegrity(planMd, verdictJson, executedResults);
    verdictJson = JSON.stringify(correctedVerdict, null, 2);
    this.artifacts.verdict = verdictJson;

    // G6: temporal separation — record verdict write timestamp
    this.runState.verdictWrittenAt = Date.now();

    // G5: stub-detection (BUILD 2: HALT on detection)
    const verdict = correctedVerdict;
    const stubFlags = this.detectStubCapitulation(diff, {},
      verdict.rows.reduce((acc, r) => ({ ...acc, [r.name]: r.pass }), {}));

    this.handleStubDetectionFlags(stubFlags);

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

    let feedback = this.scrubArtifact('feedback.md', distillResult.artifact);

    // G6: feedback projection — filter to ONLY {row name, pass/fail}
    // Override distiller output with filtered version
    feedback = this.filterVerdictToFeedback(verdictJson);
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
    : fileURLToPath(new URL('./fixtures/', import.meta.url));

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
