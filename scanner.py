#!/usr/bin/env python3
"""
scanner.py — Twitch Drops Tracker.

Fetches the public Sunkwi Twitch Drops API, filters campaigns against
`watchlist.json`, diffs the result against `state.json`, and posts Discord
embeds for new / starting-soon / ending-soon / changed campaigns.

This is a single-run script: it scans once and exits. Schedule it with your
NAS scheduler (or cron) to run daily — no Docker required.

Deduplication guarantee: every event (new, starting soon, ending soon,
changed) is sent **at most once** per campaign.

Usage:
    python scanner.py                # run one scan
    python scanner.py --dry-run      # scan without sending webhooks
    python scanner.py --test-webhook # send one test message
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

# ─── Config (defaults come from here / .env) ─────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_WEBHOOK_USERNAME = os.getenv("DISCORD_WEBHOOK_USERNAME", "Twitch Drops Tracker").strip()
DISCORD_WEBHOOK_AVATAR_URL = os.getenv("DISCORD_WEBHOOK_AVATAR_URL", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "UTC").strip()
START_SOON_HOURS = float(os.getenv("START_SOON_HOURS", "24"))
END_SOON_HOURS = float(os.getenv("END_SOON_HOURS", "24"))

NOTIFY_ON_CHANGES = os.getenv("NOTIFY_ON_CHANGES", "true").lower() in (
    "1", "true", "yes", "on",
)
ALERT_ON_FAILURE = os.getenv("ALERT_ON_FAILURE", "false").lower() in (
    "1", "true", "yes", "on",
)
FAILURE_COOLDOWN_SECONDS = float(os.getenv("FAILURE_COOLDOWN_SECONDS", "43200"))

API_BASE_URL = os.getenv(
    "API_BASE_URL", "https://twitch-drops-api.sunkwi.com"
).rstrip("/")
API_TIMEOUT_SECONDS = float(os.getenv("API_TIMEOUT_SECONDS", "30"))

WATCHLIST_PATH = SCRIPT_DIR / "watchlist.json"
STATE_PATH = SCRIPT_DIR / "state.json"

# Discord embed colour values.
COLOR_NEW = 0x9146FF      # Twitch purple
COLOR_STARTING = 0x1ABC9C
COLOR_ENDING = 0xE74C3C
COLOR_CHANGED = 0xF1C40F

STATE_VERSION = 1

log = logging.getLogger("twitch-drops-tracker")


# ─── Time helpers ────────────────────────────────────────────────────────────


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_dt(value: Any) -> Optional[dt.datetime]:
    """Parse an ISO-8601 timestamp (with or without a trailing 'Z')."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # 'Z' is valid ISO-8601 but not accepted by fromisoformat() before 3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _timezone() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def _date_format(tz: ZoneInfo) -> str:
    """Return a locale-appropriate date format for the configured timezone.

    North/South America uses MM-DD-YYYY, Europe uses DD-MM-YYYY, and
    everything else (including East Asia) keeps the ISO YYYY-MM-DD order.
    """
    key = tz.key
    if key.startswith("America/"):
        return "%m-%d-%Y"
    if key.startswith("Europe/"):
        return "%d-%m-%Y"
    return "%Y-%m-%d"


def format_dt(value: Any) -> str:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = parse_dt(value)
    if parsed is None:
        return "Unknown"
    tz = _timezone()
    return parsed.astimezone(tz).strftime(f"{_date_format(tz)} %H:%M %Z")


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


# ─── Watchlist ───────────────────────────────────────────────────────────────


def load_watchlist() -> list[str]:
    if not WATCHLIST_PATH.exists():
        WATCHLIST_PATH.write_text("[]\n")
        log.warning("Created an empty watchlist at %s", WATCHLIST_PATH)
        return []

    try:
        data = json.loads(WATCHLIST_PATH.read_text())
    except Exception as exc:
        log.error("Could not parse watchlist %s: %s", WATCHLIST_PATH, exc)
        return []

    if isinstance(data, dict):
        data = data.get("games", data.get("watchlist", []))

    if not isinstance(data, list):
        log.error("Watchlist must be a JSON list of game names.")
        return []

    return [str(item).strip() for item in data if str(item).strip()]


