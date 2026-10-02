#!/usr/bin/env python3
"""Seed a provider's Playwright storage_state with cookies copied from the user's
real, already-logged-in Chrome profile.

Only touches cookies for the domains you name (e.g. an internal SSO domain plus
the target legal database) — never the whole profile. Must be run interactively
by the user: it shells out to `security find-generic-password` to read the
Chrome Safe Storage key from macOS Keychain, which prompts for approval.

This exists because pkulaw_login.py launches a brand-new, empty Playwright
profile. Corporate SSO gates (e.g. an internal home.zhonglun.com IdP) that rely
on cookies already present in the user's daily browser fail in that empty
profile even though the user is genuinely logged in. Seeding just the relevant
cookies into the provider's storage_state file lets pkulaw_playwright.py (and a
follow-up pkulaw_login.py run, if still needed) start from an authenticated
state without copying the rest of the profile (saved passwords, other-site
cookies, history).

Prefer `pkulaw_chrome.py --login <provider>` when you can. Seeding copies a
snapshot that starts aging immediately — WKINFO's `connect.sid` expires about a
day after issue and the `autologin` family about a week — whereas an attached
Chrome keeps refreshing its own cookies. Seeding is the fallback for when that
Chrome is not running.

Decryption is delegated to `browser_cookie3` (pip install browser_cookie3)
rather than hand-rolled AES: a from-scratch PBKDF2("saltysalt", 1003 iters) +
AES-128-CBC(iv=16 spaces) implementation matched Chrome's scheme for every
byte *after* the first 16-byte block but consistently mangled a handful of
bytes inside that first block — the classic fingerprint of a wrong IV.
`browser_cookie3` tracks Chrome's actual (and occasionally changing) cookie
encryption scheme, so it decrypts cleanly where the hand-rolled version
didn't. Cookie *metadata* (host, path, expiry, httpOnly, secure, sameSite)
still comes from a direct sqlite read, since browser_cookie3's cookiejar
objects drop those flags.
"""
import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile

import browser_cookie3

import common
from providers import get_provider

CHROME_EPOCH_OFFSET_MICROS = 11644473600000000
SAMESITE_MAP = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}


def default_chrome_cookies_db(profile: str) -> pathlib.Path:
    return pathlib.Path.home() / "Library/Application Support/Google/Chrome" / profile / "Cookies"


def chrome_expires_to_unix(expires_utc: int) -> float:
    if not expires_utc:
        return -1
    return (expires_utc - CHROME_EPOCH_OFFSET_MICROS) / 1_000_000


def read_cookie_metadata(cookies_db: pathlib.Path, domains: list[str]) -> list[tuple]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = pathlib.Path(tmp) / "Cookies"
        shutil.copy2(cookies_db, tmp_db)
        wal = cookies_db.with_name(cookies_db.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, tmp_db.with_name(tmp_db.name + "-wal"))
        shm = cookies_db.with_name(cookies_db.name + "-shm")
        if shm.exists():
            shutil.copy2(shm, tmp_db.with_name(tmp_db.name + "-shm"))

        conn = sqlite3.connect(str(tmp_db))
        conn.text_factory = bytes
        cur = conn.cursor()
        where = " OR ".join(["host_key LIKE ?"] * len(domains))
        params = [f"%{d}%" for d in domains]
        cur.execute(
            f"SELECT host_key, name, path, expires_utc, is_secure, is_httponly, samesite "
            f"FROM cookies WHERE {where}",
            params,
        )
        rows = cur.fetchall()
        conn.close()
        return rows


def read_decrypted_values(cookies_db: pathlib.Path, domains: list[str]) -> dict:
    """Maps (domain, name, path) -> decrypted value using browser_cookie3."""
    values = {}
    for domain in domains:
        for c in browser_cookie3.chrome(cookie_file=str(cookies_db), domain_name=domain):
            values[(c.domain, c.name, c.path)] = c.value
    return values


