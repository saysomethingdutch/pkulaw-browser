#!/usr/bin/env python3
"""Read 人民法院案例库 (rmfyalk.court.gov.cn) through its JSON API.

The site is jQuery pages over POST /cpws_al_api/api/*; nothing worth reading is
in the HTML, so this script talks to the API the pages talk to:

  * cpwsAl/search — fields 全文/标题/案例编号/关键词/基本案情/裁判理由, ANDed;
    精确 or 模糊; 指导性案例 / 参考案例 / both; sort; up to 100 rows per page,
  * cluster endpoints — 法院/案由/法院级别/审理程序/年份/关键词 facets with hit
    counts *under the current query*, used to turn a name into a filter key,
  * cpwsAl/content — one case, every section a separate field (裁判要旨/裁判要点,
    基本案情, 裁判结果, 裁判理由, 关联索引/相关法条, 案例价值), so --fields costs
    one request per case and no section slicing.

Logged out, every endpoint answers HTTP 200 + {"code":401}. When that happens
and the saved state still holds the 统一账户 SSO cookie (HOLDONKEY), the script
replays the site's own OAuth redirect, which mints a fresh token without a
password — headless included. Only when that fails does a human need to log in
(pkulaw_chrome.py --login rmfyalk).

Like every script here the requests ride a Playwright browser context (shared
cookie jar), never a plain HTTP client. It attaches to the debuggable Chrome when
one is up, else runs headless on the saved state, and re-saves the court.gov.cn
cookies after each run so the sliding 1h expiry keeps moving.

Usage:
  # compare phrasings by hit count before fetching anything
  python3 rmfyalk_api.py --search '劳动关系 退休' --count-only

  # 裁判理由 only, 参考案例, Beijing courts, 裁判要旨 per hit for triage
  python3 rmfyalk_api.py --reason '事实劳动关系' --lib ck --court 北京市 --frag

  # one case by 入库编号 or 案号, with the sections that answer the question
  python3 rmfyalk_api.py --no 2024-12-3-007-006 --fields 裁判要旨,裁判理由
  python3 rmfyalk_api.py --case-no '（2022）京02行终909号' --fields all

  # facet keys and counts under a query (what --court/--cause resolve against)
  python3 rmfyalk_api.py --search 劳动关系 --find-nodes court 北京

  # re-mint an expired token from the SSO cookie and save it
  python3 rmfyalk_api.py --refresh

Output: one JSON object per case to --out (default under the state dir's out/),
a one-line summary per hit to stderr. Full text goes to the file, never to the
model. Cite by 入库编号 (`no`): the `id` changes on every response.

Exit codes follow the skill-wide table: 0 ok, 1 error (bad input, ambiguous
filter, API refused — read the message), 3 not logged in and the SSO cookie could
not re-mint a token, 4 browser could not start.
"""
import argparse
import base64
import html
import json
import os
import pathlib
import re
import sys
import time

from playwright.sync_api import sync_playwright

import common
from common import COURT_RE, SkillError
from providers import (
    RMFYALK_API,
    RMFYALK_CASE_NO_KEY,
    RMFYALK_CONTENT_FIELDS,
    RMFYALK_FACETS,
    RMFYALK_FIELD_ALIASES,
    RMFYALK_HOME,
    RMFYALK_LIBS,
    RMFYALK_SORTS,
    RMFYALK_TYPES,
    get_provider,
    rmfyalk_build_search_body,
    rmfyalk_section_labels,
)

# Not pkulaw_api.py's 2/5: through common.fail those print as login-gate and
# antibot-shell, which sends the agent to fix a problem it does not have.
EXIT_OK = 0
EXIT_USAGE = common.EXIT_ERROR
EXIT_LOGIN = common.EXIT_SESSION_STALE
EXIT_API = common.EXIT_ERROR

PAGE_CAP = 100          # 100 per page verified; the UI offers up to 50
TOKEN_COOKIE = "faxin-cpws-al-token"
SSO_COOKIE = "HOLDONKEY"
LOGIN_HINT = "python3 pkulaw_chrome.py --login rmfyalk   # then --save-state rmfyalk"

HEADERS = {"Content-Type": "application/json;charset=UTF-8"}

