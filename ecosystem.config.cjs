module.exports = {
  apps: [{
    name: 'circus-api',
    script: '/root/circus/start_circus.sh',
    interpreter: 'bash',
    cwd: '/root/circus',
    max_memory_restart: '2048M',
    autorestart: true,
    watch: false,
    kill_timeout: 8000,              // wait 8s for graceful shutdown before SIGKILL
    env: {},
    min_uptime: 30000,               // <30s = counts as "crashed"
    max_restarts: 10,                // give up after 10 fast restarts
    exp_backoff_restart_delay: 2000  // 2s → 4s → 8s → 16s...
  }]
};
