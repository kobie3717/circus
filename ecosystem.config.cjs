module.exports = {
  apps: [{
    name: 'circus-api',
    script: 'circus',
    args: 'serve --port 6200',
    cwd: '/root/circus',
    max_memory_restart: 2147483648,
    autorestart: true,
    watch: false,
    env: {}
  }]
};
