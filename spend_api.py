"""
spend_api.py - fetch authoritative Claude spend from the claude.ai usage API.

This is the ground-truth counterpart to the local JSONL scanner: it returns the
per-day cost and token totals Anthropic actually bills, org-wide across every
machine, which the local scanner (this machine's transcripts only) can then be
reconciled against.

Credentials live OUTSIDE the repo and are never logged. Resolution order:
  1. env: CLAUDE_AI_ORG_ID + CLAUDE_AI_COOKIE
  2. ~/.claude/claude-usage/credentials.json  {"org_id": "...", "cookie": "..."}

The cookie is a full claude.ai session cookie string (it must include
sessionKey plus the Cloudflare cookies cf_clearance/__cf_bm, or Cloudflare
rejects the request). These expire; callers should handle AuthError by
prompting for a fresh cookie.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / "claude-usage" / "credentials.json"
BASE = "https://claude.ai/api/organizations/{org}/usage/spend"

# granularity=hourly is rejected by the endpoint; daily is the finest bucket.
GRANULARITY = "daily"


class SpendApiError(Exception):
    pass


class AuthError(SpendApiError):
    """Credentials missing, expired, or rejected (401/403)."""


class RateLimitError(SpendApiError):
    """Endpoint returned 429; back off before retrying."""


def load_credentials():
    org = os.environ.get("CLAUDE_AI_ORG_ID")
    cookie = os.environ.get("CLAUDE_AI_COOKIE")
    if org and cookie:
        return org, cookie
    if CREDENTIALS_PATH.exists():
        data = json.loads(CREDENTIALS_PATH.read_text())
        org = org or data.get("org_id")
        cookie = cookie or data.get("cookie")
    if not org or not cookie:
        raise AuthError(
            "No claude.ai credentials. Set CLAUDE_AI_ORG_ID + CLAUDE_AI_COOKIE, "
            f"or write {CREDENTIALS_PATH}."
        )
    return org, cookie


def has_credentials():
    try:
        load_credentials()
        return True
    except SpendApiError:
        return False


def fetch_spend(start_date, end_date, group_by="model_tier", timeout=30):
    """Return the parsed JSON for [start_date, end_date] (inclusive, YYYY-MM-DD).

    group_by is "model_tier" or "product_surface". Raises AuthError /
    RateLimitError / SpendApiError on failure.
    """
    org, cookie = load_credentials()
    qs = urllib.parse.urlencode({
        "start_date": start_date,
        "end_date": end_date,
        "group_by": group_by,
        "granularity": GRANULARITY,
    })
    url = BASE.format(org=urllib.parse.quote(org)) + "?" + qs
    req = urllib.request.Request(url, headers={
        "accept": "*/*",
        "anthropic-client-platform": "web_claude_ai",
        "content-type": "application/json",
        "referer": "https://claude.ai/new",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "cookie": cookie,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"claude.ai rejected credentials (HTTP {e.code}); refresh cookie.")
        if e.code == 429:
            raise RateLimitError("claude.ai returned HTTP 429; back off.")
        raise SpendApiError(f"claude.ai HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise SpendApiError(f"claude.ai request failed: {e.reason}")

    if isinstance(payload, dict) and payload.get("type") == "error":
        raise SpendApiError(payload.get("error", {}).get("message", "unknown API error"))
    return payload


# API model_tier group keys use underscores and version suffixes
# (claude_opus_4_8, claude_haiku_4_5_20251001). Collapse to the pricing
# family the local scanner keys on.
def tier_family(group_key):
    k = group_key.lower()
    if "opus" in k:
        return "opus"
    if "sonnet" in k:
        return "sonnet"
    if "haiku" in k:
        return "haiku"
    return "other"


# Pull sessionKey / org id out of a raw key, a "sessionKey=…" cookie string, or
# a whole "copy as cURL" blob — whatever the user finds easiest to paste.
_SESSION_KEY_RE = re.compile(r"sessionKey=([^;\s\"']+)")
_RAW_KEY_RE = re.compile(r"(sk-ant-sid\S+)")
_ORG_RE = re.compile(r"organizations/([0-9a-fA-F-]{36})")


def parse_credentials_input(text):
    """Return {"session_key", "org_id"} extracted from arbitrary pasted text.
    Either field is None if not found."""
    text = (text or "").strip()
    m = _SESSION_KEY_RE.search(text)
    key = m.group(1) if m else None
    if not key:
        m = _RAW_KEY_RE.search(text)
        key = m.group(1).rstrip("\";',") if m else None
    m = _ORG_RE.search(text)
    return {"session_key": key, "org_id": m.group(1) if m else None}


def save_credentials(session_key, org_id=None):
    """Write the sessionKey (and org id, if given/known) to the credentials
    file. sessionKey alone authenticates, so that's all the cookie needs.
    Returns the org id now on file."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text())
        except ValueError:
            data = {}
    if org_id:
        data["org_id"] = org_id
    data["cookie"] = "sessionKey=" + session_key
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    return data.get("org_id")


def mask_key(session_key):
    return ("…" + session_key[-6:]) if session_key else "(none)"


def refresh_spend(db_path=None, start=None, end=None, group_by="model_tier"):
    """Fetch spend and upsert it into `api_spend`, recording the attempt's
    outcome in schema_meta so the dashboard can show staleness/expiry.

    Never raises for the expected failure modes; returns a status dict with
    `status` one of: ok, no_credentials, auth_failed, rate_limited,
    network_error.
    """
    import scanner
    from datetime import date, datetime

    today = date.today()
    start = start or today.replace(day=1).isoformat()
    end = end or today.isoformat()
    attempt_at = datetime.now().isoformat(timespec="seconds")

    if not has_credentials():
        return {"status": "no_credentials", "attempt_at": attempt_at,
                "message": "No claude.ai credentials set."}

    def record(status, error=""):
        try:
            conn = scanner.get_db(db_path) if db_path else scanner.get_db()
            scanner.init_db(conn)
            scanner.record_api_fetch(conn, attempt_at, status, error)
            conn.close()
        except Exception:
            pass

    try:
        data = fetch_spend(start, end, group_by=group_by)
    except AuthError as e:
        record("auth_failed", str(e))
        return {"status": "auth_failed", "attempt_at": attempt_at, "message": str(e)}
    except RateLimitError as e:
        record("rate_limited", str(e))
        return {"status": "rate_limited", "attempt_at": attempt_at, "message": str(e)}
    except SpendApiError as e:
        record("network_error", str(e))
        return {"status": "network_error", "attempt_at": attempt_at, "message": str(e)}

    conn = scanner.get_db(db_path) if db_path else scanner.get_db()
    scanner.init_db(conn)
    scanner.store_api_spend(conn, data["series"], group_by, attempt_at)
    scanner.record_api_fetch(conn, attempt_at, "ok", "")
    conn.close()
    total = sum(s["cost_minor_units"] for s in data["series"]) / 100
    days = len({s["bucket"] for s in data["series"]})
    return {"status": "ok", "attempt_at": attempt_at, "total": total, "days": days,
            "rows": len(data["series"]), "start": start, "end": end,
            "message": f"{days} days, ${total:,.2f} total"}


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-07-13"
    data = fetch_spend(start, end, group_by="model_tier")
    total = sum(s["cost_minor_units"] for s in data["series"]) / 100
    print(f"{start}..{end}: {len(data['series'])} rows, ${total:,.2f} total")
