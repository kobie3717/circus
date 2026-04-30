module.exports = {
  apps: [{
    name: 'circus-api',
    script: '/usr/bin/python3',
    args: '-m uvicorn circus.app:app --host 127.0.0.1 --port 6200',
    interpreter: 'none',
    cwd: '/root/circus',
    max_memory_restart: '2048M',
    autorestart: true,
    watch: false,
    env: {},
    // Guardrails: stop the 275-restart crash loop
    min_uptime: 60000,               // <60s = counts as "crashed"
    max_restarts: 10,                // give up after 10 fast restarts
    exp_backoff_restart_delay: 2000  // 2s → 4s → 8s → 16s...
  }]
};