PAGE_FETCH_JS = """async ({url, body}) => {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json;charset=UTF-8'},
                              credentials: 'include', body: JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""

STRIP_TAG = re.compile(r"<[^>]+>")
NET_ERROR = re.compile(r"ETIMEDOUT|ECONNRESET|ECONNREFUSED|ERR_TIMED_OUT|ERR_CONNECTION|socket hang up|Timeout",
                       re.IGNORECASE)


def clean(s) -> str:
    """Site HTML -> plain text: <br>/</p> become line breaks, ###### (the
    法条 / 审级 separator inside 关联索引) too, full-width indents go."""
    if isinstance(s, list):
        s = "；".join(str(x) for x in s)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = html.unescape(STRIP_TAG.sub("", s)).replace("######", "\n")
    lines = (ln.strip(" 　\t\r") for ln in s.splitlines())
    return "\n".join(ln for ln in lines if ln)


def one_line(s: str) -> str:
    return " ".join(clean(s).split())


class Api:
    """One logged-in context. Requests go through context.request; if that is
    refused they retry as a same-origin fetch inside a page on the site."""

    def __init__(self, session):
        self.session = session
        self.context = session.context
        self.page = None
        self._via_page = False
        self.refreshed = False

    def _ensure_page(self):
        if self.page is None:
            self.page = self.context.new_page()
            self.page.goto(RMFYALK_HOME, wait_until="domcontentloaded", timeout=60000)
        return self.page

    def _request(self, url: str, body: dict):
        """context.request.post with backoff. Connects to the site time out
        intermittently — observed 2026-10-29 after a burst of test calls, about
        one in three for minutes, no error page. WAF throttle or network path:
        not established. Either way a short wait fixes it."""
        last = None
        for wait in (0, 5, 20):
            if wait:
                print(f"network error, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
            try:
                return self.context.request.post(url, headers=HEADERS, timeout=30000,
                                                 data=json.dumps(body, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                if not NET_ERROR.search(str(exc)):
                    raise
                last = exc
        raise SkillError(f"the site stopped answering: {str(last).splitlines()[0]}", EXIT_API,
                         "connects to this site time out in spells — wait a few minutes, then send fewer requests")

    def post(self, endpoint: str, body: dict) -> dict:
        url = RMFYALK_API + endpoint
        if not self._via_page:
            r = self._request(url, body)
            if r.status == 200:
                try:
                    return r.json()
                except Exception:
                    pass
            print(f"context.request {endpoint} -> {r.status}; retrying through a page", file=sys.stderr)
            self._via_page = True
        res = self._ensure_page().evaluate(PAGE_FETCH_JS, {"url": url, "body": body})
        if res["status"] != 200:
            raise SkillError(f"{endpoint}: HTTP {res['status']}", EXIT_API)
        return json.loads(res["text"])

    def call(self, endpoint: str, body: dict) -> dict:
        """post() that turns the JSON-level gate into an exit code."""
        d = self.post(endpoint, body)
        code = str(d.get("code"))
        if code == "401":
            raise SkillError(f"{endpoint}: {d.get('msg')} (code 401)", EXIT_LOGIN, LOGIN_HINT)
        if code != "0":
            raise SkillError(f"{endpoint}: code {code} {d.get('msg')}", EXIT_API)
        return d

    # -- login ------------------------------------------------------------

    def logged_in(self) -> bool:
        return str(self.post("user/getUserInfo", {}).get("code")) == "0"

    def ensure_login(self, force_refresh: bool = False) -> None:
        if not force_refresh and self.logged_in():
            return
        if not any(c["name"] == SSO_COOKIE for c in self.context.cookies()):
            raise SkillError("not logged in, and no 统一账户 SSO cookie to re-mint a token from",
                             EXIT_LOGIN, LOGIN_HINT)
        self.refresh()

    def refresh(self) -> None:
        """Replay goLogin(): getGdLoginUrl -> account.court.gov.cn/oauth/authorize.
        With a live SSO session it bounces straight back to rmfyalk with ?code=,
        and the page's own script trades the code for a token cookie."""
        d = self.post("user/getGdLoginUrl", {})
        if str(d.get("code")) != "0" or not d.get("data"):
            raise SkillError(f"getGdLoginUrl: {d.get('msg')}", EXIT_API)
        page = self._ensure_page()
        page.goto(d["data"].replace("http:", "https:"), wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            page.wait_for_timeout(500)
            if "rmfyalk.court.gov.cn" in page.url and self.logged_in():
                self.refreshed = True
                print("refresh: SSO session re-minted the token", file=sys.stderr)
                return
            if "account.court.gov.cn" in page.url and page.url.endswith("/login"):
                break
        raise SkillError("the 统一账户 SSO session has expired too — a human login is needed",
                         EXIT_LOGIN, LOGIN_HINT)

    def token_expiry(self) -> tuple[str, str]:
        """(cookie expiry, JWT exp) as local times. Never the token itself."""
        for c in self.context.cookies():
            if c["name"] != TOKEN_COOKIE:
                continue
            jwt = ""
            try:
                part = c["value"].split(".")[1]
                exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
                jwt = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else ""
            except Exception:
                pass
            cookie = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["expires"])) if c["expires"] > 0 else "session"
            return cookie, jwt
        return "", ""

    def save_state(self, path: pathlib.Path) -> None:
        """Only court.gov.cn cookies: an attached Chrome also holds every other
        site's login, and none of that belongs in this file."""
        state = self.context.storage_state()
        state["cookies"] = [c for c in state["cookies"] if c["domain"].lstrip(".").endswith("court.gov.cn")]
        state["origins"] = [o for o in state.get("origins", []) if "court.gov.cn" in o.get("origin", "")]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)

    def close(self):
        if self.page is not None:
            try:
                self.page.close()
            except Exception:
                pass


