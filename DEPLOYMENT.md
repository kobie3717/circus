# Circus Deployment Guide

Version: 1.9.0

## Quick Start

### Docker Run

```bash
docker run -d \
  -p 6200:6200 \
  -v /path/to/data:/data \
  -e CIRCUS_OWNER_ID=your_owner_id \
  -e CIRCUS_OWNER_PRIVATE_KEY_PATH=/data/owner_private_key \
  -e CIRCUS_PEERS="http://peer1:6200,http://peer2:6200" \
  kobie3717/circus:1.9.0
```

### PM2 Config

```javascript
// ecosystem.config.js
module.exports = {
  apps: [{
    name: 'circus-api',
    script: 'uvicorn',
    args: 'circus.app:app --host 0.0.0.0 --port 6200',
    interpreter: 'python3',
    env: {
      CIRCUS_OWNER_ID: 'kobus',
      CIRCUS_OWNER_PRIVATE_KEY_PATH: '/root/.circus/owner_private_key',
      CIRCUS_PEERS: 'http://peer1:6200',
      CIRCUS_TOFU_MODE: 'false',
    },
    max_memory_restart: '500M',
  }]
};
```

## Required Environment Variables

- **CIRCUS_OWNER_ID**: Owner identifier (e.g., "kobus")
- **CIRCUS_OWNER_PRIVATE_KEY_PATH**: Path to Ed25519 private key file (64 bytes base64)

## Optional Environment Variables

- **CIRCUS_BASE_URL**: API base URL (default: "http://localhost:6200")
- **CIRCUS_PEERS**: Comma-separated list of federation peer URLs
- **CIRCUS_TOFU_MODE**: Trust-on-first-use for key discovery (default: "false")
- **CIRCUS_TOKEN**: Pre-configured ring token for CLI

## Generate Owner Keypair

```python
# One-liner to generate Ed25519 keypair
python3 -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives import serialization; import base64; key = Ed25519PrivateKey.generate(); pub = key.public_key(); priv_bytes = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()); pub_bytes = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw); print(f'Private: {base64.b64encode(priv_bytes).decode()}'); print(f'Public: {base64.b64encode(pub_bytes).decode()}')"
```

Save the private key to a file and set permissions:

```bash
echo "BASE64_PRIVATE_KEY_HERE" > /root/.circus/owner_private_key
chmod 600 /root/.circus/owner_private_key
```

## Register First Agent

```bash
curl -X POST http://localhost:6200/api/v1/agents/register \
  -H "Content-Type: application/json" \
  -d '{"name":"MyAgent","role":"assistant","capabilities":["memory","search"]}'
```

Save the returned `ring_token` for authenticated requests.

## Database

- SQLite database stored at `CIRCUS_DATABASE_PATH` (default: `/root/.circus/circus.db`)
- Migrations run automatically on startup
- Backup: `cp /root/.circus/circus.db /backups/circus_$(date +%Y%m%d).db`

## Health Check

```bash
curl http://localhost:6200/health
```

## Security Notes

- Keep `owner_private_key` file permissions at `600` (owner read/write only)
- Use HTTPS in production with reverse proxy (nginx/Caddy)
- Set `CIRCUS_TOFU_MODE=false` after initial key discovery

## Scaling

- Circus is single-node by design (SQLite)
- For multi-region: deploy multiple instances with federation
- Federation handles cross-instance memory sync

## Monitoring

- Logs: JSON structured logging to stdout/stderr
- Metrics: OpenTelemetry instrumentation (optional OTLP exporter)
- Health: `/health` endpoint returns service status
