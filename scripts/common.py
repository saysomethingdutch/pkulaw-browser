#!/usr/bin/env python3
"""Browser plumbing shared by every script in this skill.

Nothing here knows about a specific legal database; per-site URL building,
login-gate detection and row parsing live in providers.py.

Two decisions in this file exist specifically to stop agent-side token bleed,
and are worth not undoing:

1. **Every path is absolute** (see ``STATE_ROOT``). The old relative
   ``tmp/...`` defaults resolved against the *caller's* CWD, so a run started
   from a new project directory silently found no saved session, read the
   resulting login gate as a failure, and escalated to screenshot-driven
   browsing. Nine orphaned state files scattered across project folders were
   the evidence.
2. **Extractors write the payload to a file and print a short digest.**
   Returning whole-page ``inner_text()`` on stdout is what made even a
   *successful* run cost tens of thousands of tokens.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager

def find_chrome() -> str:
    """Locate real Google Chrome on macOS, Windows or Linux.

    Cross-platform on purpose: the attach workflow is the one path that needs no
    cookie decryption, so it has to work on Windows too. The cookie-copying
    fallback does not travel — it needs the macOS Keychain or Windows DPAPI, and
    that is exactly where a Windows run has been observed to give up and fall
    back to screenshot-driven browsing.
    """
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(pathlib.Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            rf"{local}\Google\Chrome\Application\chrome.exe" if local else "",
        ]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium"]

    for path in candidates:
        if path and pathlib.Path(path).exists():
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return candidates[0] if candidates else "google-chrome"


DEFAULT_CHROME = find_chrome()

# --------------------------------------------------------------------------
# Absolute state layout. Override the root with PKULAW_BROWSER_STATE.
# --------------------------------------------------------------------------

STATE_ROOT = pathlib.Path(
    os.environ.get("PKULAW_BROWSER_STATE", pathlib.Path.home() / ".agents/state/pkulaw-browser")
).expanduser().resolve()

BROWSERS_PATH = STATE_ROOT / "ms-playwright"
OUT_DIR = STATE_ROOT / "out"
# The dedicated Chrome profile we attach to over CDP. It must not be the user's
# Default profile: Chrome >= 136 refuses --remote-debugging-port there.
CDP_PROFILE = STATE_ROOT / "chrome-profile"
CDP_PORT = int(os.environ.get("PKULAW_BROWSER_CDP_PORT", "9333"))

# --------------------------------------------------------------------------
# Exit codes. The agent branches on these instead of parsing prose.
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1            # unexpected failure
EXIT_LOGIN_GATE = 2       # reached the site, got its login wall
EXIT_SESSION_STALE = 3    # no saved session, or its session cookies have expired
EXIT_BROWSER_BLOCKED = 4  # browser could not start (sandbox, missing binary, dead CDP port)
EXIT_ANTIBOT = 5          # anti-bot shell instead of content (PKULAW run headless)
EXIT_VALIDATION = 6       # real page, but missing an --expect marker or too small
EXIT_PARTIAL = 7          # metadata rendered, full text withheld by entitlement

EXIT_MEANING = {
    EXIT_OK: "ok",
    EXIT_ERROR: "error",
    EXIT_LOGIN_GATE: "login-gate",
    EXIT_SESSION_STALE: "session-stale",
    EXIT_BROWSER_BLOCKED: "browser-blocked",
    EXIT_ANTIBOT: "antibot-shell",
    EXIT_VALIDATION: "validation-failed",
    EXIT_PARTIAL: "content-truncated",
}


class SkillError(Exception):
    """An error we already know how to explain, carrying its own exit code."""

    def __init__(self, message: str, code: int = EXIT_ERROR, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


# --------------------------------------------------------------------------
# Browser selection
# --------------------------------------------------------------------------


def playwright_default_cache() -> pathlib.Path:
    """Where `playwright install` puts browsers when nobody overrides it."""
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library/Caches/ms-playwright"
    if os.name == "nt":
        return pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "ms-playwright"
    return pathlib.Path.home() / ".cache/ms-playwright"


def _dir_has_chromium(path: pathlib.Path) -> bool:
    if not path.exists():
        return False
    return any(p.name.startswith("chromium-") for p in path.iterdir() if p.is_dir())


def has_local_chromium(browsers_path: pathlib.Path) -> bool:
    """A plain `playwright install chromium` counts.

    Checking only this skill's own directory meant a machine with a perfectly
    normal Playwright install reported "no Chromium" and fell through to system
    Chrome — or to nothing, on a machine without Chrome. That is a portability
    trap rather than a real missing dependency.
    """
    return _dir_has_chromium(browsers_path) or _dir_has_chromium(playwright_default_cache())


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def cdp_alive(port: int, timeout: float = 1.5) -> dict | None:
    """Returns the /json/version payload if a debuggable Chrome is listening."""
    try:
        with urllib.request.urlopen(f"{cdp_endpoint(port)}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def resolve_browser_choice(requested: str, browsers_path: pathlib.Path, port: int) -> str:
    """`auto` prefers the user's already-logged-in Chrome over any fresh profile.

    Attaching sidesteps the whole login problem: the session lives in a real
    Chrome the user logged into by hand, so NTLM/Negotiate SSO portals that
    Playwright cannot authenticate against never enter the picture.
    """
    if requested != "auto":
        return requested
    if cdp_alive(port):
        return "attach"
    return "chromium" if has_local_chromium(browsers_path) else "chrome"


def build_launch_kwargs(browser_choice: str, chrome_path: str) -> dict:
    if browser_choice == "chrome":
        return {"executable_path": chrome_path}
    return {}


def prepare_playwright_env(browsers_path: pathlib.Path) -> None:
    """Point Playwright at this skill's browser dir only if it really has one.

    Setting it unconditionally hid an ordinary `playwright install` from
    Playwright itself, so the skill demanded a second copy of Chromium in its
    own directory on every new machine.
    """
    if _dir_has_chromium(browsers_path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)


class Session:
    """A ready-to-drive browser context plus how we got it."""

    def __init__(self, context, mode: str, headful: bool, storage_state: str | None, attached: bool):
        self.context = context
        self.mode = mode
        self.headful = headful
        self.storage_state = storage_state
        self.attached = attached

    def new_page(self):
        return self.context.new_page()


@contextmanager
def browser_session(p, provider, args):
    """Open a browser the right way for this provider, and clean up safely.

    The attach path reuses ``browser.contexts[0]`` on purpose. ``new_context()``
    on a CDP connection creates an incognito-style context that carries none of
    the profile's cookies, which would throw away the very login we attached for.
    We also never close an attached browser — that is the user's own Chrome.
    """
    browsers_path = pathlib.Path(args.browsers_path)
    port = getattr(args, "cdp_port", CDP_PORT)
    choice = resolve_browser_choice(args.browser, browsers_path, port)
    headful = bool(getattr(args, "headful", False)) or provider.force_headful

    if choice == "attach":
        if not cdp_alive(port):
            raise SkillError(
                f"no debuggable Chrome on port {port}",
                EXIT_BROWSER_BLOCKED,
                f"start it with: python3 {pathlib.Path(__file__).with_name('pkulaw_chrome.py')} --start",
            )
        browser = p.chromium.connect_over_cdp(cdp_endpoint(port))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        session = Session(context, f"attach:{port}", True, None, True)
        try:
            yield session
        finally:
            pass  # never close the user's Chrome
        return

    prepare_playwright_env(browsers_path)
    storage_state = getattr(args, "storage_state", None)
    if storage_state is None and provider.state_path.exists():
        storage_state = str(provider.state_path)

    browser = p.chromium.launch(headless=not headful, **build_launch_kwargs(choice, args.chrome))
    context = browser.new_context(storage_state=storage_state, viewport={"width": 1440, "height": 1200})
    session = Session(context, f"launch:{choice}", headful, storage_state, False)
    try:
        yield session
    finally:
        try:
            context.close()
            browser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Saved-session freshness
# --------------------------------------------------------------------------


def session_state_status(provider) -> dict:
    """Report whether a provider's saved session is usable *before* spending a
    browser launch on finding out.

    WKINFO is the reason this exists: its `connect.sid` expires about a day
    after issue and the `autologin` family about a week, so a seeded state file
    goes quietly stale and every later run reads as a mysterious login gate.

    The verdict is driven by ``primary_cookies`` — the ones that actually carry
    the login — not by every login-adjacent cookie. A first cut counted the
    session fresh if *any* of them was alive, which called a WKINFO state file
    healthy on the strength of `loginId`, a display preference, while
    `connect.sid` and the whole `autologin` family had been dead for weeks.
    Three levels, because two would force a wrong call in the middle case:

      fresh   every primary present is unexpired          -> query away
      suspect some primaries alive, some expired          -> works or doesn't; --live decides
      stale   no primaries present, or all expired        -> log in again first
    """
    path = provider.state_path
    result = {
        "path": str(path),
        "exists": path.exists(),
        "cookies": 0,
        "session_cookies": [],
        "level": "stale",
        "fresh": False,
        "usable": False,
        "stale_reason": "",
    }
    if not path.exists():
        result["stale_reason"] = "no saved session"
        return result

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["stale_reason"] = f"unreadable state file: {exc}"
        return result

    cookies = state.get("cookies", [])
    result["cookies"] = len(cookies)
    now = time.time()
    wanted = {n.lower() for n in provider.session_cookies}
    found = {}
    for c in cookies:
        name = c.get("name", "")
        if name.lower() not in wanted:
            continue
        expires = c.get("expires", -1) or -1
        # A session cookie (no expiry) restores fine from a storage_state file.
        alive = expires <= 0 or expires > now
        prev = found.get(name)
        if prev is None or expires > prev["expires"]:
            found[name] = {"name": name, "expires": expires, "alive": alive}

    result["session_cookies"] = sorted(found.values(), key=lambda c: c["name"])

    primaries = {n.lower() for n in provider.primary_cookies}
    present = [c for n, c in found.items() if n.lower() in primaries]
    if not present:
        result["stale_reason"] = f"no login-bearing cookie present ({', '.join(provider.primary_cookies)})"
        return result

    dead = [c for c in present if not c["alive"]]
    if len(dead) == len(present):
        newest = max(c["expires"] for c in present)
        when = time.strftime("%Y-%m-%d", time.localtime(newest)) if newest > 0 else "unknown"
        result["stale_reason"] = f"every login-bearing cookie expired (newest {when})"
        return result

    result["usable"] = True
    if dead:
        result["level"] = "suspect"
        result["stale_reason"] = "expired: " + ", ".join(
            f"{c['name']}({fmt_expiry(c['expires'])})" for c in dead
        )
        return result

    result["level"] = "fresh"
    result["fresh"] = True
    return result


def fmt_expiry(expires: float) -> str:
    if not expires or expires <= 0:
        return "session"
    return time.strftime("%Y-%m-%d", time.localtime(expires))


# --------------------------------------------------------------------------
# Validation and page classification
# --------------------------------------------------------------------------


def looks_valid(text: str, expected: list[str], min_bytes: int) -> tuple[bool, str]:
    size = len(text.encode("utf-8", errors="ignore"))
    if size < min_bytes:
        return False, f"content too small: {size} bytes < {min_bytes}"
    for marker in expected:
        if marker not in text:
            return False, f"missing expected marker: {marker}"
    return True, f"validated {size} bytes"


def search_query_dropped(requested: str, final: str) -> bool:
    """Catch the gate that doesn't look like one.

    An unauthenticated WKINFO search does not redirect and does not show a login
    modal. It silently discards the query and serves a generic list of recent
    judgments — real content, correct path, plenty of bytes, every "does this
    look like a page?" check passes. The only tell is that the search terms are
    gone from the final URL. Answering a case-number lookup with ten unrelated
    judgments is worse than answering with an error.

    Deliberately conservative: it fires only when *no* search-bearing parameter
    survives, so a site that merely re-encodes its own query is not flagged.
    """
    from urllib.parse import parse_qsl, urlparse

    requested_query = urlparse(requested).query
    if not requested_query:
        return False
    keys = {"simple", "q", "Keywords", "keywords", "tip"}
    asked = {k: v for k, v in parse_qsl(requested_query) if k in keys and v}
    if not asked:
        return False
    got = {k: v for k, v in parse_qsl(urlparse(final).query) if k in keys and v}
    return not any(got.get(k) for k in asked)


def request_path_dropped(requested: str, final: str) -> bool:
    """Did the site throw away the path we asked for and bounce us to its root?

    Must be judged against the request. WKINFO's gate detector originally
    treated "final path is empty" as a gate on its own, which meant a perfectly
    healthy visit to the site's *home page* reported as a login gate — and that
    is exactly the page a session snapshot loads, so a fresh login certified
    itself as failed.
    """
    from urllib.parse import urlparse

    req, fin = urlparse(requested), urlparse(final)
    if req.netloc and fin.netloc and req.netloc != fin.netloc:
        return False
    return bool(req.path.rstrip("/")) and not fin.path.rstrip("/")


def classify_navigation(provider, requested: str, title: str, final_url: str, body_text: str) -> tuple[int, str]:
    """The single verdict function. Page content *and* what the URL did to us.

    Callers used to check the page and forget the URL-level tells, each of
    which is a silent wrong answer rather than an error. One function so there
    is nowhere left to forget them.
    """
    verdict = classify_page(provider, title, final_url, body_text)
    if verdict != EXIT_OK:
        return verdict, ""
    if request_path_dropped(requested, final_url):
        return EXIT_LOGIN_GATE, "the requested path was discarded and the site bounced to its root"
    if search_query_dropped(requested, final_url):
        return EXIT_LOGIN_GATE, "the search query was discarded — these are NOT results for that query"
    return EXIT_OK, ""


def classify_page(provider, title: str, final_url: str, body_text: str) -> int:
    """Order matters.

    Anti-bot before login-gate: a PKULAW anti-bot shell carries no login
    markers, and calling it a gate sends the agent off to re-authenticate a
    session that was never the problem.

    Entitlement last, and separate from both: WKINFO recognises an institution
    by IP and renders a complete-looking detail page — title, 案由, 案号, court —
    with the judgment body replaced by 很抱歉，您没有权限查看全文内容. Nothing
    about it reads as a failure, so an agent that only checks for gates will
    happily answer 本院认为 questions from a page that contains no 本院认为.
    """
    if provider.detect_antibot(title, final_url, body_text):
        return EXIT_ANTIBOT
    if provider.detect_login_gate(title, final_url, body_text):
        return EXIT_LOGIN_GATE
    if provider.detect_partial(title, final_url, body_text):
        return EXIT_PARTIAL
    return EXIT_OK


# --------------------------------------------------------------------------
# Row extraction (search-result lists)
# --------------------------------------------------------------------------

# Rows are found by the site's own per-result element when we know it
# (Provider.row_selector), and only otherwise by climbing up from a detail link.
#
# The climb is not good enough on its own, and the way it fails is dangerous.
# A WKINFO result carries three links: the case title, a "查看详情→" button, and
# a matched-snippet link — and on commentary results the last two point at a
# *different* document than the title does. Deriving rows from links therefore
# reported a real three-hit search as three rows where two were titled
# "查看详情→" and linked to the snippet's document instead of the case. Every
# field was well-formed; the rows were simply about the wrong documents.
#
# With a row element, identity and title come from the result the site itself
# drew, and ctx is that result's own text — so neither a neighbouring row's
# court nor a sibling link's document can leak in.
UI_LABELS = ["查看详情", "查看全部", "更多", "详情", "展开", "收起", "下一页", "上一页", "返回"]

_ROW_JS = """
([pattern, rowSelector, uiLabels]) => {
  const re = new RegExp(pattern);
  const isHit = (el) => re.test(el.getAttribute('href') || '');
  const clean = (s) => (s || '').trim().replace(/\\s+/g, ' ');
  const pathOf = (el) => { try { return new URL(el.href).pathname; } catch (e) { return el.getAttribute('href'); } };
  const isLabel = (t) => uiLabels.some((l) => t.startsWith(l));
  const out = [];
  const seen = new Set();

  // Preferred: the site marks each result with its own element.
  //
  // Inside a row, match any /detail/ link rather than the library's own
  // pattern. A WKINFO case search mixes modules: a 案例评析 result's title links
  // to /case-analysis/detail/, while the judgment it comments on is linked
  // further down as /judgment-documents/detail/. Filtering by the library
  // pattern skips such a row's real title and promotes the commented-on
  // judgment in its place, which silently swaps commentary for a judgment.
  const isDetail = (el) => /\\/detail\\//.test(el.getAttribute('href') || '');
  const rows = rowSelector ? Array.from(document.querySelectorAll(rowSelector)) : [];
  if (rows.length) {
    for (const row of rows) {
      const anchors = Array.from(row.querySelectorAll('a[href]')).filter(isDetail);
      let pick = null;
      for (const a of anchors) {
        const t = clean(a.innerText || a.textContent);
        if (!t || isLabel(t)) continue;
        pick = { url: a.href, title: t };
        break;
      }
      if (!pick) continue;
      const key = new URL(pick.url).pathname;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ url: pick.url, title: pick.title, ctx: clean(row.innerText).slice(0, 600) });
    }
    return out;
  }

  // Fallback for sites whose row element we do not know yet.
  const distinctDocs = (root) => {
    const s = new Set();
    for (const c of root.querySelectorAll('a[href]')) { if (isHit(c)) s.add(pathOf(c)); }
    return s.size;
  };
  const byPath = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    if (!isHit(a)) continue;
    const title = clean(a.innerText || a.textContent);
    if (!title || isLabel(title)) continue;
    const key = pathOf(a);
    if (!byPath.has(key)) byPath.set(key, { url: a.href, title: title, el: a });
  }
  for (const rec of byPath.values()) {
    let node = rec.el, ctx = '';
    for (let i = 0; i < 6 && node.parentElement; i++) {
      const parent = node.parentElement;
      if (distinctDocs(parent) > 1) break;
      node = parent;
      const t = clean(node.innerText);
      if (t.length > rec.title.length + 8) { ctx = t; break; }
    }
    out.push({ url: rec.url, title: rec.title, ctx: ctx.slice(0, 600) });
  }
  return out;
}
"""

CASE_NO_RE = re.compile(r"[（(]\s*\d{4}\s*[）)][^\s，,；;。、'\"]{1,30}?号")
DOC_NO_RE = re.compile(r"[一-鿿]{2,12}〔\d{4}〕\d{1,6}号")
# The lookbehind keeps the court name from starting mid-sentence. Without it,
# "…债权人依法向人民法院申请…" yielded a court called
# "享有要求被执行人继续履行债务及依法向人民法院", and every citation of a
# 《最高人民法院…》 interpretation was read as the deciding court.
COURT_RE = re.compile(
    r"(?<![一-鿿《〈])[一-鿿]{2,15}?(?:人民法院|知识产权法院|海事法院|金融法院|军事法院|人民检察院|仲裁委员会)"
)
DATE_RE = re.compile(r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?")
EFFECT_RE = re.compile(r"(现行有效|已被修改|尚未生效|失效|已废止|部分失效)")
# Whether a hit is the judgment itself or commentary about it changes what you
# may cite it for, and a WKINFO case search returns both freely mixed.
DOC_TYPE_RE = re.compile(
    r"(判决书|裁定书|调解书|决定书|通知书|案例评析|裁判要旨|法官评案|典型案例|指导案例|参考案例)"
)


def parse_rows(page, provider, library: str, limit: int) -> list[dict]:
    """One round trip to the DOM, structured rows back.

    Anchored on the detail-URL shape rather than CSS class names, because class
    names on both sites churn and a broken selector here is indistinguishable
    from "no results" — the exact ambiguity that used to trigger a fallback.
    """
    from urllib.parse import urlsplit, urlunsplit

    raw = page.evaluate(
        _ROW_JS,
        [provider.detail_href_pattern[library], provider.row_selector.get(library, ""), UI_LABELS],
    )
    rows = []
    for i, item in enumerate(raw[:limit], 1):
        ctx = item.get("ctx") or ""
        haystack = f"{item['title']} {ctx}"
        # Both sites put the document id in the path, so the query string is
        # search-session tracking — 100+ bytes per row of searchId/index/module
        # noise that the model pays for and never uses. Verified: the bare path
        # loads the same detail page.
        parts = urlsplit(item["url"])
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        row = {"n": i, "title": item["title"][:160], "url": url}
        # Which corpus the hit came from — a judgment and a commentary about it
        # are not interchangeable, and a case search returns both.
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) > 1:
            row["module"] = segments[0]
        if library == "case":
            for key, rx in (("case_no", CASE_NO_RE), ("court", COURT_RE),
                            ("date", DATE_RE), ("doc_type", DOC_TYPE_RE)):
                m = rx.search(haystack)
                if m:
                    row[key] = m.group(0).strip()
        else:
            for key, rx in (("doc_no", DOC_NO_RE), ("date", DATE_RE), ("status", EFFECT_RE)):
                m = rx.search(haystack)
                if m:
                    row[key] = m.group(0).strip()
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Section extraction (detail pages)
# --------------------------------------------------------------------------


def extract_sections(text: str, wanted: list[str], headers: list[str], max_chars: int) -> tuple[str, list[str]]:
    """Return only the requested sections of a judgment or statute.

    `本院认为` + `裁判结果` is often under 10% of a judgment and answers most
    questions, so pulling just those beats returning the whole document.

    Takes the *longest* occurrence of each heading, not the first. A WKINFO
    detail page opens with a navigation strip that lists the section names
    verbatim — "本院认为 裁判结果 审判人员" — so first-match extraction returned
    283 bytes of table-of-contents out of a 49KB judgment and reported success.
    The body occurrence is the one with actual text after it.
    """
    marks: list[tuple[int, str]] = []
    for h in headers:
        start = 0
        while True:
            idx = text.find(h, start)
            if idx < 0:
                break
            marks.append((idx, h))
            start = idx + len(h)
    marks.sort()
    positions = [pos for pos, _h in marks]

    def slice_end(idx: int) -> int:
        for pos in positions:
            if pos > idx:
                return pos
        return len(text)

    chosen: list[tuple[int, int]] = []
    missing = []
    for field in wanted:
        spans, start = [], 0
        while True:
            idx = text.find(field, start)
            if idx < 0:
                break
            spans.append((idx, slice_end(idx)))
            start = idx + len(field)
        if not spans:
            missing.append(field)
            continue
        chosen.append(max(spans, key=lambda s: s[1] - s[0]))

    chunks = []
    for idx, end in sorted(chosen):
        body = text[idx:end].strip()
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n…[truncated, {end - idx} chars total]"
        chunks.append(body)
    return "\n\n".join(chunks), missing


# --------------------------------------------------------------------------
# Output: file for the payload, digest for the model
# --------------------------------------------------------------------------


def default_out_path(provider_name: str, key: str, suffix: str = "txt") -> pathlib.Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return OUT_DIR / f"{provider_name}-{stamp}.{suffix}"


def write_payload(path: pathlib.Path, payload: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path.resolve()


def print_digest(status: str, fields: dict, preview: str = "", stream=sys.stdout) -> None:
    """Twelve-ish lines, never the payload. The file path is the payload."""
    print(f"status  {status}", file=stream)
    width = max((len(k) for k in fields), default=6)
    for k, v in fields.items():
        if v is None or v == "":
            continue
        print(f"{k.ljust(width)}  {v}", file=stream)
    if preview:
        flat = " ".join(preview.split())[:220]
        print(f"{'preview'.ljust(width)}  {flat}", file=stream)


# --------------------------------------------------------------------------
# Error phrasing
# --------------------------------------------------------------------------


def classify_exception(exc: Exception) -> tuple[int, str, str]:
    """(exit_code, message, hint)"""
    if isinstance(exc, SkillError):
        return exc.code, str(exc), exc.hint
    text = str(exc)
    if "MachPortRendezvousServer" in text or "Permission denied (1100)" in text:
        return (
            EXIT_BROWSER_BLOCKED,
            "the sandbox blocked browser startup before the page loaded",
            "retry with elevated permissions, or attach to the user's Chrome (pkulaw_chrome.py --start)",
        )
    if "Crashpad/settings.dat" in text or "Operation not permitted" in text:
        return (
            EXIT_BROWSER_BLOCKED,
            "system Chrome launch was blocked by the sandbox reading Crashpad files",
            f"install a workspace-local Chromium into {BROWSERS_PATH} and retry with --browser chromium",
        )
    if "Executable doesn't exist" in text or "playwright install" in text:
        return (
            EXIT_BROWSER_BLOCKED,
            "Playwright Chromium is not installed",
            f"PLAYWRIGHT_BROWSERS_PATH={BROWSERS_PATH} playwright install chromium",
        )
    if "ECONNREFUSED" in text or "connect_over_cdp" in text:
        return (
            EXIT_BROWSER_BLOCKED,
            "could not attach to a debuggable Chrome",
            "start it with: pkulaw_chrome.py --start",
        )
    return EXIT_ERROR, text, ""


def fail(script: str, exc: Exception) -> int:
    code, message, hint = classify_exception(exc)
    print(f"{script}: {message}", file=sys.stderr)
    if hint:
        print(f"{script}: next -> {hint}", file=sys.stderr)
    print(f"{script}: exit {code} ({EXIT_MEANING.get(code, 'error')})", file=sys.stderr)
    return code


# --------------------------------------------------------------------------
# Shared CLI flags
# --------------------------------------------------------------------------


def add_browser_args(parser) -> None:
    parser.add_argument("--chrome", default=DEFAULT_CHROME, help="Chrome executable path")
    parser.add_argument(
        "--browser",
        choices=["auto", "attach", "chromium", "chrome"],
        default="auto",
        help="auto: attach to the user's debuggable Chrome if it is running, else local Chromium, else system Chrome",
    )
    parser.add_argument("--cdp-port", type=int, default=CDP_PORT, help="Port of the debuggable Chrome to attach to")
    parser.add_argument(
        "--browsers-path",
        default=str(BROWSERS_PATH),
        help="Directory for Playwright-managed browser binaries (absolute by default)",
    )


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def run_quiet(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
