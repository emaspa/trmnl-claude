#!/usr/bin/env python3
"""
claude-trmnl: Claude Code usage dashboard for TRMNL e-ink displays.

Reads local Claude Code session data and pushes rich usage metrics
to a TRMNL private plugin via webhook. Zero dependencies beyond stdlib.
"""

import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Anthropic pricing (API-equivalent $/MTok) ───────────────────────
#
# Cache writes aren't listed: they're a multiple of the input price and depend
# on the TTL the request asked for (see CACHE_WRITE_MULT). Cache reads are
# 0.1x input everywhere except Fable 5.1, which reads at 0.025x.

PRICING = {
    "claude-fable-5-1":  {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5":    {"input": 10.0, "output": 50.0, "cache_read": 1.00},
    # Covers all Opus versions (4.6+ share this price; legacy 4.1/4.0/3 were $15/$75)
    "claude-opus-5":     {"input": 5.0,  "output": 25.0, "cache_read": 0.50},
    # Sonnet 5 cut the sticker price; Sonnet 4.6 and earlier stay at $3/$15.
    "claude-sonnet-5":   {"input": 2.0,  "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.10},
}
# Fallback for models this table doesn't know. Sonnet-4.6 rates, so the cost
# line stays an estimate rather than a gap. By default this only applies to an
# Anthropic model newer than this table, since anything else is left out of the
# figures entirely (see _is_anthropic).
_DEFAULT_PRICE = {"input": 3.0, "output": 15.0, "cache_read": 0.30}

# Anthropic families. A model naming one of these is Anthropic's whatever else
# the id says.
_ANTHROPIC_FAMILIES = ("fable", "opus", "sonnet", "haiku")

# Vendors that turn up proxied through a gateway. They matter because such a
# gateway may hand back an id like "claude-gpt-6-astra": the "claude-" prefix
# proves nothing on its own, so the vendor name has to be checked for.
_FOREIGN_VENDORS = ("gpt", "qwen", "llama", "gemini", "mistral", "deepseek",
                    "grok", "kimi", "glm", "command-r", "phi")


def _name_matcher(words):
    """Match any of these names in a model id, on letter boundaries.

    Plain substring matching reads "phi" inside "claude-delphi" and drops a
    model that was Anthropic's all along. Digits are not a boundary, because
    versions run straight into the name: "qwen3.8-max" has to match "qwen".
    """
    return re.compile(r"(?<![a-z])(?:%s)(?![a-z])"
                      % "|".join(re.escape(w) for w in words))


_ANTHROPIC_RE = _name_matcher(_ANTHROPIC_FAMILIES)
_FOREIGN_RE = _name_matcher(_FOREIGN_VENDORS)

CACHE_WRITE_MULT = {"5m": 1.25, "1h": 2.0}


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
    """Collapse a model id onto a pricing key. Only versions that differ in
    price get their own key -- the rest fold into their family."""
    n = name.lower()
    if "fable" in n:
        return "claude-fable-5-1" if re.search(r"fable-?5[.-]1", n) else "claude-fable-5"
    if "opus" in n:
        return "claude-opus-5"
    if "sonnet" in n:
        return "claude-sonnet-5" if re.search(r"sonnet-?5(?!\d)", n) else "claude-sonnet-4-6"
    if "haiku" in n:
        return "claude-haiku-4-5"
    # Filter out synthetic/internal model names
    if not n or n.startswith("<") or n.startswith("_"):
        return None
    return name


_MODEL_NAMES = {
    "claude-fable-5-1": "Fable",
    "claude-fable-5": "Fable",
    "claude-opus-5": "Opus",
    "claude-sonnet-5": "Sonnet",
    "claude-sonnet-4-6": "Sonnet",
    "claude-haiku-4-5": "Haiku",
}


def _model_display(key):
    if key in _MODEL_NAMES:
        return _MODEL_NAMES[key]
    # Unknown model, most likely proxied through a gateway. Skip the "claude-"
    # prefix such ids often carry so a non-Claude model isn't labelled "Claude".
    parts = [p for p in key.split("-") if p and p != "claude"]
    return parts[0].title()[:10] if parts else key[:10]


def _is_anthropic(key):
    """Whether this model's usage belongs in the figures on the display.

    Two of the numbers shown are Anthropic-specific: the cost is priced off
    Anthropic's list, and the session/week/per-model bars come from Anthropic's
    rate-limit headers. A model reached through a gateway is neither priced by
    that table nor counted against those limits, so folding its tokens into the
    same totals invents a dollar figure and makes the totals describe a
    different population than the bars beside them.
    """
    n = (key or "").lower()
    if _ANTHROPIC_RE.search(n):
        return True
    if _FOREIGN_RE.search(n):
        return False
    # An unfamiliar "claude-…" is most likely a family this version predates.
    # Counting it is the safer mistake: dropping real usage is worse than
    # carrying a little that shouldn't be there, and it shows up as a model
    # name nobody recognises rather than vanishing.
    return n.startswith("claude")


def _calc_cost(mk, inp, out, cw5m, cw1h, cr):
    p = PRICING.get(mk, _DEFAULT_PRICE)
    return (inp * p["input"]
            + out * p["output"]
            + cw5m * p["input"] * CACHE_WRITE_MULT["5m"]
            + cw1h * p["input"] * CACHE_WRITE_MULT["1h"]
            + cr * p["cache_read"]) / 1_000_000


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


def _scan_usage(claude_dir, since, include_other=False):
    """Scan session JSONL files for token usage since a given datetime."""
    daily = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "messages": 0, "sessions": set(),
        # What was left out, so the excluded work is reportable rather than
        # merely absent. Zero when include_other folds it into the rest.
        "other_tokens": 0, "other_messages": 0,
    })
    models = defaultdict(lambda: {"tokens": 0, "messages": 0, "cost": 0.0})
    projects = defaultdict(lambda: {"tokens": 0, "messages": 0})
    since_epoch = since.timestamp()
    seen = set()

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
                _process_jsonl(jf, since, daily, models, projects, proj, seen,
                               include_other)

    return daily, models, projects


