# Twitch Drops Tracker

A lightweight, self-hosted tool that watches a user-curated list of games and
sends a Discord webhook notification whenever there's a relevant Twitch Drops
campaign — new, starting soon, ending soon, or updated.

It's a plain Python script: run it on a schedule with your NAS scheduler or
cron. No Docker required. Data comes from
[Sunkwi's public Twitch Drops API](https://github.com/SunkwiBOT/twitch-drops-api)
(`/drops`) — no auth required.

## Quick start

```bash
cd "Twitch Drops Tracker"
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
cp watchlist.example.json watchlist.json
```

Fill in `DISCORD_WEBHOOK_URL` in `.env`, then add the games you care about to
`watchlist.json`:

```json
[
  "Marvel Rivals",
  "Path of Exile 2"
]
```

Run once to test:

```bash
python scanner.py --test-webhook   # send one test message to Discord
python scanner.py --dry-run        # preview without sending
python scanner.py                  # real scan (sends notifications)
```

## How it works

```
watchlist.json (gameDisplayName strings)
        │
        ▼
scanner.py
  1. GET https://twitch-drops-api.sunkwi.com/drops
  2. Filter campaigns where gameDisplayName is in the watchlist
  3. Diff against state.json (previous scan)
  4. For new / starting-soon / ending-soon / changed campaigns:
       → build a Discord embed
       → POST to the Discord webhook URL
  5. Overwrite state.json with the current scan results
```

## Deduplication (no repeat notifications)

Each campaign is keyed by its reward `id` and recorded in `state.json` the
moment its notification is sent. On later scans, an already-reported campaign
is skipped. A campaign that's active for 14 days gets notified **once** when it
first appears, then again at most **once** if it enters the "ending soon"
window — never every day.

| Event | When it fires | Fires at most |
|---|---|---|
| New | Campaign first seen for a watched game | once |
| Starting soon | `startAt` within `START_SOON_HOURS` | once |
| Ending soon | `endAt` within `END_SOON_HOURS` | once |
| Changed | Reward list changed for a known campaign (`NOTIFY_ON_CHANGES`) | once per change |

The "once" guarantee is only applied after a webhook send succeeds. If a send
fails, the event is left un-notified and is retried on the next scan.

## Interactive CLI

Manage everything from a menu:

```bash
python cli.py
```

```
┌────────────────────────────────────────────┐
│  Twitch Drops Tracker                      │
│  Discord notifications for Twitch Drops    │
└────────────────────────────────────────────┘

  1  Run a scan (preview or apply)
  2  Add a game
  3  Remove a game
  4  View watchlist
  5  View drops for your watchlist
  6  Test Discord webhook
  7  Reset notification state

  0  Exit
```

The CLI is for setup and management; the non-interactive `scanner.py` is for
scheduled automation. Both share the same `watchlist.json` and `state.json`.

## Scheduling on your NAS

`scanner.py` scans once and exits, so schedule it with whatever scheduler your
NAS uses. Two common options:

**cron** (every day at 04:00):

```cron
0 4 * * * cd /path/to/Twitch\ Drops\ Tracker && /path/to/venv/bin/python scanner.py >> twitch-drops.log 2>&1
```

**Synology Task Scheduler** — Control Panel → Task Scheduler → Create →
Scheduled Task → User-defined script, set the schedule (e.g. daily 04:00) and
run the same command above.

> Tip: to log output, redirect to a file as shown. Add `--dry-run` to the
> command if you want to verify before letting it send webhooks.

## Configuration

All settings are environment variables, read from `.env` (see
`.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | *(empty)* | Discord webhook to post to. Required for sending. |
| `DISCORD_WEBHOOK_USERNAME` | `Twitch Drops Tracker` | Anonymous bot display name. |
| `DISCORD_WEBHOOK_AVATAR_URL` | *(empty)* | Optional bot avatar image. |
| `TIMEZONE` | `UTC` | IANA timezone used to format embed times. |
| `START_SOON_HOURS` | `24` | "Starting soon" reminder threshold. |
| `END_SOON_HOURS` | `24` | "Ending soon" reminder threshold. |
| `NOTIFY_ON_CHANGES` | `true` | Notify when a known campaign's rewards change. |
| `ALERT_ON_FAILURE` | `false` | Send a "scan failed" alert if the API is down. |
| `FAILURE_COOLDOWN_SECONDS` | `43200` | Min. gap between failure alerts. |
| `API_BASE_URL` | `https://twitch-drops-api.sunkwi.com` | API base. |
| `API_TIMEOUT_SECONDS` | `30` | HTTP timeout. |

### Timezone examples

Use IANA names (not abbreviations like `CEST`):

- `UTC` — default
- `Europe/Amsterdam`, `Europe/Berlin` — CEST/CET
- `Europe/London` — BST/GMT
- `America/New_York` — EDT/EST
- `Asia/Shanghai` — China Standard Time
- `Asia/Kolkata` — India Standard Time

## Discord embed

One embed is sent per campaign, including:

- Game name + box art (top-right thumbnail)
- Reward names + watch-time (or subscription) requirements
- Start / end time (formatted in your `TIMEZONE`)
- Channel eligibility (any channel vs. a restricted channel list)
- Status and a link to the campaign details page

## Decisions & defaults

- **Reminder thresholds** — `START_SOON_HOURS=24`, `END_SOON_HOURS=24`, plus a
  "new campaign" notification.
- **Watchlist editing** — manual JSON works, and `cli.py` provides add/remove.
- **Name matching** — exact, case-insensitive match on `gameDisplayName`.
- **Updated rewards** — a stable signature detects changes to a campaign's
  reward list and re-notifies once (`NOTIFY_ON_CHANGES`).
- **Sunkwi API reliability** — on failure the scanner logs and skips;
  `ALERT_ON_FAILURE=true` optionally sends a rate-limited "scan failed" alert.
- **IGDB** — not used; box art comes from the API (`gameBoxArtURL`).

## File structure

| File | In git? | Purpose |
|---|---|---|
| `scanner.py` | ✅ | Main scanner + webhook notifier |
| `cli.py` | ✅ | Interactive management menu |
| `watchlist.example.json` | ✅ | Example watchlist template |
| `watchlist.json` | ❌ | Your personal watchlist |
| `state.json` | ❌ | Runtime state (dedup / reminders) |
| `.env` | ❌ | Secrets, copy of `.env.example` |
| `.env.example` | ✅ | All config keys documented |
| `LICENSE` | ✅ | MIT license |
