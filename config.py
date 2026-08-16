import os

API_KEY = os.getenv("API_KEY", "cGFtYXJ0aGlzYWkyNTA0QGdtYWlsLmNvbQ.cb51e5f9024ee0fad316")
MOCK_API_BASE = os.getenv("MOCK_API_BASE", "https://pseudogram-api.onrender.com")
if os.environ.get("VERCEL"):
    DATABASE_PATH = "/tmp/linkplease.db"
else:
    DATABASE_PATH = os.getenv("DATABASE_PATH", "linkplease.db")

# Rate Limit Configuration
RATE_LIMIT_WINDOW = float(os.getenv("RATE_LIMIT_WINDOW", "60.0"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "10"))

# Retry & Worker Configuration
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
WORKER_POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL", "0.5"))
RECONCILE_POLL_INTERVAL = float(os.getenv("RECONCILE_POLL_INTERVAL", "2.0"))
