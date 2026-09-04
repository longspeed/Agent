"""Preflight the production-shaped Sendkeep configuration.

This command never connects to a service. It only reads environment variables
and the local OAuth credentials file, then reports what would block startup.

Run:

    python check_env.py
    python check_env.py --json
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).parent
ENV_FILE = ROOT / "outreach-agent" / ".env"
REQUIRED = ("APP_SECRET_KEY", "SUPABASE_URL", "SUPABASE_SECRET_KEY")
PROVIDER_KEYS = ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")


def _configured_values() -> dict[str, str]:
    values = {
        key: value or ""
        for key, value in dotenv_values(ENV_FILE).items()
        if isinstance(key, str)
    }
    values.update({key: value for key, value in os.environ.items() if value})
    return values


def collect_report() -> dict[str, list[str]]:
    values = _configured_values()
    errors = [f"Missing {key}" for key in REQUIRED if not values.get(key, "").strip()]
    if not any(values.get(key, "").strip() for key in PROVIDER_KEYS) and not values.get(
        "CLIPROXY_BASE_URL", ""
    ).strip():
        errors.append("Configure one LLM provider key or CLIPROXY_BASE_URL")

    warnings = []
    if not (ROOT / "outreach-agent" / "credentials.json").exists():
        warnings.append(
            "outreach-agent/credentials.json is missing; Google OAuth cannot be completed"
        )
    if not values.get("WORKER_HEALTH_TOKEN", "").strip():
        warnings.append("WORKER_HEALTH_TOKEN is missing; external worker monitoring is disabled")
    if not values.get("TRANSACTIONAL_EMAIL_API_KEY", "").strip() or not values.get(
        "TRANSACTIONAL_EMAIL_FROM", ""
    ).strip():
        warnings.append(
            "Transactional email is missing; Gmail reconnect/recovery alerts are disabled"
        )
    if not values.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip():
        warnings.append("GOOGLE_OAUTH_REDIRECT_URI is using the application default")

    return {"errors": errors, "warnings": warnings}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Sendkeep configuration without making network calls."
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = collect_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Sendkeep configuration preflight")
        for label in ("errors", "warnings"):
            for message in report[label]:
                print(f"{label[:-1].upper()}: {message}")
        if not report["errors"]:
            print("OK: required startup configuration is present")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