# -- facets -------------------------------------------------------------------


def cluster(api: Api, facet: str, body: dict, pdh) -> list[dict]:
    _key, endpoint = RMFYALK_FACETS[facet]
    d = api.call(f"cpwsAl/{endpoint}", dict(body, pdh=pdh))
    # Each child list also contains its parent; drop it.
    return [n for n in (d.get("data") or []) if str(n.get("key")) != str(pdh)]


def walk(api: Api, facet: str, body: dict, target: str = "", budget: int = 120):
    """Breadth-first over the facet tree, only into nodes with hits. Nodes whose
    title is part of the target name are expanded first, so 北京市第二中级人民法院
    goes 北京市 -> its children instead of touring every province."""
    queue, seen, calls = [0], [], 0
    while queue and calls < budget:
        pdh = queue.pop(0)
        calls += 1
        for n in cluster(api, facet, body, pdh):
            seen.append(n)
            title = n.get("value") or ""
            if target and title == target:
                return seen, True
            if str(n.get("isHaveChildren") or "0") != "0":
                if target and title and title in target:
                    queue.insert(0, n["key"])
                else:
                    queue.append(n["key"])
    return seen, not queue


def resolve(api: Api, facet: str, value: str, body: dict) -> str:
    """A name -> its facet key. Digits (and every 关键词) pass straight through."""
    if facet == "keyword" or value.isdigit():
        return value
    nodes, _complete = walk(api, facet, body, value)
    exact = [n for n in nodes if n.get("value") == value]
    chosen = exact or [n for n in nodes if value in (n.get("value") or "")]
    if not chosen:
        raise SkillError(f"no {facet} node named {value!r} has hits under this query — "
                         f"list them with --find-nodes {facet} '.'", EXIT_USAGE)
    if len(chosen) > 1:
        lines = "\n".join(f"  {n.get('count'):>5}  {n.get('value')}  key={n.get('key')}" for n in chosen[:20])
        raise SkillError(f"{value!r} matches {len(chosen)} {facet} nodes; pass one key:\n{lines}", EXIT_USAGE)
    n = chosen[0]
    print(f"resolved {facet} {value!r} -> key {n['key']} ({n.get('count')} hits)", file=sys.stderr)
    return str(n["key"])


# -- rows and documents ---------------------------------------------------------


def row_record(n: int, doc: dict) -> dict:
    infos = one_line(doc.get("cpws_al_infos") or "")
    rec = {
        "n": n,
        "no": doc.get("cpws_al_no"),
        "type": RMFYALK_TYPES.get(doc.get("cpws_al_type"), doc.get("cpws_al_type")),
        "title": one_line(doc.get("cpws_al_title") or ""),
        "case_no": one_line(doc.get("cpws_al_ajzh") or ""),
        "court": doc.get("cpws_al_slfy_name") or "",
        "date": doc.get("cpws_al_zs_date") or "",
        "infos": infos,
        "status": doc.get("cpws_al_status"),
        "id": doc.get("id"),
    }
    if not rec["court"]:
        m = COURT_RE.search(infos)
        rec["court"] = m.group(0) if m else ""
    if doc.get("cpws_al_sub_title"):
        rec["sub_title"] = one_line(doc["cpws_al_sub_title"]).lstrip("—-")
    if doc.get("cpws_al_cpyz"):
        rec["holding"] = clean(doc["cpws_al_cpyz"])
    return {k: v for k, v in rec.items() if v not in ("", None)}


def parse_fields(spec: str | None) -> list[str]:
    if not spec:
        return []
    if spec.strip().lower() == "all":
        return list(RMFYALK_CONTENT_FIELDS)
    out = []
    for part in re.split(r"[,，\s]+", spec.strip()):
        if not part:
            continue
        key = RMFYALK_FIELD_ALIASES.get(part)
        if key is None:
            raise SkillError(f"unknown section {part!r}; try: {', '.join(sorted(set(RMFYALK_FIELD_ALIASES) - set(RMFYALK_CONTENT_FIELDS)))}",
                             EXIT_USAGE)
        if key not in out:
            out.append(key)
    return out