def extract_cookies(cookies_db: pathlib.Path, domains: list[str]) -> list[dict]:
    metadata_rows = read_cookie_metadata(cookies_db, domains)
    values = read_decrypted_values(cookies_db, domains)

    cookies = []
    skipped = []
    for host_key, name, path, expires_utc, is_secure, is_httponly, samesite in metadata_rows:
        host_key = host_key.decode("utf-8")
        name = name.decode("utf-8")
        path = path.decode("utf-8")
        value = values.get((host_key, name, path))
        if value is None:
            skipped.append(f"{name}@{host_key}{path}")
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host_key,
                "path": path,
                "expires": chrome_expires_to_unix(expires_utc),
                "httpOnly": bool(is_httponly),
                "secure": bool(is_secure),
                "sameSite": SAMESITE_MAP.get(samesite, "Lax"),
            }
        )
    if skipped:
        print(f"seed_cookies_from_chrome.py: skipped (no decrypted value): {', '.join(skipped)}", file=sys.stderr)
    return cookies


def merge_into_state(state_path: pathlib.Path, new_cookies: list[dict]) -> dict:
    state = {"cookies": [], "origins": []}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("cookies", [])
        state.setdefault("origins", [])

    def cookie_key(c):
        return (c["name"], c["domain"], c["path"])

    existing = {cookie_key(c): i for i, c in enumerate(state["cookies"])}
    for c in new_cookies:
        k = cookie_key(c)
        if k in existing:
            state["cookies"][existing[k]] = c
        else:
            state["cookies"].append(c)
            existing[k] = len(state["cookies"]) - 1
    return state


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed a provider's storage_state with cookies copied from the user's real Chrome profile."
    )
    parser.add_argument("--provider", default="wkinfo", choices=["pkulaw", "wkinfo"])
    parser.add_argument(
        "--domain",
        action="append",
        dest="domains",
        default=[],
        help="Domain substring to match (repeatable). Defaults to the provider's own domain(s).",
    )
    parser.add_argument("--chrome-profile", default="Default", help="Chrome profile directory name")
    parser.add_argument("--cookies-db", help="Override path to the Chrome Cookies sqlite file")
    parser.add_argument("--output", help="Override output storage_state path (defaults to the provider's state_path)")
    args = parser.parse_args()

    provider = get_provider(args.provider)
    domains = list(args.domains)
    if not domains:
        from urllib.parse import urlparse

        domains = [urlparse(provider.home_url).netloc]

    cookies_db = pathlib.Path(args.cookies_db) if args.cookies_db else default_chrome_cookies_db(args.chrome_profile)
    if not cookies_db.exists():
        print(f"seed_cookies_from_chrome.py: no Cookies db at {cookies_db}", file=sys.stderr)
        return 1

    output_path = pathlib.Path(args.output) if args.output else provider.state_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cookies = extract_cookies(cookies_db, domains)
    if not cookies:
        print(f"seed_cookies_from_chrome.py: no cookies found for domains {domains} in {cookies_db}", file=sys.stderr)
        return 1

    state = merge_into_state(output_path, cookies)
    output_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    status = common.session_state_status(provider)
    common.print_digest(
        "ok" if status["fresh"] else "session-stale",
        {
            "provider": provider.name,
            "domains": ", ".join(domains),
            "cookies_seeded": len(cookies),
            "output": str(output_path.resolve()),
            "session": ", ".join(
                f"{c['name']}({common.fmt_expiry(c['expires'])}{'' if c['alive'] else ' EXPIRED'})"
                for c in status["session_cookies"]
            ) or "none of the login-bearing cookies were found",
        },
    )
    if not status["fresh"]:
        print(
            "seed_cookies_from_chrome.py: seeded, but the login-bearing cookies are missing or "
            "already expired — log into the site in your normal Chrome, then re-run",
            file=sys.stderr,
        )
        return common.EXIT_SESSION_STALE
    return common.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