def _entry_id(entry, msg):
    """Identity of the API response an entry reports usage for.

    Claude Code writes one JSONL line per content block, and every line repeats
    the whole message's usage -- so a reply with thinking + text + a tool call
    lands three times. Sessions that get resumed or forked also copy their
    history into the new file. Counting lines instead of responses inflates
    every token, cost and message figure, so fold them by (message id, request
    id). Entries with no message id can't be folded; key them by line uuid so
    they're still counted exactly once.
    """
    mid = msg.get("id")
    if not mid:
        return ("uuid", entry.get("uuid"))
    return (mid, entry.get("requestId"))


def _process_jsonl(path, since, daily, models, projects, proj, seen,
                   include_other=False):
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

                key = _entry_id(entry, msg)
                if key in seen:
                    continue
                seen.add(key)

                inp = u.get("input_tokens", 0)
                out = u.get("output_tokens", 0)
                cr = u.get("cache_read_input_tokens", 0)
                cw = u.get("cache_creation_input_tokens", 0)
                # Cache writes bill 1.25x input at the 5-minute TTL and 2x at
                # the 1-hour one. Split them when the breakdown is present;
                # older entries only carry the total, so assume the cheaper TTL.
                ttl = u.get("cache_creation") or {}
                cw1h = ttl.get("ephemeral_1h_input_tokens", 0)
                cw5m = ttl.get("ephemeral_5m_input_tokens", cw if not ttl else 0)
                mk = _model_key(msg.get("model", ""))
                if mk is None:
                    continue
                total = inp + out + cr + cw
                cost = _calc_cost(mk, inp, out, cw5m, cw1h, cr)
                dk = ts.astimezone().strftime("%Y-%m-%d")

                # A model that isn't Anthropic's has no price in the table and
                # never counted against the limits on the display, so it is
                # tallied on its own rather than folded into figures it would
                # misstate. The session id is left out too, so the session
                # count keeps describing the tokens shown next to it.
                if not include_other and not _is_anthropic(mk):
                    o = daily[dk]
                    o["other_tokens"] += total
                    o["other_messages"] += 1
                    continue

                d = daily[dk]
                d["input"] += inp
                d["output"] += out
                d["cache_read"] += cr
                d["cache_write"] += cw
                d["cost"] += cost
                d["messages"] += 1
                d["sessions"].add(path.stem)

                models[mk]["tokens"] += total
                models[mk]["messages"] += 1
                models[mk]["cost"] += cost

                projects[proj]["tokens"] += total
                projects[proj]["messages"] += 1
    except (IOError, PermissionError):
        pass


# ── Sparkline & streak ───────────────────────────────────────────────

def _day_total(daily, key, with_other=False):
    """A day's tokens. with_other adds back what the model filter left out.

    The sparkline and the streak answer "was there work that day", which a day
    spent on a model reached through a gateway plainly was. The cost and the
    limit bars are Anthropic-specific and stay filtered, so these two are the
    only places the wider count belongs.
    """
    d = daily.get(key, {})
    total = (d.get("input", 0) + d.get("output", 0)
             + d.get("cache_read", 0) + d.get("cache_write", 0))
    return total + d.get("other_tokens", 0) if with_other else total


def _sparkline(daily, days=7, with_other=False):
    blocks = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    today = datetime.now().date()
    vals = [_day_total(daily, (today - timedelta(days=i)).strftime("%Y-%m-%d"), with_other)
            for i in range(days - 1, -1, -1)]
    mx = max(vals) if any(vals) else 1
    return "".join(blocks[min(8, int(v / mx * 8))] for v in vals)


def _streak(daily, with_other=False):
    today = datetime.now().date()
    s = 0
    d = today
    # If no usage today yet, start counting from yesterday (handles timezone offsets)
    if _day_total(daily, d.strftime("%Y-%m-%d"), with_other) == 0:
        d -= timedelta(days=1)
    while _day_total(daily, d.strftime("%Y-%m-%d"), with_other) > 0:
        s += 1
        d -= timedelta(days=1)
    return s


# ── Usage scraper (PTY) ──────────────────────────────────────────────

def _read_usage(method="auto", model_ttl_min=None):
    """Current rate-limit usage as {"session": {...}, "week_all": {...}}.

    Two ways to get the same numbers. The API returns them as response headers
    on any request, which takes about half a second; the /usage TUI shows them
    too, but reading that means driving a real Claude Code session over a PTY
    for ~20 seconds.

    The catch is that no header reports the per-model weekly row, so "auto"
    reads session and week from the headers every run and refreshes just that
    row from the TUI on a much slower schedule, caching it in between.
    """
    if method == "pty":
        return _scrape_usage()

    limits = _usage_from_headers()
    if not limits:
        return {} if method == "headers" else _scrape_usage()
    if method == "headers":
        return limits

    row, stale = _cached_model_limit(model_ttl_min)
    if stale:
        fresh = _scrape_usage().get("week_model")
        if fresh:
            _store_model_limit(fresh)
            row = fresh
    if row:
        limits["week_model"] = row
    return limits


# How often "auto" re-scrapes the per-model weekly row. Each refresh costs a
# ~20s PTY session, so it stays rare -- but a cap you're close to is worth
# watching more often, since that's when the number actually moves.
_MODEL_LIMIT_TTL_MIN = 60
_MODEL_LIMIT_TTL_NEAR_CAP_MIN = 15
_MODEL_LIMIT_NEAR_CAP_PCT = 80
# Past this the cached row is dropped rather than shown. A weekly limit resets,
# and a stale 95% after a reset is worse than showing nothing.
_MODEL_LIMIT_MAX_AGE_MIN = 360


