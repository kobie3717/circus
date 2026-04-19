#!/usr/bin/env python3
"""Register owner public key in Circus DB"""

import sys
sys.path.insert(0, '/root/circus')
from circus.database import get_db
from pathlib import Path
from datetime import datetime

pub = (Path.home() / ".circus/kobus.pub").read_text().strip()
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO owner_keys (owner_id, public_key, created_at) VALUES (?, ?, ?)",
        ("kobus", pub, datetime.utcnow().isoformat())
    )
    conn.commit()
    print(f"Registered owner key for 'kobus'")
    print(f"Public key (first 20 chars): {pub[:20]}...")
