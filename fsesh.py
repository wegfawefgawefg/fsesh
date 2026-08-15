#!/usr/bin/env python3
"""Save and restore lightweight Firefox tab sessions."""

from __future__ import annotations

import argparse
import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


SESSION_VERSION = 1
MOZLZ4_MAGIC = b"mozLz40\0"
IGNORED_URLS = {"about:blank", "about:home", "about:newtab"}
VALID_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,80}$")


class FseshError(Exception):
    pass


def firefox_roots() -> list[Path]:
    return [
        Path.home() / ".config" / "mozilla" / "firefox",
        Path.home() / ".mozilla" / "firefox",
    ]


def session_dir() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "fsesh" / "sessions"


def profile_candidates() -> list[Path]:
    candidates: list[Path] = []
    for root in firefox_roots():
        ini_path = root / "profiles.ini"
        if not ini_path.exists():
            continue

        config = configparser.ConfigParser()
        config.read(ini_path)
        for section in config.sections():
            if not section.startswith("Profile"):
                continue
            profile_path = config[section].get("Path")
            if not profile_path:
                continue
            path = Path(profile_path)
            if config[section].getboolean("IsRelative", fallback=True):
                path = root / path
            if path.is_dir():
                candidates.append(path)
    return candidates


def session_sources(profile: Path) -> list[Path]:
    paths = [
        profile / "sessionstore-backups" / "recovery.jsonlz4",
        profile / "sessionstore-backups" / "recovery.baklz4",
        profile / "sessionstore.jsonlz4",
    ]
    return [path for path in paths if path.is_file()]


def newest_firefox_session() -> tuple[Path, Path]:
    choices: list[tuple[float, Path, Path]] = []
    for profile in profile_candidates():
        for source in session_sources(profile):
            choices.append((source.stat().st_mtime, profile, source))
    if not choices:
        raise FseshError("Could not find a Firefox session file")
    _, profile, source = max(choices, key=lambda item: item[0])
    return profile, source


def read_mozlz4(path: Path) -> dict[str, Any]:
    try:
        import lz4.block
    except ImportError as exc:
        raise FseshError("Python package 'lz4' is required") from exc

    raw = path.read_bytes()
    if not raw.startswith(MOZLZ4_MAGIC):
        raise FseshError(f"Not a Firefox JSONLZ4 file: {path}")
    try:
        return json.loads(lz4.block.decompress(raw[len(MOZLZ4_MAGIC) :]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise FseshError(f"Could not decode Firefox session: {path}") from exc


def current_entry(tab: dict[str, Any]) -> dict[str, Any] | None:
    entries = tab.get("entries", [])
    if not entries:
        return None
    index = tab.get("index", len(entries))
    if not isinstance(index, int) or index < 1 or index > len(entries):
        index = len(entries)
    return entries[index - 1]


def extract_windows(data: dict[str, Any]) -> list[dict[str, Any]]:
    windows = []
    for window_number, window in enumerate(data.get("windows", []), start=1):
        tabs = []
        for tab_number, tab in enumerate(window.get("tabs", []), start=1):
            entry = current_entry(tab)
            if not entry:
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url or url in IGNORED_URLS:
                continue
            tabs.append(
                {
                    "position": tab_number,
                    "title": entry.get("title", ""),
                    "url": url,
                    "pinned": bool(tab.get("pinned", False)),
                }
            )
        if tabs:
            windows.append(
                {
                    "position": window_number,
                    "selected": window.get("selected", 1),
                    "tabs": tabs,
                }
            )
    return windows


def validate_name(name: str) -> str:
    name = name.strip()
    if name in {".", ".."} or not VALID_NAME.fullmatch(name):
        raise FseshError("Session names must be 1-80 characters and cannot contain slashes")
    return name


def session_path(name: str) -> Path:
    return session_dir() / f"{validate_name(name)}.json"


def save_session(name: str) -> Path:
    name = validate_name(name)
    profile, source = newest_firefox_session()
    windows = extract_windows(read_mozlz4(source))
    tab_count = sum(len(window["tabs"]) for window in windows)
    if not tab_count:
        raise FseshError("Firefox's current session snapshot contains no restorable tabs")

    record = {
        "version": SESSION_VERSION,
        "name": name,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_profile": str(profile),
        "windows": windows,
    }
    destination = session_path(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    print(f"Saved {tab_count} tabs as '{name}'")
    return destination


def saved_sessions() -> list[tuple[str, Path]]:
    directory = session_dir()
    if not directory.is_dir():
        return []
    sessions = []
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            name = record.get("name", path.stem)
            sessions.append((str(name), path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(sessions, key=lambda item: item[0].casefold())


def read_saved_session(name: str) -> dict[str, Any]:
    path = session_path(name)
    if not path.is_file():
        raise FseshError(f"No saved session named '{name}'")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FseshError(f"Invalid session file: {path}") from exc
    if record.get("version") != SESSION_VERSION:
        raise FseshError(f"Unsupported session version in {path}")
    return record


def urls_in(record: dict[str, Any]) -> list[str]:
    return [
        tab["url"]
        for window in record.get("windows", [])
        for tab in window.get("tabs", [])
        if isinstance(tab.get("url"), str) and tab["url"]
    ]


def load_session(name: str) -> None:
    record = read_saved_session(name)
    urls = urls_in(record)
    if not urls:
        raise FseshError(f"Session '{name}' contains no tabs")
    firefox = shutil.which("firefox") or shutil.which("firefox-esr")
    if not firefox:
        raise FseshError("Could not find Firefox in PATH")

    command = [firefox, "--new-window", urls[0]]
    for url in urls[1:]:
        command.extend(["--new-tab", url])
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"Loading {len(urls)} tabs from '{name}'")


def choose(prompt: str, options: list[str] | None = None) -> str | None:
    dmenu = shutil.which("dmenu")
    if not dmenu:
        raise FseshError("Could not find dmenu in PATH")
    text = "" if options is None else "\n".join(options) + "\n"
    result = subprocess.run(
        [dmenu, "-i", "-p", prompt],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    selection = result.stdout.strip()
    return selection or None


def choose_session() -> str | None:
    return choose("load Firefox session", [name for name, _ in saved_sessions()])


def dmenu_main() -> None:
    save_label = "+ save current Firefox tabs"
    names = [name for name, _ in saved_sessions()]
    selection = choose("fsesh", [save_label, *names])
    if selection == save_label:
        name = choose("session name")
        if name:
            save_session(name)
    elif selection in names:
        load_session(selection)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    save_parser = subparsers.add_parser("save", help="save Firefox's current tabs")
    save_parser.add_argument("name", nargs="?", help="session name; dmenu prompt if omitted")

    subparsers.add_parser("list", help="list saved session names")

    load_parser = subparsers.add_parser("load", help="open a saved session")
    load_parser.add_argument("name", nargs="?", help="session name; dmenu picker if omitted")

    subparsers.add_parser("dmenu", help="show the dmenu interface")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        if args.command in {None, "dmenu"}:
            dmenu_main()
        elif args.command == "save":
            name = args.name or choose("session name")
            if name:
                save_session(name)
        elif args.command == "list":
            for name, _ in saved_sessions():
                print(name)
        elif args.command == "load":
            name = args.name or choose_session()
            if name:
                load_session(name)
    except FseshError as exc:
        print(f"fsesh: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