def _model_limit_path():
    return _find_claude_dir() / ".trmnl_model_limit"


def _cached_model_limit(ttl_min=None):
    """Cached per-model weekly row, and whether it's due a refresh."""
    try:
        cached = json.loads(_model_limit_path().read_text("utf-8"))
        row = cached["row"]
        age = datetime.now(timezone.utc).timestamp() - float(cached["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, True
    if not isinstance(row, dict) or age >= _MODEL_LIMIT_MAX_AGE_MIN * 60:
        return None, True

    if ttl_min is None:
        pct = row.get("pct")
        ttl_min = (_MODEL_LIMIT_TTL_NEAR_CAP_MIN
                   if isinstance(pct, int) and pct >= _MODEL_LIMIT_NEAR_CAP_PCT
                   else _MODEL_LIMIT_TTL_MIN)
    return row, age >= ttl_min * 60


def _store_model_limit(row):
    try:
        _model_limit_path().write_text(
            json.dumps({"ts": datetime.now(timezone.utc).timestamp(), "row": row}),
            encoding="utf-8")
    except OSError:
        pass


def _usage_from_headers():
    """Read usage from the rate-limit headers on a 1-token API call.

    Costs one token to measure, and the headers are undocumented, so treat any
    failure as "no data" and let the caller fall back to the PTY scrape.

    Raw HTTP rather than the anthropic SDK on purpose: this asks for response
    metadata rather than a completion, and the project stays stdlib-only.
    """
    cred = _find_claude_dir() / ".credentials.json"
    try:
        oauth = json.loads(cred.read_text("utf-8")).get("claudeAiOauth", {})
    except (OSError, ValueError):
        return {}
    # Claude Code rotates this token every few hours, so never cache it.
    token = oauth.get("accessToken")
    if not token:
        return {}

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": "claude-haiku-4-5",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            headers = r.headers
    except urllib.error.HTTPError as e:
        # 429 still carries the utilization headers -- that's the case we most
        # want to report. Anything else (401 on an expired token, 5xx) has none.
        headers = e.headers
    except Exception:
        return {}

    out = {}
    for key, prefix in (("session", "5h"), ("week_all", "7d")):
        util = headers.get(f"anthropic-ratelimit-unified-{prefix}-utilization")
        reset = headers.get(f"anthropic-ratelimit-unified-{prefix}-reset")
        if util is None:
            continue
        try:
            pct = round(float(util) * 100)
        except ValueError:
            continue
        out[key] = {"pct": pct, "resets": _fmt_reset(reset)}
    # No per-model counterpart exists, so week_sonnet stays absent and the
    # dashboard shows it as "—". Use --usage-method pty if you need it.
    return out


def _fmt_reset(epoch):
    """Format a reset timestamp for the display: '2:59pm', or 'Sep 18, 9am'
    once it's past midnight."""
    try:
        t = datetime.fromtimestamp(int(epoch)).astimezone()
    except (TypeError, ValueError):
        return None

    def _fmt(fmt):
        try:
            return t.strftime(fmt)
        except ValueError:            # Windows strftime has no %-
            return t.strftime(fmt.replace("%-", "%#"))

    clock = _fmt("%-I:%M%p").lower()
    if t.date() == datetime.now().date():
        return clock
    return _fmt("%b %-d, ") + clock


def _scrape_usage():
    """Scrape /usage from Claude Code via PTY. Returns dict with session/week pct.

    Never raises: a missing PTY library, a `claude` that isn't on PATH or a
    session that dies mid-scrape all mean "no usage data", not a failed run.
    """
    import threading
    try:
        try:
            from winpty import PtyProcess
        except ImportError:
            import pexpect
            return _scrape_usage_pexpect(pexpect)
        return _scrape_usage_winpty(PtyProcess, threading)
    except Exception:
        return {}


def _spawn_env():
    """Environment for a scraper's `claude` process.

    These sessions live ~25 seconds. That isn't long enough for the auto
    updater to finish fetching a new release, so on a short polling interval it
    restarts the download every run and leaves a truncated binary in
    ~/.cache/claude/staging each time -- which only gets cleaned up after an
    update that succeeds. Opt these spawns out; interactive sessions and
    `claude update` still update normally.
    """
    return {**os.environ, "DISABLE_AUTOUPDATER": "1"}


def _scrape_usage_winpty(PtyProcess, threading):
    """Windows: use winpty."""
    proc = PtyProcess.spawn('claude', dimensions=(45, 180), env=_spawn_env())
    output = []

    def reader():
        while proc.isalive():
            try:
                chunk = proc.read(4096)
                if chunk:
                    output.append(chunk)
            except EOFError:
                break
            except Exception:
                import time; time.sleep(0.2)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    import time
    time.sleep(6)

    if not proc.isalive():
        return {}

    proc.write('/usage\r')
    time.sleep(8)
    proc.write('\x1b')
    time.sleep(1)
    proc.write('/exit\r')
    time.sleep(2)
    try:
        proc.close(force=True)
    except Exception:
        pass
    t.join(timeout=2)
    return _parse_usage_output("".join(output))


def _scrape_usage_pexpect(pexpect):
    """macOS/Linux: use pexpect.

    pexpect's .before only holds bytes received before the last expect() match,
    so it never captures the /usage panel (drawn after the prompt). Drain the
    PTY continuously instead. The slash-command autocomplete also swallows the
    first Enter, so type /usage, let the menu settle, then send an explicit CR.
    """
    import time
    buf = []

    def drain(seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                buf.append(proc.read_nonblocking(8192, timeout=0.4))
            except Exception:
                pass

    proc = pexpect.spawn('claude', dimensions=(55, 200), encoding='utf-8',
                         timeout=30, env=_spawn_env())
    try:
        proc.expect([r'[>❯]', r'\u2570'], timeout=10)
    except Exception:
        pass
    drain(6)            # wait for the TUI to come up
    proc.send('/usage')
    time.sleep(2)       # let the autocomplete menu render
    proc.send('\r')     # submit the command
    time.sleep(1)
    proc.send('\r')     # extra CR in case the first was swallowed by autocomplete
    drain(9)            # capture the usage panel as it draws
    proc.send('\x1b')
    time.sleep(0.5)
    proc.sendline('/exit')
    drain(2)
    try:
        proc.close()
    except Exception:
        pass
    return _parse_usage_output("".join(buf))


def _parse_usage_output(raw):
    """Parse /usage TUI output into dict."""
    clean = re.sub(
        r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]'
        r'|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][AB012]|\x1b[>=<]',
        '', raw
    )

    def row(match):
        """Percentage and reset time from the block a heading introduces."""
        block = clean[match.start():match.start() + 400]
        pct = re.search(r'(\d+)\s*%', block)
        reset = re.search(r'[Rr]esets?\s*(.+?)(?:\r|\n|$)', block)
        return {
            "pct": int(pct.group(1)) if pct else None,
            "resets": reset.group(1).strip() if reset else None,
        }

    result = {}
    # The /usage TUI is drawn with cursor positioning, so winpty capture often
    # merges adjacent words ("Currentsession", "allmodels", "ResetsJun4") and
    # drops the odd character. Keep whitespace optional (\s*) so percentages
    # still parse.
    for key, pattern in {
        "session": r"(?i)current\s*session",
        "week_all": r"(?i)(?:current\s*)?week\s*\(?\s*all\s*models",
    }.items():
        m = re.search(pattern, clean)
        if m:
            result[key] = row(m)

    # Plans that cap one model separately get a third row. Which model that is
    # has changed over time (Sonnet once, Fable now) and differs by plan, so
    # read the name off the panel instead of matching one hardcoded model.
    m = re.search(r'(?i)(?:current\s*)?week\s*\(\s*(?!all\s*models)'
                  r'([A-Za-z][\w .-]{0,20}?)\s*\)', clean)
    if m:
        result["week_model"] = {**row(m), "name": m.group(1).strip().title()}
    return result


# ── Fleet ────────────────────────────────────────────────────────────
#
# Several machines can share one display. Each reads only its own ~/.claude,
# so their numbers add up rather than overlap, but only one of them may post:
# TRMNL keeps the last payload it received, so two hosts posting their own
# halves means the display shows whichever half arrived most recently.
#
# The master pulls each secondary's store, merges, posts the sum, then pushes
# the merged store back out. If it goes quiet for longer than the takeover
# window, a secondary posts instead, pairing its own fresh scan with the last
# figures it holds for everyone else. Because the store is keyed by host, a
# secondary replaces its own entry and leaves the rest alone, so taking over
# never double-counts. When the master returns it pulls those stores and keeps
# the newest entry per host, which absorbs the outage with no special case.
#
# Secondaries never need to reach the master. The heartbeat travels inside the
# store the master pushes, so a secondary decides whether to take over by
# reading a local file. That matters: the master can ssh out, and the reverse
# direction is usually not set up.
#
# Past two machines, "a secondary takes over" isn't enough: every secondary
# sees the same stale heartbeat at the same moment and they all post, which is
# the flipping display again. So secondaries queue. The store records when each
# host first appeared, and succession follows that join order, each successor
# waiting one stagger interval longer than the one ahead of it. If the first
# successor is down too, it simply never posts, the heartbeat keeps ageing, and
# the next one's turn arrives on its own.

FLEET_STORE_NAME = ".trmnl_fleet.json"
_FLEET_DEFAULTS = {"takeover_after_min": 45, "stale_after_min": 60,
                   "successor_stagger_min": 15}
_FLEET_REMOTE_CMD = "~/trmnl-claude/run.sh"
_FLEET_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
_FLEET_TIMEOUT = 120


def _this_host():
    return socket.gethostname().split(".")[0]


def _fleet_config_path(explicit=None):
    if explicit:
        return Path(explicit)
    env = os.environ.get("TRMNL_FLEET_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "fleet.json"


def _load_fleet_config(explicit=None):
    """Fleet config, or None for a single-machine install.

    A missing, malformed or one-host config is not an error: it means this
    install has no fleet, and everything behaves as it did before.
    """
    try:
        cfg = json.loads(_fleet_config_path(explicit).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    hosts = [h for h in (cfg.get("hosts") or []) if isinstance(h, dict) and h.get("name")]
    if len(hosts) < 2 or not cfg.get("master"):
        return None
    out = dict(_FLEET_DEFAULTS)
    for k in _FLEET_DEFAULTS:
        if isinstance(cfg.get(k), int) and cfg[k] > 0:
            out[k] = cfg[k]
    out["master"] = cfg["master"]
    out["hosts"] = hosts
    return out


def _aggregate_local(claude_dir, since, include_other=False):
    """This machine's contribution, in the shape hosts exchange.

    Sets aren't JSON, so session ids travel as a sorted list and become a set
    again on merge.
    """
    daily, models, projects = _scan_usage(claude_dir, since, include_other)
    sub, tier = _read_credentials(claude_dir)
    return {
        "host": _this_host(),
        "ts": datetime.now(timezone.utc).timestamp(),
        "active": _count_active_sessions(claude_dir),
        # Account-wide, so any host that can read them speaks for all of them.
        # macOS keeps these in the Keychain rather than in a file, so a Mac
        # reports Unknown and has to borrow the answer from someone else.
        "sub": sub,
        "tier": tier,
        # Which population this host counted. The poster passes its own setting
        # when it collects, so these normally agree; a host that took over on
        # its own schedule is how they come apart.
        "include_other": bool(include_other),
        "daily": {k: {**v, "sessions": sorted(v["sessions"])} for k, v in daily.items()},
        "models": {k: dict(v) for k, v in models.items()},
        "projects": {k: dict(v) for k, v in projects.items()},
    }


def _merge_aggregates(aggs, stale_cut=None):
    """Sum per-host aggregates into the shapes the payload renders from.

    Counts add because hosts scan disjoint transcripts. Session ids union
    instead: one session is one session, and ids are uuids, so a host can't
    collide with another. Active sessions are a right-now reading, so a host
    whose aggregate has gone stale contributes none.
    """
    daily = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
        "cost": 0.0, "messages": 0, "sessions": set(),
        "other_tokens": 0, "other_messages": 0,
    })
    models = defaultdict(lambda: {"tokens": 0, "messages": 0, "cost": 0.0})
    projects = defaultdict(lambda: {"tokens": 0, "messages": 0})
    active = 0
    fresh = 0

    # Newest host that could actually read the plan speaks for the account.
    sub = tier = None
    for a in sorted((x for x in aggs if isinstance(x, dict)),
                    key=lambda x: float(x.get("ts", 0)), reverse=True):
        if a.get("sub") and a["sub"] != "Unknown":
            sub, tier = a["sub"], a.get("tier", "—")
            break

    for a in aggs:
        if not isinstance(a, dict):
            continue
        current = stale_cut is None or float(a.get("ts", 0)) >= stale_cut
        fresh += 1 if current else 0
        if current:
            active += a.get("active", 0) or 0
        for dk, dd in (a.get("daily") or {}).items():
            d = daily[dk]
            for f in ("input", "output", "cache_read", "cache_write", "messages",
                      "other_tokens", "other_messages"):
                d[f] += dd.get(f, 0) or 0
            d["cost"] += dd.get("cost", 0.0) or 0.0
            d["sessions"] |= set(dd.get("sessions") or ())
        for mk, md in (a.get("models") or {}).items():
            m = models[mk]
            m["tokens"] += md.get("tokens", 0) or 0
            m["messages"] += md.get("messages", 0) or 0
            m["cost"] += md.get("cost", 0.0) or 0.0
        for pk, pd in (a.get("projects") or {}).items():
            p = projects[pk]
            p["tokens"] += pd.get("tokens", 0) or 0
            p["messages"] += pd.get("messages", 0) or 0

    return daily, models, projects, active, fresh, sub, tier


def _fleet_store_path():
    return _find_claude_dir() / FLEET_STORE_NAME


def _load_store():
    try:
        s = json.loads(_fleet_store_path().read_text("utf-8"))
    except (OSError, ValueError):
        s = None
    if not isinstance(s, dict):
        s = {}
    if not isinstance(s.get("hosts"), dict):
        s["hosts"] = {}
    if not isinstance(s.get("last_post"), dict):
        s["last_post"] = {}
    if not isinstance(s.get("last_post_by"), dict):
        s["last_post_by"] = {}
    # Stores written before posts were tracked per host carry only the single
    # most recent one. Seed from it, or every secondary reads a master that has
    # never posted and takes over on the first run after an upgrade.
    lp = s["last_post"]
    if lp.get("by") and lp.get("ts") and lp["by"] not in s["last_post_by"]:
        s["last_post_by"][lp["by"]] = lp["ts"]
    if not isinstance(s.get("members"), dict):
        s["members"] = {}
    return s


def _ensure_member(store, host, now):
    """Record when a host first joined. Succession follows this order."""
    if host not in store["members"]:
        store["members"][host] = {"joined": now}


def _successor_rank(cfg, store, me):
    """Where this host sits in the queue behind the master.

    Rank 0 takes over first. Oldest member wins, with the name as a tiebreak so
    two hosts that joined in the same second still queue deterministically. A
    host the config doesn't list goes last.
    """
    queue = [h["name"] for h in cfg["hosts"] if h["name"] != cfg["master"]]
    members = store.get("members") or {}

    def joined(name):
        try:
            return float(members[name]["joined"])
        except (KeyError, TypeError, ValueError):
            return float("inf")   # never seen: behind everyone who has

    queue.sort(key=lambda n: (joined(n), n))
    return queue.index(me) if me in queue else len(queue)


def _save_store(store):
    try:
        p = _fleet_store_path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(store), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _merge_stores(into, other):
    """Fold another host's store in, keeping the newest entry per host."""
    if not isinstance(other, dict):
        return into
    for host, agg in (other.get("hosts") or {}).items():
        if not isinstance(agg, dict):
            continue
        cur = into["hosts"].get(host)
        if not cur or float(agg.get("ts", 0)) > float(cur.get("ts", 0)):
            into["hosts"][host] = agg
    # Join times settle the other way round: the earliest sighting is the true
    # one, so a host that rebuilt its store can't jump the queue.
    for host, info in (other.get("members") or {}).items():
        if not isinstance(info, dict) or "joined" not in info:
            continue
        cur = into["members"].get(host)
        try:
            if not cur or float(info["joined"]) < float(cur["joined"]):
                into["members"][host] = info
        except (KeyError, TypeError, ValueError):
            into["members"][host] = info

    theirs = other.get("last_post") or {}
    if float(theirs.get("ts", 0)) > float(into["last_post"].get("ts", 0)):
        into["last_post"] = theirs

    for host, ts in (other.get("last_post_by") or {}).items():
        try:
            if float(ts) > float(into["last_post_by"].get(host, 0) or 0):
                into["last_post_by"][host] = ts
        except (TypeError, ValueError):
            continue

    # The cached limits travel with the store, which is the only way a host
    # that can't read them itself ever gets any.
    mine = into.get("limits") or {}
    yours = other.get("limits") or {}
    try:
        if float(yours.get("ts", 0)) > float(mine.get("ts", 0)):
            into["limits"] = yours
    except (TypeError, ValueError):
        pass
    return into


def _remote(host, extra_arg, stdin=None):
    """Run this script on another host. None if it can't be reached."""
    target = host.get("ssh")
    if not target:
        return None
    cmd = host.get("cmd") or _FLEET_REMOTE_CMD
    try:
        r = subprocess.run(
            ["ssh", *_FLEET_SSH_OPTS, target, f"{cmd} {extra_arg}"],
            input=stdin, capture_output=True, timeout=_FLEET_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return r if r.returncode == 0 else None


def _pull_store(host, include_other=False):
    """Collect a host's store.

    Whether models that aren't Anthropic's count is decided when a host scans,
    not when the payload is built, so the poster passes its own setting along.
    Otherwise a fleet where the hosts disagree posts a silent mixture.
    """
    arg = "--emit-store" + (" --include-other-models" if include_other else "")
    r = _remote(host, arg)
    if r is None:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def _push_store(host, store):
    return _remote(host, "--ingest-store",
                   stdin=json.dumps(store).encode("utf-8")) is not None


# ── Build TRMNL payload ─────────────────────────────────────────────

def build_payload(usage_method="auto", model_ttl_min=None,
                  aggregates=None, stale_cut=None, hosts_total=0,
                  usage_limits=None, include_other=False,
                  strict_activity=False):
    cd = _find_claude_dir()
    # Day boundaries follow the local clock, so "today" means the same thing
    # here as it does on the wall and in the usage reset times below.
    now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_ago = today_start - timedelta(days=7)

    sub_type, tier = _read_credentials(cd)
    if aggregates is None:
        aggregates = [_aggregate_local(cd, since=seven_ago, include_other=include_other)]
    (daily, models, projects, active, fresh,
     fleet_sub, fleet_tier) = _merge_aggregates(aggregates, stale_cut)
    if sub_type == "Unknown" and fleet_sub:
        sub_type, tier = fleet_sub, fleet_tier
    if usage_limits is None:
        usage_limits = ({} if usage_method == "off"
                        else _read_usage(usage_method, model_ttl_min))

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
    t_other = td.get("other_tokens", 0)
    t_other_msgs = td.get("other_messages", 0)
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
    w_total = w_msgs = 0
    w_cost = 0.0
    w_sessions = set()   # union, not a sum: a session spanning two days is one session
    for dk, dd in daily.items():
        if dk >= week_start_key:
            w_total += _day_total(daily, dk)
            w_cost += dd.get("cost", 0.0)
            w_sessions |= dd.get("sessions", set())
            w_msgs += dd.get("messages", 0)
    w_sess = len(w_sessions)

    # Model breakdown (sorted by tokens desc)
    if not include_other:
        models = {k: v for k, v in models.items() if _is_anthropic(k)}
    models_sorted = sorted(models.items(), key=lambda x: x[1]["tokens"], reverse=True)
    total_model_tokens = sum(m["tokens"] for _, m in models_sorted)

    # Date format in local time (Windows vs Unix)
    local_now = datetime.now()
    try:
        updated = local_now.strftime("%b %-d, %H:%M")
    except ValueError:
        updated = local_now.strftime("%b %#d, %H:%M")

    mv = {
        "sub": sub_type,
        "tier": tier,
        "active": active,
        # Reporting hosts out of configured ones, blank on a single machine.
        # Without it, a host that stops reporting just looks like a quiet day.
        "fleet": f"{fresh}/{hosts_total}" if hosts_total > 1 else "",
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
        # Activity, not spend: these count a day's work whoever served it,
        # unless --strict-activity says otherwise. The trend arrow above is
        # deliberately not in this group, since it describes the Today total
        # sitting beside it and has to count the same population.
        "spark": _sparkline(daily, with_other=not strict_activity),
        "streak": _streak(daily, with_other=not strict_activity),
        # Top project
        "top_project": max(projects, key=lambda k: projects[k]["tokens"]) if projects else "—",
        # Tokens today from models left out of everything above, so the work is
        # reportable rather than merely missing. Blank when there were none, or
        # when --include-other-models folded them into the figures instead. No
        # shipped template renders these.
        "o_tokens": fmt_tokens(t_other),
        "o_messages": t_other_msgs,
        # Usage limits
        "u_session": usage_limits.get("session", {}).get("pct", "—"),
        "u_week": usage_limits.get("week_all", {}).get("pct", "—"),
        # Third limit row: present only on plans that cap one model separately,
        # and the model varies -- u_model carries whichever one the panel named.
        "u_sonnet": usage_limits.get("week_model", {}).get("pct", "—"),
        "u_model": usage_limits.get("week_model", {}).get("name", "Model"),
        "u_reset": usage_limits.get("session", {}).get("resets", ""),
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
    import subprocess
    uuid = os.environ.get("TRMNL_PLUGIN_UUID")
    if not uuid:
        print("Error: TRMNL_PLUGIN_UUID environment variable not set", file=sys.stderr)
        sys.exit(1)

    url = f"https://usetrmnl.com/api/custom_plugins/{uuid}"
    payload = json.dumps({"merge_variables": merge_variables})

    r = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        capture_output=True, timeout=30,
    )
    stdout = r.stdout.decode("utf-8", errors="replace").strip()
    lines = stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else ""
    code = lines[-1]
    if code.startswith("2"):
        print(f"OK ({code}): {body}")
    else:
        print(f"HTTP {code}: {body}", file=sys.stderr)
        sys.exit(1)


# ── Test data ────────────────────────────────────────────────────────

def _test_payload():
    """Sample payload with multi-model data for previewing the layout."""
    return {
        "sub": "Max",
        "tier": "20x",
        "active": 2,
        "fleet": "2/2",
        "t_input": "1.2M",
        "t_output": "245K",
        "t_cache_r": "3.4M",
        "t_cache_w": "890K",
        "t_total": "5.7M",
        "t_cost": "$18.50",
        "t_sessions": 4,
        "t_messages": 142,
        "trend": "up",
        "w_tokens": "28.3M",
        "w_cost": "$67.89",
        "w_sessions": 12,
        "w_messages": 847,
        "spark": "\u2581\u2583\u2585\u2587\u2584\u2586\u2588\u2585",
        "streak": 7,
        "top_project": "trmnl-claude",
        "o_tokens": "0",
        "o_messages": 0,
        "updated": "Apr 4, 11:22",
        "m1_name": "Opus",
        "m1_tokens": "18.2M",
        "m1_pct": 64,
        "m1_cost": "$312",
        "m2_name": "Sonnet",
        "m2_tokens": "8.1M",
        "m2_pct": 29,
        "m2_cost": "$24.30",
        "m3_name": "Haiku",
        "m3_tokens": "2.0M",
        "m3_pct": 7,
        "m3_cost": "$1.60",
        "u_session": 42,
        "u_week": 18,
        "u_sonnet": 5,
        "u_model": "Fable",
        "u_reset": "in 3h",
    }


# ── Debounce ─────────────────────────────────────────────────────────

def _debounce_path():
    return _find_claude_dir() / ".trmnl_last_push"


def _should_run(minutes):
    """Return True if enough time has passed since last push."""
    ts_file = _debounce_path()
    if not ts_file.exists():
        return True
    try:
        last = float(ts_file.read_text("utf-8").strip())
        return (datetime.now(timezone.utc).timestamp() - last) >= minutes * 60
    except Exception:
        return True


def _mark_pushed():
    """Record the current time as last push."""
    try:
        _debounce_path().write_text(str(datetime.now(timezone.utc).timestamp()), encoding="utf-8")
    except Exception:
        pass


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    # Windows consoles default to cp1252, which can't encode the block
    # characters echoed back in TRMNL's response (sparkline/usage bars).
    # Without this, the success print() raises UnicodeEncodeError *before*
    # _mark_pushed() runs, so the debounce timestamp never updates and every
    # Notification fires an un-throttled post until TRMNL rate-limits us.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    import argparse
    parser = argparse.ArgumentParser(
        description="Claude Code usage dashboard for TRMNL e-ink displays")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print payload JSON without posting to TRMNL")
    parser.add_argument("--test", action="store_true",
                        help="Use sample multi-model data for layout preview")
    parser.add_argument("--no-scrape", action="store_true",
                        help="Don't read the usage limits on this host (a fleet "
                             "host still shows ones cached in its store)")
    parser.add_argument("--usage-method", choices=["auto", "headers", "pty"],
                        default="auto",
                        help="How to read usage limits: 'headers' asks the API "
                             "(~0.5s, costs one token), 'pty' drives the /usage "
                             "TUI (~20s), 'auto' tries headers then falls back")
    parser.add_argument("--model-limit-ttl", type=int, metavar="MIN", default=None,
                        help="How stale the per-model weekly row may get before "
                             "'auto' re-scrapes it (default: 60 min, 15 when the "
                             "limit is above 80%%)")
    parser.add_argument("--debounce", type=int, metavar="MIN", default=0,
                        help="Skip if last push was less than MIN minutes ago")
    parser.add_argument("--fleet-config", metavar="PATH", default=None,
                        help="Fleet config file (default: $TRMNL_FLEET_CONFIG, "
                             "else fleet.json beside this script)")
    # Mutually exclusive, because --strict-activity cannot be honoured once
    # --include-other-models has folded the gateway's tokens into the totals:
    # nothing records which of them were which any more, so the streak would
    # quietly stay wide. Refusing both is better than ignoring one.
    others_group = parser.add_mutually_exclusive_group()
    others_group.add_argument("--include-other-models", action="store_true",
                              help="Count models that aren't Anthropic's in the "
                                   "totals, cost and model breakdown. They are "
                                   "left out by default: no entry in the price "
                                   "table, and no bearing on the usage limits "
                                   "shown beside them")
    others_group.add_argument("--strict-activity", action="store_true",
                              help="Count only Anthropic models in the streak "
                                   "and sparkline. By default those two count a "
                                   "day's work whoever served it, since they say "
                                   "whether you worked rather than what it cost")
    parser.add_argument("--no-fleet", action="store_true",
                        help="Ignore the fleet config and post this host alone")
    parser.add_argument("--emit-store", action="store_true",
                        help="Refresh this host's entry, print the fleet store "
                             "as JSON and exit (what the posting host runs over "
                             "ssh to collect; prints nothing else)")
    parser.add_argument("--ingest-store", action="store_true",
                        help="Merge a fleet store read from stdin into the local "
                             "one and exit (how the posting host pushes the "
                             "merged store back out)")
    args = parser.parse_args()

    # Fleet plumbing. Neither posts, and --emit-store skips the usage limits
    # entirely: the master reads those itself, and scraping them here would
    # put a 20s PTY on the far end of every collection.
    if args.emit_store:
        cd = _find_claude_dir()
        since = (datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7))
        store = _load_store()
        me = _this_host()
        _ensure_member(store, me, datetime.now(timezone.utc).timestamp())
        store["hosts"][me] = _aggregate_local(cd, since, args.include_other_models)
        _save_store(store)
        print(json.dumps(store))
        return
    if args.ingest_store:
        try:
            incoming = json.loads(sys.stdin.read())
        except ValueError:
            sys.exit(1)
        _save_store(_merge_stores(_load_store(), incoming))
        return

    if args.debounce and not _should_run(args.debounce):
        sys.exit(0)

    if args.test:
        payload = _test_payload()
        post_and_exit(payload, args)
        return

    cfg = None if args.no_fleet else _load_fleet_config(args.fleet_config)
    if cfg is None:
        payload = build_payload(
            usage_method="off" if args.no_scrape else args.usage_method,
            model_ttl_min=args.model_limit_ttl,
            include_other=args.include_other_models,
            strict_activity=args.strict_activity)
        post_and_exit(payload, args)
        return

    _run_fleet(args, cfg)


def _run_fleet(args, cfg):
    me = _this_host()
    is_master = me == cfg["master"]
    others = [h for h in cfg["hosts"] if h["name"] != me]
    now = datetime.now(timezone.utc).timestamp()

    # Copying the example config without editing it leaves every name wrong,
    # and the symptom is a host counted as absent forever. Say so out loud.
    if len(others) == len(cfg["hosts"]):
        print(f"Warning: this host is '{me}', which the fleet config doesn't "
              f"list. Names must match `hostname -s`.", file=sys.stderr)

    cd = _find_claude_dir()
    since = (datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7))

    # Own entry is always a fresh scan; whatever the store held for this host
    # is superseded, which is what keeps a takeover from counting us twice.
    store = _load_store()
    _ensure_member(store, me, now)
    store["hosts"][me] = _aggregate_local(cd, since, args.include_other_models)

    def posted_ago(host):
        try:
            return now - float(store["last_post_by"].get(host, 0) or 0)
        except (TypeError, ValueError):
            return float("inf")

    def master_quiet_for():
        # Measured against the master's own last post, not the last post by
        # anyone. A successor that took over would otherwise reset the very
        # clock it checks, stand down against itself on its next run, and leave
        # the display untouched for a whole takeover window at a time.
        return posted_ago(cfg["master"])

    def another_host_posted():
        # Any other host, not only ones ranked ahead. Once someone is driving
        # the display the queue has done its job, and handing back to a
        # returning senior successor would buy nothing but a window where both
        # of them post.
        return min((posted_ago(h["name"]) for h in others), default=float("inf"))

    if not is_master:
        rank = _successor_rank(cfg, store, me)
        wait_min = (cfg["takeover_after_min"]
                    + rank * cfg["successor_stagger_min"])
        # Stand down if the master is posting for all of us, if a successor
        # ahead in the queue gets first refusal, or if this host already knows
        # another one is driving. That last case is worth catching here rather
        # than after collecting: during an outage every idle successor would
        # otherwise ssh to every host on every run, stalling on the master's
        # dead port each time, only to learn what its own store already said.
        if (master_quiet_for() <= wait_min * 60
                or another_host_posted() <= cfg["takeover_after_min"] * 60):
            # Keep the fresh scan so whoever does post has something current to
            # collect, and stay off the display.
            _save_store(store)
            return

    # Whoever is about to post collects first, so the display carries the
    # freshest numbers every reachable host can give, not the poster's own
    # beside a set of cached ones.
    for h in others:
        got = _pull_store(h, args.include_other_models)
        if got:
            _merge_stores(store, got)

    # Collecting can reveal that the master, or another successor, has posted
    # in the meantime. Yield instead of posting over them. This repeats the
    # check above against fresher information, and catches the case the local
    # store can't: a driver whose push never reached this host.
    if not is_master and min(master_quiet_for(),
                             another_host_posted()) <= cfg["takeover_after_min"] * 60:
        _save_store(store)
        return

    # Reading the limits needs the OAuth token from ~/.claude/.credentials.json,
    # which macOS doesn't have: it keeps them in the Keychain. Rather than let a
    # Mac's takeover blank the three bars, cache whatever the last host that
    # could read them saw, and reuse it while it's worth showing. Past that the
    # bars go blank on purpose, because a percentage that outlived its reset is
    # worse than no percentage.
    limits = ({} if args.no_scrape
              else _read_usage(args.usage_method, args.model_limit_ttl))
    if limits:
        store["limits"] = {"ts": now, "data": limits}
    else:
        cached = store.get("limits") or {}
        if now - float(cached.get("ts", 0) or 0) < cfg["stale_after_min"] * 60:
            limits = cached.get("data") or {}

    # Configured hosts only. A machine dropped from the config still has an
    # entry in everyone's store, and counting it would keep its tokens in the
    # weekly total indefinitely and push the marker past the host count.
    counted = {n: a for n, a in store["hosts"].items()
               if any(h["name"] == n for h in cfg["hosts"])}

    # Summing hosts that counted different populations gives a total that means
    # nothing in particular, and nothing on the display would show it.
    odd = sorted(n for n, a in counted.items()
                 if bool(a.get("include_other")) != bool(args.include_other_models))
    if odd:
        print(f"Warning: {', '.join(odd)} scanned with a different "
              f"--include-other-models setting than this host, so the totals "
              f"mix models that aren't Anthropic's with ones that exclude them.",
              file=sys.stderr)

    payload = build_payload(
        usage_method="off" if args.no_scrape else args.usage_method,
        model_ttl_min=args.model_limit_ttl,
        aggregates=list(counted.values()),
        stale_cut=now - cfg["stale_after_min"] * 60,
        hosts_total=len(cfg["hosts"]),
        usage_limits=limits,
        include_other=args.include_other_models,
        strict_activity=args.strict_activity)

    if args.dry_run:
        print(json.dumps(payload, indent=2, default=str))
        return

    post_to_trmnl(payload)
    _mark_pushed()
    store["last_post"] = {"ts": now, "by": me}
    store["last_post_by"][me] = now
    _save_store(store)

    # The heartbeat rides along, so pushing is also how secondaries learn the
    # master is alive. A secondary that took over pushes to whoever it can
    # reach, which is usually nobody until the master is back.
    for h in others:
        _push_store(h, store)


def post_and_exit(payload, args):
    if args.dry_run:
        print(json.dumps(payload, indent=2, default=str))
    else:
        post_to_trmnl(payload)
        _mark_pushed()


if __name__ == "__main__":
    main()
