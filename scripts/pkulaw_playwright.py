#!/usr/bin/env python3
"""Read 北大法宝 / 威科先行 through a real browser, cheaply.

Supports PKULAW and WKINFO via --provider.

Output policy: the payload goes to a *file*, stdout gets a ~12-line digest.
Returning whole-page text to the model is what made a successful search cost
tens of thousands of tokens, so `--format text` is opt-in, not the default.

Cheapest to most expensive, pick the smallest that answers the question:
  --format rows          structured result list, ~30 tokens per hit
  --fields 本院认为,裁判结果  just those sections of a detail page
  --format digest        full text to a file + a digest (default)
  --format text          full text on stdout (last resort)
"""
import argparse
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

import common
from common import (
    EXIT_LOGIN_GATE,
    EXIT_OK,
    EXIT_SESSION_STALE,
    EXIT_VALIDATION,
    SkillError,
)
from providers import get_provider, provider_for_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read PKULAW/WKINFO through a real browser.")
    parser.add_argument("target", nargs="?", help="URL or keyword string")
    parser.add_argument("--search", help="Search with this keyword string")
    parser.add_argument("--provider", default="pkulaw", choices=["pkulaw", "wkinfo"])
    parser.add_argument("--library", default="pfnl", help="case aliases (pfnl/case/judgment-documents) or law aliases (law/chl/legislation)")
    parser.add_argument("--search-keyword-type", help="Search field: Fulltext or Title")
    parser.add_argument("--match-type", default="Exact", help="PKULAW match type (ignored by wkinfo)")
    parser.add_argument("--range-type", default="Piece", help="PKULAW range type (ignored by wkinfo)")
    parser.add_argument("--storage-state", help="Playwright storage_state JSON; defaults to the provider's saved session")
    parser.add_argument("--headful", action="store_true", help="Force a visible window (always on where the provider requires it)")
    parser.add_argument("--wait-ms", type=int, default=5000)
    parser.add_argument("--scrolls", type=int, default=None,
                        help="Wheel steps to trigger lazy-loaded body text "
                             "(default: 6 when reading a document, 0 for --format rows)")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--expect", action="append", default=[], help="Marker that must appear in the extracted text")
    parser.add_argument("--min-bytes", type=int, default=200)
    parser.add_argument("--format", choices=["digest", "rows", "text", "html", "json"], default="digest")
    parser.add_argument("--rows-limit", type=int, default=20, help="Max result rows for --format rows")
    parser.add_argument("--fields", help="Comma-separated sections to keep, e.g. 本院认为,裁判结果")
    parser.add_argument("--field-chars", type=int, default=4000, help="Max chars per extracted section")
    parser.add_argument("--urls-file", help="Batch mode: one URL per line, one browser launch for all of them")
    parser.add_argument("--out", help="Write the payload here (default: a file under the skill's out/ dir)")
    parser.add_argument("--out-dir", help="Batch mode output directory")
    parser.add_argument("--screenshot", help="Write a screenshot to this file")
    common.add_browser_args(parser)
    return parser


def read_page(page, url: str, args) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page.wait_for_timeout(args.wait_ms)
    scroll_in_body(page, default_scrolls(args))
    return {
        "title": page.title(),
        "final_url": page.url,
        "text": page.locator("body").inner_text(timeout=15000),
    }


def default_scrolls(args) -> int:
    """List pages need no scrolling; documents do. --scrolls overrides both."""
    if args.scrolls is not None:
        return args.scrolls
    return 0 if args.format == "rows" else 6


def scroll_in_body(page, times: int) -> None:
    """WKINFO detail pages lazy-load the judgment body.

    Without this the DOM holds only the navigation strip, which repeats the
    section headings verbatim — so `--fields 本院认为` extracts the table of
    contents and reports success. extract_sections() already prefers the
    longest occurrence of a heading, but that cannot help when the nav strip
    is the *only* occurrence present.
    """
    for _ in range(max(0, times)):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(700)
    if times > 0:
        page.mouse.wheel(0, -4000 * times)


def gate_exit_code(provider, stale: bool, attached: bool) -> tuple[int, str, str]:
    """A gate on a stale session is a chore; a gate on a fresh one is a bug.

    Collapsing both into one code is what made the agent guess, and guessing is
    what escalated to screenshot-driven browsing."""
    if attached:
        return (EXIT_SESSION_STALE, "the attached Chrome is not logged in",
                f"python3 pkulaw_chrome.py --login {provider.name}")
    if stale:
        return (EXIT_SESSION_STALE, "saved session is expired or missing",
                f"python3 pkulaw_doctor.py --provider {provider.name}")
    return (EXIT_LOGIN_GATE, "login gate despite an apparently fresh session",
            f"python3 pkulaw_check_login.py --provider {provider.name}")


