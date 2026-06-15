#!/usr/bin/env python3
"""Test experience boost in routing."""
import sqlite3
import sys
from pathlib import Path

# Add circus to path
sys.path.insert(0, str(Path(__file__).parent))

from circus.services.routing import _get_experience_boost

# Connect to database
db_path = Path.home() / ".circus" / "circus.db"
conn = sqlite3.connect(str(db_path))

# Test 1: Check experience boost for octo in hydra-note/debug (should be high)
print("Test 1: Experience boost for octo-b0c49c in hydra-note/debug")
boost = _get_experience_boost(conn, "hydra-note", "debug", "octo-b0c49c")
print(f"  Boost: {boost:.4f}")
if boost > 0:
    print("  ✓ Experience boost is working!")
else:
    print("  ✗ Expected positive boost")

# Test 2: Check experience boost for different environment (should be 0)
print("\nTest 2: Experience boost for octo-b0c49c in different-env/debug")
boost = _get_experience_boost(conn, "different-env", "debug", "octo-b0c49c")
print(f"  Boost: {boost:.4f}")
if boost == 0:
    print("  ✓ No boost for unmatched environment!")
else:
    print("  ✗ Expected zero boost")

# Test 3: Check experience boost for different agent (should be 0)
print("\nTest 3: Experience boost for friday-174577 in hydra-note/debug")
boost = _get_experience_boost(conn, "hydra-note", "debug", "friday-174577")
print(f"  Boost: {boost:.4f}")
if boost == 0:
    print("  ✓ No boost for different agent!")
else:
    print("  ✗ Expected zero boost")

conn.close()
print("\n✓ All experience boost tests passed!")
