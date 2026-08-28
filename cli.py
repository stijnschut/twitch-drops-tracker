#!/usr/bin/env python3
"""Interactive menu-driven CLI for Twitch Drops Tracker."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from scanner import (
    format_dt,
    get_watched_campaigns,
    load_watchlist,
    reset_state,
    run_scan,
    save_watchlist,
    send_test_webhook,
)

BASE_DIR = Path(__file__).resolve().parent
console = Console()
_err = Console(stderr=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _print_header(title: str) -> None:
    console.print()
    console.print(Panel(f"[bold cyan]{title}[/]", box=box.ROUNDED))
    console.print()


def _print_error(msg: str) -> None:
    _err.print(f"[bold red]Error:[/] {msg}")


def _print_success(msg: str) -> None:
    console.print(f"[bold green]✓[/] {msg}")


def _print_warning(msg: str) -> None:
    console.print(f"[bold yellow]⚠[/] {msg}")


def _print_info(msg: str) -> None:
    console.print(f"[bold blue]ℹ[/] {msg}")


def _wait_for_enter() -> None:
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/]", default="")
    console.print()


# ─── Menu ────────────────────────────────────────────────────────────────────

def _show_home_menu() -> int:
    console.clear()
    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]Twitch Drops Tracker[/]\n"
            "[dim]Discord notifications for Twitch Drops campaigns[/]",
            box=box.DOUBLE_EDGE,
        )
    )
    console.print()

    menu_items = [
        ("1", "Run a scan (preview or apply)"),
        ("2", "Add a game"),
        ("3", "Remove a game"),
        ("4", "View watchlist"),
        ("5", "View drops for your watchlist"),
        ("6", "Test Discord webhook"),
        ("7", "Reset notification state"),
        ("0", "Exit"),
    ]

    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("Key", style="bold cyan", width=3)
    table.add_column("Description")
    for key, desc in menu_items:
        table.add_row(key, desc)

    console.print(table)
    console.print()

    choice = Prompt.ask(
        "[bold]Select an option[/]",
        choices=[str(i) for i in range(8)],
        default="0",
    )
    return int(choice)


# ─── Menu handlers ───────────────────────────────────────────────────────────

def _run_scan() -> None:
    _print_header("Run a scan")
    dry_run = Confirm.ask(
        "Dry run (preview only, no Discord messages)?", default=True
    )

    events = run_scan(dry_run=dry_run)

    console.print()
    if not events:
        _print_info("No new / starting / ending / changed campaigns.")
    else:
        label = "Would send" if dry_run else "Sent"
        console.print(f"[bold]{label}:[/]")
        for event in events:
            console.print(
                f"  [bold green]•[/] {event['event']:<13} "
                f"[cyan]{event['game']}[/] — {event['name']}"
            )
    _wait_for_enter()


def _add_game() -> None:
    _print_header("Add a game")
    games = load_watchlist()

    name = Prompt.ask(
        "Twitch game name (exact display name, e.g. 'Marvel Rivals')"
    ).strip()
    if not name:
        return

    if any(existing.lower() == name.lower() for existing in games):
        _print_warning(f"'{name}' is already in the watchlist.")
    else:
        games.append(name)
        save_watchlist(games)
        _print_success(f"Added '{name}'.")
    _wait_for_enter()


def _remove_game() -> None:
    _print_header("Remove a game")
    games = load_watchlist()

    if not games:
        _print_info("Watchlist is empty.")
        _wait_for_enter()
        return

    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Game")
    for index, name in enumerate(sorted(games, key=str.lower), start=1):
        table.add_row(str(index), name)

    console.print(table)
    console.print()

    choice = IntPrompt.ask(
        "Number to remove",
        choices=[str(i) for i in range(1, len(games) + 1)],
    )

    # Match the sorted display back to the actual list entry.
    sorted_games = sorted(games, key=str.lower)
    removed = sorted_games[choice - 1]
    games.remove(removed)
    save_watchlist(games)
    _print_success(f"Removed '{removed}'.")
    _wait_for_enter()


def _view_watchlist() -> None:
    _print_header("Watchlist")
    games = load_watchlist()

    if not games:
        _print_info("Watchlist is empty. Use option 2 to add games.")
    else:
        for name in sorted(games, key=str.lower):
            console.print(f"  • {name}")
    _wait_for_enter()


def _view_drops() -> None:
    _print_header("Drops for your watchlist")

    campaigns = get_watched_campaigns()

    if not campaigns:
        _print_info("No active drops found (or the API is unreachable).")
    else:
        table = Table(box=box.SIMPLE)
        table.add_column("Game", style="cyan")
        table.add_column("Campaign")
        table.add_column("Status")
        table.add_column("Starts")
        table.add_column("Ends")
        table.add_column("# rewards", justify="right")
        for campaign in campaigns:
            table.add_row(
                campaign["game"],
                campaign["name"],
                str(campaign["status"] or "Unknown"),
                format_dt(campaign["start"]),
                format_dt(campaign["end"]),
                str(campaign["reward_count"]),
            )
        console.print(table)
    _wait_for_enter()


def _test_webhook() -> None:
    _print_header("Test Discord webhook")

    if send_test_webhook():
        _print_success("Test message sent.")
    else:
        _print_error("Failed to send. Check DISCORD_WEBHOOK_URL in .env.")
    _wait_for_enter()


def _reset_state() -> None:
    _print_header("Reset notification state")
    _print_warning(
        "This makes every current campaign look 'new' again, so the next scan "
        "will re-notify them."
    )

    if Confirm.ask("Reset state.json?", default=False):
        reset_state()
        _print_success("state.json removed. Next scan treats campaigns as new.")
    _wait_for_enter()


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    if not sys.stdout.isatty():
        _err.print(
            "Interactive mode requires a terminal. "
            "Use 'python scanner.py' for a non-interactive scan."
        )
        sys.exit(1)

    load_dotenv(BASE_DIR / ".env")

    while True:
        try:
            choice = _show_home_menu()
            if choice == 0:
                console.print("\n[bold cyan]Goodbye![/] 🎮\n")
                break
            elif choice == 1:
                _run_scan()
            elif choice == 2:
                _add_game()
            elif choice == 3:
                _remove_game()
            elif choice == 4:
                _view_watchlist()
            elif choice == 5:
                _view_drops()
            elif choice == 6:
                _test_webhook()
            elif choice == 7:
                _reset_state()
        except KeyboardInterrupt:
            console.print("\n\n[bold yellow]Interrupted. Exiting.[/]")
            break
        except Exception:
            console.print_exception()
            _wait_for_enter()


if __name__ == "__main__":
    main()
