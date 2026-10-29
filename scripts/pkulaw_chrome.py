#!/usr/bin/env python3
"""Manage the dedicated, debuggable Chrome that the extractors attach to.

Why this exists
---------------
The firm intranet in front of 威科先行 (`home.zhonglun.com`) answers with
`WWW-Authenticate: Negotiate, NTLM` and there is no Kerberos ticket cache on
this machine, so the user authenticates by typing a domain password into
Chrome's *native* auth dialog. Playwright cannot surface that dialog — it
supports HTTP Basic only — and no amount of scripting changes that.

The way around it is to stop trying. This script starts a plain Google Chrome
that the user logs into by hand; the only unusual thing about it is an open
debugging port. Because the user launched it (no `--enable-automation`), the
native NTLM dialog behaves normally. Afterwards `pkulaw_playwright.py
--browser attach` drives that already-authenticated browser.

It must be a dedicated `--user-data-dir`: Chrome >= 136 refuses
`--remote-debugging-port` on the Default profile.

Never type the user's domain password on their behalf. Hand them the window.
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

import common
from providers import PROVIDERS, get_provider

INTRANET_URL = os.environ.get("PKULAW_BROWSER_INTRANET", "https://home.zhonglun.com/")


def chrome_argv(port: int, chrome: str, extra: list[str]) -> list[str]:
    return [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={common.CDP_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        *extra,
    ]


def start(port: int, chrome: str, url: str | None) -> int:
    info = common.cdp_alive(port)
    if info:
        if url:
            subprocess.Popen(chrome_argv(port, chrome, [url]),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        common.print_digest("ok", {"chrome": info.get("Browser", "?"), "port": port,
                                   "profile": common.CDP_PROFILE, "note": "already running"})
        return common.EXIT_OK

    common.CDP_PROFILE.mkdir(parents=True, exist_ok=True)
    log = common.STATE_ROOT / "chrome.log"
    detach: dict = {}
    if os.name == "nt":
        detach["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        detach["start_new_session"] = True
    with open(log, "ab") as fh:
        subprocess.Popen(
            chrome_argv(port, chrome, [url] if url else ["about:blank"]),
            stdout=fh, stderr=fh, **detach,
        )

    for _ in range(30):
        time.sleep(0.5)
        info = common.cdp_alive(port)
        if info:
            common.print_digest("ok", {"chrome": info.get("Browser", "?"), "port": port,
                                       "profile": common.CDP_PROFILE, "log": log})
            return common.EXIT_OK

    print(f"pkulaw_chrome.py: Chrome did not open port {port} within 15s; see {log}", file=sys.stderr)
    return common.EXIT_BROWSER_BLOCKED


def stop(port: int) -> int:
    """Close it through its own debug port.

    Asking the browser to close itself is exact and portable — no pkill, no
    pattern matching, and no way to take down the user's daily Chrome, which
    does not have this port open in the first place.
    """
    how = "cdp"
    if common.cdp_alive(port):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                p.chromium.connect_over_cdp(common.cdp_endpoint(port)).close()
        except Exception:
            # Fall back to a signal only where we can target this exact profile.
            # On Windows there is no equivalent that cannot also hit the user's
            # daily Chrome, so leave the window alone and say so.
            if os.name == "nt":
                how = "manual"
            else:
                how = "signal"
                common.run_quiet(["pkill", "-f", f"--user-data-dir={common.CDP_PROFILE}"])
    time.sleep(1)
    still = common.cdp_alive(port)
    common.print_digest("ok" if not still else "error",
                        {"port": port, "stopped": not still, "via": how})
    if still:
        print(f"pkulaw_chrome.py: port {port} still answering — close that Chrome window by hand",
              file=sys.stderr)
    return common.EXIT_OK if not still else common.EXIT_ERROR


def status(port: int) -> int:
    info = common.cdp_alive(port)
    if not info:
        common.print_digest("session-stale", {
            "port": port, "running": False, "profile": common.CDP_PROFILE,
            "next": "python3 pkulaw_chrome.py --start",
        })
        return common.EXIT_SESSION_STALE
    common.print_digest("ok", {
        "port": port, "running": True, "chrome": info.get("Browser", "?"),
        "profile": common.CDP_PROFILE,
    })
    return common.EXIT_OK


def login(provider_name: str, port: int, chrome: str, url: str | None) -> int:
    provider = get_provider(provider_name)
    # WKINFO's login lives behind the intranet one-click; PKULAW has CARSI;
    # RMFYALK's list page bounces to the court system's unified account login.
    target = url or (INTRANET_URL if provider.name == "wkinfo" else provider.login_url or provider.home_url)
    rc = start(port, chrome, target)
    if rc != common.EXIT_OK:
        return rc
    print(
        f"\nA Chrome window is open at {target}\n"
        f"  1. Log in there by hand ({provider.login_hint or 'the site login'}).\n"
        f"  2. Confirm you can load {provider.home_url} while logged in.\n"
        f"  3. Then run:  python3 pkulaw_chrome.py --save-state {provider.name}\n"
        f"     (optional — it lets headless runs work after this Chrome closes)\n"
        f"\nLeave this window open. Extractors will attach with --browser attach.\n",
        flush=True,
    )
    return common.EXIT_OK


def save_state(provider_name: str, port: int) -> int:
    """Snapshot the attached Chrome's cookies into the provider's state file.

    Makes the login usable by launch-mode runs too, so closing the window later
    does not immediately put the skill back at a login gate.
    """
    from playwright.sync_api import sync_playwright

    provider = get_provider(provider_name)
    if not common.cdp_alive(port):
        print(f"pkulaw_chrome.py: nothing listening on port {port}; --start first", file=sys.stderr)
        return common.EXIT_BROWSER_BLOCKED

    provider.state_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(common.cdp_endpoint(port))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            # Probe a real search, not the home page: the home page renders for
            # anyone, so it cannot tell a logged-in browser from a logged-out one.
            page.goto(provider.probe_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            # Body first: if a redirect lands mid-read, title and URL must come
            # from the page the body came from.
            body = common.read_body(page)
            verdict, note = common.classify_navigation(
                provider, provider.probe_url, page.title(), page.url, body,
            )
            gated = verdict != common.EXIT_OK
            context.storage_state(path=str(provider.state_path))
        finally:
            try:
                page.close()
            except Exception:
                pass

    state = common.session_state_status(provider)
    common.print_digest(
        "ok" if state["fresh"] and not gated else "session-stale",
        {
            "provider": provider.name,
            "saved": provider.state_path,
            "cookies": state["cookies"],
            "probe": common.EXIT_MEANING[verdict] + (f" — {note}" if note else ""),
            "session_cookies": ", ".join(
                f"{c['name']}({common.fmt_expiry(c['expires'])})" for c in state["session_cookies"]
            ) or "none",
        },
    )
    if gated or not state["fresh"]:
        print("pkulaw_chrome.py: that Chrome is not logged in yet — finish the login, then re-run",
              file=sys.stderr)
        return common.EXIT_SESSION_STALE
    return common.EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the debuggable Chrome the extractors attach to.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", action="store_true", help="Launch it (or report it already running)")
    group.add_argument("--stop", action="store_true", help="Close it (matches only this profile)")
    group.add_argument("--status", action="store_true", help="Is it running? (default)")
    group.add_argument("--login", choices=sorted(PROVIDERS), help="Open the right login page and explain the manual step")
    group.add_argument("--save-state", choices=sorted(PROVIDERS), dest="save_state",
                       help="Snapshot its cookies into the provider's state file")
    parser.add_argument("--port", type=int, default=common.CDP_PORT)
    parser.add_argument("--url", help="Override the page to open")
    parser.add_argument("--chrome", default=common.DEFAULT_CHROME)
    args = parser.parse_args()

    try:
        if args.start:
            return start(args.port, args.chrome, args.url)
        if args.stop:
            return stop(args.port)
        if args.login:
            return login(args.login, args.port, args.chrome, args.url)
        if args.save_state:
            return save_state(args.save_state, args.port)
        return status(args.port)
    except Exception as exc:
        return common.fail("pkulaw_chrome.py", exc)


if __name__ == "__main__":
    raise SystemExit(main())
