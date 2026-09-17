# claude-trmnl

Claude Code usage dashboard for [TRMNL](https://usetrmnl.com) e-ink displays.

![Claude Code Dashboard on TRMNL](screenshot.png)

Reads local Claude Code session data and pulls live usage limits from the API's rate-limit headers. Several machines can share one display and have their numbers added up. Cross-platform (Windows, macOS, Linux). One file, stdlib only. The fallback usage scraper needs `pywinpty` (Windows) or `pexpect` (Unix).

## What it shows

| Metric | Description |
|--------|-------------|
| **Subscription** | Plan type (Pro/Max) and rate limit tier (5x/20x) |
| **Usage limits** | Session %, weekly % and any per-model cap, with progress bars and reset time |
| **Active sessions** | Currently running Claude Code instances |
| **Fleet marker** | Hosts reporting out of hosts configured, `3/3`. Blank on a single machine |
| **Today's tokens** | Input, output, cache read, cache write breakdown |
| **API-equivalent cost** | What today's usage would cost at API prices |
| **Session & message counts** | How many sessions and messages today |
| **Model breakdown** | Per-model usage with percentage bars (last 7 days plus today) |
| **Weekly totals** | Tokens, cost, sessions for the current week, Monday to now |
| **7-day sparkline** | Activity trend, one block per day, any model |
| **Usage streak** | Consecutive days of Claude Code usage, any model |
| **Trend indicator** | Up/down/flat vs yesterday |

The payload also carries `top_project` (most active project by tokens), a per-model cost `m1_cost`, `m2_cost`, `m3_cost`, and `o_tokens` / `o_messages` (today's usage from models that aren't Anthropic's, see below). None of the four shipped templates render those. They are there for a template of your own.

## How it works

```
~/.claude/
  .credentials.json   -->  subscription type + tier
  sessions/*.json     -->  active session count
  projects/**/*.jsonl  -->  token usage per message

api.anthropic.com     -->  session/weekly usage % + reset times
  (rate-limit headers)      (falls back to the /usage TUI over a PTY)
        |
   claude_trmnl.py  <--ssh-->  other machines, if fleet.json says so
        |
   POST merge_variables
        |
   usetrmnl.com/api/custom_plugins/{UUID}
        |
   TRMNL renders Liquid template to PNG
        |
   e-ink display pulls image on next wake
```

The script never uploads a template. It posts numbers, and TRMNL renders them with whatever markup you pasted into the plugin.

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

### 2. Create the plugin in TRMNL

In the TRMNL dashboard, add a Private Plugin with the Webhook strategy. Its settings page shows the Plugin UUID, which the script needs. The markup editor has one tab per layout. Paste the matching file from `templates/` into each:

| Layout | File |
|--------|------|
| Full | `templates/full.html` |
| Half horizontal | `templates/half_horizontal.html` |
| Half vertical | `templates/half_vertical.html` |
| Quadrant | `templates/quadrant.html` |

Whenever a template in this repo changes, paste it again. Nothing syncs it for you.

### 3. Configure

Set your Plugin UUID. Pick **one** of these approaches.

**Option A: environment variable**

Set `TRMNL_PLUGIN_UUID` as a system environment variable:
- **Windows**: Settings > System > About > Advanced system settings > Environment Variables > New
- **macOS/Linux**: add `export TRMNL_PLUGIN_UUID="your-uuid"` to `~/.bashrc` or `~/.zshrc`

**Option B: wrapper script (recommended for a fleet)**

Create a `run.bat` (Windows) or `run.sh` (macOS/Linux) in the project folder:

```bat
@rem run.bat (Windows)
set TRMNL_PLUGIN_UUID=your-uuid-here
py claude_trmnl.py --no-scrape
```

```bash
# run.sh (macOS/Linux)
export TRMNL_PLUGIN_UUID="your-uuid-here"
python claude_trmnl.py "$@"
```

Or copy `.env.example` to `.env`, fill in the UUID, and have `run.sh` source it:

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"   # cron gets a bare PATH
set -a; . "$DIR/.env"; set +a
exec python3 "$DIR/claude_trmnl.py" "$@"
```

The `"$@"` matters once you have a fleet. Other hosts run this script over ssh with `--emit-store` and `--ingest-store` appended, and the wrapper has to pass them through. `.env` and `fleet.json` are gitignored, so a `git pull` won't clobber them.

### 4. Run

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

# Post this machine's stats alone, ignoring fleet.json
python claude_trmnl.py --no-fleet
```

The full flag list is under "Command-line reference" below.

### How the token numbers are counted

Claude Code writes one JSONL line per content block, and every line repeats the whole message's usage. A reply with thinking, text and a tool call therefore lands three times. Resumed and forked sessions copy their history into a new file on top of that. Counting lines would multiply every figure on the display, so the scan folds entries by message and request id first.

Days are local, not UTC, so "today" ends when your clock says it does.

Costs are what the same tokens would have cost at API list prices. A subscription doesn't charge per token, so treat the figure as a comparison rather than a bill. Cache writes cost 1.25x input at the 5-minute TTL and 2x at the 1-hour one, and each message records which it used.

### Models that aren't Anthropic's

If you route another vendor's model through Claude Code, via a gateway that speaks the Anthropic API, its transcripts land in the same JSONL files. By default the script leaves those entries out of every figure that Anthropic prices or rate-limits: no tokens, no cost, no message or session count, no model or project row, and the trend arrow follows the Today total it sits beside. Two widgets count the other way. The streak and the sparkline answer "was there work that day", and a day spent entirely on a gateway model was still a day of work, so those two count every model. The display therefore shows two populations on purpose. Anything with a dollar figure or a limit bar next to it is Anthropic only; the two activity widgets are everything.

The reason is that two of the numbers on the display only make sense for Anthropic models. The cost comes off Anthropic's price list, and the session, week and per-model bars come from Anthropic's rate-limit headers. A GPT or Qwen reply is priced by neither and counted against neither. Folding its tokens in invents a dollar figure for it and leaves the totals describing a different population than the bars sitting next to them. On the fleet this was written for, that had put $264 of a $724 week on the display for models that cost nothing against the subscription, and the model slots read Gpt, Opus, Qwen3.8 while the per-model bar beside them warned Fable was at 95%.

Detection is by name. A model id naming one of the families `fable`, `opus`, `sonnet` or `haiku` counts, whatever else the id says. Failing that, an id naming a known other vendor (`gpt`, `qwen`, `llama`, `gemini`, `mistral`, `deepseek`, `grok`, `kimi`, `glm`, `command-r`, `phi`) is excluded. Anything else that starts with `claude` counts, priced at Sonnet 4.6 rates, so an Anthropic family newer than this file shows up under an unfamiliar name rather than vanishing. The `claude-` prefix alone proves nothing, because a gateway can hand back ids like `claude-gpt-6-astra`.

That leaves one gap. A new Anthropic family whose id does not start with `claude` and does not contain one of the four family names would be dropped until someone adds it to `_ANTHROPIC_FAMILIES`. A model from a vendor missing from the list, and not prefixed `claude`, is also dropped, which is the right outcome by accident.

`--strict-activity` narrows the streak and sparkline to Anthropic models too, so every number on the display describes one population. `--include-other-models` restores the old behaviour, counting everything at Sonnet 4.6 rates where the price table has no entry. If your gateway bills you per token and you want the display to reflect that spend, or you want the totals to mean "everything Claude Code did today" regardless of the bars, use it. In a fleet, each host filters when it scans, and the poster passes its own setting along when it collects, so hosts it can reach count the same population. A host it couldn't reach, or one that took over on its own timer with different flags, can still disagree. The poster then prints a warning to stderr naming that host rather than summing the mixture in silence. Passing the flag in every host's timer entry keeps that from happening.

What was excluded is tallied rather than discarded. Two merge variables, `o_tokens` and `o_messages`, carry today's excluded tokens and message count, blank when there were none. No shipped template renders them, but a template of your own can.

If you upgrade with a gateway in use, expect the numbers to move the first time the new version posts, in both directions. Week totals, cost, message counts and the model breakdown shrink to Anthropic-only usage. The streak and the sparkline can go up, because a day that only had gateway traffic now counts as a day worked and draws a block. That is the fix working, not data loss.

### Reading the usage limits

The session and weekly percentages come from `anthropic-ratelimit-unified-*` response headers, which every API response carries. Getting them means one `max_tokens: 1` request, which takes about half a second and spawns no `claude` process.

`--usage-method` picks how to read them:

| Value | What it does |
|-------|--------------|
| `auto` (default) | Headers every run, plus a cached per-model row (see below). Falls back to the PTY when the headers return nothing |
| `headers` | Headers only, no PTY at all |
| `pty` | Drives the `/usage` TUI over a PTY, as earlier versions did |

No header reports the per-model weekly row. Plans that cap one model separately show a third bar in `/usage`, labelled with whichever model that is (Fable at the time of writing, Sonnet before it). That is often the limit you actually run into first, so `auto` reads session and week from the headers on every run and refreshes that one row from the TUI on a slower schedule, caching it in `~/.claude/.trmnl_model_limit` in between.

The refresh interval backs off when there's nothing to watch and tightens when there is: every 60 minutes normally, every 15 once the row is above 80%. `--model-limit-ttl MIN` overrides both. A cached row older than 6 hours is dropped rather than shown, because a weekly limit resets and a stale 95% after a reset is worse than a blank.

So a typical run costs half a second, and roughly one run an hour costs 20 seconds. `--usage-method headers` skips the PTY entirely if you would rather not pay that.

Two other things to know about the header method:

- **It spends a token to measure tokens.** One per run, which also nudges the number it reports by a rounding error's worth.
- **The headers are undocumented**, so they may change. That's the other reason the PTY scraper is still here rather than deleted.

The header method reads the OAuth token from `~/.claude/.credentials.json`. macOS keeps that token in the Keychain instead, so on a Mac the headers return nothing and `auto` falls back to the PTY on every run. That is a 20-second `claude` session each time. A Mac that should stay cheap wants `--usage-method headers` (blank bars) or, in a fleet, can borrow the numbers from another host (see below).

Anything that spawns `claude` on a short interval should also set `DISABLE_AUTOUPDATER=1`, which the PTY path does for its own spawns. A scraper session lives about 25 seconds, which isn't long enough for the auto-updater to finish downloading a release. On a 5-minute schedule it restarts that download every run and leaves a truncated binary in `~/.cache/claude/staging` each time. Those only get cleaned up after an update that succeeds, so they accumulate. Interactive sessions and `claude update` are unaffected.

### 5. Schedule

Run every 5-10 minutes to keep your display updated. TRMNL accepts 12 webhook posts an hour per plugin.

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

### 6. Live updates with Claude Code hooks (optional)

You can have Claude Code push dashboard updates while you're using it. Add a `Stop` hook to `~/.claude/settings.json`. It fires after each response, so the dashboard stays fresh during active work:

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

`--debounce 5` reads `~/.claude/.trmnl_last_push` before doing anything else and exits if the last post was under 5 minutes ago, so the throttled invocations cost nothing and the 12-an-hour limit holds. The trailing `&` keeps the hook from delaying your next prompt. A `Notification` hook also works but mostly fires after about 60s idle, so the dashboard won't refresh during continuous back-and-forth work.

Restart Claude Code after editing `settings.json` for the hook to take effect.

## Sharing one display between machines

TRMNL keeps the last payload it received. Two machines each posting their own numbers means the display shows whichever arrived most recently, flipping between halves of the picture instead of adding them together. A fleet fixes that by letting one machine post the sum.

### Roles

One host is the master. Every run it scans its own `~/.claude`, pulls each other host's numbers over ssh, merges them, reads the usage limits, posts, and pushes the merged result back out to every host it can reach. The others are secondaries. Their runs scan themselves, save the result locally, check whether the master is still posting, and stop. No ssh, no usage read, no post.

Hosts trade per-day, per-model and per-project aggregates rather than finished figures. A streak, a sparkline or a weekly total can't be rebuilt from two rendered numbers. Counts add, because each host scans transcripts nobody else has. Session ids union rather than add, so a session that spans two days still counts once in the week.

Two things the master run needs that a secondary run doesn't: the plugin UUID, and the ability to read the usage limits. Since any host may end up posting during an outage, give every host the UUID.

### Setting up a second machine

Assume `workstation` will be the master and `laptop` the secondary. Both already run the script on their own timer from steps 1 to 5 above.

1. On `workstation`, make sure `ssh laptop.local` works without a password or a passphrase prompt. The script runs ssh with `BatchMode=yes`, so anything interactive fails. `ssh-copy-id laptop.local` is the usual fix. A `Host` alias from `~/.ssh/config` works as the target too.

2. Check that the remote command runs. The default is `~/trmnl-claude/run.sh`, so the clone on `laptop` has to live there, or you set `cmd` in the config to whatever does run it:

   ```bash
   ssh -o BatchMode=yes laptop.local '~/trmnl-claude/run.sh --emit-store' | head -c 300
   ```

   That should print JSON starting with `{"hosts": {"laptop": ...`. If it prints nothing, the wrapper script isn't passing arguments through (see step 3 of Setup).

3. Copy `fleet.example.json` to `fleet.json` beside the script, on both machines, with identical contents:

   ```json
   {
     "master": "workstation",
     "hosts": [
       { "name": "workstation", "ssh": "workstation.local" },
       { "name": "laptop", "ssh": "laptop.local" }
     ]
   }
   ```

   `name` must match `hostname -s` on that machine, and the master must appear in `hosts` too. The timing keys (`takeover_after_min`, `stale_after_min`, `successor_stagger_min`) are optional and shown later. `--fleet-config PATH` or `$TRMNL_FLEET_CONFIG` point at a file somewhere else.

4. On `workstation`, run `python claude_trmnl.py --dry-run`. The output should show `"fleet": "2/2"`. A `1/2` means the pull from `laptop` failed. Run the ssh line from step 2 again and read stderr.

5. Run `python claude_trmnl.py` on `workstation` once for real, so its push writes a heartbeat into `laptop`'s store. A secondary with no heartbeat on file assumes the master has never posted and takes over, so skipping this means `laptop` posts its own numbers on every run until the master's first push arrives. Harmless, but confusing. From then on, a secondary that finds a healthy master prints nothing and posts nothing, which is correct and a little unnerving the first time. To see what it holds, run `python claude_trmnl.py --emit-store` on it. To see its own numbers alone, `--no-fleet --dry-run`.

If the config names a host that doesn't exist, that host shows as missing forever and the title bar reads `1/2`. If the config leaves out the host it runs on, the script prints `Warning: this host is 'x', which the fleet config doesn't list` to stderr and behaves as a last-in-line secondary. Both symptoms mean a `name` doesn't match `hostname -s`.

With two machines this is all the ssh you need. The secondary never has to reach the master, because the master's push writes the heartbeat into a file on the secondary, and that file is what the secondary reads to decide whether the master is alive.

### Adding a third

Add `mac-mini` to `hosts` in `fleet.json` on all three machines, and give the master an `ssh` target as well:

```json
{
  "master": "workstation",
  "hosts": [
    { "name": "workstation", "ssh": "workstation.local" },
    { "name": "laptop", "ssh": "laptop.local" },
    { "name": "mac-mini", "ssh": "mac-mini.local" }
  ]
}
```

Then set up ssh from every host to every other, all six directions, and test each one with the `--emit-store` line above. The master needs to reach the secondaries so it can collect. The secondaries need to reach each other, and the master, for a check that only matters during an outage: before a secondary posts in the master's place, it collects from whoever it can reach and looks at the heartbeat that comes back. If another host posted more recently than `takeover_after_min` ago, it stands down. Without that check, two successors that can't see each other both post, and you're back to a display that flips between two versions of the totals. They agree on the sum but not on which host's numbers are freshest.

A Windows host in a fleet needs an ssh server and a `cmd` value, since `~/trmnl-claude/run.sh` won't run there.

### The store and how it merges

Every host keeps `~/.claude/.trmnl_fleet.json` (or under `~/.config/claude` if that is where its Claude dir lives). It holds four things:

- `hosts`: the latest aggregate seen from each host, keyed by hostname, each stamped with the time it was scanned
- `members`: when each host was first seen, which sets the succession order
- `last_post`: when the display was last posted to, and by whom
- `last_post_by`: when each host last posted, keyed by hostname. This is the heartbeat the takeover checks read
- `limits`: the usage percentages, from the last host that could read them

Whenever a store arrives from another host, the two merge one key at a time. The newest aggregate per host wins, so a host's own fresh scan always beats a copy of it someone else cached. The earliest `joined` time per host wins, so deleting your store and letting it rebuild doesn't move you up the queue. The newest `last_post` wins, and so does the newest `last_post_by` entry per host. The newest `limits` win.

Because the store is keyed by host, a secondary that takes over replaces its own entry and leaves the others alone. That is what stops a takeover from counting a machine twice. When the master comes back, it pulls the stores, keeps the newest entry per host, and carries on with no special recovery step.

`--emit-store` prints a host's store after refreshing its own entry. `--ingest-store` reads a store from stdin and merges it in. Those two flags are the whole protocol, and they are what the master runs on the far end of ssh.

### Succession

Past two machines, "a secondary takes over" isn't enough. Every secondary would see the same stale heartbeat in the same minute and all of them would post, which is the flipping display again with extra steps. So secondaries queue, in the order they first appeared in the store. The first successor waits `takeover_after_min` of master silence. Each one behind it waits `successor_stagger_min` longer. With the defaults:

| Master silent for more than | Who posts |
|---|---|
| 45 min | first successor |
| 60 min | second, if the first didn't |
| 75 min | third, if neither did |

Join order comes from the store rather than the config, so reordering `hosts` changes nothing, and because the earliest sighting wins on merge, a machine can't jump the queue by rebuilding its store. To reset the order you'd have to delete the store on every host at once. Hosts that joined in the same second fall back to alphabetical order. A successor that is itself down never posts, the heartbeat keeps ageing, and the next one's turn arrives without anyone coordinating it.

The queue is measured against the master's own last post, so a successor that took over keeps posting on its normal timer for as long as the master stays quiet. Every other successor stands down as soon as its own store shows that some other host posted within the last `takeover_after_min`. The queue only decides who takes over. Once someone is driving, it keeps the display until the master returns, and a senior successor that comes back from its own outage yields to it rather than taking the display back. Forcing that handover would buy nothing except a window where both post.

The stand-down check runs twice on purpose. The first pass reads the local store, which the driver's pushes keep current, so an idle successor exits without an ssh call during an outage. The second pass repeats the same check after collecting, against fresher information, and catches the one case the local file can't: a driver whose push never reached this host. A driver that dies is replaced the same way the master was, once its last post is more than `takeover_after_min` old.

Keep `takeover_after_min` well above your timer interval, or a master that is merely slow gets overtaken.

### Failover and recovery

While the master is down, the first successor posts its own fresh scan next to the last figures it holds for everyone else, plus whatever it collected from the hosts it can reach. Hosts it can't reach keep contributing the numbers from their last cached entry. Once such an entry is older than `stale_after_min` (60 by default), that host stops counting as present in the fleet marker and its active-session count drops to zero. Its tokens still count toward today and the week, since those were real.

When the master returns, its next run pulls every store, takes the newest entry per host and the newest `last_post`, posts, and pushes the merged store back out. The successor's next run then sees a heartbeat younger than `takeover_after_min` and goes quiet. Nothing to clean up on either side.

The other direction works the same way. A secondary that was off for a day comes back, its timer runs, the master's next pull picks up its fresh scan, and the marker goes from `2/3` back to `3/3`.

Retiring a machine is a config edit. Remove it from `hosts` in `fleet.json` everywhere and it stops contributing on the next post. Only hosts the config lists go into the payload, so an entry left behind in the store neither holds its tokens in the weekly total nor pushes the marker past the host count.

### A Mac in the fleet

macOS keeps Claude's credentials in the Keychain rather than in `~/.claude/.credentials.json`. A Mac therefore can't read the plan name, and can't read the OAuth token the rate-limit headers need. It counts its own tokens normally and borrows the rest. The plan comes from whichever host most recently read one. The usage percentages ride along in the store's `limits` entry, cached by whoever last fetched them and dropped once they are older than `stale_after_min`. Past that the bars go blank on purpose, because a percentage that outlived its reset is worse than none.

Two consequences. A Mac master with `pexpect` installed pays the 20-second PTY scrape on every run, because `auto` falls back to it when the headers return nothing; give it `--usage-method headers` and it will use the cached store limits instead. And a fleet of nothing but Macs shows `Unknown` in the title bar, with usage bars only if one of them scrapes the TUI.

### The fleet marker

The title bar shows how many hosts reported out of how many the config lists, `3/3`. It is blank on a single machine. Watch it. A machine that quietly stops reporting looks exactly like a quiet day otherwise, which is the failure this whole arrangement exists to prevent.

The marker is a merge variable called `fleet`, and the four templates in `templates/` render it. If you pasted your templates before the fleet feature existed, paste them again from the repo or the marker won't show.

### A worked example

Three machines. `workstation` is the master and runs cron every 5 minutes. `laptop` was the first secondary added and `mac-mini` the second, so the store's `members` puts `laptop` first in line. All three hold the plugin UUID and can ssh to each other.

A normal minute: `workstation` scans itself, runs `run.sh --emit-store` on `laptop` and `mac-mini` over ssh, merges the three scans, reads the usage limits from the headers, posts, and pushes the merged store to both. `laptop` and `mac-mini` run their own timers, scan, see a heartbeat a few minutes old, and stop. The title bar reads `3/3`.

At 10:00 `workstation` loses power. Its last post was 09:58. Every 5 minutes `laptop` scans, finds the heartbeat aged 7, 12, 17 minutes and stops. At 10:45 the heartbeat is 47 minutes old, past `takeover_after_min`. `laptop` pulls from `mac-mini` (fresh scan) and from `workstation` (unreachable, 8-second connect timeout). The heartbeat is still 47 minutes old, so nobody beat it to the post. `laptop` runs Linux, so it reads the limits from the headers itself. It posts and pushes the store to `mac-mini`. The display now shows `3/3` still, because `workstation`'s cached entry from 09:58 is under an hour old, and the tokens `workstation` counted this morning are still in the total.

At 10:50 `laptop` runs again. The master is still silent, its own post doesn't count against it, so it collects and posts again. It goes on doing that every 5 minutes. The same minute `mac-mini` runs. It is second in line, so its threshold is 60 minutes of master silence, and the master has been silent for 52. It stops. At 11:00 the master has been silent for 62, but `laptop`'s pushes have been landing in `mac-mini`'s store every 5 minutes, so `mac-mini` sees a post 5 minutes old and stands down without calling anyone. Had `laptop` been down as well, `mac-mini` would have collected at 11:00, found nobody posting, and taken over.

At 10:58 the `workstation` entry turns 60 minutes old and stops counting. The marker turns `2/3` on `laptop`'s 11:00 post.

At 12:10 `workstation` boots. Its cron fires, it pulls both stores, keeps its own fresh scan and the newest entry for each of the others, posts as the master (it never checks the heartbeat) and pushes. The marker goes back to `3/3`. At 12:15 `laptop` sees the master posted 5 minutes ago and stands down.

### Caveats

Hosts hold different transcripts. Per-response dedupe runs within a host, not across them, so syncing `~/.claude` between machines with something like Syncthing would count the shared sessions on both.

Keep the timer running on every machine. The master's ssh pull runs a scan on the far end, so a secondary's numbers stay fresh even without its own timer. Its timer is what lets it notice the master has gone quiet.

`--dry-run` on a secondary whose master is healthy prints nothing, because the run stops at the heartbeat check. `--dry-run` on the master does the full collection, including the ssh calls, so it is a good connectivity test. Neither writes the merged store or pushes it.

`--no-scrape` on a fleet host means that host doesn't read the limits itself, but if its store holds a cached set younger than `stale_after_min` those still appear on the display.

## Command-line reference

| Flag | Default | What it does |
|------|---------|--------------|
| `--dry-run` | | Print the payload as JSON instead of posting |
| `--test` | | Post sample multi-model data, for checking a layout |
| `--no-scrape` | | Don't read the usage limits on this host |
| `--usage-method {auto,headers,pty}` | `auto` | How to read the usage limits, see above |
| `--model-limit-ttl MIN` | 60, or 15 above 80% | How stale the per-model row may get before `auto` re-scrapes it |
| `--debounce MIN` | 0 | Exit at once if the last post was under MIN minutes ago |
| `--include-other-models` | | Count models that aren't Anthropic's in the totals, cost and model breakdown, at Sonnet 4.6 rates where the price table has no entry |
| `--strict-activity` | | Count only Anthropic models in the streak and sparkline as well |
| `--fleet-config PATH` | `$TRMNL_FLEET_CONFIG`, else `fleet.json` beside the script | Where the fleet config lives |
| `--no-fleet` | | Ignore the fleet config and post this host alone |
| `--emit-store` | | Refresh this host's entry in the fleet store, print the store as JSON, exit |
| `--ingest-store` | | Merge a fleet store read from stdin into the local one, exit |

The script writes three small files next to your Claude data, `~/.claude/.trmnl_last_push` for the debounce, `~/.claude/.trmnl_model_limit` for the cached per-model row, and `~/.claude/.trmnl_fleet.json` for the fleet store. The first two can go at any time. Deleting the fleet store on a secondary also deletes its copy of the heartbeat, so in a two-machine fleet it posts on its own until the master's next push sets it straight, five minutes at most. In a larger fleet it pulls the heartbeat from a peer first and stays quiet.

## License

GPL-3.0, see [LICENSE](LICENSE)
