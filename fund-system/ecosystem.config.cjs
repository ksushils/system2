module.exports = {
  apps: [{
    name: "fund-system",
    script: "./server/index.js",
    cwd: "/root/fund-system",
    env: {
      NODE_ENV: "production",
      PORT: 3210,
      ADMIN_PIN: "8d896d9d",
      SCANNER_API_KEY: "490e7dda6faa789abe6f53f92b817522f65c4a5595d7c86251b1c4af8fb49aaf",
      CORS_ORIGINS: "http://72.62.134.167:3210,https://n8n.srv1282556.hstgr.cloud",
      FMP_API_KEY: process.env.FMP_API_KEY || "",
      DATABASE_URL: "postgresql://postgres:pSqL9vN4mR7wXyZ123!@127.0.0.1:5432/fund_system",
      USE_POSTGRES: "true",
      DUAL_WRITE: "true"
    },
    instances: 1,
    exec_mode: "fork",
    max_memory_restart: "1G",
    error_file: "./logs/err.log",
    out_file: "./logs/out.log",
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    merge_logs: true,
    autorestart: true,
    min_uptime: "10s",
    max_restarts: 5,
    restart_delay: 3000
  }]
};
