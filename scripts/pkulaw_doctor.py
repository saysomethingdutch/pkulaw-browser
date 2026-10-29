#!/usr/bin/env python3
"""Preflight: answer "will a query work right now, and if not, what do I run?"

Run this once at the start of a task, before any extraction. It is deliberately
cheap — file reads and one TCP probe, no browser — so there is never a reason to
skip it and guess instead.

It exists because the failure it diagnoses is silent. A saved WKINFO session
dies about a day after issue (`connect.sid`) or about a week (`autologin`), and
every run after that looks like an unexplained login gate. Guessing at that
point is what used to escalate into screenshot-driven browsing.
"""
import argparse
import pathlib
import sys

import common
from providers import PROVIDERS, get_provider


def chromium_status() -> str:
    """Name the pinned build, not just "some chromium-* exists" — the drift case
    is exactly the one where a directory exists and no launch works without the
    fallback in common.with_chromium_fallback."""
    want = common.expected_chromium_revision()
    have = sorted({rev for kind in (True, False) for rev, _ in common.installed_chromium_builds(kind)},
                  reverse=True)
    if not have:
        return f"missing — playwright install chromium into {common.BROWSERS_PATH}"
    if want and int(want) not in have:
        return f"pinned build {want} absent, installed {have[0]} — scripts fall back to it"
    return f"installed (build {want or have[0]})"


def check_env(port: int) -> tuple[dict, bool]:
    info = common.cdp_alive(port)
    try:
        import browser_cookie3  # noqa: F401
        cookie3 = True
    except Exception:
        cookie3 = False
    fields = {
        "state-root": common.STATE_ROOT,
        "chrome-cdp": (f"running {info.get('Browser', '?')} on :{port}" if info
                       else f"not running (:{port})"),
        "chromium": chromium_status(),
        "chrome-app": "present" if pathlib.Path(common.DEFAULT_CHROME).exists() else "MISSING",
        "browser_cookie3": "installed" if cookie3 else "missing — pip install browser_cookie3",
    }
    return fields, bool(info)


def check_provider(provider, attach_up: bool, live: bool, args) -> tuple[dict, bool, str]:
    state = common.session_state_status(provider)
    cookies = ", ".join(
        f"{c['name']}({common.fmt_expiry(c['expires'])}{'' if c['alive'] else ' EXPIRED'})"
        for c in state["session_cookies"]
    ) or "none found"

    verdict = state["level"]
    if state["stale_reason"]:
        verdict = f"{verdict} — {state['stale_reason']}"
    fields = {
        "state": f"{state['path']}  ({state['cookies']} cookies)" if state["exists"] else f"{state['path']}  (absent)",
        "session": cookies,
        "verdict": verdict,
        "profile": provider.profile_dir if provider.profile_dir.exists() else f"{provider.profile_dir} (absent)",
    }

    live_ok = None
    if live:
        live_ok, note = probe_live(provider, args)
        fields["live-probe"] = note
    elif attach_up:
        # An open debug port proves a browser exists, not that anyone logged
        # into it. Reporting "usable" on that basis is the same false
        # confidence this script exists to remove.
        fields["attach"] = f"port {args.cdp_port} is up — login NOT verified"

    # Without a live probe, the saved session is the only evidence we have.
    usable = live_ok if live_ok is not None else state["fresh"]

    if usable:
        nxt = ""
    elif provider.name == "rmfyalk" and state["level"] == "suspect":
        # Token cookie dead, SSO cookie still there: no human needed yet.
        nxt = "python3 rmfyalk_api.py --refresh   # re-mints the token from the 统一账户 SSO cookie"
    elif live_ok is False:
        nxt = ("python3 pkulaw_login.py --provider pkulaw   # CARSI, no intranet needed"
               if provider.name == "pkulaw" else f"python3 pkulaw_chrome.py --login {provider.name}")
    elif attach_up:
        nxt = f"python3 pkulaw_doctor.py --provider {provider.name} --live   # confirm that Chrome is logged in"
    elif state["level"] == "suspect":
        nxt = f"python3 pkulaw_doctor.py --provider {provider.name} --live   # half the login cookies expired; only a real load can tell"
    elif provider.name == "pkulaw":
        nxt = "python3 pkulaw_login.py --provider pkulaw   # CARSI, no intranet needed"
    else:
        nxt = f"python3 pkulaw_chrome.py --login {provider.name}"
    return fields, usable, nxt


def probe_live(provider, args) -> tuple[bool, str]:
    """Actually load the provider's probe URL. Costs a browser launch — opt-in."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            with common.browser_session(p, provider, args) as session:
                page = session.new_page()
                try:
                    page.goto(provider.probe_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    # Body first: if a redirect lands mid-read, title and URL
                    # must come from the page the body came from.
                    body = common.read_body(page)
                    verdict, _note = common.classify_navigation(
                        provider, provider.probe_url, page.title(), page.url, body,
                    )
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        return verdict == common.EXIT_OK, f"{session.mode} -> {common.EXIT_MEANING[verdict]}"
    except Exception as exc:
        code, message, _hint = common.classify_exception(exc)
        return False, f"{common.EXIT_MEANING.get(code, 'error')}: {message[:80]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight the PKULAW/WKINFO session and environment.")
    parser.add_argument("--provider", default="all", choices=[*sorted(PROVIDERS), "all"])
    parser.add_argument("--live", action="store_true",
                        help="Also load each provider's probe URL in a browser (slower, definitive)")
    common.add_browser_args(parser)
    args = parser.parse_args()
    args.headful = False
    args.storage_state = None

    env, attach_up = check_env(args.cdp_port)
    names = sorted(PROVIDERS) if args.provider == "all" else [args.provider]

    blocks, all_usable, nexts = [], True, []
    for name in names:
        provider = get_provider(name)
        fields, usable, nxt = check_provider(provider, attach_up, args.live, args)
        blocks.append((name, fields))
        all_usable = all_usable and usable
        if nxt:
            nexts.append(nxt)

    common.print_digest("ok" if all_usable else "session-stale", env)
    for name, fields in blocks:
        print()
        print(f"provider  {name}")
        width = max(len(k) for k in fields)
        for k, v in fields.items():
            print(f"  {k.ljust(width)}  {v}")

    if nexts:
        print("\nnext")
        for n in nexts:
            print(f"  {n}")
        print("\nRun these from this directory:")
        print(f"  {pathlib.Path(__file__).parent}")
    else:
        print("\nnext\n  nothing — go ahead and query")

    return common.EXIT_OK if all_usable else common.EXIT_SESSION_STALE


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        sys.exit(common.fail("pkulaw_doctor.py", exc))
