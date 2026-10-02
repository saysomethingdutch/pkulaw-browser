#!/usr/bin/env python3
"""Copy a page out of the user's visible, logged-in Chrome via the clipboard.

Level 3 of the escalation ladder — the last automated step before stopping and
asking the user. Still a script: it drives Chrome by AppleScript and reads the
clipboard, so it costs a few hundred tokens, not the thousands that
screenshot-and-click browsing costs.

Direct network fetch stays disabled. Both sites answer plain requests with an
anti-bot shell, and the previous version of this file carried a helper that
reconstructed those anti-bot cookies; it has been removed along with the dead
direct-fetch path.
"""
import argparse
import pathlib
import subprocess
import sys
import time

import common
from providers import provider_for_url


def normalize_url(raw_url: str, source_view: bool) -> tuple[str, bool]:
    if raw_url.startswith("view-source:"):
        return raw_url[len("view-source:"):], True
    return raw_url, source_view


def run(cmd: list[str], capture: bool = True, input_text: str | None = None) -> str:
    proc = subprocess.run(cmd, check=True, text=True, input=input_text, capture_output=capture)
    return proc.stdout if capture else ""


def chrome_open_and_copy(url: str, source_view: bool, settle: float) -> str:
    """Select-all + copy by keystroke, not by menu item name.

    The previous version clicked menu items called 「全选」/「复制」, so it broke
    on any Chrome that wasn't running a Chinese UI. ⌘A / ⌘C are locale-independent.
    """
    target_url = f"view-source:{url}" if source_view and not url.startswith("view-source:") else url
    run(["pbcopy"], capture=False, input_text="")
    applescript = f'''
tell application "Google Chrome"
  activate
  set URL of active tab of front window to "{target_url}"
end tell
delay {settle}
tell application "System Events"
  tell process "Google Chrome"
    set frontmost to true
    keystroke "a" using command down
    delay 0.5
    keystroke "c" using command down
  end tell
end tell
delay 1
'''
    run(["osascript", "-e", applescript])
    return run(["pbpaste"])


def active_chrome_tab() -> tuple[str, str]:
    applescript = '''
tell application "Google Chrome"
  return (title of active tab of front window) & linefeed & (URL of active tab of front window)
end tell
'''
    lines = run(["osascript", "-e", applescript]).splitlines()
    if len(lines) >= 2:
        return lines[0], lines[1]
    return "", lines[0] if lines else ""


def chrome_copy_with_retry(url, source_view, expected, min_bytes, retries, open_in_chrome, settle) -> str:
    last = "chrome copy did not run"
    for attempt in range(1, retries + 1):
        if open_in_chrome:
            text = chrome_open_and_copy(url, source_view, settle)
        else:
            _title, active_url = active_chrome_tab()
            if not active_url:
                raise common.SkillError("could not read the active Chrome tab", common.EXIT_BROWSER_BLOCKED,
                                        "open a Chrome window with the page loaded, then retry")
            text = chrome_open_and_copy(active_url, False, settle)
        valid, reason = common.looks_valid(text, expected, min_bytes)
        if valid:
            return text
        last = f"attempt {attempt}: {reason}"
        time.sleep(1)
    raise common.SkillError(last, common.EXIT_VALIDATION,
                            "confirm the Chrome tab is logged in and fully rendered, then retry")


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy PKULAW/WKINFO pages from a visible, logged-in Chrome window.")
    parser.add_argument("url", help="PKULAW or WKINFO URL, or a view-source URL")
    parser.add_argument("--out", help="Write the payload here (default: a file under the skill's out/ dir)")
    parser.add_argument("--output", dest="out", help=argparse.SUPPRESS)  # back-compat
    parser.add_argument("--expect", action="append", default=[], help="Marker that must appear in the copy")
    parser.add_argument("--min-bytes", type=int, default=1000)
    parser.add_argument("--chrome-copy", action="store_true", help="Required: copy from a visible Chrome tab")
    parser.add_argument("--open-in-chrome", action="store_true", help="Load the URL in the active tab before copying")
    parser.add_argument("--source-view", action="store_true", help="Open/copy as view-source")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--settle", type=float, default=6.0, help="Seconds to wait for the page before copying")
    parser.add_argument("--fields", help="Keep only these sections, e.g. 本院认为,裁判结果")
    parser.add_argument("--field-chars", type=int, default=4000)
    parser.add_argument("--format", choices=["digest", "text"], default="digest")
    args = parser.parse_args()

    if not args.chrome_copy:
        parser.error("direct fetch is disabled; use --chrome-copy with a visible, logged-in Chrome window")

    url, source_view = normalize_url(args.url, args.source_view)
    provider = provider_for_url(url)

    try:
        text = chrome_copy_with_retry(
            url=url,
            source_view=source_view,
            expected=args.expect,
            min_bytes=args.min_bytes,
            retries=args.retries,
            open_in_chrome=args.open_in_chrome or source_view,
            settle=args.settle,
        )

        if provider is not None and provider.detect_login_gate("", url, text):
            raise common.SkillError(
                f"the copied tab is showing {provider.name}'s login gate",
                common.EXIT_LOGIN_GATE,
                "log that Chrome window in, then retry",
            )

        payload, missing = text, []
        if args.fields:
            kind = "law" if ("/chl/" in url or "/legislation/" in url) else "case"
            headers = provider.section_headers[kind] if provider else []
            wanted = [f.strip() for f in args.fields.split(",") if f.strip()]
            payload, missing = common.extract_sections(text, wanted, headers, args.field_chars)

        if args.format == "text":
            sys.stdout.write(payload)
            return common.EXIT_OK

        name = provider.name if provider else "page"
        out_path = pathlib.Path(args.out) if args.out else common.default_out_path(name, url, "html" if source_view else "txt")
        saved = common.write_payload(out_path, payload)
        common.print_digest("ok", {
            "provider": name,
            "mode": "chrome-copy" + (":view-source" if source_view else ""),
            "url": url,
            "bytes": len(payload.encode("utf-8", errors="ignore")),
            "sections": args.fields or None,
            "missing": ", ".join(missing) if missing else None,
            "saved": saved,
        }, preview=payload[:300])
        return common.EXIT_OK
    except subprocess.CalledProcessError as exc:
        return common.fail("pkulaw_fetch.py", common.SkillError(
            f"AppleScript control of Chrome failed: {exc}", common.EXIT_BROWSER_BLOCKED,
            "grant Terminal/Claude accessibility + automation permission for Google Chrome in System Settings > Privacy",
        ))
    except Exception as exc:
        return common.fail("pkulaw_fetch.py", exc)


if __name__ == "__main__":
    raise SystemExit(main())
