import os
from hashlib import sha256
from ipaddress import ip_network


def number(name, default, minimum=1):
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


DATABASE_URL = os.getenv("DATABASE_URL") or ("dbname=sputnik" if os.getenv("PGHOST") else "")
APP_URL = (os.getenv("APP_URL") or "http://127.0.0.1:" + os.getenv("SPUTNIK_PORT", "18080")).rstrip("/")
SESSION_DAYS = number("SESSION_DAYS", 1)
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_LOCAL_MODE = os.getenv("AI_LOCAL_MODE", "").lower() in ("1", "true", "yes")
AI_CONSENT_SCOPE = sha256((AI_BASE_URL + "|" + str(AI_LOCAL_MODE)).encode()).hexdigest()
AI_MODEL = os.getenv("AI_MODEL", "deepseek-flash")
AI_REASONING_EFFORT = os.getenv("AI_REASONING_EFFORT", "")
AI_MAX_OUTPUT_TOKENS = number("AI_MAX_OUTPUT_TOKENS", 4096)
AI_MAX_PROMPT_BYTES = number("AI_MAX_PROMPT_BYTES", 30000)
AI_SESSION_TOKENS = number("AI_SESSION_TOKENS", 60000)
AI_ANON_DAILY_TOKENS = number("AI_ANON_DAILY_TOKENS", 200000)
AI_SESSION_REQUESTS = number("AI_SESSION_REQUESTS", 30)
AI_IP_DAILY_REQUESTS = number("AI_IP_DAILY_REQUESTS", 40)
TRUSTED_PROXY_NETWORKS = [
    ip_network(value.strip()) for value in os.getenv("TRUSTED_PROXY_NETWORKS", "").split(",") if value.strip()
]
AI_TIMEOUT_SECONDS = number("AI_TIMEOUT_SECONDS", 40)
TELEMETRY_DAYS = number("TELEMETRY_DAYS", 30)
YANDEX_CLIENT_ID = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
ADMIN_YANDEX_IDS = set(filter(None, os.getenv("ADMIN_YANDEX_IDS", "").replace(" ", "").split(",")))
GEOCODER_URL = os.getenv("GEOCODER_URL", "https://nominatim.openstreetmap.org").rstrip("/")
GEOCODER_USER_AGENT = os.getenv("GEOCODER_USER_AGENT", "").strip()
GEOCODER_ATTRIBUTION = os.getenv("GEOCODER_ATTRIBUTION", "© OpenStreetMap contributors")
