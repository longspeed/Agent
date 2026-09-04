"""Fast, credential-free checks for the local developer workflow.

Run from the repository root:

    python tests/test_devex_tools.py
"""

import os
import subprocess
import sys
import traceback
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run_script(path, *args):
    env = os.environ.copy()
    for key in (
        "APP_SECRET_KEY",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "OPENROUTER_API_KEY",
    ):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(ROOT / path), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_worker_help_does_not_load_external_configuration():
    result = _run_script("outreach-agent/watch_replies.py", "--help")
    assert result.returncode == 0
    assert "--once" in result.stdout
    assert "Traceback" not in result.stderr


def test_send_help_does_not_load_external_configuration():
    result = _run_script("outreach-agent/send_outreach.py", "--help")
    assert result.returncode == 0
    assert "account_email" in result.stdout
    assert "Traceback" not in result.stderr


def test_invalid_worker_option_fails_before_external_configuration():
    result = _run_script("outreach-agent/watch_replies.py", "--not-a-real-option")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert "Traceback" not in result.stderr


def test_local_demo_is_seeded_and_never_sends():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

        import dev_server

        client = TestClient(dev_server.app)
        state = client.get("/api/demo/state")
        assert state.status_code == 200
        assert state.json()["summary"]["needs_review"] == 3
        assert state.json()["safe_to_send"] is False
        for auth_path in ("/signup", "/login"):
            auth_redirect = client.get(auth_path, follow_redirects=False)
            assert auth_redirect.status_code == 303
            assert auth_redirect.headers["location"] == "/demo"
        assert client.get("/api/openapi.json").status_code == 200
        assert client.get("/api/plan").json()["daily_send_limit"] == 25

        xlsx = client.get("/api/lead-sheet-template.xlsx")
        assert xlsx.status_code == 200
        assert xlsx.content[:2] == b"PK"
        csv = client.get("/api/lead-sheet-template.csv")
        assert csv.status_code == 200
        assert "Name,Email,Company" in csv.text

        send = client.post("/api/demo/send")
        assert send.status_code == 409
        assert "never sends email" in send.json()["detail"]

        reviewed = client.post("/api/demo/queue/reply-maya-chen/review")
        assert reviewed.status_code == 200
        assert reviewed.json()["state"]["summary"]["needs_review"] == 2

        reset = client.post("/api/demo/reset")
        assert reset.status_code == 200
        assert reset.json()["state"]["summary"]["needs_review"] == 3


def main():
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    if failed:
        print("Failed:", ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
