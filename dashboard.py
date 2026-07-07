"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime

from scanner import VERSION, init_db

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# Which surface is rendering the dashboard: "web" (standalone `cli.py dashboard`)
# or "vscode" (embedded in the extension's sidebar webview). serve() sets this
# from the --surface flag the extension passes. The footer reads it to decide
# what to show — the web build promotes the VS Code extension and offers a
# "check GitHub for a newer release" update link; the embedded build shows just
# the version (VS Code updates the extension itself, and a GitHub-release check
# would misfire there because the Marketplace publish lags the GitHub release).
SURFACE = "web"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    # The dashboard reads while a background scan may be committing (cmd_dashboard
    # serves first, scans in a background thread; /api/rescan scans in-process too).
    # Wait briefly for write locks instead of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. cmd_dashboard binds and serves
    # *before* its background scan runs init_db, so on the first load after an
    # upgrade a pre-existing DB may still be on the old schema — the subagent
    # queries below reference the `agents` table and the `is_subagent`/`agent_id`
    # columns and would raise "no such table: agents" until the scan caught up.
    # init_db is idempotent (CREATE ... IF NOT EXISTS + additive column checks),
    # so this is a cheap no-op once migrated.
    init_db(conn)

    # ── All models (for filter UI) ────────────────────────────────────────────
    # GROUP BY uses the normalised expression too so NULL and '' don't end up
    # as two separate "unknown" rows.
    model_rows = conn.execute("""
        SELECT COALESCE(NULLIF(model, ''), 'unknown') as model
        FROM turns
        GROUP BY COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(NULLIF(model, ''), 'unknown') as model,
            COALESCE(is_subagent, 0)   as is_subagent,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            SUM(cache_creation_1h_tokens) as cache_creation_1h,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, COALESCE(NULLIF(model, ''), 'unknown'), COALESCE(is_subagent, 0)
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "is_subagent":    r["is_subagent"] or 0,
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(NULLIF(model, ''), 'unknown')    as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, COALESCE(NULLIF(model, ''), 'unknown')
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, total_cache_creation_1h,
            model, turn_count, git_branch, topic
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            # Full id: the table truncates for display, but the CSV export
            # needs the whole thing (an 8-char prefix isn't uniquely useful).
            "session_id":    r["session_id"],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "topic":         r["topic"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
            "cache_creation_1h": r["total_cache_creation_1h"] or 0,
        })

    # ── Subagent breakdown by type, by day & model ────────────────────────────
    # JOIN turns to agents (parent tool_result metadata captured by the scanner).
    # acompact-* ids are Claude Code's auto-compaction subagent (no parent
    # dispatch record); anything else without a match is shown as 'unknown'.
    AGENT_TYPE_EXPR = (
        "COALESCE(a.agent_type, "
        "CASE WHEN t.agent_id LIKE 'acompact-%' THEN 'auto-compact' "
        "ELSE 'unknown' END)"
    )

    subagent_daily_rows = conn.execute(f"""
        SELECT
            substr(t.timestamp, 1, 10)               as day,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            SUM(t.cache_creation_1h_tokens)          as cache_creation_1h,
            COUNT(DISTINCT t.agent_id)               as dispatches,
            COUNT(*)                                 as turns
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.is_subagent = 1
        GROUP BY day, agent_type, model
        ORDER BY day, agent_type
    """).fetchall()

    subagent_by_type = [{
        "day":            r["day"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "dispatches":     r["dispatches"] or 0,
        "turns":          r["turns"] or 0,
    } for r in subagent_daily_rows]

    # ── Top individual subagent dispatches (one row per agent_id) ─────────────
    top_dispatch_rows = conn.execute(f"""
        SELECT
            t.agent_id                               as agent_id,
            {AGENT_TYPE_EXPR}                        as agent_type,
            COALESCE(NULLIF(t.model, ''), 'unknown') as model,
            MIN(t.timestamp)                         as start_ts,
            SUM(t.input_tokens)                      as input,
            SUM(t.output_tokens)                     as output,
            SUM(t.cache_read_tokens)                 as cache_read,
            SUM(t.cache_creation_tokens)             as cache_creation,
            SUM(t.cache_creation_1h_tokens)          as cache_creation_1h,
            COUNT(*)                                 as turns,
            a.dispatched_in_session                  as parent_session,
            a.total_duration_ms                      as duration_ms,
            a.tool_use_count                         as tool_uses,
            a.status                                 as status
        FROM turns t
        LEFT JOIN agents a ON t.agent_id = a.agent_id
        WHERE t.is_subagent = 1 AND t.agent_id IS NOT NULL
        GROUP BY t.agent_id
        ORDER BY (SUM(t.input_tokens) + SUM(t.output_tokens)
                  + SUM(t.cache_read_tokens) + SUM(t.cache_creation_tokens)) DESC
    """).fetchall()

    top_dispatches = [{
        "agent_id":       r["agent_id"],
        "agent_type":     r["agent_type"],
        "model":          r["model"],
        "start":          (r["start_ts"] or "")[:16].replace("T", " "),
        "start_date":     (r["start_ts"] or "")[:10],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "turns":          r["turns"] or 0,
        "duration_ms":    r["duration_ms"],
        "tool_uses":      r["tool_uses"],
        "status":         r["status"],
    } for r in top_dispatch_rows]

    # ── Daily per-project per-model (for the daily project spend charts) ──────
    daily_project_rows = conn.execute("""
        SELECT
            substr(t.timestamp, 1, 10)                  as day,
            COALESCE(s.project_name, 'unknown')          as project,
            COALESCE(NULLIF(t.model, ''), 'unknown')     as model,
            COALESCE(t.is_subagent, 0)                   as is_subagent,
            SUM(t.input_tokens)                          as input,
            SUM(t.output_tokens)                         as output,
            SUM(t.cache_read_tokens)                     as cache_read,
            SUM(t.cache_creation_tokens)                 as cache_creation,
            SUM(t.cache_creation_1h_tokens)              as cache_creation_1h,
            COUNT(*)                                     as turns
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY day, project, t.model, is_subagent
        ORDER BY day, project, t.model
    """).fetchall()

    daily_by_project = [{
        "day":            r["day"],
        "project":        r["project"],
        "model":          r["model"],
        "is_subagent":    r["is_subagent"] or 0,
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "cache_creation_1h": r["cache_creation_1h"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_project_rows]

    conn.close()

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "daily_by_project": daily_by_project,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "subagent_by_type": subagent_by_type,
        "top_dispatches":  top_dispatches,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%231e1f20'/><text x='32' y='45' font-family='system-ui,Arial,sans-serif' font-size='38' font-weight='700' fill='%23d97757' text-anchor='middle'>%24</text></svg>">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>window.APP_CONFIG = __APP_CONFIG_JSON__;</script>
<style>
  :root {
    /* Solarized Light */
    --bg: #FDF6E3;      /* base3 — page base */
    --card: #EEE8D5;    /* base2 — raised one step above the page */
    --border: #D3CBB7;
    --text: #586E75;    /* base01 */
    --muted: #93A1A1;   /* base1 */
    --accent: #CB4B16;  /* orange */
    --blue: #268BD2;
    --green: #859900;
    --red: #DC322F;
    --raised: #E4DCC8;  /* hover / raised surfaces — top of the elevation ladder */
    --selected: #E9E2CD;  /* selected chips / tabs (neutral, not accent) */
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  /* VS Code-style scrollbars. The dashboard renders inside a webview iframe,
     which doesn't inherit VS Code's --vscode-* theme variables, so we set the
     scrollbar here: no arrows, tan thumb (#D3CBB7, #93A1A1 on hover) over an
     #EEE8D5 track, in a 21px gutter. Also fits the light UI standalone. */
  * { scrollbar-width: auto; scrollbar-color: #D3CBB7 #EEE8D5; }
  ::-webkit-scrollbar { width: 21px; height: 21px; }
  ::-webkit-scrollbar-track { background: #EEE8D5; }
  ::-webkit-scrollbar-thumb { background-color: #D3CBB7; border: 3px solid transparent; background-clip: padding-box; }
  ::-webkit-scrollbar-thumb:hover { background-color: #93A1A1; }
  ::-webkit-scrollbar-thumb:active { background-color: #93A1A1; }
  ::-webkit-scrollbar-corner { background: #EEE8D5; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--text); }
  header .header-title { display: flex; align-items: center; gap: 10px; }
  /* The icon is a monochrome silhouette (white shape on transparent). We paint
     it with the title color via a CSS mask + background-color, so it matches
     `header h1` — the lightest text color. */
  header .header-icon {
    width: 26px; height: 26px; flex-shrink: 0; display: block;
    background-color: var(--text);
    -webkit-mask: url("icon.svg") no-repeat center / contain;
    mask: url("icon.svg") no-repeat center / contain;
  }
  header .meta { color: var(--muted); font-size: 12px; text-align: right; line-height: 1.5; margin-right: 20px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  /* Model multi-select: a compact trigger in the bar that opens a grouped panel. */
  .model-select { position: relative; flex-shrink: 0; }
  .model-trigger { display: flex; align-items: center; gap: 8px; min-width: 170px; max-width: 320px; padding: 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .model-trigger:hover, .model-trigger.open { border-color: var(--accent); }
  #model-trigger-label { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .model-caret { color: var(--muted); font-size: 10px; flex-shrink: 0; transition: transform 0.15s; }
  .model-trigger.open .model-caret { transform: rotate(180deg); }
  .model-panel { position: absolute; top: calc(100% + 6px); left: 0; z-index: 50; min-width: 250px; max-width: 340px; max-height: 360px; overflow-y: auto; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); }
  .model-panel[hidden] { display: none; }
  .model-panel-actions { display: flex; gap: 6px; padding-bottom: 8px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
  .model-group-label { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); padding: 8px 8px 4px; }
  .model-cb-label { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 12px; color: var(--muted); transition: background 0.12s, color 0.12s; user-select: none; }
  .model-cb-label:hover { background: var(--raised); color: var(--text); }
  .model-cb-label.checked { color: var(--text); }
  .model-cb-label input { display: none; }
  .model-cb-box { width: 15px; height: 15px; flex-shrink: 0; border-radius: 4px; border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 10px; line-height: 1; color: transparent; transition: background 0.12s, border-color 0.12s; }
  .model-cb-label.checked .model-cb-box { background: var(--accent); border-color: var(--accent); color: #fff; }
  .model-cb-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  /* Date range — a compact dropdown. The old segmented button row (8 buttons)
     wrapped badly in the narrow VS Code panel; a single select stays put. Styled
     to match the model trigger. */
  .range-select { position: relative; flex-shrink: 0; }
  .range-select select { appearance: none; -webkit-appearance: none; min-width: 150px; padding: 5px 30px 5px 10px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 12px; cursor: pointer; transition: border-color 0.15s; }
  .range-select select:hover, .range-select select:focus { border-color: var(--accent); outline: none; }
  .range-select::after { content: "\25BE"; position: absolute; right: 11px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 10px; pointer-events: none; }
  .range-select option { background: var(--card); color: var(--text); }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .spend-limit-summary { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
  .spend-limit-summary .amount { font-size: 22px; font-weight: 700; }
  .spend-limit-summary .pct { font-size: 13px; color: var(--muted); }
  .spend-limit-bar-track { height: 8px; border-radius: 4px; background: var(--raised); overflow: hidden; margin-bottom: 8px; }
  .spend-limit-bar-fill { height: 100%; background: var(--green); border-radius: 4px; transition: width 0.2s; }
  .spend-limit-bar-fill.over { background: var(--accent); }
  .spend-limit-breakdown { font-size: 12px; color: var(--muted); }
  .spend-limit-projection { display: block; font-size: 13px; margin-top: 6px; }
  .spend-limit-caption { font-size: 11px; color: var(--muted); margin-top: 6px; }
  .spend-limit-chart-wrap { position: relative; height: 220px; margin-top: 4px; }
  /* Two independent legend groups instead of Chart.js's single centered row, so
     each group sits directly above the axis (left/right) it describes. */
  .spend-legend-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
  .spend-legend-group { display: flex; gap: 14px; flex-wrap: wrap; }
  .spend-legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); cursor: pointer; user-select: none; }
  .spend-legend-item.hidden { opacity: 0.4; text-decoration: line-through; }
  .spend-legend-swatch { width: 12px; height: 12px; border-radius: 2px; flex-shrink: 0; }
  .spend-limit-settings { display: flex; flex-direction: column; gap: 14px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
  .spend-limit-settings-row { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
  .spend-limit-settings label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .spend-limit-settings input { width: 220px; padding: 5px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: 13px; }
  .spend-limit-settings input:focus { outline: none; border-color: var(--accent); }
  .spend-reports-list { display: flex; flex-direction: column; gap: 4px; max-height: 160px; overflow-y: auto; }
  .spend-report-row { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text); padding: 4px 8px; border-radius: 5px; background: var(--raised); }
  .spend-report-row .date { color: var(--muted); min-width: 90px; }
  .spend-report-row .amount { flex: 1; }
  .spend-report-del { background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 15px; line-height: 1; padding: 0 4px; }
  .spend-report-del:hover { color: var(--red); }
  .spend-reports-empty { font-size: 12px; color: var(--muted); font-style: italic; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  /* min-width:0 lets the grid column shrink below the canvas's intrinsic
     pixel width; without it, narrowing the window can't narrow the container,
     so Chart.js's ResizeObserver never fires until a data refresh rebuilds the
     canvas. (Expanding already works — 1fr columns grow freely.) */
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; min-width: 0; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: var(--raised); color: var(--text); }
  .tz-btn.active { background: var(--selected); color: var(--text); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: var(--red); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--raised); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(72,160,199,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .topic-cell { box-sizing: border-box; min-width: 160px; max-width: 260px; overflow-wrap: anywhere; font-size: 12px; color: var(--text); }
  .untitled { color: var(--muted); font-style: italic; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }
  .table-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 12px; }
  .table-foot:empty { margin-top: 0; }
  .show-more-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  .show-more-btn:hover { color: var(--text); border-color: var(--accent); }
  .show-more-link { color: var(--blue); text-decoration: none; font-size: 12px; cursor: pointer; }
  .show-more-link:hover { text-decoration: underline; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }
  .footer-content a.update-link { color: var(--accent); font-weight: 600; }

  /* Inline info affordance (e.g. the dispatches table) — native title tooltip. */
  .info-icon { display: inline-flex; align-items: center; vertical-align: middle; margin-left: 3px; color: var(--muted); cursor: help; }
  .info-icon svg { display: block; }
  .info-icon:hover { color: var(--text); }

  /* Collapsible cards — a full section fold, independent of in-table Show
     more/less (which only pages rows). Collapsing hides the card body and its
     header controls, leaving just the caret + title. State persists per card in
     localStorage. */
  .card-caret { display: inline-block; width: 0.9em; margin-right: 7px; font-size: 14px; line-height: 1; color: inherit; transform: rotate(90deg); transition: transform 0.15s; }
  .collapsed .card-caret { transform: rotate(0deg); }
  .chart-card > h2, .chart-header > h2, .section-title { cursor: pointer; user-select: none; }
  .chart-card > h2:hover, .chart-header > h2:hover, .section-title:hover { color: var(--text); }
  .info-icon:focus-visible, .chart-card > h2:focus-visible, .chart-header > h2:focus-visible, .section-title:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chart-card.collapsed > h2, .chart-card.collapsed > .chart-header { margin-bottom: 0; }
  .table-card.collapsed > .section-title, .table-card.collapsed > .section-header { margin-bottom: 0; }
  .chart-card.collapsed > *:not(h2):not(.chart-header),
  .chart-card.collapsed .chart-header > *:not(h2),
  .table-card.collapsed > *:not(.section-title):not(.section-header),
  .table-card.collapsed .section-header > *:not(.section-title) { display: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <div class="header-title">
    <span class="header-icon" role="img" aria-label="Claude Usage"></span>
    <h1>Claude Code Usage</h1>
  </div>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Scan for new usage since the last update. Adds new turns without affecting existing history.">&#x21bb; Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div class="model-select" id="model-select">
    <button class="model-trigger" id="model-trigger" aria-haspopup="true" aria-expanded="false" onclick="toggleModelPanel(event)">
      <span id="model-trigger-label">All models</span>
      <span class="model-caret">&#9662;</span>
    </button>
    <div class="model-panel" id="model-panel" hidden>
      <div class="model-panel-actions">
        <button class="filter-btn" onclick="selectAllModels()">All</button>
        <button class="filter-btn" onclick="clearAllModels()">None</button>
      </div>
      <div id="model-checkboxes"></div>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-select">
    <select id="range-select" aria-label="Date range" onchange="setRange(this.value)">
      <option value="today">Today</option>
      <option value="week">This Week</option>
      <option value="month">This Month</option>
      <option value="prev-month">Previous Month</option>
      <option value="7d">Last 7 Days</option>
      <option value="30d">Last 30 Days</option>
      <option value="90d">Last 90 Days</option>
      <option value="all">All Time</option>
    </select>
  </div>
</div>

<div class="container">
  <div class="table-card" id="sec-spend-limit" data-card="spend-limit">
    <div class="section-header">
      <div class="section-title"><span class="card-caret">&#9656;</span>Monthly Spend Limit</div>
      <button class="export-btn" onclick="toggleSpendLimitSettings()" title="Set your monthly limit and any spend from other machines/products not tracked here">Edit</button>
    </div>
    <div id="spend-limit-body">
      <div class="spend-limit-summary">
        <span class="amount" id="spend-limit-amount"></span>
        <span class="pct" id="spend-limit-pct"></span>
      </div>
      <div class="spend-limit-bar-track"><div class="spend-limit-bar-fill" id="spend-limit-bar"></div></div>
      <div class="spend-limit-breakdown" id="spend-limit-breakdown"></div>
      <span class="spend-limit-projection" id="spend-limit-projection"></span>
      <div class="spend-legend-row">
        <div class="spend-legend-group" id="spend-legend-left"></div>
        <div class="spend-legend-group" id="spend-legend-right"></div>
      </div>
      <div class="spend-limit-chart-wrap"><canvas id="chart-spend-daily"></canvas></div>
      <div class="spend-limit-caption">Projected from your last 7 days of local usage; non-tracked spend estimated from your latest report.</div>
      <div class="spend-limit-settings" id="spend-limit-settings" style="display:none;">
        <div class="spend-limit-settings-row">
          <label>Monthly limit ($)<input type="number" id="spend-limit-input" min="0" step="1"></label>
          <button class="export-btn" onclick="saveSpendLimitAmount()">Save limit</button>
        </div>
        <div class="spend-limit-settings-row">
          <label>What Claude.ai shows as "spent this month" ($)<input type="number" id="spend-report-input" min="0" step="0.01" placeholder="e.g. 276.50"></label>
          <button class="export-btn" onclick="addSpendReport()">Add report for today</button>
        </div>
        <div class="spend-reports-list" id="spend-reports-list"></div>
      </div>
    </div>
  </div>
  <div class="charts-grid">
    <div class="chart-card wide" id="sec-daily-cost" data-card="daily-cost">
      <h2><span class="card-caret">&#9656;</span><span id="daily-cost-chart-title">Daily Spend by Model</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily-cost"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-daily-project" data-card="daily-project">
      <h2><span class="card-caret">&#9656;</span><span id="daily-project-chart-title">Daily Spend by Project</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily-project"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-daily-project-model" data-card="daily-project-model">
      <h2><span class="card-caret">&#9656;</span><span id="daily-project-model-chart-title">Daily Spend by Project per Model</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily-project-model"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-hourly" data-card="hourly">
      <div class="chart-header">
        <h2><span class="card-caret">&#9656;</span><span id="hourly-chart-title">Average Hourly Distribution</span></h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card" id="sec-models" data-card="model-chart">
      <h2><span class="card-caret">&#9656;</span>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card" id="sec-projects" data-card="project-chart">
      <h2><span class="card-caret">&#9656;</span>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-subagents" data-card="subagent-chart">
      <h2><span class="card-caret">&#9656;</span><span id="subagent-chart-title">Subagent Tokens by Type</span></h2>
      <div class="chart-wrap"><canvas id="chart-subagent"></canvas></div>
    </div>
    <div class="chart-card wide" id="sec-daily" data-card="daily">
      <h2><span class="card-caret">&#9656;</span><span id="daily-chart-title">Daily Token Usage</span></h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
  </div>
  <div class="stats-row" id="stats-row"></div>
  <div class="table-card" id="sec-cost-model" data-card="cost-by-model">
    <div class="section-title"><span class="card-caret">&#9656;</span>Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th>
        <th class="sortable" onclick="setModelSort('turns')">Turns <span class="sort-icon" id="msort-turns"></span></th>
        <th class="sortable" onclick="setModelSort('input')">Input <span class="sort-icon" id="msort-input"></span></th>
        <th class="sortable" onclick="setModelSort('output')">Output <span class="sort-icon" id="msort-output"></span></th>
        <th class="sortable" onclick="setModelSort('cache_read')">Cache Read <span class="sort-icon" id="msort-cache_read"></span></th>
        <th class="sortable" onclick="setModelSort('cache_creation')">Cache Creation <span class="sort-icon" id="msort-cache_creation"></span></th>
        <th class="sortable" onclick="setModelSort('cost')">Est. Cost <span class="sort-icon" id="msort-cost"></span></th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
    <div class="table-foot" id="model-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-dispatches" data-card="dispatches">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Top Subagent Dispatches <span class="info-icon" tabindex="0" role="img" aria-label="About this table" title="Ranked by total tokens. &quot;unknown&quot; means the parent dispatch record wasn't found."><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg></span></div><button class="export-btn" onclick="exportDispatchesCSV()" title="Export all filtered subagent dispatches to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Type</th><th>Started</th><th>Model</th><th>Turns</th><th>Tool Uses</th>
        <th>Duration</th><th>Input</th><th>Output</th><th>Cache Read</th><th>Tokens</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="dispatches-body"></tbody>
    </table>
    <div class="table-foot" id="dispatches-foot"></div>
  </div>
  <div class="table-card" id="sec-sessions" data-card="sessions">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Recent Sessions</div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Project</th>
        <th>Title</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
    <div class="table-foot" id="sessions-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-project" data-card="cost-by-project">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-cost-foot"></div>
  </div>
  <div class="table-card" id="sec-cost-branch" data-card="cost-by-branch">
    <div class="section-header"><div class="section-title"><span class="card-caret">&#9656;</span>Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th>Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')">Sessions <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')">Turns <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')">Input <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')">Output <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')">Est. Cost <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
    <div class="table-foot" id="project-branch-cost-foot"></div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of June 2026. Only models containing <em>fable</em>, <em>mythos</em>, <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
    <p id="footer-meta"></p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let allModelsList = [];
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByModel = [];
let lastByProject = [];
let lastByProjectBranch = [];
let lastFilteredDispatches = [];
let sessionSortDir = 'desc';

// Tables reveal rows in steps: 10 -> 25 -> 50, capped at 50 because rendering
// more than that visibly hurts performance. Past 50 the footer offers a
// "Download CSV to see more" link instead of another in-table step, plus a
// Show less button that resets straight back to 10. Limits persist across
// re-renders so sorting/filtering keeps the user's chosen depth (visible rows
// always reflect the active sort).
const TABLE_STEPS = [10, 25, 50];
const TABLE_MAX = TABLE_STEPS[TABLE_STEPS.length - 1];  // hard cap on in-table rows
// Don't paginate a table that barely exceeds the first step — paging away one or
// two rows just to show a "Show more" button is more annoying than helpful. Below
// this many rows a table always renders in full (no toggle).
const PAGINATE_THRESHOLD = 12;
function nextTableLimit(current, total) {
  for (const s of TABLE_STEPS) {
    if (s > current && s < total) return s;
  }
  return Math.min(total, TABLE_MAX);  // reveal everything, but never past the cap
}
// Rows to actually show: everything when the table is small enough to skip
// paging, otherwise the user's current step.
function shownCount(limit, total) {
  return total <= PAGINATE_THRESHOLD ? total : limit;
}
let modelLimit = TABLE_STEPS[0];
let sessionsLimit = TABLE_STEPS[0];
let projectLimit = TABLE_STEPS[0];
let branchLimit = TABLE_STEPS[0];
let dispatchesLimit = TABLE_STEPS[0];
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, June 2026) ─────────────────────────────────────
const PRICING = {
  // Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
  // (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
  // cache_write is the 5m-TTL rate (1.25x input); cache_write_1h the 1h rate (2x).
  'claude-fable-5':    { input: 10.00, output: 50.00, cache_write: 12.50, cache_write_1h: 20.00, cache_read: 1.00 },
  'claude-mythos-5':   { input: 10.00, output: 50.00, cache_write: 12.50, cache_write_1h: 20.00, cache_read: 1.00 },
  'claude-opus-4-8':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_write_1h: 10.00, cache_read: 0.50 },
  'claude-opus-4-7':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_write_1h: 10.00, cache_read: 0.50 },
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_write_1h: 10.00, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_write_1h: 10.00, cache_read: 0.50 },
  'claude-sonnet-4-7': { input:  3.00, output: 15.00, cache_write:  3.75, cache_write_1h:  6.00, cache_read: 0.30 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_write_1h:  6.00, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_write_1h:  6.00, cache_read: 0.30 },
  'claude-haiku-4-7':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_write_1h:  2.00, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_write_1h:  2.00, cache_read: 0.10 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_write_1h:  2.00, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('fable') || m.includes('mythos') ||
         m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('fable') || m.includes('mythos')) return PRICING['claude-fable-5'];
  if (m.includes('opus'))   return PRICING['claude-opus-4-8'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation, cacheCreation1h) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  // cacheCreation is the combined cache-write total; cacheCreation1h is the
  // 1h-TTL subset billed at 2x. The rest bills at the 5m 1.25x rate.
  const c1h = cacheCreation1h || 0;
  const c5m = Math.max(0, cacheCreation - c1h);
  return (
    inp       * p.input          / 1e6 +
    out       * p.output         / 1e6 +
    cacheRead * p.cache_read     / 1e6 +
    c5m       * p.cache_write    / 1e6 +
    c1h       * p.cache_write_1h / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }); }
function fmtCostBig(c) { return '$' + c.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

// ── Chart colors ───────────────────────────────────────────────────────────
// Solarized Light, kept in sync with the CSS :root variables so charts match
// the page. Chart legends/axes use C.axis (a touch darker than --muted so
// small labels stay legible on the light card); grid uses C.border.
const C = {
  text:   '#586E75',
  muted:  '#93A1A1',
  axis:   '#657B83',
  border: '#D3CBB7',
  card:   '#EEE8D5',
  blue:   '#268BD2',
  green:  '#859900',
  red:    '#DC322F',
  accent: '#CB4B16',
  amber:  '#B58900',
  purple: '#6C71C4',
  teal:   '#2AA198',
  mauve:  '#D33682',
};
const TOKEN_COLORS = {
  input:          'rgba(38,139,210,0.85)',   // blue
  output:         'rgba(203,75,22,0.85)',    // accent / orange
  cache_read:     'rgba(133,153,0,0.85)',    // green
  cache_creation: 'rgba(181,137,0,0.85)',    // yellow
};
// Hover lifts: bars/series go to full opacity (a touch bolder).
const TOKEN_HOVER = {
  input:          'rgba(38,139,210,1)',
  output:         'rgba(203,75,22,1)',
  cache_read:     'rgba(133,153,0,1)',
  cache_creation: 'rgba(181,137,0,1)',
};
// Donut / categorical palette — the full Solarized accent set (orange, yellow,
// green, blue, magenta, violet, cyan, red) rather than a saturated rainbow.
const MODEL_COLORS = ['#CB4B16','#B58900','#859900','#268BD2','#D33682','#6C71C4','#2AA198','#DC322F'];

// Subagent type swatches (table tag tint), matching the palette.
const AGENT_TYPE_COLORS = {
  'general-purpose':   '#268BD2',
  'Explore':           '#6C71C4',
  'Plan':              '#B58900',
  'claude-code-guide': '#2AA198',
  'auto-compact':      '#657B83',
  'unknown':           '#93A1A1',
};
function colorForAgentType(t) { return AGENT_TYPE_COLORS[t] || '#859900'; }

// Per-model-family swatches for the daily spend chart — subagent series reuse
// the same family color, darkened, so "Opus" and "Opus Subagent" read as a
// matched pair rather than two unrelated colors.
const SPEND_FAMILY_COLORS = {
  fable: '#D33682', mythos: '#D33682',
  opus: '#CB4B16', sonnet: '#268BD2', haiku: '#859900',
};
function spendBaseColor(model) {
  const ml = (model || '').toLowerCase();
  for (const k of Object.keys(SPEND_FAMILY_COLORS)) {
    if (ml.includes(k)) return SPEND_FAMILY_COLORS[k];
  }
  return '#6C71C4';
}
function darkenHex(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 0xff, g = (n >> 8) & 0xff, b = n & 0xff;
  const mix = (c) => Math.round(c * (1 - amt));
  return '#' + [mix(r), mix(g), mix(b)].map(x => x.toString(16).padStart(2, '0')).join('');
}
function colorForSpendSeries(model, isSubagent) {
  const base = spendBaseColor(model);
  return isSubagent ? darkenHex(base, 0.35) : base;
}
function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

// Tooltip color swatches: solid fill, no border (Chart.js's default draws a
// bordered box that looked offset/inconsistent). Lines use their solid stroke
// color instead of the translucent area fill.
Chart.defaults.color = C.axis;
// multiKeyBackground defaults to white and is drawn behind each tooltip swatch,
// peeking out as a thin white border on plain-box charts — make it transparent.
Chart.defaults.plugins.tooltip.multiKeyBackground = 'transparent';
Chart.defaults.plugins.tooltip.callbacks.labelColor = (ctx) => {
  const ds = ctx.dataset || {};
  let col = Array.isArray(ds.backgroundColor) ? ds.backgroundColor[ctx.dataIndex] : ds.backgroundColor;
  if (ds.type === 'line') col = ds.borderColor;
  return { borderColor: col, backgroundColor: col, borderWidth: 0 };
};

// Legend visibility must survive repaints (filter changes, auto-refresh, sort) —
// the charts are destroyed and rebuilt each render, which otherwise resets any
// series the user toggled off. We track hidden series by label per chart and
// reapply on rebuild: dataset charts via `dataset.hidden`, the doughnut via
// per-slice data visibility (see applyModelHidden).
const hiddenSeries = { daily: new Set(), dailyCost: new Set(), dailyProject: new Set(), dailyProjectModel: new Set(), hourly: new Set(), project: new Set(), model: new Set(), subagent: new Set(), spendDaily: new Set() };
function legendToggle(key) {
  return (e, item, legend) => {
    const ci = legend.chart;
    const ds = ci.data.datasets[item.datasetIndex];
    ds.hidden = !ds.hidden;
    if (ds.hidden) hiddenSeries[key].add(ds.label); else hiddenSeries[key].delete(ds.label);
    ci.update();
  };
}

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'today') {
    const t = iso(today);
    return { start: t, end: t };
  }
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  const sel = document.getElementById('range-select');
  if (sel) sel.value = range;  // keep the dropdown in sync with programmatic calls
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('fable') || ml.includes('mythos')) return 0;
  if (ml.includes('opus'))   return 1;
  if (ml.includes('sonnet')) return 2;
  if (ml.includes('haiku'))  return 3;
  return 4;
}

function sortedModels(models) {
  return [...models].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
}

// Compact display name for the collapsed trigger, e.g. "claude-opus-4-8" ->
// "Opus 4.8", "claude-fable-5" -> "Fable 5". Non-Anthropic ids fall back to the
// basename with any provider prefix and trailing date suffix stripped.
function shortModelName(m) {
  const ml = m.toLowerCase();
  let family = null;
  if (ml.includes('fable'))       family = 'Fable';
  else if (ml.includes('mythos')) family = 'Mythos';
  else if (ml.includes('opus'))   family = 'Opus';
  else if (ml.includes('sonnet')) family = 'Sonnet';
  else if (ml.includes('haiku'))  family = 'Haiku';
  if (family) {
    const two = m.match(/(\d+)[._-](\d+)/);
    if (two) return family + ' ' + two[1] + '.' + two[2];
    const one = m.match(/(\d+)/);
    return one ? family + ' ' + one[1] : family;
  }
  let base = m.split('/').pop().split(':')[0];
  base = base.replace(/[-_]?\d{6,}.*$/, '');
  return base || m;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) {
    const billable = allModels.filter(m => isBillable(m));
    // Fallback: if the user only has non-billable / unknown models (e.g. all
    // local-LLM runs), default to all models so the dashboard isn't blank.
    return new Set(billable.length ? billable : allModels);
  }
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  const expected = billable.length ? billable : allModels;
  if (selectedModels.size !== expected.length) return false;
  return expected.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  allModelsList = [...allModels];
  selectedModels = readURLModels(allModels);
  const sorted = sortedModels(allModels);
  const anthropic = sorted.filter(m => isBillable(m));
  const other     = sorted.filter(m => !isBillable(m));
  const rowHTML = m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}" title="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      <span class="model-cb-box">&#10003;</span>
      <span class="model-cb-text">${esc(m)}</span>
    </label>`;
  };
  let html = '';
  // Only show a group heading when both groups are present — a single-group
  // list doesn't need a label.
  const labelled = anthropic.length && other.length;
  if (anthropic.length) {
    if (labelled) html += '<div class="model-group-label">Anthropic</div>';
    html += anthropic.map(rowHTML).join('');
  }
  if (other.length) {
    if (labelled) html += '<div class="model-group-label">Other providers</div>';
    html += other.map(rowHTML).join('');
  }
  document.getElementById('model-checkboxes').innerHTML = html;
  updateModelTriggerLabel();
}

// Collapsed trigger text, in priority order:
//   "All models"     — everything selected
//   "No models"      — nothing selected
//   "All Anthropic"  — every Anthropic model (opus/sonnet/haiku/mythos/fable)
//                      selected and no other provider; "+N" if some others too
//   "Fable 5, Opus 4.7 +5" — otherwise, first two names + overflow count
function updateModelTriggerLabel() {
  const labelEl = document.getElementById('model-trigger-label');
  if (!labelEl) return;
  const n = selectedModels.size;
  if (n === 0)                    { labelEl.textContent = 'No models';  return; }
  if (n === allModelsList.length) { labelEl.textContent = 'All models'; return; }
  const anthropic = allModelsList.filter(m => isBillable(m));
  const others    = allModelsList.filter(m => !isBillable(m));
  if (anthropic.length && anthropic.every(m => selectedModels.has(m))) {
    // n < total (handled above), so when others exist at least one is unselected.
    const otherSel = others.filter(m => selectedModels.has(m)).length;
    labelEl.textContent = otherSel ? 'All Anthropic +' + otherSel : 'All Anthropic';
    return;
  }
  const chosen = sortedModels(allModelsList).filter(m => selectedModels.has(m));
  const shown = chosen.slice(0, 2).map(shortModelName);
  const extra = chosen.length - shown.length;
  labelEl.textContent = shown.join(', ') + (extra > 0 ? ' +' + extra : '');
}

function toggleModelPanel(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById('model-panel');
  const trigger = document.getElementById('model-trigger');
  const open = panel.hidden;
  panel.hidden = !open;
  trigger.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeModelPanel() {
  const panel = document.getElementById('model-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  const trigger = document.getElementById('model-trigger');
  trigger.classList.remove('open');
  trigger.setAttribute('aria-expanded', 'false');
}

// Close the panel on outside click or Escape. Clicks inside #model-select
// (including the checkboxes and All/None) keep it open so multiple models can
// be toggled in one pass.
document.addEventListener('click', (e) => {
  const sel = document.getElementById('model-select');
  if (sel && !sel.contains(e.target)) closeModelPanel();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModelPanel(); });

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateModelTriggerLabel();
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateModelTriggerLabel(); updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation, a.cache_creation_1h);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation, b.cache_creation_1h);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    d.cache_creation_1h += r.cache_creation_1h || 0;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.cache_creation_1h += r.cache_creation_1h || 0;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.cache_creation_1h += s.cache_creation_1h || 0;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation, s.cache_creation_1h);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, cache_creation_1h: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.cache_creation_1h += s.cache_creation_1h || 0;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation, s.cache_creation_1h);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation, m.cache_creation_1h), 0),
    subagent_tokens: (rawData.subagent_by_type || [])
      .filter(r => selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end))
      .reduce((s, r) => s + r.input + r.output + r.cache_read + r.cache_creation, 0),
  };

  // Hourly aggregation (filtered by model + range, then bucketed by UTC hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ);

  // Subagent breakdown by type (filtered by range + selected models)
  const subagentTypeMap = {};
  for (const r of (rawData.subagent_by_type || [])) {
    if (!selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end && r.day > end) continue;
    const k = r.agent_type;
    if (!subagentTypeMap[k]) subagentTypeMap[k] = { agent_type: k, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = subagentTypeMap[k];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }
  const byAgentType = Object.values(subagentTypeMap).sort((a, b) =>
    (b.input + b.output + b.cache_read + b.cache_creation) -
    (a.input + a.output + a.cache_read + a.cache_creation));

  // Daily spend by model, split into non-subagent / subagent series (e.g.
  // "Opus" vs "Opus Subagent") so the chart shows where cost is dispatched
  // vs delegated. Non-billable models cost $0 but still get their own series
  // (rather than being dropped) so no local activity is silently missing from
  // the chart's legend.
  const spendSeriesMap = {};
  const spendDaySet = new Set();
  for (const r of filteredDaily) {
    const key = r.model + '\x00' + (r.is_subagent ? 1 : 0);
    if (!spendSeriesMap[key]) {
      spendSeriesMap[key] = {
        model: r.model,
        isSubagent: !!r.is_subagent,
        label: shortModelName(r.model) + (r.is_subagent ? ' Subagent' : ''),
        byDay: {},
      };
    }
    const cost = calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation, r.cache_creation_1h);
    spendSeriesMap[key].byDay[r.day] = (spendSeriesMap[key].byDay[r.day] || 0) + cost;
    spendDaySet.add(r.day);
  }
  const spendSeries = Object.values(spendSeriesMap).sort((a, b) => {
    const pa = modelPriority(a.model), pb = modelPriority(b.model);
    if (pa !== pb) return pa - pb;
    if (a.model !== b.model) return a.model.localeCompare(b.model);
    return (a.isSubagent ? 1 : 0) - (b.isSubagent ? 1 : 0);
  });

  // Manually-reported non-tracked spend (see Monthly Spend Limit) is real
  // cost that never appears in `turns`, so fold its daily bump in as its own
  // series — otherwise this chart would silently undercount actual spend on
  // days with a report.
  const nonTrackedByDay = computeBumpsForRange(loadSpendCfg(), start, end);
  const hasNonTracked = Object.values(nonTrackedByDay).some(v => v);
  if (hasNonTracked) {
    Object.keys(nonTrackedByDay).forEach(d => spendDaySet.add(d));
    spendSeries.push({ model: null, isSubagent: false, nonTracked: true, label: 'Non-tracked (reported)', byDay: nonTrackedByDay });
  }
  const spendDays = [...spendDaySet].sort();

  // Daily spend by project, and by project+model, filtered by model + date
  // range like the other daily series. Both cap to the top N by total cost
  // over the filtered range and fold the rest into a single "Other" series,
  // so a long tail of one-off projects doesn't turn the legend into noise.
  const filteredDailyProject = (rawData.daily_by_project || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  const PROJECT_SPEND_TOP_N = 8;
  const projSpendByDay = {};   // project -> day -> cost
  const projSpendTotal = {};   // project -> total cost
  const projModelByDay = {};   // "project\x00model" -> day -> cost
  const projModelTotal = {};   // "project\x00model" -> total cost
  const projModelDaySet = new Set();
  for (const r of filteredDailyProject) {
    const cost = calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation, r.cache_creation_1h);
    projSpendByDay[r.project] = projSpendByDay[r.project] || {};
    projSpendByDay[r.project][r.day] = (projSpendByDay[r.project][r.day] || 0) + cost;
    projSpendTotal[r.project] = (projSpendTotal[r.project] || 0) + cost;

    const pmKey = r.project + '\x00' + r.model;
    projModelByDay[pmKey] = projModelByDay[pmKey] || {};
    projModelByDay[pmKey][r.day] = (projModelByDay[pmKey][r.day] || 0) + cost;
    projModelTotal[pmKey] = (projModelTotal[pmKey] || 0) + cost;
    projModelDaySet.add(r.day);
  }
  const projectSpendDays = [...projModelDaySet].sort();

  const shortProjectLabel = p => p.length > 22 ? '…' + p.slice(-20) : p;

  const rankedProjects = Object.keys(projSpendTotal).sort((a, b) => projSpendTotal[b] - projSpendTotal[a]);
  const projectSpendSeries = rankedProjects.slice(0, PROJECT_SPEND_TOP_N).map((p, i) => ({
    label: shortProjectLabel(p),
    color: MODEL_COLORS[i % MODEL_COLORS.length],
    byDay: projSpendByDay[p],
  }));
  const overflowProjects = rankedProjects.slice(PROJECT_SPEND_TOP_N);
  if (overflowProjects.length) {
    const byDay = {};
    for (const p of overflowProjects) {
      for (const [d, v] of Object.entries(projSpendByDay[p])) byDay[d] = (byDay[d] || 0) + v;
    }
    projectSpendSeries.push({ label: `Other (${overflowProjects.length})`, color: C.muted, byDay });
  }

  // Same top-N-plus-Other cap, but keyed by project+model so each bar segment
  // shows which model drove a project's spend. Color reuses the per-model-family
  // hue from the Daily Spend by Model chart, darkened per project rank within
  // that model — so "Sonnet" segments read as one family across projects while
  // still being distinguishable from each other.
  const rankedProjectModels = Object.keys(projModelTotal).sort((a, b) => projModelTotal[b] - projModelTotal[a]);
  const modelRank = {};
  const projectModelSpendSeries = rankedProjectModels.slice(0, PROJECT_SPEND_TOP_N).map(key => {
    const [project, model] = key.split('\x00');
    const rank = modelRank[model] || 0;
    modelRank[model] = rank + 1;
    return {
      label: `${shortProjectLabel(project)} · ${shortModelName(model)}`,
      color: darkenHex(spendBaseColor(model), Math.min(rank * 0.22, 0.6)),
      byDay: projModelByDay[key],
    };
  });
  const overflowProjectModels = rankedProjectModels.slice(PROJECT_SPEND_TOP_N);
  if (overflowProjectModels.length) {
    const byDay = {};
    for (const key of overflowProjectModels) {
      for (const [d, v] of Object.entries(projModelByDay[key])) byDay[d] = (byDay[d] || 0) + v;
    }
    projectModelSpendSeries.push({ label: `Other (${overflowProjectModels.length})`, color: C.muted, byDay });
  }

  // Top dispatches: filter by range + selected model. Keep the full filtered set
  // (already ranked by tokens server-side) so the table can page it like Recent
  // Sessions — show more/less plus CSV export of everything.
  const filteredDispatches = (rawData.top_dispatches || []).filter(d =>
    selectedModels.has(d.model) && (!start || d.start_date >= start) && (!end || d.start_date <= end)
  );

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('daily-cost-chart-title').textContent = 'Daily Spend by Model \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('daily-project-chart-title').textContent = 'Daily Spend by Project \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('daily-project-model-chart-title').textContent = 'Daily Spend by Project per Model \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('subagent-chart-title').textContent = 'Subagent Tokens by Type \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderSpendLimit();
  renderDailyChart(daily);
  renderDailyCostChart(spendDays, spendSeries);
  renderProjectSpendChart(projectSpendDays, projectSpendSeries);
  renderProjectModelSpendChart(projectSpendDays, projectModelSpendSeries);
  renderHourlyChart(hourlyAgg);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderSubagentChart(byAgentType);
  lastFilteredDispatches = filteredDispatches;
  renderTopDispatches(lastFilteredDispatches);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByModel = byModel;
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions);
  renderModelCostTable(lastByModel);
  renderProjectCostTable(lastByProject);
  renderProjectBranchCostTable(lastByProjectBranch);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Subagent Tokens', value: fmt(t.subagent_tokens || 0), sub: 'included in totals' },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, June 2026', color: C.green },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

// ── Monthly spend limit ──────────────────────────────────────────────────────
// Tracks progress toward a user-configured monthly $ cap (e.g. an Anthropic
// Enterprise org spend limit). Independent of the range/model filters above —
// it always reflects the current calendar month across every model, because
// that's what a real billing limit resets against.
//
// This dashboard only sees Claude Code usage on this machine, so the gap
// between that and the real org-wide total is tracked as dated "reports":
// the user periodically types in the cumulative "spent this month" figure
// Claude.ai shows them, stamped with the date it was reported. Each report
// implies a "non-tracked usage" bump — whatever gap remains between the
// reported total and (this machine's cumulative cost + bumps already
// accounted for) so far this month. Since a report only pins down a
// cumulative total as of its date, not which day the gap actually happened
// on, the bump is smeared evenly across every day since the previous report
// (or since the 1st of the month, for the first report). Bumps never move
// once assigned, so local usage recorded after a report's date adds on top
// of it rather than double-counting.
const SPEND_CFG_KEY = 'cu_spend_limit_cfg';
const DEFAULT_SPEND_CFG = { limit: 1500, reports: {} };

function loadSpendCfg() {
  try {
    const saved = JSON.parse(localStorage.getItem(SPEND_CFG_KEY) || 'null');
    if (saved && typeof saved.limit === 'number') {
      return { limit: saved.limit, reports: (saved.reports && typeof saved.reports === 'object') ? saved.reports : {} };
    }
  } catch (e) {}
  return { limit: DEFAULT_SPEND_CFG.limit, reports: {} };
}

function saveSpendCfg(cfg) {
  try { localStorage.setItem(SPEND_CFG_KEY, JSON.stringify(cfg)); } catch (e) {}
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

// date (YYYY-MM-DD) -> this machine's local Claude Code cost that day, all models.
function dailyLocalCostMap() {
  const map = {};
  for (const r of (rawData?.daily_by_model || [])) {
    map[r.day] = (map[r.day] || 0) + calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation, r.cache_creation_1h);
  }
  return map;
}

function nextDayISO(iso) {
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

// Every ISO date from monthStart to monthEnd inclusive.
function monthDayList(monthStart, monthEnd) {
  const days = [];
  for (let d = monthStart; d <= monthEnd; d = nextDayISO(d)) days.push(d);
  return days;
}

// Walks the month's reports in date order. Each report's cumulative "spent
// this month" figure implies a total gap (reported total minus local cost
// through that date minus bumps already assigned on earlier dates this
// month), which is smeared evenly across every day from the previous
// report's date (exclusive) through this report's date (inclusive) — or
// from the 1st of the month for the first report.
function computeMonthlyBumps(cfg, monthStart, monthEnd) {
  const localByDay = dailyLocalCostMap();
  const localCumThrough = (targetDate) =>
    Object.entries(localByDay)
      .filter(([d]) => d >= monthStart && d <= targetDate)
      .reduce((s, [, v]) => s + v, 0);

  const dates = Object.keys(cfg.reports).filter(d => d >= monthStart && d <= monthEnd).sort();
  const byDate = {};
  let cumBump = 0;
  let spanStart = monthStart;
  for (const date of dates) {
    const bump = Math.max(0, cfg.reports[date] - localCumThrough(date) - cumBump);
    const spanDays = monthDayList(spanStart, date);
    const perDay = bump / spanDays.length;
    for (const d of spanDays) byDate[d] = (byDate[d] || 0) + perDay;
    cumBump += bump;
    spanStart = nextDayISO(date);
  }
  return { byDate, total: cumBump };
}

// Same per-day bump as computeMonthlyBumps, generalized to any [start, end]
// range rather than a single calendar month. Reports reset cumulative "spent
// this month" tracking at each month boundary, so this walks every calendar
// month a report falls in, then keeps only the days inside the requested range.
function computeBumpsForRange(cfg, start, end) {
  const reportDates = Object.keys(cfg.reports);
  if (!reportDates.length) return {};
  const monthKeys = new Set(reportDates.map(d => d.slice(0, 7)));
  const byDate = {};
  for (const mk of monthKeys) {
    const [y, m] = mk.split('-').map(Number);
    const monthStart = mk + '-01';
    const monthEnd = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
    Object.assign(byDate, computeMonthlyBumps(cfg, monthStart, monthEnd).byDate);
  }
  const filtered = {};
  for (const [d, v] of Object.entries(byDate)) {
    if (v && (!start || d >= start) && (!end || d <= end)) filtered[d] = v;
  }
  return filtered;
}

// Mean local daily cost over the trailing `windowDays` calendar days ending on
// `asOfISO` (inclusive). Idle days count as $0, so a burst doesn't get diluted
// by only averaging over active days.
function trailingDailyBurn(localByDay, asOfISO, windowDays) {
  let sum = 0;
  let d = asOfISO;
  for (let i = 0; i < windowDays; i++) {
    sum += localByDay[d] || 0;
    const prev = new Date(d + 'T00:00:00Z');
    prev.setUTCDate(prev.getUTCDate() - 1);
    d = prev.toISOString().slice(0, 10);
  }
  return sum / windowDays;
}

function fmtMonthDay(iso) {
  return new Date(iso + 'T00:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

// Projects cumulative spend to month-end from the trailing 7-day local burn
// rate, scaled by how much higher the reconciled (local + non-tracked) total
// is than local cost alone — so the forecast tracks the real org-wide rate,
// not just what this machine can see. The actual/historical line only ever
// reflects ground truth (local cost + bumps already assigned); it is not
// smoothed or backfilled, so it can dip low right after a report until local
// usage catches back up — the forecast's calibration multiplier is what keeps
// the projection itself honest in the meantime.
function computeSpendProjection(cfg, monthStart, monthEnd) {
  const days = monthDayList(monthStart, monthEnd);
  const localByDay = dailyLocalCostMap();
  const bumps = computeMonthlyBumps(cfg, monthStart, monthEnd);
  const today = todayISO();
  const elapsed = days.filter(d => d <= today).length;

  let run = 0;
  const cumReconciled = days.map(d => {
    run += (localByDay[d] || 0) + (bumps.byDate[d] || 0);
    return d <= today ? run : null;
  });
  const reconciledToday = cumReconciled[Math.max(0, elapsed - 1)] || 0;
  const localCumToDate = days.filter(d => d <= today).reduce((s, d) => s + (localByDay[d] || 0), 0);

  const window = Math.min(7, Math.max(1, elapsed));
  const burnRate = trailingDailyBurn(localByDay, today, window);
  const multiplier = localCumToDate > 0 ? reconciledToday / localCumToDate : 1;
  const dailyBurn = burnRate * multiplier;

  let p = reconciledToday;
  const cumProjected = days.map(d => {
    if (d < today) return null;
    if (d === today) return reconciledToday;
    p += dailyBurn;
    return p;
  });
  const projectedEnd = p;
  const limitData = days.map(() => cfg.limit);

  const showProjection = elapsed >= 3 && localCumToDate > 0;
  const overBudget = reconciledToday >= cfg.limit;
  const crossDate = (!overBudget && dailyBurn > 0)
    ? (days.find((d, i) => d >= today && cumProjected[i] >= cfg.limit) || null)
    : null;

  return {
    days,
    localData: days.map(d => localByDay[d] || 0),
    bumpData: days.map(d => bumps.byDate[d] || 0),
    cumReconciled, cumProjected, limitData,
    reconciledToday, projectedEnd, crossDate,
    showProjection, overBudget,
  };
}

function toggleSpendLimitSettings() {
  const el = document.getElementById('spend-limit-settings');
  const opening = el.style.display === 'none';
  el.style.display = opening ? 'flex' : 'none';
  if (opening) {
    document.getElementById('spend-limit-input').value = loadSpendCfg().limit;
    document.getElementById('spend-report-input').value = '';
  }
}

function saveSpendLimitAmount() {
  const limit = parseFloat(document.getElementById('spend-limit-input').value);
  const cfg = loadSpendCfg();
  cfg.limit = Number.isFinite(limit) && limit >= 0 ? limit : DEFAULT_SPEND_CFG.limit;
  saveSpendCfg(cfg);
  renderSpendLimit();
}

function addSpendReport() {
  const input = document.getElementById('spend-report-input');
  const amount = parseFloat(input.value);
  if (!Number.isFinite(amount) || amount < 0) return;
  const cfg = loadSpendCfg();
  cfg.reports[todayISO()] = amount;
  saveSpendCfg(cfg);
  input.value = '';
  renderSpendLimit();
}

function deleteSpendReport(date) {
  const cfg = loadSpendCfg();
  delete cfg.reports[date];
  saveSpendCfg(cfg);
  renderSpendLimit();
}

function renderSpendReportsList(cfg, monthStart, monthEnd) {
  const el = document.getElementById('spend-reports-list');
  if (!el) return;
  const dates = Object.keys(cfg.reports).filter(d => d >= monthStart && d <= monthEnd).sort().reverse();
  if (!dates.length) {
    el.innerHTML = '<div class="spend-reports-empty">No reports yet this month.</div>';
    return;
  }
  el.innerHTML = dates.map(d => `
    <div class="spend-report-row">
      <span class="date">${esc(d)}</span>
      <span class="amount">${esc(fmtCostBig(cfg.reports[d]))}</span>
      <button class="spend-report-del" onclick="deleteSpendReport('${esc(d)}')" title="Delete this report">&times;</button>
    </div>
  `).join('');
}

function renderSpendDailyChart(proj, cfg) {
  const canvas = document.getElementById('chart-spend-daily');
  if (!canvas) return;

  const projectedOver = proj.overBudget || proj.projectedEnd >= cfg.limit;
  // `order` controls z-index (lower draws first/behind) and tooltip sequence.
  // Give the left-axis (y) bars the lowest values so they sit behind the
  // right-axis (y1) lines, which get increasing values so they draw on top.
  const datasets = [
    { type: 'bar', label: 'Claude Code (this machine)', hidden: hiddenSeries.spendDaily.has('Claude Code (this machine)'), data: proj.localData, backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'spend', yAxisID: 'y', order: 0 },
    { type: 'bar', label: 'Non-tracked usage',          hidden: hiddenSeries.spendDaily.has('Non-tracked usage'),          data: proj.bumpData,  backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'spend', yAxisID: 'y', order: 0 },
    { type: 'line', label: 'Monthly limit', hidden: hiddenSeries.spendDaily.has('Monthly limit'), data: proj.limitData, yAxisID: 'y1', order: 1,
      borderColor: C.amber, borderDash: [3, 3], borderWidth: 1.5, pointRadius: 0, tension: 0, spanGaps: true },
    { type: 'line', label: 'Cumulative spend', hidden: hiddenSeries.spendDaily.has('Cumulative spend'), data: proj.cumReconciled, yAxisID: 'y1', order: 2,
      borderColor: C.green, backgroundColor: 'rgba(116,201,145,0.10)', borderWidth: 2, pointRadius: 0, tension: 0, spanGaps: false },
  ];
  if (proj.showProjection) {
    datasets.push({ type: 'line', label: 'Projected', hidden: hiddenSeries.spendDaily.has('Projected'), data: proj.cumProjected, yAxisID: 'y1', order: 3,
      borderColor: projectedOver ? C.red : C.accent, borderDash: [6, 4], borderWidth: 2, pointRadius: 0, tension: 0, spanGaps: false });
  }

  const y1Max = Math.max(cfg.limit, proj.projectedEnd || proj.reconciledToday || 0) * 1.1;

  const ctx = canvas.getContext('2d');
  if (charts.spendDaily) charts.spendDaily.destroy();
  charts.spendDaily = new Chart(ctx, {
    data: { labels: proj.days, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => ctx.dataset.label + ': ' + fmtCostBig(ctx.parsed.y) } },
      },
      scales: {
        x:  { ticks: { color: C.axis, maxRotation: 0, autoSkip: true, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, stacked: true, ticks: { color: C.axis, callback: v => '$' + v }, grid: { color: C.border },      title: { display: true, text: 'Daily $',      color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, suggestedMax: y1Max,   ticks: { color: C.axis, callback: v => '$' + v }, grid: { drawOnChartArea: false }, title: { display: true, text: 'Cumulative $', color: C.axis, font: { size: 11 } } },
      }
    }
  });

  renderSpendLegend(datasets);
}

// Chart.js's built-in legend renders one row/column with no way to split
// items toward the axis each belongs to, so this draws two independent
// legend groups instead: left-axis (bar) series above the left axis, the
// right-axis (line) series above the right axis. Clicking an item toggles
// that dataset's visibility, mirroring legendToggle()'s behavior.
function renderSpendLegend(datasets) {
  const leftEl = document.getElementById('spend-legend-left');
  const rightEl = document.getElementById('spend-legend-right');
  if (!leftEl || !rightEl) return;

  const itemHTML = (ds, index) => {
    const color = ds.borderColor || ds.backgroundColor;
    const hidden = hiddenSeries.spendDaily.has(ds.label);
    return `<span class="spend-legend-item${hidden ? ' hidden' : ''}" data-index="${index}">
      <span class="spend-legend-swatch" style="background:${color}"></span>${esc(ds.label)}
    </span>`;
  };

  const left = [], right = [];
  datasets.forEach((ds, i) => (ds.yAxisID === 'y' ? left : right).push(itemHTML(ds, i)));
  leftEl.innerHTML = left.join('');
  rightEl.innerHTML = right.join('');

  const onClick = (e) => {
    const item = e.target.closest('.spend-legend-item');
    if (!item || !charts.spendDaily) return;
    const ds = charts.spendDaily.data.datasets[Number(item.dataset.index)];
    const nowHidden = !ds.hidden;
    ds.hidden = nowHidden;
    if (nowHidden) hiddenSeries.spendDaily.add(ds.label); else hiddenSeries.spendDaily.delete(ds.label);
    charts.spendDaily.update();
    item.classList.toggle('hidden', nowHidden);
  };
  leftEl.onclick = onClick;
  rightEl.onclick = onClick;
}

function renderSpendLimit() {
  const cfg = loadSpendCfg();
  const { start, end } = getRangeBounds('month');
  const proj = computeSpendProjection(cfg, start, end);
  const localCost = proj.localData.reduce((s, v) => s + v, 0);
  const bumpTotal = proj.bumpData.reduce((s, v) => s + v, 0);
  const total = proj.reconciledToday;
  const pct = cfg.limit > 0 ? (total / cfg.limit) * 100 : 0;

  document.getElementById('spend-limit-amount').textContent =
    fmtCostBig(total) + ' of ' + fmtCostBig(cfg.limit);
  document.getElementById('spend-limit-pct').textContent = pct.toFixed(1) + '% used';

  const bar = document.getElementById('spend-limit-bar');
  bar.style.width = Math.min(pct, 100) + '%';
  bar.classList.toggle('over', pct >= 100);

  document.getElementById('spend-limit-breakdown').textContent =
    'This machine (Claude Code): ' + fmtCostBig(localCost) +
    '  ·  Non-tracked usage (reported): ' + fmtCostBig(bumpTotal) +
    '  ·  Calendar month to date';

  const projEl = document.getElementById('spend-limit-projection');
  const monthEndLabel = fmtMonthDay(end);
  if (proj.overBudget) {
    projEl.textContent = 'Limit exceeded — ' + fmtCostBig(total - cfg.limit) + ' over';
    projEl.style.color = C.red;
  } else if (!proj.showProjection) {
    projEl.textContent = 'Projection available after a few days of data';
    projEl.style.color = 'var(--muted)';
  } else if (proj.projectedEnd >= cfg.limit) {
    const overBy = proj.projectedEnd - cfg.limit;
    projEl.textContent = 'Projected ' + fmtCostBig(proj.projectedEnd) + ' by ' + monthEndLabel +
      ' (' + fmtCostBig(overBy) + ' over the ' + fmtCostBig(cfg.limit) + ' limit)' +
      (proj.crossDate ? ' · on pace to cross the limit around ' + fmtMonthDay(proj.crossDate) : '');
    projEl.style.color = C.accent;
  } else {
    projEl.textContent = 'Projected ' + fmtCostBig(proj.projectedEnd) + ' by ' + monthEndLabel;
    projEl.style.color = C.green;
  }

  renderSpendDailyChart(proj, cfg);
  renderSpendReportsList(cfg, start, end);
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the unique days in the input so the caller can compute per-day averages.
function aggregateHourly(rows, tzMode) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = utcHourToDisplay(r.hour, tzMode);
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  const dayCount = days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' averaged · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors      = agg.hours.map(h => h.peak ? 'rgba(199,78,57,0.9)' : TOKEN_COLORS.input);
  const barHoverColors = agg.hours.map(h => h.peak ? 'rgba(199,78,57,1)'   : TOKEN_HOVER.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          hidden: hiddenSeries.hourly.has('Avg turns / hour'),
          data: turns,
          backgroundColor: barColors,
          hoverBackgroundColor: barHoverColors,
          pointStyle: 'rect',
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          hidden: hiddenSeries.hourly.has('Avg output tokens / hour'),
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(217,119,87,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: TOKEN_HOVER.output,
          pointStyle: 'circle',
          pointBackgroundColor: TOKEN_COLORS.output,
          pointBorderColor: TOKEN_COLORS.output,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { onClick: legendToggle('hourly'), labels: { color: C.axis, usePointStyle: true, boxWidth: 8, boxHeight: 8 } },
        tooltip: {
          usePointStyle: true,
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: C.axis, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: C.border } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: C.axis, callback: v => v.toFixed(1) },     grid: { color: C.border }, title: { display: true, text: 'Avg turns / hour',         color: C.axis, font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: C.axis, font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.daily.has('Input'),          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         hidden: hiddenSeries.daily.has('Output'),         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     hidden: hiddenSeries.daily.has('Cache Read'),     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', hidden: hiddenSeries.daily.has('Cache Creation'), data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('daily'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y:  { position: 'left',  ticks: { color: C.green, callback: v => fmt(v) }, grid: { color: C.border }, title: { display: true, text: 'Cache', color: C.green } },
        y1: { position: 'right', ticks: { color: C.blue, callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: C.blue } },
      }
    }
  });
}

function renderDailyCostChart(days, series) {
  const ctx = document.getElementById('chart-daily-cost').getContext('2d');
  if (charts.dailyCost) charts.dailyCost.destroy();
  if (!series.length) { charts.dailyCost = null; return; }
  charts.dailyCost = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: days,
      datasets: series.map(s => {
        const color = s.nonTracked ? TOKEN_COLORS.cache_creation : colorForSpendSeries(s.model, s.isSubagent);
        return {
          label: s.label,
          hidden: hiddenSeries.dailyCost.has(s.label),
          data: days.map(d => s.byDay[d] || 0),
          backgroundColor: color,
          hoverBackgroundColor: color,
          stack: 'cost',
        };
      })
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('dailyCost'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmtCost(ctx.raw)}`,
          footer: items => ` Total: ${fmtCost(items.reduce((s, it) => s + it.raw, 0))}`,
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, callback: v => fmtCost(v) }, grid: { color: C.border } },
      }
    }
  });
}

function renderProjectSpendChart(days, series) {
  const ctx = document.getElementById('chart-daily-project').getContext('2d');
  if (charts.dailyProject) charts.dailyProject.destroy();
  if (!series.length) { charts.dailyProject = null; return; }
  charts.dailyProject = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: days,
      datasets: series.map(s => ({
        label: s.label,
        hidden: hiddenSeries.dailyProject.has(s.label),
        data: days.map(d => s.byDay[d] || 0),
        backgroundColor: s.color,
        hoverBackgroundColor: s.color,
        stack: 'cost',
      }))
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('dailyProject'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmtCost(ctx.raw)}`,
          footer: items => ` Total: ${fmtCost(items.reduce((s, it) => s + it.raw, 0))}`,
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, callback: v => fmtCost(v) }, grid: { color: C.border } },
      }
    }
  });
}

function renderProjectModelSpendChart(days, series) {
  const ctx = document.getElementById('chart-daily-project-model').getContext('2d');
  if (charts.dailyProjectModel) charts.dailyProjectModel.destroy();
  if (!series.length) { charts.dailyProjectModel = null; return; }
  charts.dailyProjectModel = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: days,
      datasets: series.map(s => ({
        label: s.label,
        hidden: hiddenSeries.dailyProjectModel.has(s.label),
        data: days.map(d => s.byDay[d] || 0),
        backgroundColor: s.color,
        hoverBackgroundColor: s.color,
        stack: 'cost',
      }))
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('dailyProjectModel'), labels: { color: C.axis, boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmtCost(ctx.raw)}`,
          footer: items => ` Total: ${fmtCost(items.reduce((s, it) => s + it.raw, 0))}`,
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, callback: v => fmtCost(v) }, grid: { color: C.border } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, hoverBackgroundColor: MODEL_COLORS, hoverOffset: 8, borderWidth: 2, borderColor: C.card, hoverBorderColor: C.card }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: C.axis, boxWidth: 12, font: { size: 11 } },
          onClick: (e, item, legend) => {
            const ci = legend.chart;
            ci.toggleDataVisibility(item.index);
            const label = ci.data.labels[item.index];
            if (!ci.getDataVisibility(item.index)) hiddenSeries.model.add(label); else hiddenSeries.model.delete(label);
            ci.update();
          },
        },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
  // Reapply any slices the user toggled off in a previous render.
  byModel.forEach((m, i) => {
    if (hiddenSeries.model.has(m.model) && charts.model.getDataVisibility(i)) charts.model.toggleDataVisibility(i);
  });
  charts.model.update();
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  hidden: hiddenSeries.project.has('Input'),  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input,  hoverBackgroundColor: TOKEN_HOVER.input },
        { label: 'Output', hidden: hiddenSeries.project.has('Output'), data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output, hoverBackgroundColor: TOKEN_HOVER.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: { legend: { onClick: legendToggle('project'), labels: { color: C.axis, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderSubagentChart(byType) {
  const ctx = document.getElementById('chart-subagent').getContext('2d');
  if (charts.subagent) charts.subagent.destroy();
  if (!byType.length) { charts.subagent = null; return; }
  charts.subagent = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byType.map(t => t.agent_type),
      datasets: [
        { label: 'Input',          hidden: hiddenSeries.subagent.has('Input'),          data: byType.map(t => t.input),          backgroundColor: TOKEN_COLORS.input,          hoverBackgroundColor: TOKEN_HOVER.input,          stack: 'tokens' },
        { label: 'Output',         hidden: hiddenSeries.subagent.has('Output'),         data: byType.map(t => t.output),         backgroundColor: TOKEN_COLORS.output,         hoverBackgroundColor: TOKEN_HOVER.output,         stack: 'tokens' },
        { label: 'Cache Read',     hidden: hiddenSeries.subagent.has('Cache Read'),     data: byType.map(t => t.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     hoverBackgroundColor: TOKEN_HOVER.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', hidden: hiddenSeries.subagent.has('Cache Creation'), data: byType.map(t => t.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, hoverBackgroundColor: TOKEN_HOVER.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false, resizeDelay: 150,
      plugins: {
        legend: { onClick: legendToggle('subagent'), labels: { color: C.axis, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}`,
          footer: items => {
            const total = items.reduce((s, it) => s + it.raw, 0);
            const row = byType[items[0].dataIndex];
            return ` Total: ${fmt(total)} · ${row.turns} turns`;
          }
        } }
      },
      scales: {
        x: { stacked: true, ticks: { color: C.axis, callback: v => fmt(v) }, grid: { color: C.border } },
        y: { stacked: true, ticks: { color: C.axis, font: { size: 11 } }, grid: { color: C.border } },
      }
    }
  });
}

function renderTopDispatches(rows) {
  const body = document.getElementById('dispatches-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:24px">No subagent dispatches in selected range.</td></tr>';
    renderTableToggle('dispatches-foot', 0, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
    return;
  }
  const shown = rows.slice(0, shownCount(dispatchesLimit, rows.length));
  body.innerHTML = shown.map(d => {
    const tokensTotal = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation, d.cache_creation_1h);
    const costCell = isBillable(d.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const col = colorForAgentType(d.agent_type);
    const typeStyle = `background:${col}22;color:${col};border:1px solid ${col}44`;
    return `<tr>
      <td><span class="model-tag" style="${typeStyle}">${esc(d.agent_type)}</span></td>
      <td class="muted">${esc(d.start || '—')}</td>
      <td><span class="model-tag">${esc(d.model)}</span></td>
      <td class="num">${d.turns}</td>
      <td class="num">${d.tool_uses != null ? d.tool_uses : '—'}</td>
      <td class="muted">${fmtDuration(d.duration_ms)}</td>
      <td class="num">${fmt(d.input)}</td>
      <td class="num">${fmt(d.output)}</td>
      <td class="num">${fmt(d.cache_read)}</td>
      <td class="num"><strong>${fmt(tokensTotal)}</strong></td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('dispatches-foot', rows.length, dispatchesLimit, 'lessDispatchRows', 'moreDispatchRows', 'exportDispatchesCSV');
}

// Fills a table card's footer with the row-reveal control. Three states:
//   - more rows fit under the cap        -> "Show more" (plus "Show less" once expanded)
//   - cap reached but more records exist -> "Download CSV to see all (N)" + "Show less"
//   - every row is already visible       -> "Show less"
// "Show less" is hidden at the initial step (nothing to collapse yet). Renders
// nothing when the whole table fits in the first step. Carets: more = down (▾),
// less = up (▴).
function renderTableToggle(footId, total, limit, lessName, moreName, csvName) {
  const foot = document.getElementById(footId);
  if (!foot) return;
  if (total <= PAGINATE_THRESHOLD) { foot.innerHTML = ''; return; }
  const less = '<button class="show-more-btn" onclick="' + lessName + '()">Show less ▴</button>';
  const more = '<button class="show-more-btn" onclick="' + moreName + '()">Show more ▾</button>';
  let html;
  if (limit < total && limit < TABLE_MAX) {
    // more rows fit under the cap; Show less only once we're past the first step
    html = (limit > TABLE_STEPS[0] ? less : '') + more;
  } else if (limit < total) {           // cap reached, remaining rows only via CSV
    html = '<a class="show-more-link" href="#" onclick="' + csvName + '(); return false;">Download CSV to see all (' + total + ')</a>' + less;
  } else {                              // everything already visible
    html = less;
  }
  foot.innerHTML = html;
}

// After collapsing a table, bring its top back into view — the user may have
// scrolled down through the expanded rows.
function scrollTableToTop(bodyId) {
  const card = document.getElementById(bodyId)?.closest('.table-card');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "Show more" advances one step (capped at TABLE_MAX); "Show less" resets to the
// first step and scrolls back to the top of that table.
function moreModelRows()   { modelLimit    = nextTableLimit(modelLimit,    lastByModel.length);        renderModelCostTable(lastByModel); }
function lessModelRows()   { modelLimit    = TABLE_STEPS[0]; renderModelCostTable(lastByModel);            scrollTableToTop('model-cost-body'); }
function moreSessionRows() { sessionsLimit = nextTableLimit(sessionsLimit, lastFilteredSessions.length); renderSessionsTable(lastFilteredSessions); }
function lessSessionRows() { sessionsLimit = TABLE_STEPS[0]; renderSessionsTable(lastFilteredSessions);    scrollTableToTop('sessions-body'); }
function moreProjectRows() { projectLimit  = nextTableLimit(projectLimit,  lastByProject.length);       renderProjectCostTable(lastByProject); }
function lessProjectRows() { projectLimit  = TABLE_STEPS[0]; renderProjectCostTable(lastByProject);        scrollTableToTop('project-cost-body'); }
function moreBranchRows()  { branchLimit   = nextTableLimit(branchLimit,   lastByProjectBranch.length); renderProjectBranchCostTable(lastByProjectBranch); }
function lessBranchRows()  { branchLimit   = TABLE_STEPS[0]; renderProjectBranchCostTable(lastByProjectBranch); scrollTableToTop('project-branch-cost-body'); }
function moreDispatchRows(){ dispatchesLimit = nextTableLimit(dispatchesLimit, lastFilteredDispatches.length); renderTopDispatches(lastFilteredDispatches); }
function lessDispatchRows(){ dispatchesLimit = TABLE_STEPS[0]; renderTopDispatches(lastFilteredDispatches);            scrollTableToTop('dispatches-body'); }

function renderSessionsTable(sessions) {
  const shown = sessions.slice(0, shownCount(sessionsLimit, sessions.length));
  document.getElementById('sessions-body').innerHTML = shown.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation, s.cache_creation_1h);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const titleCell = s.topic
      ? `<td class="topic-cell" title="${esc(s.topic)}">${esc(s.topic)}</td>`
      : `<td class="topic-cell"><span class="untitled">Untitled</span></td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${esc(s.session_id.slice(0, 8))}&hellip;</td>
      <td>${esc(s.project)}</td>
      ${titleCell}
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('sessions-foot', sessions.length, sessionsLimit, 'lessSessionRows', 'moreSessionRows', 'exportSessionsCSV');
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation, a.cache_creation_1h);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation, b.cache_creation_1h);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  const sorted = sortModels(byModel);
  const shown = sorted.slice(0, shownCount(modelLimit, sorted.length));
  document.getElementById('model-cost-body').innerHTML = shown.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation, m.cache_creation_1h);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
  renderTableToggle('model-cost-foot', sorted.length, modelLimit, 'lessModelRows', 'moreModelRows', 'exportModelCSV');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  const sorted = sortProjects(byProject);
  const shown = sorted.slice(0, shownCount(projectLimit, sorted.length));
  document.getElementById('project-cost-body').innerHTML = shown.map(p => {
    return `<tr>
      <td>${esc(p.project)}</td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-cost-foot', sorted.length, projectLimit, 'lessProjectRows', 'moreProjectRows', 'exportProjectsCSV');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  // Sort by the selected column (default: cost desc), consistent with the Cost by
  // Model / Cost by Project tables. Project name is only a stable tiebreaker when
  // the sorted column ties, so a project's branches stay grouped & deterministic
  // without overriding the primary order.
  return [...rows].sort((a, b) => {
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    return pa < pb ? -1 : pa > pb ? 1 : 0;
  });
}

function renderProjectBranchCostTable(rows) {
  const sorted = sortProjectBranch(rows);
  const shown = sorted.slice(0, shownCount(branchLimit, sorted.length));
  document.getElementById('project-branch-cost-body').innerHTML = shown.map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
  renderTableToggle('project-branch-cost-foot', sorted.length, branchLimit, 'lessBranchRows', 'moreBranchRows', 'exportProjectBranchCSV');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportModelCSV() {
  const header = ['Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = sortModels(lastByModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation, m.cache_creation_1h);
    return [m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('cost_by_model', header, rows);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Title', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation, s.cache_creation_1h);
    return [s.session_id, s.project, s.topic, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

function exportDispatchesCSV() {
  const header = ['Type', 'Agent ID', 'Started', 'Model', 'Turns', 'Tool Uses', 'Duration (ms)', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Total Tokens', 'Est. Cost', 'Status'];
  const rows = lastFilteredDispatches.map(d => {
    const total = d.input + d.output + d.cache_read + d.cache_creation;
    const cost = calcCost(d.model, d.input, d.output, d.cache_read, d.cache_creation, d.cache_creation_1h);
    return [d.agent_type, d.agent_id, d.start, d.model, d.turns,
            d.tool_uses != null ? d.tool_uses : '', d.duration_ms != null ? d.duration_ms : '',
            d.input, d.output, d.cache_read, d.cache_creation, total, cost.toFixed(4), d.status || ''];
  });
  downloadCSV('subagent_dispatches', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      // The server binds and serves before the initial scan finishes, so on a
      // fresh start the DB may not exist yet. Show a non-destructive notice and
      // retry instead of nuking the page — once the background scan creates the
      // DB, the next poll renders normally.
      const meta = document.getElementById('meta');
      if (meta) meta.innerHTML = esc(d.error) + ' — retrying…';
      if (rawData === null) setTimeout(loadData, 3000);
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? '<br>Auto-refresh in 30s' : '';
    document.getElementById('meta').innerHTML = 'Updated: ' + esc(d.generated_at) + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL into the dropdown
      selectedRange = readURLRange();
      const rangeSel = document.getElementById('range-select');
      if (rangeSel) rangeSel.value = selectedRange;
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

// ── Footer meta: version, extension promo, update check ──────────────────────
// APP_CONFIG is injected server-side (see do_GET). { version, surface }.
const APP_CONFIG = window.APP_CONFIG || { version: '', surface: 'web' };
const REPO_URL = 'https://github.com/phuryn/claude-usage';
const MARKETPLACE_URL = 'https://marketplace.visualstudio.com/items?itemName=PawelHuryn.claude-usage-phuryn';
const UPDATE_CACHE_KEY = 'cu_update_check';
const UPDATE_CACHE_TTL = 24 * 60 * 60 * 1000;  // re-check GitHub at most once a day

// Compare dotted numeric versions ("1.3.0"); leading "v" tolerated. Returns
// true only when `latest` is strictly ahead of `current`.
function isNewer(latest, current) {
  const a = String(latest).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  const b = String(current).replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0, y = b[i] || 0;
    if (x > y) return true;
    if (x < y) return false;
  }
  return false;
}

function appendUpdateLink(latest) {
  const el = document.getElementById('footer-meta');
  if (!el || !el.innerHTML) return;
  const a = document.createElement('a');
  a.className = 'update-link';
  a.href = REPO_URL + '/releases/latest';
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = 'Update to v' + latest;
  el.insertAdjacentHTML('beforeend', '&nbsp;&middot;&nbsp;');
  el.appendChild(a);
}

// Web only. Asks GitHub's public releases API whether a newer release exists and,
// if so, appends an "Update to vX.Y.Z" link. Cached in localStorage for 24h and
// fully fail-silent (offline / rate-limited / blocked -> no link, no error). No
// usage data is sent; this is a plain unauthenticated GET of release metadata.
function checkForUpdate(current) {
  let cached = null;
  try { cached = JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY) || 'null'); } catch (e) {}
  if (cached && cached.latest && cached.ts && (Date.now() - cached.ts) < UPDATE_CACHE_TTL) {
    if (isNewer(cached.latest, current)) appendUpdateLink(cached.latest);
    return;
  }
  fetch('https://api.github.com/repos/phuryn/claude-usage/releases/latest', {
    headers: { 'Accept': 'application/vnd.github+json' }
  })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || !data.tag_name) return;
      const latest = String(data.tag_name).replace(/^v/, '');
      try { localStorage.setItem(UPDATE_CACHE_KEY, JSON.stringify({ ts: Date.now(), latest: latest })); } catch (e) {}
      if (isNewer(latest, current)) appendUpdateLink(latest);
    })
    .catch(() => {});  // fail-silent: never let a version check disrupt the dashboard
}

function initFooterMeta() {
  const el = document.getElementById('footer-meta');
  if (!el) return;
  const v = APP_CONFIG.version || '';
  const parts = [];
  if (v) {
    parts.push('Version <a href="' + REPO_URL + '/releases/tag/v' + esc(v) + '" target="_blank" rel="noopener">v' + esc(v) + '</a>');
  }
  // The web build promotes the extension; the embedded build is already in it.
  if (APP_CONFIG.surface !== 'vscode') {
    parts.push('<a href="' + MARKETPLACE_URL + '" target="_blank" rel="noopener">Get the VS Code extension</a>');
  }
  el.innerHTML = parts.join('&nbsp;&middot;&nbsp;');
  // VS Code auto-updates the extension, so only the web build checks for updates.
  if (v && APP_CONFIG.surface !== 'vscode') checkForUpdate(v);
}

// ── Section nav + collapsible cards ─────────────────────────────────────────
// The dashboard is one long scroll. The sticky jump bar teleports between
// sections; collapsible cards fold away the ones you don't use. Collapse state
// persists per card in localStorage and is independent of in-table Show
// more/less (which only pages rows within a single table).
const COLLAPSE_KEY = 'cu_collapsed_cards';
const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function loadCollapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveCollapsedSet(set) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) {}
}

// Charts created while their card is collapsed (display:none) lay out at zero
// size; resize them once the card is shown again so Chart.js repaints to fit.
function resizeChartsIn(card) {
  card.querySelectorAll('canvas').forEach(cv => {
    const ch = Object.values(charts).find(c => c && c.canvas === cv);
    if (ch) ch.resize();
  });
}

function setCardCollapsed(card, collapsed) {
  card.classList.toggle('collapsed', collapsed);
  const title = card.querySelector('h2, .section-title');
  if (title) title.setAttribute('aria-expanded', String(!collapsed));
}

function toggleCard(card) {
  const collapsed = !card.classList.contains('collapsed');
  setCardCollapsed(card, collapsed);
  const set = loadCollapsedSet();
  if (collapsed) set.add(card.dataset.card); else set.delete(card.dataset.card);
  saveCollapsedSet(set);
  if (!collapsed) requestAnimationFrame(() => resizeChartsIn(card));
}

function initCollapsibleCards() {
  const container = document.querySelector('.container');
  if (!container) return;

  // Restore persisted collapse state + make each title an accessible toggle.
  const collapsed = loadCollapsedSet();
  document.querySelectorAll('[data-card]').forEach(card => {
    const title = card.querySelector('h2, .section-title');
    if (title) {
      title.setAttribute('role', 'button');
      title.setAttribute('tabindex', '0');
      title.title = 'Collapse / expand section';
    }
    setCardCollapsed(card, collapsed.has(card.dataset.card));
  });

  // Toggle a card from its title (caret included). Inner controls (CSV, TZ, sort
  // headers) sit outside the title selector, so they keep their own behaviour.
  const TITLE_SEL = '.chart-card > h2, .chart-header > h2, .table-card > .section-title, .section-header > .section-title';
  const onTitleActivate = (e) => {
    if (e.target.closest('.info-icon')) return;  // info tooltip, not a collapse toggle
    if (e.type === 'keydown') { if (e.key !== 'Enter' && e.key !== ' ') return; e.preventDefault(); }
    const title = e.target.closest(TITLE_SEL);
    const card = title && title.closest('[data-card]');
    if (card) toggleCard(card);
  };
  container.addEventListener('click', onTitleActivate);
  container.addEventListener('keydown', onTitleActivate);
}

initFooterMeta();
initCollapsibleCards();
loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


def find_icon_file():
    """Locate the extension's icon.svg across both run contexts.

    - Bundled in the .vsix: this file lives at ``python/dashboard.py`` and the
      icon is a sibling-of-parent at ``../resources/icon.svg``.
    - Standalone repo (``python cli.py dashboard``): this file is the repo-root
      ``dashboard.py`` and the icon is at ``vscode-extension/resources/icon.svg``.

    Returns the first existing path, or ``None`` so the /icon.svg route can 404
    gracefully (the header ``<img>`` then just renders empty alt text).
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here.parent / "resources" / "icon.svg",
        here / "vscode-extension" / "resources" / "icon.svg",
    ):
        if candidate.is_file():
            return candidate
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        # self.path includes the query string, but every URL the UI emits has
        # one (e.g. "/?range=all"); compare the bare path so bookmarkable
        # URLs don't fall through to 404.
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # Inject runtime config (version + surface) the page can't know at
            # author time. json.dumps produces a valid JS object literal for the
            # `window.APP_CONFIG = __APP_CONFIG_JSON__;` placeholder in the head.
            config = json.dumps({"version": VERSION, "surface": SURFACE})
            html = HTML_TEMPLATE.replace("__APP_CONFIG_JSON__", config)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            # Pass DB_PATH explicitly: get_dashboard_data's default arg is frozen
            # to the original module global at def time, so a bare call would ignore
            # a monkey-patched dashboard.DB_PATH (same contract as /api/rescan). This
            # also keeps the dashboard reading the configured DB rather than a stale
            # path captured at import.
            data = get_dashboard_data(DB_PATH)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/icon.svg":
            icon = find_icon_file()
            if icon is None:
                self.send_response(404)
                self.end_headers()
                return
            body = icon.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            # Incremental scan: ingest new/changed JSONL without touching
            # existing rows. The DB is append-only and the only durable store
            # of history once Claude Code prunes old transcripts, so we must
            # never delete it here — scan() dedupes via the message_id index.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None, surface=None):
    global SURFACE
    if surface:
        SURFACE = surface
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