def handle_single(page, url: str, provider, library: str, args, state_fresh: bool, attached: bool) -> int:
    got = read_page(page, url, args)
    text, title, final_url = got["text"], got["title"], got["final_url"]

    if args.screenshot:
        page.screenshot(path=args.screenshot, full_page=True)

    verdict, note = common.classify_navigation(provider, url, title, final_url, text)
    if verdict == common.EXIT_ANTIBOT:
        raise SkillError(
            f"{provider.name} served its anti-bot shell instead of content",
            common.EXIT_ANTIBOT,
            "this provider must run in a visible browser; use --browser attach or --headful",
        )
    if verdict == EXIT_LOGIN_GATE:
        code, why, hint = gate_exit_code(provider, not state_fresh, attached)
        detail = f"{note} ({why})" if note else why
        raise SkillError(f"{provider.name} login gate: {detail}", code, hint)
    # Entitlement walls still carry usable metadata, so write what there is and
    # report the shortfall rather than throwing the page away.
    partial = verdict == common.EXIT_PARTIAL

    if args.format == "rows":
        rows = common.parse_rows(page, provider, library, args.rows_limit)
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        if args.out:
            common.write_payload(pathlib.Path(args.out), payload)
        common.print_digest(
            "ok",
            {"provider": provider.name, "mode": args.mode, "library": library,
             "url": final_url, "rows": len(rows)},
        )
        if not rows:
            print(
                "rows  0 — no anchors matched this library's detail-URL pattern; "
                "re-run with --format digest to see what the page actually is",
                file=sys.stderr,
            )
        print(payload)
        return EXIT_OK

    payload = text
    missing: list[str] = []
    if args.fields:
        wanted = [f.strip() for f in args.fields.split(",") if f.strip()]
        payload, missing = common.extract_sections(
            text, wanted, provider.section_headers[library], args.field_chars
        )
        if not payload:
            raise SkillError(
                f"none of the requested sections were found: {', '.join(wanted)}"
                + (" — the page is an entitlement stub, so the text is not there to find"
                   if partial else ""),
                common.EXIT_PARTIAL if partial else EXIT_VALIDATION,
                f"python3 pkulaw_chrome.py --login {provider.name}" if partial
                else "re-run without --fields to see the document's actual headings",
            )

    if args.format == "html":
        payload = page.content()

    valid, reason = common.looks_valid(text, args.expect, args.min_bytes)
    if not valid:
        raise SkillError(reason, EXIT_VALIDATION, "check the URL, or widen --expect / lower --min-bytes")

    out_path = pathlib.Path(args.out) if args.out else common.default_out_path(
        provider.name, final_url, "html" if args.format == "html" else "txt"
    )

    if args.format == "text":
        # --out used to be dropped here, so `--format text --out FILE` wrote
        # nothing and only the stdout copy survived.
        if args.out:
            print(f"saved: {common.write_payload(out_path, payload)}", file=sys.stderr)
        sys.stdout.write(payload)
        return EXIT_OK

    saved = common.write_payload(out_path, payload)

    status = common.EXIT_MEANING[common.EXIT_PARTIAL] if partial else "ok"
    fields = {
        "provider": provider.name,
        "mode": args.mode,
        "library": library,
        "url": final_url,
        "title": title[:90],
        "bytes": len(payload.encode("utf-8", errors="ignore")),
        "sections": args.fields if args.fields else None,
        "missing": ", ".join(missing) if missing else None,
        "markers": " ".join(f"{m}✓" for m in args.expect) if args.expect else None,
        "saved": saved,
        "note": ("metadata only — the site withheld the full text; "
                 f"python3 pkulaw_chrome.py --login {provider.name}") if partial else None,
    }
    if args.format == "json":
        print(json.dumps({**{k: str(v) for k, v in fields.items() if v is not None},
                          "status": status}, ensure_ascii=False, indent=2))
    else:
        common.print_digest(status, fields, preview=payload[:400])
    return common.EXIT_PARTIAL if partial else EXIT_OK