def add_sections(api: Api, rec: dict, fields: list[str], max_chars: int) -> None:
    d = api.call("cpwsAl/content", {"gid": rec["id"]})
    doc = ((d.get("data") or {}).get("data")) or {}
    labels = rmfyalk_section_labels(doc)
    sections = {}
    for f in fields:
        text = clean(doc.get(f"cpws_al_{f}"))
        if not text:
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…[truncated, {len(text)} chars total]"
        sections[labels.get(f, f)] = text
    rec["sections"] = sections
    if doc.get("cpws_al_keyword"):
        rec["keywords"] = doc["cpws_al_keyword"]
    if doc.get("cpws_al_update_time"):
        # The page footer: 本案例文本已于…作出调整 — the text you quote is the adjusted one.
        rec["text_adjusted"] = doc["cpws_al_update_time"].split(" ")[0]


def summary(rec: dict) -> str:
    kind = (rec.get("type") or "")[:2]
    return " | ".join(x for x in (f"{kind} {rec.get('no', '')}", rec.get("case_no"), rec.get("court"),
                                  rec.get("title", "")[:60]) if x)


# -- main -------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    q = ap.add_argument_group("what to search (repeatable, combinable — all terms are ANDed)")
    q.add_argument("--search", action="append", default=[], help="全文")
    q.add_argument("--title", action="append", default=[], help="标题")
    q.add_argument("--reason", action="append", default=[], help="裁判理由 only")
    q.add_argument("--facts", action="append", default=[], help="基本案情 only")
    q.add_argument("--keyword", action="append", default=[], help="关键词 (the case's own tags)")
    q.add_argument("--no", action="append", default=[], help="入库编号, e.g. 2024-12-3-007-006")
    q.add_argument("--case-no", help="案号, e.g. （2022）京02行终909号")
    q.add_argument("--fuzzy", action="store_true", help="模糊检索 (default 精确检索)")

    flt = ap.add_argument_group("filters (a name is resolved against the live facet; digits pass through)")
    flt.add_argument("--lib", choices=sorted(RMFYALK_LIBS), default="all",
                     help="zdx 指导性案例, ck 参考案例, all both (default)")
    flt.add_argument("--court", help="审理法院 name or key, e.g. 北京市 / 北京市第二中级人民法院 / 0204")
    flt.add_argument("--cause", help="案由/罪名 name or key, e.g. 劳动争议 / 20000533")
    flt.add_argument("--level", help="法院级别, e.g. 中级法院")
    flt.add_argument("--procedure", help="审理程序, e.g. 二审")
    flt.add_argument("--year", help="审判年份, e.g. 2023")
    flt.add_argument("--tag", help="关键词 facet value")
    ap.add_argument("--sort", choices=sorted(RMFYALK_SORTS), default="relevance")

    out = ap.add_argument_group("output")
    out.add_argument("--count-only", action="store_true",
                     help="print the hit count (split 指导性/参考) and stop — compare phrasings with it")
    out.add_argument("--max", type=int, default=50, help="stop after this many cases (default 50)")
    out.add_argument("--fields", help="also fetch these sections per case: 裁判要旨,裁判理由,… or all")
    out.add_argument("--field-chars", type=int, default=6000, help="max chars per section")
    out.add_argument("--frag", type=int, nargs="?", const=200,
                     help="print each hit's 裁判要旨 (default 200 chars) for triage")
    out.add_argument("--find-nodes", nargs=2, metavar=("FACET", "REGEX"),
                     help=f"list facet nodes with hits under the query; FACET in {', '.join(RMFYALK_FACETS)}")
    out.add_argument("--refresh", action="store_true",
                     help="make sure the token is live (re-minting it from the SSO cookie if not), save, report expiry")
    out.add_argument("--out", help="output JSONL path (default: auto under the state dir)")
    common.add_browser_args(ap)
    args = ap.parse_args()
    args.headful = False
    args.storage_state = None

    terms = ([("qw", t) for t in args.search] + [("title", t) for t in args.title]
             + [("cply", t) for t in args.reason] + [("jbaq", t) for t in args.facts]
             + [("keyword", t) for t in args.keyword] + [("albh", t) for t in args.no])
    if not (terms or args.case_no or args.refresh or args.find_nodes):
        ap.error("give at least one of --search/--title/--reason/--facts/--keyword/--no/--case-no")
    if args.case_no and terms:
        ap.error("--case-no runs as 高级检索 and cannot be combined with term searches; "
                 "filter its rows afterwards instead")

    provider = get_provider("rmfyalk")
    try:
        fields = parse_fields(args.fields)
        with sync_playwright() as p:
            with common.browser_session(p, provider, args) as session:
                api = Api(session)
                try:
                    return run(api, provider, args, terms, fields)
                finally:
                    api.close()
    except SkillError as exc:
        return common.fail("rmfyalk_api.py", exc)
    except Exception as exc:  # noqa: BLE001
        return common.fail("rmfyalk_api.py", exc)


