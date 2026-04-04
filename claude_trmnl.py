#!/usr/bin/env python3
"""
claude-trmnl: Claude Code usage dashboard for TRMNL e-ink displays.

Reads local Claude Code session data and pushes rich usage metrics
to a TRMNL private plugin via webhook. Zero dependencies beyond stdlib.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Anthropic pricing (API-equivalent $/MTok) ───────────────────────

PRICING = {
    "claude-opus-4-6":   {"input": 15.0,  "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3.0,   "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.0,  "cache_write": 1.0,   "cache_read": 0.08},
}
_DEFAULT_PRICE = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}


# ── Formatting ───────────────────────────────────────────────────────

def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def fmt_cost(c):
    if c >= 100:
        return f"${c:,.0f}"
    if c >= 10:
        return f"${c:.1f}"
    return f"${c:.2f}"


# ── Model helpers ────────────────────────────────────────────────────

def _model_key(name):
    n = name.lower()
    if "opus" in n:
        return "claude-opus-4-6"
    if "sonnet" in n:
        return "claude-sonnet-4-6"
    if "haiku" in n:
        return "claude-haiku-4-5"
    # Filter out synthetic/internal model names
    if not n or n.startswith("<") or n.startswith("_"):
        return None
    return name


def _model_display(key):
    return {
        "claude-opus-4-6": "Opus",
        "claude-sonnet-4-6": "Sonnet",
        "claude-haiku-4-5": "Haiku",
    }.get(key, key.split("-")[0].title() if "-" in key else key[:10])


def _calc_cost(mk, inp, out, cw, cr):
    p = PRICING.get(mk, _DEFAULT_PRICE)
    return (inp * p["input"] + out * p["output"]
            + cw * p["cache_write"] + cr * p["cache_read"]) / 1_000_000


# ── Project name extraction ─────────────────────────────────────────

def _project_name(encoded):
    """Extract readable project name from encoded dir name.
    'C--Users-emanuele-github-trmnl-claude' -> 'trmnl-claude'
    """
    if "--claude-worktrees-" in encoded:
        encoded = encoded.split("--claude-worktrees-")[0]
    parts = encoded.split("--")
    last = parts[-1] if len(parts) > 1 else encoded
    segments = last.split("-")
    markers = {"github", "gitlab", "bitbucket", "repos", "projects"}
    for i, s in enumerate(segments):
        if s.lower() in markers and i + 1 < len(segments):
            return "-".join(segments[i + 1:])
    return "-".join(segments[-2:]) if len(segments) > 1 else segments[0]


# ── Data collection ─────────────────────────────────────────────────

def _find_claude_dir():
    home = Path.home()
    for d in [home / ".claude", home / ".config" / "claude"]:
        if d.exists():
            return d
    return home / ".claude"


def _read_credentials(claude_dir):
    cred = claude_dir / ".credentials.json"
    if not cred.exists():
        return "Unknown", "—"
    try:
        data = json.loads(cred.read_text("utf-8"))
        oauth = data.get("claudeAiOauth", {})
        sub = oauth.get("subscriptionType", "unknown").capitalize()
        tier = oauth.get("rateLimitTier", "")
        parts = tier.split("_")
        tier_short = next(
            (p for p in reversed(parts) if p.endswith("x") and p[:-1].isdigit()),
            "standard",
        )
        return sub, tier_short
    except Exception:
        return "Unknown", "—"


def _count_active_sessions(claude_dir):
    sdir = claude_dir / "sessions"
    if not sdir.exists():
        return 0
    count = 0
    for f in sdir.glob("*.json"):
        try:
            pid = json.loads(f.read_text("utf-8")).get("pid")
            if pid and _pid_alive(pid):
                count += 1
        except Exception:
            pass
    return count


def _pid_alive(pid):
    if sys.platform == "win32":
        import subprocess
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scan_usage(claude_dir, since):
    """Scan session JSONL files for token usage since a given datetime."""
    daily = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "messages": 0, "sessions": set(),
    })
    models = defaultdict(lambda: {"tokens": 0, "messages": 0, "cost": 0.0})
    projects = defaultdict(lambda: {"tokens": 0, "messages": 0})
    since_epoch = since.timestamp()

    for base in [claude_dir / "projects", Path.home() / ".config" / "claude" / "projects"]:
        if not base.exists():
            continue
        for pdir in base.iterdir():
            if not pdir.is_dir():
                continue
            proj = _project_name(pdir.name)
            for jf in pdir.glob("*.jsonl"):
                try:
                    if jf.stat().st_mtime < since_epoch - 3600:
                        continue
                except OSError:
                    continue
                _process_jsonl(jf, since, daily, models, projects, proj)

    return daily, models, projects


def _process_jsonl(path, since, daily, models, projects, proj):
    session_days = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                ts_str = entry.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < since:
                    continue

                msg = entry.get("message", {})
                u = msg.get("usage")
                if not u:
                    continue

                inp = u.get("input_tokens", 0)
                out = u.get("output_tokens", 0)
                cr = u.get("cache_read_input_tokens", 0)
                cw = u.get("cache_creation_input_tokens", 0)
                mk = _model_key(msg.get("model", ""))
                if mk is None:
                    continue
                total = inp + out + cr + cw
                cost = _calc_cost(mk, inp, out, cw, cr)
                dk = ts.strftime("%Y-%m-%d")

                d = daily[dk]
                d["input"] += inp
                d["output"] += out
                d["cache_read"] += cr
                d["cache_write"] += cw
                d["cost"] += cost
                d["messages"] += 1
                if dk not in session_days:
                    d["sessions"].add(path.stem)
                    session_days.add(dk)

                models[mk]["tokens"] += total
                models[mk]["messages"] += 1
                models[mk]["cost"] += cost

                projects[proj]["tokens"] += total
                projects[proj]["messages"] += 1
    except (IOError, PermissionError):
        pass


# ── Sparkline & streak ───────────────────────────────────────────────

def _day_total(daily, key):
    d = daily.get(key, {})
    return d.get("input", 0) + d.get("output", 0) + d.get("cache_read", 0) + d.get("cache_write", 0)


def _sparkline(daily, days=7):
    blocks = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    today = datetime.now(timezone.utc).date()
    vals = [_day_total(daily, (today - timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(days - 1, -1, -1)]
    mx = max(vals) if any(vals) else 1
    return "".join(blocks[min(8, int(v / mx * 8))] for v in vals)


def _streak(daily):
    today = datetime.now(timezone.utc).date()
    s = 0
    d = today
    while _day_total(daily, d.strftime("%Y-%m-%d")) > 0:
        s += 1
        d -= timedelta(days=1)
    return s


# ── Build TRMNL payload ─────────────────────────────────────────────

def build_payload():
    cd = _find_claude_dir()
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_ago = today_start - timedelta(days=7)

    sub_type, tier = _read_credentials(cd)
    active = _count_active_sessions(cd)
    daily, models, projects = _scan_usage(cd, since=seven_ago)

    # Today
    today_key = now.strftime("%Y-%m-%d")
    td = daily.get(today_key, {})
    t_in = td.get("input", 0)
    t_out = td.get("output", 0)
    t_cr = td.get("cache_read", 0)
    t_cw = td.get("cache_write", 0)
    t_total = t_in + t_out + t_cr + t_cw
    t_cost = td.get("cost", 0.0)
    t_msgs = td.get("messages", 0)
    t_sess = len(td.get("sessions", set()))

    # Yesterday (trend)
    yest_key = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    y_total = _day_total(daily, yest_key)
    if y_total == 0:
        trend = "new"
    elif t_total > y_total * 1.1:
        trend = "up"
    elif t_total < y_total * 0.9:
        trend = "down"
    else:
        trend = "flat"

    # Week totals
    week_start_key = (today_start - timedelta(days=today_start.weekday())).strftime("%Y-%m-%d")
    w_total = w_msgs = w_sess = 0
    w_cost = 0.0
    for dk, dd in daily.items():
        if dk >= week_start_key:
            w_total += _day_total(daily, dk)
            w_cost += dd.get("cost", 0.0)
            w_sess += len(dd.get("sessions", set()))
            w_msgs += dd.get("messages", 0)

    # Model breakdown (sorted by tokens desc)
    models_sorted = sorted(models.items(), key=lambda x: x[1]["tokens"], reverse=True)
    total_model_tokens = sum(m["tokens"] for _, m in models_sorted)

    # Date format (Windows vs Unix)
    try:
        updated = now.strftime("%b %-d, %H:%M")
    except ValueError:
        updated = now.strftime("%b %#d, %H:%M")

    mv = {
        "sub": sub_type,
        "tier": tier,
        "active": active,
        # Today
        "t_input": fmt_tokens(t_in),
        "t_output": fmt_tokens(t_out),
        "t_cache_r": fmt_tokens(t_cr),
        "t_cache_w": fmt_tokens(t_cw),
        "t_total": fmt_tokens(t_total),
        "t_cost": fmt_cost(t_cost),
        "t_sessions": t_sess,
        "t_messages": t_msgs,
        "trend": trend,
        # Week
        "w_tokens": fmt_tokens(w_total),
        "w_cost": fmt_cost(w_cost),
        "w_sessions": w_sess,
        "w_messages": w_msgs,
        # Sparkline & streak
        "spark": _sparkline(daily),
        "streak": _streak(daily),
        # Top project
        "top_project": max(projects, key=lambda k: projects[k]["tokens"]) if projects else "—",
        # Timestamp
        "updated": updated,
    }

    # Model slots (up to 3)
    for i in range(3):
        idx = i + 1
        if i < len(models_sorted):
            mk, md = models_sorted[i]
            pct = int(md["tokens"] / total_model_tokens * 100) if total_model_tokens else 0
            mv[f"m{idx}_name"] = _model_display(mk)
            mv[f"m{idx}_tokens"] = fmt_tokens(md["tokens"])
            mv[f"m{idx}_pct"] = pct
            mv[f"m{idx}_cost"] = fmt_cost(md["cost"])
        else:
            mv[f"m{idx}_name"] = ""
            mv[f"m{idx}_tokens"] = ""
            mv[f"m{idx}_pct"] = 0
            mv[f"m{idx}_cost"] = ""

    return mv


# ── TRMNL webhook ────────────────────────────────────────────────────

def post_to_trmnl(merge_variables):
    uuid = os.environ.get("TRMNL_PLUGIN_UUID")
    if not uuid:
        print("Error: TRMNL_PLUGIN_UUID environment variable not set", file=sys.stderr)
        sys.exit(1)

    url = f"https://usetrmnl.com/api/custom_plugins/{uuid}"
    data = json.dumps({"merge_variables": merge_variables}).encode("utf-8")

    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"OK ({resp.status}): {resp.read().decode('utf-8')}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Claude Code usage dashboard for TRMNL e-ink displays")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print payload JSON without posting to TRMNL")
    args = parser.parse_args()

    payload = build_payload()

    if args.dry_run:
        print(json.dumps(payload, indent=2, default=str))
    else:
        post_to_trmnl(payload)


if __name__ == "__main__":
    main()
