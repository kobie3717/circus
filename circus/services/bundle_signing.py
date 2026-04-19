"""Canonical bundle serialization for signature envelope.

SIGNING CONTRACT (MVP, sorted-keys JSON):
- UTF-8 bytes output
- Dict keys sorted recursively (lexicographic)
- List order preserved (lists are ordered data)
- Compact separators: (',', ':') — no insignificant whitespace
- JSON-native types only: dict, list, str, int, float, bool, None
- Any other type (datetime, bytes, set, custom object) raises
  BundleSerializationError — NO best-effort coercion

Rationale: bilateral federation between our own nodes. Deterministic
bytes required for signature verification. Best-effort coercion would
let two nodes produce different bytes for the same logical payload and
break verification.

Future: may migrate to RFC 8785 JCS if federation opens to third-party
implementations. The `CANONICALIZATION_VERSION` constant is the hook
for that migration.
"""

import json
from typing import Any

# Schema version for the canonicalization contract.
# Bumped if/when we migrate to JCS.
CANONICALIZATION_VERSION = "sorted-keys-v1"

# Fields excluded from signing (transport / signature envelope itself).
# Anything in this set is stripped from the bundle before serialization.
EXCLUDED_FROM_SIGNING = frozenset({"signature", "_transport", "_received_at"})

# JSON-native types that are allowed in a signable payload.
_ALLOWED_TYPES = (dict, list, str, int, float, bool, type(None))


class BundleSerializationError(ValueError):
    """Raised when a bundle contains values that cannot be canonically serialized."""


def _validate_structure(value: Any, path: str = "$") -> None:
    """Recursively validate that all values are JSON-native.

    Raises BundleSerializationError with the exact path of the offending value.
    """
    if isinstance(value, bool):
        # bool is a subclass of int — check it first
        return
    if isinstance(value, (str, int, float)) or value is None:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise BundleSerializationError(
                    f"non-string dict key at {path}: {type(k).__name__}={k!r}"
                )
            _validate_structure(v, f"{path}.{k}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _validate_structure(item, f"{path}[{i}]")
        return
    # Not a JSON-native type — explicit rejection
    raise BundleSerializationError(
        f"non-JSON-native value at {path}: "
        f"{type(value).__name__}={value!r}"
    )


def canonicalize_for_signing(bundle: dict) -> bytes:
    """Produce deterministic UTF-8 bytes for signing.

    Strips signature/transport fields, validates JSON-native types
    recursively, sorts dict keys at every level, emits compact JSON.

    Args:
        bundle: dict representation of a MemoryBundle (may include
            signature or transport fields — they'll be stripped)

    Returns:
        UTF-8 encoded bytes suitable for Ed25519 signing.

    Raises:
        BundleSerializationError: if any nested value is not a JSON-
            native type (datetime, bytes, set, custom object, non-str
            dict keys all rejected).
        TypeError: if bundle is not a dict.
    """
    if not isinstance(bundle, dict):
        raise TypeError(
            f"bundle must be dict, got {type(bundle).__name__}"
        )

    # Strip excluded fields (signature envelope, transport metadata)
    signable = {k: v for k, v in bundle.items() if k not in EXCLUDED_FROM_SIGNING}

    # Validate structure before serializing — fail loud with precise path
    _validate_structure(signable)

    # Serialize with sorted keys recursively (json.dumps sort_keys=True
    # does recursive sort) and compact separators.
    # allow_nan=False rejects NaN/Infinity (not valid JSON per RFC 8259).
    # Wrap the resulting ValueError into BundleSerializationError so every
    # failure at the signing boundary raises a single, uniform error type.
    try:
        canonical = json.dumps(
            signable,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,  # preserve unicode in UTF-8 output
            allow_nan=False,     # NaN/Infinity not in JSON spec
        )
    except ValueError as exc:
        # json.dumps raises ValueError for NaN/Infinity when allow_nan=False.
        # Normalize to our boundary error so callers catch one exception type.
        raise BundleSerializationError(
            f"non-JSON-native float value (NaN or Infinity): {exc}"
        ) from exc

    return canonical.encode("utf-8")
