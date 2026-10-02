#!/usr/bin/env python3
"""Complete a manual login once in a dedicated Playwright profile, and persist it.

This is the right tool for PKULAW, whose CARSI login is a normal web form.

It is the *wrong* tool for WKINFO behind an NTLM/Negotiate intranet portal:
this launches an automation-flagged browser that cannot show the native OS
credential dialog. Use `pkulaw_chrome.py --login wkinfo` there instead — it
hands the user a plain Chrome and attaches afterwards.
"""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

import common
from providers import get_provider


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a browser with a dedicated profile, let the user log in, and persist the session."
    )
    parser.add_argument("--provider", default="pkulaw", choices=["pkulaw", "wkinfo"])
    parser.add_argument("--profile-dir", help="Persistent profile directory (defaults per provider, absolute)")
    parser.add_argument("--state-path", help="Where to save storage_state JSON (defaults per provider, absolute)")
    parser.add_argument("--url", help="URL to open for login (defaults to the provider home page)")
    common.add_browser_args(parser)
    args = parser.parse_args()

    provider = get_provider(args.provider)
    profile_dir = pathlib.Path(args.profile_dir) if args.profile_dir else provider.profile_dir
    state_path = pathlib.Path(args.state_path) if args.state_path else provider.state_path
    login_url = args.url or provider.home_url

    if args.provider == "wkinfo":
        print(
            "pkulaw_login.py: note — if WKINFO sits behind the firm's NTLM intranet, this path "
            "cannot complete that login. Use: python3 pkulaw_chrome.py --login wkinfo",
            file=sys.stderr,
        )

    browsers_path = pathlib.Path(args.browsers_path)
    choice = common.resolve_browser_choice(args.browser, browsers_path, args.cdp_port)
    if choice == "attach":
        # Attaching means a logged-in Chrome already exists; snapshot it instead.
        print(
            "pkulaw_login.py: a debuggable Chrome is already running — snapshot it instead:\n"
            f"  python3 pkulaw_chrome.py --save-state {provider.name}",
            file=sys.stderr,
        )
        return common.EXIT_OK
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        common.prepare_playwright_env(browsers_path)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 1200},
                **common.build_launch_kwargs(choice, args.chrome),
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            print(
                f"\n{provider.name} login browser opened at {login_url}\n"
                f"  1. Complete the {provider.name} login in that window.\n"
                "  2. Come back here and press Enter.\n",
                flush=True,
            )
            input()
            page.wait_for_timeout(1500)
            context.storage_state(path=str(state_path))
            title, final_url = page.title(), page.url
            body = page.locator("body").inner_text(timeout=15000)
            gated = provider.detect_login_gate(title, final_url, body)
            context.close()

        state = common.session_state_status(provider)
        common.print_digest(
            "ok" if state["fresh"] and not gated else "session-stale",
            {
                "provider": provider.name,
                "browser": choice,
                "profile": profile_dir,
                "saved": state_path,
                "cookies": state["cookies"],
                "login_gate_seen": gated,
                "session": ", ".join(
                    f"{c['name']}({common.fmt_expiry(c['expires'])})" for c in state["session_cookies"]
                ) or "none",
            },
        )
        if gated or not state["fresh"]:
            print("pkulaw_login.py: the saved session does not look logged in — retry the login",
                  file=sys.stderr)
            return common.EXIT_SESSION_STALE
        return common.EXIT_OK
    except Exception as exc:
        return common.fail("pkulaw_login.py", exc)


if __name__ == "__main__":
    raise SystemExit(main())
