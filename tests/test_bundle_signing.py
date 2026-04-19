"""Tests for canonical bundle serialization."""

import datetime
import json

import pytest

from circus.services.bundle_signing import (
    BundleSerializationError,
    CANONICALIZATION_VERSION,
    EXCLUDED_FROM_SIGNING,
    canonicalize_for_signing,
)


class TestCanonicalizeForSigning:
    """Tests for canonicalize_for_signing function."""

    def test_same_bundle_same_bytes(self):
        """Same bundle dict produces identical bytes twice."""
        bundle = {
            "bundle_id": "bundle-123",
            "peer_id": "peer-abc",
            "memories": [{"id": "mem-1", "content": "test"}],
            "timestamp": "2026-04-18T10:00:00Z"
        }
        bytes1 = canonicalize_for_signing(bundle)
        bytes2 = canonicalize_for_signing(bundle)
        assert bytes1 == bytes2

    def test_different_insertion_order_same_bytes(self):
        """Same bundle with different key insertion order produces identical bytes."""
        bundle1 = {
            "bundle_id": "bundle-123",
            "peer_id": "peer-abc",
            "timestamp": "2026-04-18T10:00:00Z",
            "memories": [{"id": "mem-1", "content": "test"}],
        }
        # Different insertion order
        bundle2 = {
            "memories": [{"id": "mem-1", "content": "test"}],
            "timestamp": "2026-04-18T10:00:00Z",
            "peer_id": "peer-abc",
            "bundle_id": "bundle-123",
        }
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)

    def test_nested_dicts_different_order_same_bytes(self):
        """Nested dicts with different key order produce identical bytes."""
        bundle1 = {
            "bundle_id": "bundle-123",
            "memories": [{
                "id": "mem-1",
                "provenance": {
                    "hop_count": 1,
                    "confidence": 0.9,
                    "original_author": "claw-abc"
                }
            }]
        }
        bundle2 = {
            "bundle_id": "bundle-123",
            "memories": [{
                "id": "mem-1",
                "provenance": {
                    "original_author": "claw-abc",
                    "hop_count": 1,
                    "confidence": 0.9
                }
            }]
        }
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)

    def test_deep_nesting_deterministic(self):
        """Deeply nested structures (4+ levels) produce identical bytes."""
        bundle1 = {
            "a": {"b": {"c": {"d": {"e": "value"}}}},
            "z": 1
        }
        bundle2 = {
            "z": 1,
            "a": {"b": {"c": {"d": {"e": "value"}}}}
        }
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)

    def test_list_order_preserved(self):
        """List element order is preserved (not sorted)."""
        bundle1 = {"items": [1, 2, 3]}
        bundle2 = {"items": [3, 2, 1]}
        bytes1 = canonicalize_for_signing(bundle1)
        bytes2 = canonicalize_for_signing(bundle2)
        assert bytes1 != bytes2  # Different order → different bytes

    def test_list_of_dicts_inner_keys_sorted_list_order_preserved(self):
        """List of dicts: inner dict keys sorted, list order preserved."""
        bundle = {
            "memories": [
                {"id": "mem-2", "content": "second"},
                {"id": "mem-1", "content": "first"}
            ]
        }
        result = canonicalize_for_signing(bundle)
        # Decode to verify structure
        decoded = json.loads(result.decode("utf-8"))
        # List order should be preserved
        assert decoded["memories"][0]["id"] == "mem-2"
        assert decoded["memories"][1]["id"] == "mem-1"
        # But inner keys should be sorted
        assert list(decoded["memories"][0].keys()) == ["content", "id"]

    def test_empty_list_and_dict(self):
        """Empty lists and dicts are handled correctly."""
        bundle = {
            "empty_list": [],
            "empty_dict": {},
            "nested": {"also_empty": {}}
        }
        result = canonicalize_for_signing(bundle)
        assert isinstance(result, bytes)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["empty_list"] == []
        assert decoded["empty_dict"] == {}

    def test_signature_field_excluded(self):
        """Signature field is stripped before serialization."""
        bundle_with_sig = {
            "bundle_id": "bundle-123",
            "signature": "base64-signature-string",
            "peer_id": "peer-abc"
        }
        bundle_without_sig = {
            "bundle_id": "bundle-123",
            "peer_id": "peer-abc"
        }
        assert canonicalize_for_signing(bundle_with_sig) == canonicalize_for_signing(bundle_without_sig)

    def test_transport_field_excluded(self):
        """_transport field is stripped before serialization."""
        bundle1 = {"bundle_id": "bundle-123", "_transport": "http"}
        bundle2 = {"bundle_id": "bundle-123"}
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)

    def test_received_at_field_excluded(self):
        """_received_at field is stripped before serialization."""
        bundle1 = {"bundle_id": "bundle-123", "_received_at": "2026-04-18T10:00:00Z"}
        bundle2 = {"bundle_id": "bundle-123"}
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)

    def test_non_excluded_similar_fields_not_stripped(self):
        """Fields that look similar to excluded fields are NOT stripped."""
        bundle = {
            "bundle_id": "bundle-123",
            "signatures": ["sig1", "sig2"],  # Not "signature"
            "transport": "http",  # Not "_transport"
            "received_at": "2026-04-18T10:00:00Z"  # Not "_received_at"
        }
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert "signatures" in decoded
        assert "transport" in decoded
        assert "received_at" in decoded

    def test_unicode_strings_preserved(self):
        """Unicode strings are preserved in UTF-8 output."""
        bundle = {
            "portuguese": "não",
            "afrikaans": "nie",
            "emoji": "🔐"
        }
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["portuguese"] == "não"
        assert decoded["afrikaans"] == "nie"
        assert decoded["emoji"] == "🔐"

    def test_long_strings_handled(self):
        """Long strings are handled correctly."""
        bundle = {
            "long_content": "x" * 10000
        }
        result = canonicalize_for_signing(bundle)
        assert isinstance(result, bytes)
        assert len(result) > 10000

    def test_empty_string_valid(self):
        """Empty strings are valid."""
        bundle = {"empty": ""}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["empty"] == ""

    def test_datetime_rejected(self):
        """datetime objects raise BundleSerializationError."""
        bundle = {
            "timestamp": datetime.datetime(2026, 4, 18, 10, 0, 0)
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "$.timestamp" in str(exc_info.value)
        assert "datetime" in str(exc_info.value)

    def test_bytes_rejected(self):
        """bytes objects raise BundleSerializationError."""
        bundle = {
            "data": b"raw bytes"
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "$.data" in str(exc_info.value)
        assert "bytes" in str(exc_info.value)

    def test_set_rejected(self):
        """set objects raise BundleSerializationError."""
        bundle = {
            "tags": {"tag1", "tag2"}
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "$.tags" in str(exc_info.value)
        assert "set" in str(exc_info.value)

    def test_custom_object_rejected(self):
        """Custom class instances raise BundleSerializationError."""
        class CustomObject:
            pass

        bundle = {
            "custom": CustomObject()
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "$.custom" in str(exc_info.value)
        assert "CustomObject" in str(exc_info.value)

    def test_non_string_dict_key_rejected(self):
        """Non-string dict keys raise BundleSerializationError."""
        # JSON doesn't support non-string keys, but Python does
        # We need to test this via nested structure since top-level
        # dict is filtered already
        bundle = {
            "metadata": {
                123: "numeric key"  # This will fail
            }
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "non-string dict key" in str(exc_info.value)
        assert "$.metadata" in str(exc_info.value)

    def test_error_path_nested_list(self):
        """Error messages include correct path for nested list items."""
        bundle = {
            "memories": [
                {"id": "mem-1"},
                {"id": "mem-2", "bad_data": datetime.datetime.now()}
            ]
        }
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "$.memories[1].bad_data" in str(exc_info.value)

    def test_not_dict_raises_type_error(self):
        """Top-level non-dict raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            canonicalize_for_signing([1, 2, 3])
        assert "bundle must be dict" in str(exc_info.value)

    def test_nan_rejected(self):
        """NaN raises BundleSerializationError (uniform signing-boundary error type)."""
        bundle = {"value": float("nan")}
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "non-JSON-native float" in str(exc_info.value)

    def test_infinity_rejected(self):
        """Positive Infinity raises BundleSerializationError."""
        bundle = {"value": float("inf")}
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "non-JSON-native float" in str(exc_info.value)

    def test_negative_infinity_rejected(self):
        """Negative Infinity raises BundleSerializationError."""
        bundle = {"value": float("-inf")}
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        assert "non-JSON-native float" in str(exc_info.value)

    def test_nan_in_nested_structure_rejected(self):
        """NaN nested inside a list/dict still raises BundleSerializationError."""
        bundle = {"memories": [{"confidence": float("nan")}]}
        with pytest.raises(BundleSerializationError):
            canonicalize_for_signing(bundle)

    def test_nan_error_preserves_cause(self):
        """BundleSerializationError chains the original ValueError via __cause__."""
        bundle = {"value": float("nan")}
        with pytest.raises(BundleSerializationError) as exc_info:
            canonicalize_for_signing(bundle)
        # Wrapping preserves the underlying json.dumps ValueError for debugging
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_very_nested_structure_deterministic(self):
        """Very deeply nested structure (5 levels) is deterministic."""
        bundle1 = {
            "a": {"b": {"c": {"d": {"e": [1, 2, 3]}}}}
        }
        bundle2 = {
            "a": {"b": {"c": {"d": {"e": [1, 2, 3]}}}}
        }
        assert canonicalize_for_signing(bundle1) == canonicalize_for_signing(bundle2)


class TestContractMetadata:
    """Tests for contract metadata constants."""

    def test_canonicalization_version_exists(self):
        """CANONICALIZATION_VERSION constant exists and is correct."""
        assert CANONICALIZATION_VERSION == "sorted-keys-v1"

    def test_excluded_from_signing_is_frozenset(self):
        """EXCLUDED_FROM_SIGNING is a frozenset."""
        assert isinstance(EXCLUDED_FROM_SIGNING, frozenset)

    def test_excluded_from_signing_contains_signature(self):
        """EXCLUDED_FROM_SIGNING contains 'signature'."""
        assert "signature" in EXCLUDED_FROM_SIGNING

    def test_excluded_from_signing_contains_transport(self):
        """EXCLUDED_FROM_SIGNING contains '_transport'."""
        assert "_transport" in EXCLUDED_FROM_SIGNING

    def test_excluded_from_signing_contains_received_at(self):
        """EXCLUDED_FROM_SIGNING contains '_received_at'."""
        assert "_received_at" in EXCLUDED_FROM_SIGNING

    def test_bundle_serialization_error_is_value_error(self):
        """BundleSerializationError is a ValueError subclass."""
        assert issubclass(BundleSerializationError, ValueError)


class TestEdgeCases:
    """Additional edge case tests."""

    def test_boolean_true_not_confused_with_int(self):
        """Boolean True is preserved as boolean, not int."""
        bundle = {"flag": True, "number": 1}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["flag"] is True
        assert decoded["number"] == 1

    def test_boolean_false_not_confused_with_int(self):
        """Boolean False is preserved as boolean, not int."""
        bundle = {"flag": False, "number": 0}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["flag"] is False
        assert decoded["number"] == 0

    def test_none_value_preserved(self):
        """None values are preserved (null in JSON)."""
        bundle = {"nullable": None}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["nullable"] is None

    def test_float_precision_preserved(self):
        """Float precision is preserved in serialization."""
        bundle = {"confidence": 0.123456789}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert abs(decoded["confidence"] - 0.123456789) < 1e-9

    def test_negative_numbers_valid(self):
        """Negative numbers are valid."""
        bundle = {"value": -42, "float_val": -3.14}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["value"] == -42
        assert abs(decoded["float_val"] - (-3.14)) < 1e-9

    def test_zero_values_valid(self):
        """Zero values (int and float) are valid."""
        bundle = {"zero_int": 0, "zero_float": 0.0}
        result = canonicalize_for_signing(bundle)
        decoded = json.loads(result.decode("utf-8"))
        assert decoded["zero_int"] == 0
        assert decoded["zero_float"] == 0.0
