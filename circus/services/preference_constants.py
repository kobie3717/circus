"""Preference field allowlist — control-plane gatekeeper for mutable behavior deltas.

This module defines the ONLY fields that can be mutated via behavior-delta preference
memories. Adding fields to this allowlist requires design review — these are control-plane
mutations that affect bot behavior at runtime.

Week 4 MVP allowlist (4 fields):
- user.language_preference (e.g., "af", "en")
- user.response_verbosity (e.g., "terse", "normal", "verbose")
- user.tone_preference (e.g., "casual", "formal", "direct")
- user.format_preference (e.g., "markdown", "plain", "bullets")

Explicitly NOT allowed (design lock):
- Tool permissions
- Safety settings
- System prompt wholesale rewrites
- Model selection
- Network/auth config
"""

ALLOWLISTED_PREFERENCE_FIELDS = frozenset([
    "user.language_preference",
    "user.response_verbosity",
    "user.tone_preference",
    "user.format_preference",
])
