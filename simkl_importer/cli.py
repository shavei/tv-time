"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .auth import ensure_token, verify_token
from .client import AuthError, BudgetExceeded, SimklClient, SimklError
from .config import DEFAULT_DAILY_LIMIT, Credentials, Store
from .discovery import ask, collect_session, run_discovery
from .images import DEFAULT_WIDTH, PosterRenderer
from .matching import Matcher
from .models import WatchItem
from .parsers import ParseError, load_file
from .sync import DEFAULT_BATCH_SIZE, push_history, write_unmatched_csv
from .web import run_web_discovery

BANNER = r"""
 ____  _       _    _   _____                       _
/ ___|(_)_ __ | | _| | |_   _|_ __ ___  _ __   ___ | |_
\___ \| | '_ \| |/ / |   | | | '_ ` _ \| '_ \ / _ \| __|
 ___) | | | | |   <| |   | | | | | | | | |_) | (_) | |_
|____/|_|_| |_|_|\_\_|   |_| |_| |_| |_| .__/ \___/ \__|
                                       |_|
 TV Time -> Simkl watch history importer
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simkl-import",
        description="Rebuild your Simkl watch history from a TV Time export "
        "and/or an interactive discovery session.",
    )
    parser.add_argument("--file", type=Path, help="watched.csv / watched.json to import")
    parser.add_argument("--discover", action="store_true", help="run interactive discovery mode")
    parser.add_argument("--send", action="store_true", help="send the saved queue and exit")
    parser.add_argument("--dry-run", action="store_true", help="do everything except the POST")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before sending")
    parser.add_argument("--auth-flow", choices=("oob", "pin"), default="oob",
                        help="oob = paste the code from the browser (default), pin = enter a code at simkl.com/pin")
    parser.add_argument("--reauth", action="store_true", help="force a new access token")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"items per /sync/history request (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT,
                        help=f"local guard on requests per UTC day (default {DEFAULT_DAILY_LIMIT}, 0 disables)")
    parser.add_argument("--no-resolve", action="store_true",
                        help="skip the search step and let Simkl match on title+year alone")
    parser.add_argument("--interactive-match", action="store_true",
                        help="ask before accepting a weak title match")
    parser.add_argument("--refresh-library", action="store_true",
                        help="re-download the list of titles already on your account")
    parser.add_argument("--tui", action="store_true",
                        help="run discovery in the terminal instead of the browser")
    parser.add_argument("--web-port", type=int, default=0,
                        help="port for the browser picker (default: pick a free one)")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the picker URL instead of opening a browser")
    parser.add_argument("--no-posters", action="store_true",
                        help="do not draw poster thumbnails in the terminal walkthrough")
    parser.add_argument("--poster-width", type=int, default=DEFAULT_WIDTH,
                        help=f"poster width in terminal columns (default {DEFAULT_WIDTH})")
    parser.add_argument("--no-taste", action="store_true",
                        help="do not rank discovery candidates by your taste profile")
    parser.add_argument("--forget-rejected", action="store_true",
                        help="ask again about titles you previously said no to")
    parser.add_argument("--unmatched-out", type=Path, default=Path("unmatched.csv"),
                        help="where to write titles that could not be imported")
    parser.add_argument("--home", type=Path, help="override the config directory (~/.simkl-importer)")
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    return parser


# ------------------------------------------------------------------ plumbing


def get_client(args, store: Store) -> SimklClient:
    credentials = store.load_credentials()
    if credentials is None or not credentials.client_id:
        credentials = store.prompt_credentials(credentials)
    return SimklClient(
        credentials=credentials,
        store=store,
        daily_limit=args.daily_limit,
        verbose=args.verbose,
    )


def authenticate(client: SimklClient, store: Store, args, force: bool = False) -> bool:
    try:
        if args.auth_flow == "oob" and not client.credentials.client_secret and not client.credentials.access_token:
            store.prompt_credentials(client.credentials)
        ensure_token(client, store, flow=args.auth_flow, force=force or args.reauth)
    except SimklError as exc:
        print(f"\n  Authentication failed: {exc}")
        return False

    profile = verify_token(client)
    if profile and isinstance(profile, dict):
        user = (profile.get("user") or {}).get("name") or "your account"
        account_type = (profile.get("account") or {}).get("type", "")
        print(f"  Authenticated as {user}{f' ({account_type})' if account_type else ''}.")
        return True
    print("  Warning: could not verify the token against /users/settings.")
    return True


def load_queue(store: Store) -> List[WatchItem]:
    return [WatchItem.from_dict(raw) for raw in store.load_queue()]


def save_queue(store: Store, queue: List[WatchItem]) -> None:
    store.save_queue([item.to_dict() for item in queue])


def merge_into_queue(queue: List[WatchItem], new_items: List[WatchItem]) -> List[WatchItem]:
    index = {item.dedupe_key(): item for item in queue}
    order = [item.dedupe_key() for item in queue]
    for item in new_items:
        key = item.dedupe_key()
        if key in index:
            index[key].merge(item)
        else:
            index[key] = item
            order.append(key)
    return [index[key] for key in order]


# -------------------------------------------------------------------- actions


def action_import_file(args, client: SimklClient, store: Store, queue: List[WatchItem]) -> List[WatchItem]:
    path = args.file
    if path is None:
        answer = ask("  Path to watched.csv / watched.json: ")
        if not answer:
            return queue
        path = Path(answer).expanduser()

    print(f"\nReading {path} ...")
    try:
        items = load_file(path)
    except ParseError as exc:
        print(f"  Could not read the file: {exc}")
        return queue
    if not items:
        print("  No usable rows found.")
        return queue

    movies = sum(1 for item in items if item.is_movie)
    print(f"  Parsed {len(items)} title(s): {movies} movie(s), {len(items) - movies} show(s).")
    for item in items[:5]:
        print(f"    - {item.label():<40.40} {item.describe_progress()}")
    if len(items) > 5:
        print(f"    ... and {len(items) - 5} more")

    unmatched: List[WatchItem] = []
    if args.no_resolve:
        print("\n  Skipping ID resolution (--no-resolve); Simkl will match on title+year.")
        matched = items
    else:
        print("\nResolving titles against Simkl (cached between runs)...")
        matcher = Matcher(client, store, interactive=args.interactive_match)
        matched, unmatched = matcher.resolve_all(items)
        print(f"\n  Matched {len(matched)}/{len(items)}.")
        if unmatched:
            written = write_unmatched_csv(args.unmatched_out, unmatched)
            if written:
                print(f"  Wrote {len(unmatched)} unmatched title(s) to {written}")

    return merge_into_queue(queue, matched)


def make_renderer(args, client: SimklClient, store: Store) -> PosterRenderer:
    return PosterRenderer(
        session=client.session,
        cache_dir=store.poster_dir,
        width=args.poster_width,
        enabled=not args.no_posters,
    )


def action_discover(args, client: SimklClient, store: Store, queue: List[WatchItem]) -> List[WatchItem]:
    if args.forget_rejected:
        store.save_rejected({})
        print("  Cleared the list of titles you previously passed on.")
    print("\n" + "=" * 60)
    print("Discovery mode - let's work out what you have watched")
    print("=" * 60)

    try:
        session = collect_session(
            client,
            store,
            queue,
            refresh_library=args.refresh_library,
            use_taste=not args.no_taste,
        )
        if not session["ranked"]:
            return queue
        if args.tui:
            queue = run_discovery(
                client,
                store,
                existing=queue,
                renderer=make_renderer(args, client, store),
                session=session,
            )
        else:
            queue = run_web_discovery(
                session,
                store,
                queue,
                port=args.web_port,
                open_browser=not args.no_browser,
            )
    except (KeyboardInterrupt, EOFError):
        print("\n  Discovery interrupted; keeping what was queued.")
    save_queue(store, queue)
    return queue


def action_send(args, client: SimklClient, store: Store, queue: List[WatchItem]) -> List[WatchItem]:
    if not queue:
        print("\n  The queue is empty - import a file or run discovery first.")
        return queue

    movies = sum(1 for item in queue if item.is_movie)
    episodes = sum(len(season.episodes) for item in queue for season in item.seasons.values())
    print("\n" + "-" * 60)
    print(f"Ready to send: {len(queue)} title(s) - {movies} movie(s), "
          f"{len(queue) - movies} show(s), {episodes} explicit episode(s)")
    print("-" * 60)
    for item in queue[:10]:
        print(f"  {item.label():<42.42} {item.describe_progress()}")
    if len(queue) > 10:
        print(f"  ... and {len(queue) - 10} more")

    if not args.yes and not args.dry_run:
        confirm = ask("\n  Write these to your Simkl account? [y/N]: ")
        if not confirm.lower().startswith("y"):
            print("  Cancelled; the queue is still saved.")
            return queue

    report = push_history(client, queue, batch_size=args.batch_size, dry_run=args.dry_run)
    print(report.summary())

    if args.dry_run:
        print("\n  Dry run - queue kept.")
        return queue

    if report.not_found:
        written = write_unmatched_csv(args.unmatched_out, [], report)
        if written:
            print(f"  Wrote rejected item(s) to {written}")

    if report.batches_failed:
        print("\n  Some batches failed; the queue has been kept so you can re-run --send.")
    else:
        store.clear_queue()
        print("\n  Queue cleared. Check https://simkl.com/ to see the imported history.")
    return [] if not report.batches_failed else queue


# ----------------------------------------------------------------------- menu


def menu(args, client: SimklClient, store: Store) -> None:
    queue = load_queue(store)
    while True:
        used = store.requests_today()
        print("\n" + "=" * 60)
        print(f"Queue: {len(queue)} item(s)   |   API requests today: {used}"
              + (f"/{args.daily_limit}" if args.daily_limit else ""))
        print("=" * 60)
        print("  1) Authenticate / re-authenticate")
        print("  2) Import from a watched.csv / watched.json")
        print("  3) Discovery mode (answer yes/no about popular titles)")
        print("  4) Review and send the queue to Simkl")
        print("  5) Clear the queue")
        print("  6) Quit")
        choice = ask("\n  Choose [4]: ", "4")

        try:
            if choice == "1":
                authenticate(client, store, args, force=True)
            elif choice == "2":
                queue = action_import_file(args, client, store, queue)
                save_queue(store, queue)
            elif choice == "3":
                queue = action_discover(args, client, store, queue)
            elif choice == "4":
                queue = action_send(args, client, store, queue)
                save_queue(store, queue)
            elif choice == "5":
                store.clear_queue()
                queue = []
                print("  Queue cleared.")
            elif choice in ("6", "q", "quit", "exit"):
                print("  Bye.")
                return
            else:
                print("  Not a valid choice.")
        except AuthError as exc:
            print(f"\n  {exc}")
        except BudgetExceeded as exc:
            print(f"\n  {exc}")
        except SimklError as exc:
            print(f"\n  API error: {exc}")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print(BANNER)

    store = Store(args.home)
    client = get_client(args, store)

    needs_auth = not client.credentials.access_token or args.reauth
    if needs_auth and not authenticate(client, store, args, force=args.reauth):
        return 1

    try:
        if args.file or args.discover or args.send:
            queue = load_queue(store)
            if args.file:
                queue = action_import_file(args, client, store, queue)
                save_queue(store, queue)
            if args.discover:
                queue = action_discover(args, client, store, queue)
            if args.send or args.file or args.discover:
                queue = action_send(args, client, store, queue)
                save_queue(store, queue)
            return 0
        menu(args, client, store)
    except (KeyboardInterrupt, EOFError):
        print("\n  Interrupted. The queue is saved; re-run with --send to finish.")
        return 130
    except AuthError as exc:
        print(f"\n  {exc}")
        return 1
    except BudgetExceeded as exc:
        print(f"\n  {exc}")
        return 1
    except SimklError as exc:
        print(f"\n  API error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
