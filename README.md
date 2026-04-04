# claude-trmnl

Advanced Claude Code usage dashboard for [TRMNL](https://usetrmnl.com) e-ink displays.

Reads your local Claude Code session data directly -- no screen scraping, no API keys, zero dependencies beyond Python stdlib.

![full view](https://img.shields.io/badge/view-full%20%7C%20half%20%7C%20quadrant-black)

## What it shows

| Metric | Description |
|--------|-------------|
| **Subscription** | Plan type (Pro/Max) and rate limit tier (5x/20x) |
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
  .credentials.json  -->  subscription type + tier
  sessions/*.json    -->  active session count
  projects/**/*.jsonl -->  token usage per message
                              |
                     claude_trmnl.py
                              |
                     POST merge_variables
                              |
                     usetrmnl.com/api/custom_plugins/{UUID}
                              |
                     TRMNL renders Liquid template to PNG
                              |
                     e-ink display pulls image on next wake
```

## Setup

### 1. Create the TRMNL plugin

1. Go to your [TRMNL dashboard](https://usetrmnl.com) > **Plugins** > **Private Plugin**
2. Name it "Claude Code"
3. Set strategy to **Webhook**
4. Copy the **Plugin UUID** from the settings
5. Paste one of the templates from `templates/` into the markup editor:
   - `full.html` -- full screen (800x480)
   - `half_horizontal.html` -- top/bottom half (800x240)
   - `half_vertical.html` -- left/right half (400x480)
   - `quadrant.html` -- quarter screen (400x240)

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set your TRMNL_PLUGIN_UUID
```

### 3. Run

```bash
# Test locally (prints JSON, does not post)
python claude_trmnl.py --dry-run

# Post to TRMNL
source .env  # or: export TRMNL_PLUGIN_UUID=...
python claude_trmnl.py
```

### 4. Schedule

Run every 5-10 minutes to keep your display updated.

**Linux/macOS (cron):**
```bash
# crontab -e
*/5 * * * * cd /path/to/claude-trmnl && source .env && python claude_trmnl.py
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
    <string>cd /path/to/claude-trmnl &amp;&amp; source .env &amp;&amp; python claude_trmnl.py</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
```

**Windows (Task Scheduler):**
```powershell
# Run every 5 minutes
$action = New-ScheduledTaskAction -Execute "python" `
  -Argument "claude_trmnl.py" `
  -WorkingDirectory "C:\path\to\claude-trmnl"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "claude-trmnl" `
  -Action $action -Trigger $trigger -Settings $settings
```

Set `TRMNL_PLUGIN_UUID` as a system environment variable, or wrap the call in a script that loads `.env`.

## Comparison with claude-usage-trmnl

| Feature | [claude-usage-trmnl](https://github.com/carledwards/claude-usage-trmnl) | claude-trmnl |
|---------|------|---|
| Data source | Screen-scrapes `/usage` TUI via pexpect | Reads local JSONL files directly |
| Dependencies | pexpect, pyte | None (stdlib only) |
| Platform | macOS only | Windows, macOS, Linux |
| Metrics | 3 percentage bars | 15+ metrics |
| Token breakdown | No | Input/output/cache read/cache write |
| Cost tracking | No | API-equivalent cost |
| Model breakdown | No | Per-model % with progress bars |
| Sparkline | No | 7-day activity trend |
| Streak counter | No | Consecutive usage days |
| Active sessions | No | Live session count |
| Subscription info | No | Plan type + tier |

## License

MIT