def handle_batch(session, urls: list[str], provider, library: str, args, state_fresh: bool, attached: bool) -> int:
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else common.default_out_path(
        provider.name, "".join(urls), "d"
    ).with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)

    page = session.new_page()
    results, worst = [], EXIT_OK
    try:
        for i, url in enumerate(urls, 1):
            try:
                got = read_page(page, url, args)
                verdict, _note = common.classify_navigation(
                    provider, url, got["title"], got["final_url"], got["text"]
                )
                if verdict != EXIT_OK:
                    code = verdict
                    if verdict == EXIT_LOGIN_GATE:
                        code, _why, _hint = gate_exit_code(provider, not state_fresh, attached)
                    results.append((i, common.EXIT_MEANING[code], 0, url, ""))
                    worst = max(worst, code)
                    continue
                payload, status = got["text"], "ok"
                if args.fields:
                    wanted = [f.strip() for f in args.fields.split(",") if f.strip()]
                    sections, _missing = common.extract_sections(
                        payload, wanted, provider.section_headers[library], args.field_chars
                    )
                    # Falling back to the whole page is fine; calling that "ok"
                    # is not — a batch of zero-byte "ok" rows reads as success.
                    if sections:
                        payload = sections
                    else:
                        status = "no-sections"
                        worst = max(worst, common.EXIT_VALIDATION)
                written = common.write_payload(out_dir / f"{i:03d}.txt", payload)
                size = len(payload.encode("utf-8", errors="ignore"))
                if size < args.min_bytes:
                    status = "too-small"
                    worst = max(worst, common.EXIT_VALIDATION)
                results.append((i, status, size, url, str(written)))
            except Exception as exc:  # one bad URL must not sink the batch
                results.append((i, f"error: {str(exc)[:60]}", 0, url, ""))
                worst = max(worst, common.EXIT_ERROR)
    finally:
        try:
            page.close()
        except Exception:
            pass

    ok = sum(1 for r in results if r[1] == "ok")
    common.print_digest(
        "ok" if worst == EXIT_OK else common.EXIT_MEANING[worst],
        {"provider": provider.name, "mode": args.mode, "urls": len(urls),
         "ok": ok, "failed": len(urls) - ok, "out_dir": out_dir.resolve()},
    )
    for i, status, size, url, path in results:
        print(f"{i:03d}  {status:<16} {size:>7}  {pathlib.Path(path).name or '-':<10} {url}")
    return worst


def main() -> int:
    args = build_parser().parse_args()

    urls: list[str] = []
    if args.urls_file:
        urls = [ln.strip() for ln in pathlib.Path(args.urls_file).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        if not urls:
            print("pkulaw_playwright.py: --urls-file is empty", file=sys.stderr)
            return common.EXIT_ERROR

    target = args.search or args.target
    if not target and not urls:
        build_parser().error("provide a URL/target, --search, or --urls-file")

    try:
        provider = get_provider(args.provider)
        probe = target if target else urls[0]
        # A bare URL identifies the provider more reliably than the default flag.
        if not args.search and probe.startswith("http"):
            detected = provider_for_url(probe)
            if detected is not None:
                provider = detected
        if provider.api_script:
            print(f"pkulaw_playwright.py: {provider.name} renders everything from a JSON API — "
                  f"use {provider.api_script}", file=sys.stderr)
            return common.EXIT_ERROR
        library = provider.normalize_library(args.library)
    except ValueError as exc:
        build_parser().error(str(exc))

    search_keyword_type = args.search_keyword_type or provider.default_keyword_type[library]
    state = common.session_state_status(provider)

    if args.search:
        url = provider.build_search_url(
            keywords=target,
            library=library,
            search_keyword_type=search_keyword_type,
            match_type=args.match_type,
            range_type=args.range_type,
        )
    else:
        url = target

    try:
        with sync_playwright() as p:
            with common.browser_session(p, provider, args) as session:
                args.mode = session.mode
                # An attached Chrome carries its own live cookies, so the saved
                # state file's freshness says nothing about it.
                state_fresh = True if session.attached else state["fresh"]
                if urls:
                    return handle_batch(session, urls, provider, library, args, state_fresh, session.attached)
                page = session.new_page()
                try:
                    return handle_single(page, url, provider, library, args, state_fresh, session.attached)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
    except Exception as exc:
        return common.fail("pkulaw_playwright.py", exc)


if __name__ == "__main__":
    raise SystemExit(main())
