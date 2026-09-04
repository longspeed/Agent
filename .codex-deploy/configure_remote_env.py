from pathlib import Path
import sys


env_path = Path("/home/daytona/sendkeep/outreach-agent/.env")
base_url = sys.argv[1].rstrip("/")
updates = {
    "PUBLIC_BASE_URL": base_url,
    "GOOGLE_LOGIN_REDIRECT_URI": f"{base_url}/auth/google/callback",
    "GOOGLE_OAUTH_REDIRECT_URI": f"{base_url}/api/google/callback",
    "SECURE_COOKIES": "1",
    "WEB_CONCURRENCY": "1",
    "ENABLE_API_DOCS": "0",
}

lines = env_path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []
for line in lines:
    stripped = line.lstrip()
    key = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else None
    if key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(line)

for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")

env_path.write_text("\n".join(result) + "\n", encoding="utf-8")
env_path.chmod(0o600)
