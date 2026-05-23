"""Submit / list / inspect WhatsApp message templates via the Meta Graph API.

Usage:
    uv run python scripts/whatsapp_template_submit.py list
    uv run python scripts/whatsapp_template_submit.py submit templates/appointment_reminder_v1.json
    uv run python scripts/whatsapp_template_submit.py status appointment_reminder_v1
    uv run python scripts/whatsapp_template_submit.py delete appointment_reminder_v1

Required environment variables (read from .env if python-dotenv is installed):
    WHATSAPP_ACCESS_TOKEN              system-user token with whatsapp_business_management
    WHATSAPP_BUSINESS_ACCOUNT_ID       WABA id (NOT the phone number id)
    WHATSAPP_GRAPH_VERSION             optional, defaults to v22.0

The WABA id is shown in Meta Business Manager → WhatsApp Manager → Settings.
A submitted template usually transitions APPROVED in 1–60 minutes; until
APPROVED, sends will fail with `(#132001) Template name does not exist`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _required_env() -> tuple[str, str, str]:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    waba = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
    if not token:
        raise SystemExit("WHATSAPP_ACCESS_TOKEN is unset")
    if not waba:
        raise SystemExit(
            "WHATSAPP_BUSINESS_ACCOUNT_ID is unset — find it in Meta Business "
            "Manager → WhatsApp Manager → Settings"
        )
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v22.0")
    return token, waba, version


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def cmd_list() -> int:
    token, waba, version = _required_env()
    url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
    r = httpx.get(url, headers=_headers(token), timeout=15)
    if r.status_code != 200:
        print(json.dumps(r.json(), indent=2))
        return 1
    body = r.json()
    for t in body.get("data", []):
        print(f"{t['status']:12} {t.get('language','-'):4} {t['name']}")
    return 0


def cmd_submit(template_path: str) -> int:
    token, waba, version = _required_env()
    path = Path(template_path)
    if not path.exists():
        raise SystemExit(f"template file not found: {path}")
    template = json.loads(path.read_text())
    url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
    r = httpx.post(url, headers=_headers(token), json=template, timeout=15)
    print(f"HTTP {r.status_code}")
    print(json.dumps(r.json(), indent=2))
    return 0 if r.status_code == 200 else 1


def cmd_status(name: str) -> int:
    token, waba, version = _required_env()
    url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
    r = httpx.get(
        url,
        headers=_headers(token),
        params={"name": name},
        timeout=15,
    )
    if r.status_code != 200:
        print(json.dumps(r.json(), indent=2))
        return 1
    body = r.json()
    matches = [t for t in body.get("data", []) if t["name"] == name]
    if not matches:
        print(f"no template named {name!r}")
        return 1
    for t in matches:
        print(json.dumps(t, indent=2))
    return 0


def cmd_delete(name: str) -> int:
    token, waba, version = _required_env()
    url = f"https://graph.facebook.com/{version}/{waba}/message_templates"
    r = httpx.delete(
        url,
        headers=_headers(token),
        params={"name": name},
        timeout=15,
    )
    print(f"HTTP {r.status_code}")
    print(json.dumps(r.json(), indent=2))
    return 0 if r.status_code == 200 else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "list":
        return cmd_list()
    if cmd == "submit" and len(argv) >= 3:
        return cmd_submit(argv[2])
    if cmd == "status" and len(argv) >= 3:
        return cmd_status(argv[2])
    if cmd == "delete" and len(argv) >= 3:
        return cmd_delete(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
