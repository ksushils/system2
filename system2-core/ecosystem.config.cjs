module.exports = {
  apps: [{
    name: "entry-monitor",
    script: "./entry_trigger_monitor.py",
    cwd: "/root/system2-core",
    interpreter: "/root/system2-core/.venv/bin/python",
    env: {
      NODE_ENV: "production"
    },
    instances: 1,
    exec_mode: "fork",
    max_memory_restart: "512M",
    error_file: "./logs/entry-monitor-err.log",
    out_file: "./logs/entry-monitor-out.log",
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    merge_logs: true,
    autorestart: true,
    min_uptime: "10s",
    max_restarts: 5,
    restart_delay: 3000
  }]
};
