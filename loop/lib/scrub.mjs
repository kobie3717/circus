// scrub.mjs — Vendored egress scrubber for secret detection
// Provenance: bot-circus lib/experience-bridge.mjs::scrubEgress (non-durable branch v3-landing)
// Content hash (SHA-256): 825342315d5188e630a306601f4012868fd5a444c7f1f957699ace57582ac5b1
// Vendored for BUILD 2 to remove cross-repo filesystem dependency

// Egress scrub — for artifacts/output. Redacts secrets and returns { action, hits, text }.
const EG_LABELLED = /\b(api[_-]?key|secret|token|password|passwd|bearer)\b(\s*[:=]\s*["']?)[A-Za-z0-9+/_.\-]{8,}["']?/gi;
const EG_PEM = /-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)/g;
const EG_CONN = /\b(?:postgres(?:ql)?|mysql|redis|amqp|mongodb(?:\+srv)?):\/\/[^\s:@]+:[^\s@]+@[^\s]+/gi;
const EG_JWT = /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b/g;
const EG_AKIA = /\bAKIA[0-9A-Z]{16}\b/g;
const EG_TGTOK = /\b\d{8,10}:[A-Za-z0-9_-]{35}\b/g;
const EG_HEX = /\b[0-9a-f]{32,}\b/g;

export function scrubEgress(text) {
  let hits = 0;
  const hit = (tag) => { hits++; return `[REDACTED:${tag}]`; };
  let out = String(text)
    .replace(EG_PEM, () => hit('pem'))
    .replace(EG_CONN, () => hit('conn-string'))
    .replace(EG_JWT, () => hit('jwt'))
    .replace(EG_AKIA, () => hit('aws-key'))
    .replace(EG_TGTOK, () => hit('tg-token'))
    .replace(EG_LABELLED, (m, label, sep) => { hits++; return `${label}${sep}[REDACTED]`; })
    .replace(EG_HEX, (m) => { hits++; return `[REDACTED:hex…${m.slice(-4)}]`; });
  return { action: hits ? 'redacted' : 'clean', hits, text: out };
}
