# claude-trmnl

Advanced Claude Code usage dashboard for [TRMNL](https://usetrmnl.com) e-ink displays.

![Claude Code Dashboard on TRMNL](screenshot.png)

Reads local Claude Code session data and pulls live usage limits from the API's rate-limit headers. Cross-platform (Windows, macOS, Linux). Stdlib only. The fallback usage scraper needs `pywinpty` (Windows) or `pexpect` (Unix).

## What it shows

| Metric | Description |
|--------|-------------|
| **Subscription** | Plan type (Pro/Max) and rate limit tier (5x/20x) |
| **Usage limits** | Session %, weekly % and any per-model cap, with progress bars and reset time |
| **Active sessions** | Currently running Claude Code instances |
| **Today's tokens** | Input, output, cache read, cache write breakdown |
| **API-equivalent cost** | What today's usage would cost at API prices |
| **Session & message counts** | How many sessions and messages today |
| **Model breakdown** | Per-model usage with percentage bars (7-day) |
| **Weekly totals** | Tokens, cost, sessions for the current week |
| **7-day sparkline** | Visual activity trend |
| **Usage streak** | Consecutive days of Claude Code usage |
| **Top project** | Most active project by token usage |
| **Trend indicator** | Up/down/flat vs yesterday |

## How it works

```
~/.claude/
  .credentials.json   -->  subscription type + tier
  sessions/*.json     -->  active session count
  projects/**/*.jsonl  -->  token usage per message

api.anthropic.com     -->  session/weekly usage % + reset times
  (rate-limit headers)      (falls back to the /usage TUI over a PTY)
        |
   claude_trmnl.py
        |
   POST merge_variables
        |
   trmnl.com/api/custom_plugins/{UUID}
        |
   TRMNL renders Liquid template to PNG
        |
   e-ink display pulls image on next wake
```

## Setup

### 1. Install dependencies (optional)

Nothing to install for the default setup. The usage limits come from rate-limit headers the API returns on any request, which the stdlib can read on its own.

The fallback scraper reads the same numbers out of the `/usage` TUI and does need a PTY library:

```bash
# Windows
pip install pywinpty

# macOS/Linux
pip install pexpect
```

Use `--no-scrape` to leave the usage limits out entirely.

### 2. Configure

Set your Plugin UUID. Pick **one** of these approaches:

**Option A: Environment variable (recommended)**

Set `TRMNL_PLUGIN_UUID` as a system environment variable:
- **Windows**: Settings > System > About > Advanced system settings > Environment Variables > New
- **macOS/Linux**: add `export TRMNL_PLUGIN_UUID="your-uuid"` to `~/.bashrc` or `~/.zshrc`

**Option B: Wrapper script**

Create a `run.bat` (Windows) or `run.sh` (macOS/Linux) in the project folder:

```bat
@rem run.bat (Windows)
set TRMNL_PLUGIN_UUID=your-uuid-here
py claude_trmnl.py --no-scrape
```

```bash
# run.sh (macOS/Linux)
export TRMNL_PLUGIN_UUID="your-uuid-here"
python claude_trmnl.py
```

### 3. Run

```bash
# Test locally (prints JSON, does not post)
python claude_trmnl.py --dry-run

# Post to TRMNL
python claude_trmnl.py

# Post without the usage limits
python claude_trmnl.py --no-scrape

# Force one method or the other (see "Reading the usage limits" below)
python claude_trmnl.py --usage-method pty

# Preview with sample multi-model data
python claude_trmnl.py --test
```

### How the token numbers are counted

Claude Code writes one JSONL line per content block, and every line repeats the whole message's usage. A reply with thinking, text and a tool call therefore lands three times. Resumed and forked sessions copy their history into a new file on top of that. Counting lines would multiply every figure on the display, so the scan folds entries by message and request id first.

Days are local, not UTC, so "today" ends when your clock says it does.

Costs are what the same tokens would have cost at API list prices. A subscription doesn't charge per token, so treat the figure as a comparison rather than a bill. Cache writes cost 1.25x input at the 5-minute TTL and 2x at the 1-hour one, and each message records which it used. Models the script doesn't recognize, one proxied through a gateway for instance, fall back to Sonnet rates.

### Reading the usage limits

The session and weekly percentages come from `anthropic-ratelimit-unified-*` response headers, which every API response carries. Getting them means one `max_tokens: 1` request, which takes about half a second and spawns no `claude` process.

`--usage-method` picks how to read them:

| Value | What it does |
|-------|--------------|
| `auto` (default) | Headers every run, plus a cached per-model row (see below) |
| `headers` | Headers only, no PTY at all |
| `pty` | Drives the `/usage` TUI over a PTY, as earlier versions did |

No header reports the per-model weekly row. Plans that cap one model separately show a third bar in `/usage`, labelled with whichever model that is (Fable at the time of writing, Sonnet before it). That is often the limit you actually run into first, so `auto` reads session and week from the headers on every run and refreshes just that one row from the TUI on a slower schedule, caching it in between.

The refresh interval backs off when there's nothing to watch and tightens when there is: every 60 minutes normally, every 15 once the row is above 80%. `--model-limit-ttl MIN` overrides both. A cached row older than 6 hours is dropped rather than shown, because a weekly limit resets and a stale 95% after a reset is worse than a blank.

So a typical run costs half a second, and roughly one run an hour costs 20 seconds. `--usage-method headers` skips the PTY entirely if you would rather not pay that.

Two other things to know about the header method:

- **It spends a token to measure tokens.** One per run, which also nudges the number it reports by a rounding error's worth.
- **The headers are undocumented**, so they may change. That's the other reason the PTY scraper is still here rather than deleted.

Anything that spawns `claude` on a short interval should also set `DISABLE_AUTOUPDATER=1`, which the PTY path now does for its own spawns. A scraper session lives about 25 seconds, which isn't long enough for the auto-updater to finish downloading a release. On a 5-minute schedule it restarts that download every run and leaves a truncated binary in `~/.cache/claude/staging` each time. Those only get cleaned up after an update that succeeds, so they accumulate. Interactive sessions and `claude update` are unaffected.

### 4. Schedule

Run every 5-10 minutes to keep your display updated.

**Windows (Task Scheduler):**

If using a wrapper script:
```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\claude-trmnl\run.bat" `
  -WorkingDirectory "C:\path\to\claude-trmnl"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "claude-trmnl" `
  -Action $action -Trigger $trigger -Settings $settings
```

If using a system environment variable:
```powershell
$action = New-ScheduledTaskAction -Execute "python" `
  -Argument "claude_trmnl.py" `
  -WorkingDirectory "C:\path\to\claude-trmnl"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "claude-trmnl" `
  -Action $action -Trigger $trigger -Settings $settings
```

**Linux/macOS (cron):**
```bash
# crontab -e
*/5 * * * * cd /path/to/claude-trmnl && ./run.sh
```

**macOS (launchd):**
```xml
<!-- ~/Library/LaunchAgents/com.claude-trmnl.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-trmnl</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd /path/to/claude-trmnl &amp;&amp; ./run.sh</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

### 5. Live updates with Claude Code hooks (optional)

You can have Claude Code automatically push dashboard updates while you're actively using it. Add a `Stop` hook to `~/.claude/settings.json` (fires after each response, so the dashboard stays fresh during active work):

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "cmd /c start /min /b py C:\\path\\to\\claude-trmnl\\claude_trmnl.py --debounce 5"
          }
        ]
      }
    ]
  }
}
```

On macOS/Linux:
```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/claude-trmnl/claude_trmnl.py --debounce 5 &"
          }
        ]
      }
    ]
  }
}
```

> Tip: A `Stop` hook runs after every response. The `--debounce 5` check runs first and exits instantly on throttled invocations (before any scraping), so the extra calls are essentially free. A `Notification` hook also works but mostly fires only after ~60s idle, so the dashboard won't refresh during continuous back-and-forth work.

The `--debounce 5` flag ensures it only pushes once every 5 minutes (respecting TRMNL's 12/hour rate limit). The command runs in the background so it doesn't slow down your responses.

Restart Claude Code after editing `settings.json` for the hook to take effect.

## License

GPL-3.0 -- see [LICENSE](LICENSE)
