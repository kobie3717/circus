#!/usr/bin/env python3
"""Generate Ed25519 keypair for Circus owner authentication"""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64
from pathlib import Path

private_key = Ed25519PrivateKey.generate()
private_bytes = private_key.private_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption()
)
public_bytes = private_key.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw
)

d = Path.home() / ".circus"
d.mkdir(exist_ok=True)
(d / "kobus.key").write_text(base64.b64encode(private_bytes).decode())
(d / "kobus.pub").write_text(base64.b64encode(public_bytes).decode())
(d / "kobus.key").chmod(0o600)

print("Private:", base64.b64encode(private_bytes).decode())
print("Public:", base64.b64encode(public_bytes).decode())
print("\nKeypair saved to ~/.circus/")