def run(api: Api, provider, args, terms, fields) -> int:
    api.ensure_login(force_refresh=False)
    if args.refresh:
        cookie, jwt = api.token_expiry()
        api.save_state(provider.state_path)
        common.print_digest("ok", {"mode": api.session.mode, "refreshed": api.refreshed,
                                   "cookie-expires": cookie, "token-expires": jwt,
                                   "saved": provider.state_path})
        if not (terms or args.case_no or args.find_nodes):
            return EXIT_OK

    filters = {}
    if args.case_no:
        filters[RMFYALK_CASE_NO_KEY] = args.case_no

    def body(page=1, size=1):
        return rmfyalk_build_search_body(terms, args.lib, page, size, args.fuzzy, args.sort, filters)

    for facet, value in (("court", args.court), ("cause", args.cause), ("level", args.level),
                         ("procedure", args.procedure), ("year", args.year), ("keyword", args.tag)):
        if value:
            filters[RMFYALK_FACETS[facet][0]] = resolve(api, facet, value, body())

    if args.find_nodes:
        facet, pattern = args.find_nodes
        if facet not in RMFYALK_FACETS:
            raise SkillError(f"unknown facet {facet!r}; try: {', '.join(RMFYALK_FACETS)}", EXIT_USAGE)
        nodes, complete = walk(api, facet, body())
        rx = re.compile(pattern)
        for n in nodes:
            if rx.search(n.get("value") or ""):
                print(f"{n.get('count'):>6}  {n.get('value')}  key={n.get('key')}")
        if not complete:
            print("(tree walk stopped at its request budget — narrow the query for the rest)", file=sys.stderr)
        persist(api, provider)
        return EXIT_OK

    probe = api.call("cpwsAl/search", body())
    total = int(((probe.get("data") or {}).get("totalCount")) or 0)
    split_body = body()
    split_body["searchParams"].pop("lib", None)    # the page drops lib for this call too
    split = api.call("cpwsAl/cpwsAlTypeNextLeftCluster", split_body).get("data") or []
    by_type = ", ".join(f"{s.get('value')} {s.get('count')}" for s in split)
    print(f"mode={api.session.mode} count={total} lib={args.lib} ({by_type or 'no split'})", file=sys.stderr)
    if args.count_only:
        print(total)
        persist(api, provider)
        return EXIT_OK

    cap = min(total, args.max) if args.max else total
    records, page = [], 1
    size = min(PAGE_CAP, max(cap, 1))
    while len(records) < cap:
        d = api.call("cpwsAl/search", body(page, size))
        docs = (d.get("data") or {}).get("datas") or []
        if not docs:
            break
        for doc in docs:
            if doc.get("cpws_al_title"):
                records.append(row_record(len(records) + 1, doc))
        page += 1
    records = records[:cap]

    if fields:
        for i, rec in enumerate(records):
            if i:
                time.sleep(0.4)    # one request per case; don't burst
            add_sections(api, rec, fields, args.field_chars)

    key = json.dumps([terms, filters, args.lib, args.fuzzy, args.sort, fields], ensure_ascii=False)
    out_path = pathlib.Path(args.out) if args.out else common.default_out_path("rmfyalk", key, "jsonl")
    written = common.write_payload(out_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    print(f"wrote {len(records)} cases -> {written}", file=sys.stderr)
    for rec in records:
        print(f"{rec['n']:>3}  {summary(rec)}", file=sys.stderr)
        if args.frag and rec.get("holding"):
            print(f"     {' '.join(rec['holding'].split())[:args.frag]}", file=sys.stderr)
    persist(api, provider)
    return EXIT_OK


def persist(api: Api, provider) -> None:
    """Keep the saved session moving: every API call slides the token cookie's
    1h expiry, but only in the live context — save it back. An attached Chrome
    is saved only after a refresh; --save-state is the explicit way otherwise."""
    if not api.session.attached or api.refreshed:
        api.save_state(provider.state_path)


if __name__ == "__main__":
    raise SystemExit(main())
