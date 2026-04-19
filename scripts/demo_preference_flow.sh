#!/usr/bin/env bash
# Circus Memory Commons — Week 4 Preference Demo
# Wrapper script for the Python demo

set -euo pipefail

cd "$(dirname "$0")/.."

# Run demo and filter out OTEL telemetry noise (JSON blobs with trace_id/span_id)
python3 scripts/demo_preference_flow.py 2>&1 | grep -v '"trace_id":' | grep -v '"span_id":' | grep -v '"context":' | grep -v '"kind":' | grep -v '"parent_id":' | grep -v '"start_time":' | grep -v '"end_time":' | grep -v '"status":' | grep -v '"attributes":' | grep -v '"events":' | grep -v '"links":' | grep -v '"resource":' | grep -v '"trace_state":' | grep -v '"status_code":' | grep -v '"asgi.event.type":' | grep -v '"http\.' | grep -v '"net\.' | grep -v '"service\.' | grep -v 'schema_url' | grep -v '^{$' | grep -v '^}$' | grep -v '^    },$' | grep -v '^    }$' | grep -v '"name":' | grep -v 'Loading weights' | grep -v 'LOAD REPORT' | grep -v 'UNEXPECTED' | grep -v '^Key' | grep -v '^---' | grep -v 'can be ignored' | grep -v 'Warning.*HF' | grep -v '^\s*},' | grep -v '^\s*}$' || true
