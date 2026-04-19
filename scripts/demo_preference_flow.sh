#!/usr/bin/env bash
# Circus Memory Commons — Week 5 Preference Demo
# Wrapper script for the Python demo

set -euo pipefail

# W5 REQUIREMENT: Check for owner private key env var
if [ -z "${CIRCUS_OWNER_PRIVATE_KEY_PATH:-}" ]; then
  echo "ERROR: CIRCUS_OWNER_PRIVATE_KEY_PATH is not set." >&2
  echo "Run: python -m circus.cli owner-keygen --owner demo-owner --output /tmp/demo-owner.key" >&2
  echo "Then: export CIRCUS_OWNER_PRIVATE_KEY_PATH=/tmp/demo-owner.key" >&2
  echo "Then: export CIRCUS_OWNER_ID=demo-owner" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

# Run demo and filter out OTEL telemetry noise (JSON blobs with trace_id/span_id)
python3 scripts/demo_preference_flow.py 2>&1 | grep -v '"trace_id":' | grep -v '"span_id":' | grep -v '"context":' | grep -v '"kind":' | grep -v '"parent_id":' | grep -v '"start_time":' | grep -v '"end_time":' | grep -v '"status":' | grep -v '"attributes":' | grep -v '"events":' | grep -v '"links":' | grep -v '"resource":' | grep -v '"trace_state":' | grep -v '"status_code":' | grep -v '"asgi.event.type":' | grep -v '"http\.' | grep -v '"net\.' | grep -v '"service\.' | grep -v 'schema_url' | grep -v '^{$' | grep -v '^}$' | grep -v '^    },$' | grep -v '^    }$' | grep -v '"name":' | grep -v 'Loading weights' | grep -v 'LOAD REPORT' | grep -v 'UNEXPECTED' | grep -v '^Key' | grep -v '^---' | grep -v 'can be ignored' | grep -v 'Warning.*HF' | grep -v '^\s*},' | grep -v '^\s*}$' || true
