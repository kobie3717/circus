"""Preference field allowlist — control-plane gatekeeper for mutable behavior deltas.

This module defines the ONLY fields that can be mutated via behavior-delta preference
memories. Adding fields to this allowlist requires design review — these are control-plane
mutations that affect bot behavior at runtime.

Week 4 MVP allowlist (4 fields):
- user.language_preference (e.g., "af", "en")
- user.response_verbosity (e.g., "terse", "normal", "verbose")
- user.tone_preference (e.g., "casual", "formal", "direct")
- user.format_preference (e.g., "markdown", "plain", "bullets")

Week 8 expanded allowlist (9 fields total):
- user.code_style (e.g., "with_comments", "no_comments")
- user.explanation_depth (e.g., "none", "brief", "full")
- user.confirmation_style (e.g., "autonomous", "confirm_first", "always")
- user.timezone (e.g., "Africa/Johannesburg", "UTC+2")
- agent.proactive_suggestions (e.g., "enabled", "on_errors_only", "disabled")

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
    "user.code_style",
    "user.explanation_depth",
    "user.confirmation_style",
    "user.timezone",
    "agent.proactive_suggestions",
])
