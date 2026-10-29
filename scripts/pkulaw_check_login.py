#!/usr/bin/env python3
"""Load a provider's probe URL and report whether the session actually works.

Prefer `pkulaw_doctor.py` first — it answers the same question from files alone,
with no browser launch. Use this when you need the definitive answer.

Prints a digest, never the page. The old version echoed 2000 characters of body
text on every check, which is a lot of tokens to spend on a yes/no.
"""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

import common
from providers import PROVIDERS, get_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether a saved session still works.")
    parser.add_argument("--provider", default="pkulaw", choices=sorted(PROVIDERS))
    parser.add_argument("--state-path", help="storage_state JSON (defaults per provider, absolute)")
    parser.add_argument("--url", help="URL to test (defaults to the provider's probe search)")
    parser.add_argument("--headful", action="store_true")
    common.add_browser_args(parser)
    args = parser.parse_args()

    provider = get_provider(args.provider)
    args.storage_state = args.state_path
    test_url = args.url or provider.probe_url

    state = common.session_state_status(provider)
    choice = common.resolve_browser_choice(args.browser, pathlib.Path(args.browsers_path), args.cdp_port)
    if choice != "attach" and not state["exists"]:
        print(
            f"pkulaw_check_login.py: no saved session at {state['path']}\n"
            f"  next -> python3 pkulaw_doctor.py --provider {provider.name}",
            file=sys.stderr,
        )
        return common.EXIT_SESSION_STALE

    try:
        with sync_playwright() as p:
            with common.browser_session(p, provider, args) as session:
                page = session.new_page()
                try:
                    page.goto(test_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3500)
                    # Body first: if a redirect lands mid-read, title and URL
                    # must come from the page the body came from.
                    body = common.read_body(page)
                    title, final_url = page.title(), page.url
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                mode = session.mode
                attached = session.attached

        verdict, note = common.classify_navigation(provider, test_url, title, final_url, body)
        if verdict == common.EXIT_LOGIN_GATE and not attached and not state["fresh"]:
            verdict = common.EXIT_SESSION_STALE

        common.print_digest(
            common.EXIT_MEANING[verdict],
            {
                "provider": provider.name,
                "mode": mode,
                "url": final_url,
                "title": title[:90],
                "bytes": len(body.encode("utf-8", errors="ignore")),
                "state": state["path"] if not attached else "(attached Chrome's own cookies)",
                "state_fresh": state["fresh"] if not attached else "n/a",
                "note": note or None,
            },
            preview=body[:200],
        )
        return verdict
    except Exception as exc:
        return common.fail("pkulaw_check_login.py", exc)


if __name__ == "__main__":
    raise SystemExit(main())
