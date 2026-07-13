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


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-07-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-07-13"
    data = fetch_spend(start, end, group_by="model_tier")
    total = sum(s["cost_minor_units"] for s in data["series"]) / 100
    print(f"{start}..{end}: {len(data['series'])} rows, ${total:,.2f} total")