def save_watchlist(games: list[str]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(games, indent=2, ensure_ascii=False) + "\n")


# ─── State ───────────────────────────────────────────────────────────────────


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "campaigns": {},
        "last_scan_at": None,
        "last_failure_notified_at": None,
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return empty_state()
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception as exc:
        log.error("Could not parse state %s: %s", STATE_PATH, exc)
        return empty_state()

    if not isinstance(state, dict):
        return empty_state()
    state.setdefault("campaigns", {})
    state.setdefault("last_scan_at", None)
    state.setdefault("last_failure_notified_at", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(STATE_PATH)


def reset_state() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


# ─── API ─────────────────────────────────────────────────────────────────────


def fetch_drops() -> list[dict[str, Any]]:
    url = f"{API_BASE_URL}/drops"
    log.info("Fetching %s", url)
    resp = requests.get(
        url,
        timeout=API_TIMEOUT_SECONDS,
        headers={
            "Accept": "application/json",
            "User-Agent": "twitch-drops-tracker/1.0",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    # Legacy endpoint returns the array directly; v2 wraps it in {data: []}.
    if isinstance(data, dict):
        data = data.get("data", data)
    if not isinstance(data, list):
        raise ValueError("Unexpected API response shape (expected a list).")
    return data


# ─── Reward / signature helpers ──────────────────────────────────────────────


def drop_display_name(time_based_drop: dict[str, Any]) -> str:
    """Prefer the actual item name from benefitEdges over the tier label."""
    for edge in time_based_drop.get("benefitEdges") or []:
        benefit = edge.get("benefit") or {}
        if benefit.get("name"):
            return str(benefit["name"])
    return str(time_based_drop.get("name") or "Reward")


def drop_requirement(time_based_drop: dict[str, Any]) -> str:
    minutes = time_based_drop.get("requiredMinutesWatched") or 0
    subs = time_based_drop.get("requiredSubs") or 0
    parts = []
    if minutes:
        parts.append(f"{minutes} min")
    if subs:
        parts.append(f"{subs} sub(s)")
    return " + ".join(parts) if parts else "—"


_QUANTITY_SUFFIX_RE = re.compile(r"^(?P<name>.*?)\s*\*(?P<qty>\d+)$")


def format_reward_name(time_based_drop: dict[str, Any]) -> str:
    """Return the reward display name with its quantity moved to the front.

    Many publishers encode quantity as a ``*N`` suffix (e.g.
    ``Advanced Energy Bag*2``). Discord embeds read more naturally as
    ``2x Advanced Energy Bag``, so normalise that one pattern while leaving
    already-prefixed names (``5x Dungeon Key``) and plain names untouched.
    """
    name = drop_display_name(time_based_drop).strip()
    match = _QUANTITY_SUFFIX_RE.match(name)
    if match:
        base = match.group("name").strip()
        qty = match.group("qty")
        if base:
            return f"{qty}x {base}"
    return name


def reward_signature(reward: dict[str, Any]) -> str:
    """Stable fingerprint of a campaign's reward list.

    Start/end timestamps are intentionally excluded: they change formatting
    between API responses and are handled separately by the reminder logic.
    """
    drops: list[dict[str, Any]] = []
    for tbd in reward.get("timeBasedDrops") or []:
        benefits = sorted(
            str((edge.get("benefit") or {}).get("name"))
            for edge in tbd.get("benefitEdges") or []
            if (edge.get("benefit") or {}).get("name")
        )
        drops.append(
            {
                "name": drop_display_name(tbd),
                "minutes": tbd.get("requiredMinutesWatched"),
                "subs": tbd.get("requiredSubs"),
                "benefits": benefits,
            }
        )
    drops.sort(key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False))

    payload = {"name": reward.get("name"), "drops": drops}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─── Discord ─────────────────────────────────────────────────────────────────


def send_webhook(embeds: list[dict[str, Any]]) -> bool:
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not set — not sending.")
        return False

    payload: dict[str, Any] = {"username": DISCORD_WEBHOOK_USERNAME, "embeds": embeds}
    if DISCORD_WEBHOOK_AVATAR_URL:
        payload["avatar_url"] = DISCORD_WEBHOOK_AVATAR_URL

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL, json=payload, timeout=API_TIMEOUT_SECONDS
        )
        # Discord rate limit: honour Retry-After once, then retry.
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            log.warning("Discord rate limited — waiting %.1fs", retry_after)
            time.sleep(retry_after)
            resp = requests.post(
                DISCORD_WEBHOOK_URL, json=payload, timeout=API_TIMEOUT_SECONDS
            )

        if resp.status_code >= 400:
            log.error(
                "Discord webhook returned %s: %s",
                resp.status_code,
                resp.text[:500],
            )
            return False
        return True
    except requests.RequestException as exc:
        log.error("Discord webhook request failed: %s", exc)
        return False


def send_test_webhook() -> bool:
    embed = {
        "title": "✅ Twitch Drops Tracker — webhook test",
        "description": "If you can see this, the Discord webhook is configured correctly.",
        "color": COLOR_STARTING,
        "timestamp": iso(now_utc()),
        "footer": {"text": "Twitch Drops Tracker"},
    }
    ok = send_webhook([embed])
    if ok:
        log.info("Test webhook sent successfully.")
    else:
        log.error("Test webhook failed — check DISCORD_WEBHOOK_URL.")
    return ok


def eligibility(reward: dict[str, Any]) -> str:
    allow = reward.get("allow") or {}
    channels = allow.get("channels") or []
    if allow.get("isEnabled") and channels:
        names = [
            str(channel.get("displayName") or channel.get("name"))
            for channel in channels
            if channel.get("displayName") or channel.get("name")
        ]
        if names:
            if len(names) <= 5:
                return "Restricted: " + ", ".join(names)
            return (
                f"Restricted to {len(names)} channels "
                f"(e.g. {', '.join(names[:5])}, …)"
            )
    return "Any channel"


def build_reward_lines(reward: dict[str, Any]) -> str:
    drops = reward.get("timeBasedDrops") or []

    def sort_key(tbd: dict[str, Any]) -> tuple[int, int]:
        minutes = tbd.get("requiredMinutesWatched") or 0
        # Watch-time rewards first (shortest first); non-watch rewards
        # (subscriptions, etc.) are pushed to the end.
        return (0 if minutes else 1, minutes)

    drops = sorted(drops, key=sort_key)

    lines: list[str] = []
    for tbd in drops:
        lines.append(f"• **{format_reward_name(tbd)}** — {drop_requirement(tbd)}")

    if not drops:
        if reward.get("eventBasedDrops"):
            lines.append("• Event / mission-based drops — see campaign page.")
        else:
            lines.append("• Details on the campaign page.")

    if len(lines) > 20:
        extra = len(lines) - 20
        lines = lines[:20]
        lines.append(f"… and {extra} more")
    return "\n".join(lines)


def build_embed(
    reward: dict[str, Any],
    game_name: str,
    box_art: Optional[str],
    event: str,
    now: dt.datetime,
) -> dict[str, Any]:
    reward_name = reward.get("name") or "Twitch Drops campaign"
    description = (reward.get("description") or "").strip()

    titles = {
        "new": f"New Twitch Drops: {reward_name}",
        "starting_soon": f"⏰ Starting soon: {reward_name}",
        "ending_soon": f"⚠️ Ending soon: {reward_name}",
        "changed": f"🔁 Updated: {reward_name}",
    }
    colors = {
        "new": COLOR_NEW,
        "starting_soon": COLOR_STARTING,
        "ending_soon": COLOR_ENDING,
        "changed": COLOR_CHANGED,
    }

    body_parts = []
    if description:
        body_parts.append(description[:1200])
        body_parts.append("")  # Blank line between the description and "Rewards".
    body_parts.append("**Rewards**")
    body_parts.append(build_reward_lines(reward))
    embed_description = "\n".join(body_parts)[:4096]

    embed: dict[str, Any] = {
        "title": titles[event][:256],
        "description": embed_description,
        "color": colors[event],
        "fields": [
            {"name": "Game", "value": game_name[:1024]},
            {"name": "Starts", "value": format_dt(reward.get("startAt"))},
            {"name": "Ends", "value": format_dt(reward.get("endAt"))},
            {
                "name": "Status",
                "value": str(reward.get("status") or "Unknown"),
            },
            {
                "name": "Eligibility",
                "value": eligibility(reward)[:1024],
            },
        ],
        "timestamp": iso(now),
    }

    details_url = reward.get("detailsURL") or "https://www.twitch.tv/drops/campaigns"
    if details_url:
        embed["url"] = details_url

    if box_art:
        embed["thumbnail"] = {"url": box_art}

    return embed


# ─── Campaign classification & state updates ─────────────────────────────────


def _in_window(ts: Optional[dt.datetime], hours: float, now: dt.datetime) -> bool:
    if ts is None or ts <= now:
        return False
    return (ts - now).total_seconds() <= hours * 3600


def initial_reminder_flags(
    start: Optional[dt.datetime], end: Optional[dt.datetime], now: dt.datetime
) -> tuple[Optional[str], Optional[str]]:
    start_at = iso(now) if _in_window(start, START_SOON_HOURS, now) else None
    end_at = iso(now) if _in_window(end, END_SOON_HOURS, now) else None
    return start_at, end_at


def classify(
    record: Optional[dict[str, Any]],
    signature: str,
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
    now: dt.datetime,
) -> Optional[str]:
    if record is None:
        return "new"

    if NOTIFY_ON_CHANGES and record.get("signature") != signature:
        return "changed"

    if (
        _in_window(start, START_SOON_HOURS, now)
        and not record.get("notified_start_soon_at")
    ):
        return "starting_soon"

    if (
        _in_window(end, END_SOON_HOURS, now)
        and not record.get("notified_end_soon_at")
    ):
        return "ending_soon"

    return None


def apply_notification(
    state: dict[str, Any],
    reward_id: str,
    record: Optional[dict[str, Any]],
    reward: dict[str, Any],
    game_name: str,
    signature: str,
    event: str,
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
    now: dt.datetime,
) -> None:
    if record is None:
        record = {
            "game": game_name,
            "name": reward.get("name"),
            "start_at": reward.get("startAt"),
            "end_at": reward.get("endAt"),
            "signature": signature,
            "notified_new_at": None,
            "notified_start_soon_at": None,
            "notified_end_soon_at": None,
            "notified_change_at": None,
        }
        state["campaigns"][reward_id] = record

    record["game"] = game_name
    record["name"] = reward.get("name")
    record["start_at"] = reward.get("startAt")
    record["end_at"] = reward.get("endAt")
    record["signature"] = signature

    if event == "new":
        record["notified_new_at"] = iso(now)
        start_at, end_at = initial_reminder_flags(start, end, now)
        record["notified_start_soon_at"] = start_at
        record["notified_end_soon_at"] = end_at
    elif event == "changed":
        record["notified_change_at"] = iso(now)
        start_at, end_at = initial_reminder_flags(start, end, now)
        record["notified_start_soon_at"] = start_at
        record["notified_end_soon_at"] = end_at
    elif event == "starting_soon":
        record["notified_start_soon_at"] = iso(now)
    elif event == "ending_soon":
        record["notified_end_soon_at"] = iso(now)


# ─── Failure alert ───────────────────────────────────────────────────────────


def maybe_alert_failure(
    state: dict[str, Any], message: str, now: dt.datetime
) -> None:
    if not ALERT_ON_FAILURE:
        return

    last = parse_dt(state.get("last_failure_notified_at"))
    if last and (now - last).total_seconds() < FAILURE_COOLDOWN_SECONDS:
        return

    embed = {
        "title": "⚠️ Twitch Drops Tracker — scan failed",
        "description": str(message)[:2000],
        "color": COLOR_ENDING,
        "timestamp": iso(now),
    }
    if send_webhook([embed]):
        state["last_failure_notified_at"] = iso(now)


# ─── Pruning ─────────────────────────────────────────────────────────────────


def prune_state(state: dict[str, Any], seen_ids: set[str], now: dt.datetime) -> None:
    """Remove campaigns that are no longer worth tracking.

    A campaign is dropped as soon as its stored end date has passed. Records
    without a usable end date are only dropped once they are no longer
    returned by the API, so a temporarily missing (but still active) campaign
    does not get re-notified as new.
    """
    for reward_id in list(state["campaigns"]):
        record = state["campaigns"][reward_id]
        end = parse_dt(record.get("end_at"))
        if end is not None and end <= now:
            del state["campaigns"][reward_id]
        elif end is None and reward_id not in seen_ids:
            del state["campaigns"][reward_id]


# ─── Live view (used by cli.py) ──────────────────────────────────────────────


def get_watched_campaigns() -> list[dict[str, Any]]:
    """Return active/upcoming campaigns for the games in the watchlist."""
    watchlist = load_watchlist()
    watch_set = {name.lower() for name in watchlist}

    try:
        campaigns = fetch_drops()
    except Exception as exc:
        log.error("Could not fetch drops: %s", exc)
        return []

    now = now_utc()
    result: list[dict[str, Any]] = []

    for item in campaigns:
        if not isinstance(item, dict):
            continue
        game_name = str(item.get("gameDisplayName") or "").strip()
        if game_name.lower() not in watch_set:
            continue
        for reward in item.get("rewards") or []:
            end = parse_dt(reward.get("endAt") or item.get("endAt"))
            if end is not None and end <= now:
                continue
            result.append(
                {
                    "game": game_name,
                    "name": reward.get("name"),
                    "status": reward.get("status"),
                    "start": reward.get("startAt"),
                    "end": reward.get("endAt"),
                    "reward_count": len(reward.get("timeBasedDrops") or []),
                }
            )

    return result


# ─── Main scan ───────────────────────────────────────────────────────────────


def run_scan(dry_run: bool = False) -> list[dict[str, Any]]:
    """Run one scan. Returns the events that were sent (or would be sent)."""
    state = load_state()
    now = now_utc()

    watchlist = load_watchlist()
    if not watchlist:
        log.warning("Watchlist is empty — add game names to %s.", WATCHLIST_PATH)

    try:
        campaigns = fetch_drops()
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        if not dry_run:
            maybe_alert_failure(state, f"Could not fetch drops: {exc}", now)
            save_state(state)
        return []

    watch_set = {name.lower() for name in watchlist}
    seen_ids: set[str] = set()
    events: list[dict[str, Any]] = []

    for item in campaigns:
        if not isinstance(item, dict):
            continue
        game_name = str(item.get("gameDisplayName") or "").strip()
        if game_name.lower() not in watch_set:
            continue

        box_art = item.get("gameBoxArtURL")
        for reward in item.get("rewards") or []:
            reward_id = reward.get("id")
            if not reward_id:
                continue
            seen_ids.add(reward_id)

            end = parse_dt(reward.get("endAt") or item.get("endAt"))
            start = parse_dt(reward.get("startAt") or item.get("startAt"))

            # Ignore campaigns that have already ended.
            if end is not None and end <= now:
                continue

            signature = reward_signature(reward)
            record = state["campaigns"].get(reward_id)
            event = classify(record, signature, start, end, now)

            if event is None:
                continue

            if dry_run:
                log.info("[dry-run] %s -> %s", event, reward.get("name"))
                events.append(
                    {"event": event, "game": game_name, "name": reward.get("name")}
                )
                continue

            embed = build_embed(reward, game_name, box_art, event, now)
            if not send_webhook([embed]):
                # Keep the event un-notified so it is retried on the next scan.
                log.warning("Skipping state update for %s (send failed).", reward_id)
                continue

            log.info("%s -> %s", event, reward.get("name"))
            events.append(
                {"event": event, "game": game_name, "name": reward.get("name")}
            )
            apply_notification(
                state,
                reward_id,
                record,
                reward,
                game_name,
                signature,
                event,
                start,
                end,
                now,
            )

    if dry_run:
        log.info("Dry run complete — no state changes or webhooks written.")
        return events

    prune_state(state, seen_ids, now)
    state["last_scan_at"] = iso(now)
    save_state(state)
    log.info("Scan complete — %d notification(s) sent.", len(events))
    return events


# ─── Entry point ─────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Twitch Drops Tracker scanner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and print what would be sent, but do not post webhooks.",
    )
    parser.add_argument(
        "--test-webhook",
        action="store_true",
        help="Send a single test message to the configured webhook and exit.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()

    if args.test_webhook:
        send_test_webhook()
        sys.exit(0)

    run_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
