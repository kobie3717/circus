"""Preference field allowlist — control-plane gatekeeper for mutable behavior deltas.

This module defines the ONLY fields that can be mutated via behavior-delta preference
memories. Adding fields to this allowlist requires design review — these are control-plane
mutations that affect bot behavior at runtime.

Week 8: Expanded to 9 fields with metadata-driven validation.

Explicitly NOT allowed (design lock):
- Tool permissions
- Safety settings
- System prompt wholesale rewrites
- Model selection
- Network/auth config
"""

from dataclasses import dataclass


@dataclass
class PreferenceField:
    """Preference field metadata for validation and documentation."""
    name: str
    description: str
    valid_values: list[str] | None  # None = free text
    default: str | None
    activation_threshold: float  # Override global threshold
    category: str  # "communication" | "behavior" | "display" | "workflow"


# Preference field registry (W8)
PREFERENCE_REGISTRY: dict[str, PreferenceField] = {
    # Existing fields (W4)
    "user.language_preference": PreferenceField(
        name="user.language_preference",
        description="Preferred response language",
        valid_values=["en", "af", "pt", "es", "fr"],
        default="en",
        activation_threshold=0.7,
        category="communication"
    ),
    "user.response_verbosity": PreferenceField(
        name="user.response_verbosity",
        description="Preferred response length/detail",
        valid_values=["terse", "normal", "verbose"],
        default="normal",
        activation_threshold=0.7,
        category="communication"
    ),
    "user.tone_preference": PreferenceField(
        name="user.tone_preference",
        description="Preferred communication tone",
        valid_values=["casual", "professional", "technical", "friendly"],
        default="casual",
        activation_threshold=0.7,
        category="communication"
    ),
    "user.format_preference": PreferenceField(
        name="user.format_preference",
        description="Preferred output format",
        valid_values=["plain", "markdown", "bullet_points", "structured"],
        default="markdown",
        activation_threshold=0.7,
        category="display"
    ),

    # NEW fields (W8)
    "user.code_style": PreferenceField(
        name="user.code_style",
        description="Preferred code example style",
        valid_values=["concise", "verbose", "with_comments", "no_comments"],
        default="concise",
        activation_threshold=0.75,
        category="behavior"
    ),
    "user.explanation_depth": PreferenceField(
        name="user.explanation_depth",
        description="How much to explain before doing",
        valid_values=["none", "brief", "full"],
        default="brief",
        activation_threshold=0.7,
        category="behavior"
    ),
    "user.confirmation_style": PreferenceField(
        name="user.confirmation_style",
        description="When to ask for confirmation before acting",
        valid_values=["always", "destructive_only", "never"],
        default="destructive_only",
        activation_threshold=0.8,
        category="workflow"
    ),
    "user.timezone": PreferenceField(
        name="user.timezone",
        description="User's local timezone",
        valid_values=None,  # Free text IANA timezone
        default="UTC",
        activation_threshold=0.9,
        category="behavior"
    ),
    "agent.proactive_suggestions": PreferenceField(
        name="agent.proactive_suggestions",
        description="Whether agent should offer unsolicited suggestions",
        valid_values=["enabled", "disabled", "on_errors_only"],
        default="enabled",
        activation_threshold=0.75,
        category="behavior"
    ),
}

# Legacy frozenset for backward compatibility (W4 code may still use this)
ALLOWLISTED_PREFERENCE_FIELDS = frozenset(PREFERENCE_REGISTRY.keys())
