#!/usr/bin/env python3
"""Read WKINFO through its JSON search API: filters, 同段 search, pagination, full text.

The URL-based scripts (`pkulaw_playwright.py --search`) only see the first DOM
snapshot (~25 rows) and cannot filter at all — `fq=jurisdiction` silently returns
0, and a region has to be smuggled into the keywords as noise. This script posts
the same JSON the Angular list page posts to POST /csi/search, which adds:

  * court/region, 案由 and 裁判日期 filters via `searchScope.treeNodeIds`,
  * 本院认为 + 同段 search via `judgedReasonDatabase` and the backtick span operator,
  * a cheap hit count (`--count-only`) for comparing candidate queries,
  * pagination via `pageInfo.offset` (loop until offset+got >= docCount),
  * structured full-text fields directly in each documentList item —
    additionalFields.courtText (审理法院), .documentNumber (案号),
    .courtOpinion (本院认为), .court1Judgement (一审法院认为), .caseDetail (全文),
    .judgmentDate, .instanceText, .causeOfActionText,
  * the matched paragraph itself in `panorama.hitUnits[].unitContent` (`--frag`),
    which is enough to triage dozens of hits without opening any document.

Like every other script here, the request rides the Playwright browser context
(shared cookie jar with the saved session) — NOT a plain HTTP fetch, so it stays
inside the skill's no-direct-fetch rule. It attaches to the debuggable Chrome
when one is running, and falls back to headless + saved state otherwise.

Usage:
  # narrow a query before fetching anything — compare hit counts
  python3 pkulaw_api.py --reason '劳动关系 确认 存在 注销' --court 北京市 --count-only

  # 本院认为 · 同段 · 北京 · 近五年 · 劳动争议, with hit fragments for triage
  python3 pkulaw_api.py --reason '劳动关系 确认 存在 注销' \
      --court 北京市 --cause 劳动争议 --since 2021.08.26 --until 2026.08.26 --frag

  # list court-tree nodes matching a pattern (ids to pass to --court)
  python3 pkulaw_api.py --find-courts '苏州|昆山|常熟' --search '医疗补助费'

  # every Suzhou case matching the keywords, paginated to completion
  python3 pkulaw_api.py --search '医疗补助费 病假 平均工资' \
      --court 'courtǁ017000000江苏省/012060000江苏省苏州市中级人民法院辖区ǁǂ'

  # law library
  python3 pkulaw_api.py --search '劳动合同' --index law.legislation

Output: one JSON object per document to --out (default under the state dir's
out/), a one-line summary per hit to stderr. Full text goes to the file, never
to the model.

Exit codes: 0 ok, 2 bad input / ambiguous filter, 5 API refused the request.
"""
import argparse
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

import common
from providers import (
    TREE_SEP,
    WKINFO_API_SEARCH,
    WKINFO_FQ_DROP_NOTICES,
    get_provider,
    wkinfo_api_query_string,
    wkinfo_build_api_body,
    wkinfo_date_filters,
    wkinfo_date_node,
    wkinfo_filtertree_url,
    wkinfo_reason_query_string,
    wkinfo_title_query_string,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_API = 5

WKINFO_LIST_URL = "https://law.wkinfo.com.cn/judgment-documents/list?simple=x"

# Same-origin fetch, used when the plain APIRequestContext is refused. Running
# inside the page inherits the SPA's origin and credentials.
PAGE_FETCH_JS = """async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify(body),
  });
  return {status: r.status, text: await r.text()};
}"""

PAGE_GET_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  return {status: r.status, text: await r.text()};
}"""

STRIP_TAG = re.compile(r"<[^>]+>")


def clean(s: str | None) -> str:
    return STRIP_TAG.sub("", s or "")


class Session:
    """One WKINFO session. Prefers the attached Chrome, then headless+state.

    Requests go through context.request (cheap, no page load). If the API
    refuses that — some deployments check Origin — it transparently retries
    through a real page on the site's own origin.
    """

    def __init__(self, playwright, provider):
        self.page = None
        self._via_page = False
        port = common.CDP_PORT
        if common.cdp_alive(port):
            self.browser = playwright.chromium.connect_over_cdp(common.cdp_endpoint(port))
            self.context = self.browser.contexts[0]
            self.mode = f"attach:{port}"
            self._owns_browser = False
        else:
            state = str(provider.state_path) if provider.state_path.exists() else None
            if state is None:
                raise SystemExit(
                    f"no saved session at {provider.state_path} — run "
                    f"python3 pkulaw_chrome.py --login {provider.name}"
                )
            self.browser = playwright.chromium.launch(headless=True)
            self.context = self.browser.new_context(storage_state=state)
            self.mode = "headless+state"
            self._owns_browser = True

    def _ensure_page(self):
        if self.page is None:
            self.page = self.context.new_page()
            self.page.goto(WKINFO_LIST_URL, wait_until="domcontentloaded", timeout=120000)
            self.page.wait_for_timeout(3500)
        return self.page

    def post(self, url, body):
        if not self._via_page:
            r = self.context.request.post(
                url, headers={"Content-Type": "application/json"},
                data=json.dumps(body, ensure_ascii=False),
            )
            if r.status == 200:
                return r.status, r.json()
            print(f"context.request POST -> {r.status}; retrying through a page",
                  file=sys.stderr)
            self._via_page = True
        res = self._ensure_page().evaluate(PAGE_FETCH_JS, {"url": url, "body": body})
        return res["status"], json.loads(res["text"])

    def get(self, url):
        if not self._via_page:
            r = self.context.request.get(url)
            if r.status == 200:
                return r.status, r.json()
            self._via_page = True
        res = self._ensure_page().evaluate(PAGE_GET_JS, url)
        return res["status"], json.loads(res["text"])

    def close(self):
        if self.page is not None:
            self.page.close()
        # On a CDP attachment this only drops the connection; the user's
        # Chrome window stays up.
        self.browser.close()


def walk_tree(node, out):
    out.append(node)
    for child in (node.get("children") or []):
        walk_tree(child, out)
    return out


def fetch_tree(sess, body, tree):
    """Run a 1-row probe to mint a searchId, then pull that facet tree."""
    status, data = sess.post(WKINFO_API_SEARCH, body)
    if status != 200 or "searchMetadata" not in data:
        print(f"search status {status}: {json.dumps(data, ensure_ascii=False)[:400]}",
              file=sys.stderr)
        return None, None
    meta = data["searchMetadata"]
    status, tree_data = sess.get(wkinfo_filtertree_url(meta.get("searchId"), tree))
    if status != 200:
        print(f"filtertree/{tree} status {status}", file=sys.stderr)
        return meta, None
    return meta, walk_tree(tree_data, [])


def find_nodes(sess, body, tree, pattern):
    meta, nodes = fetch_tree(sess, body, tree)
    if nodes is None:
        return EXIT_API
    print(f"searchId={meta.get('searchId')}  docCount={meta.get('docCount')}", file=sys.stderr)
    rx = re.compile(pattern)
    for node in nodes:
        if rx.search(node.get("title") or ""):
            print(f'{node.get("nHits"):>7}  {node.get("title")}  nodeId={node.get("nodeId")}')
    return EXIT_OK


def resolve_node(sess, body, tree, value):
    """Turn a plain name into a treeNodeId. A raw id is passed straight through.

    Resolving at runtime beats a hardcoded code table, which only ever covers
    whatever regions someone happened to test. Exact title match wins; an
    ambiguous name is reported rather than guessed.
    """
    if TREE_SEP in value:
        return value
    meta, nodes = fetch_tree(sess, body, tree)
    if nodes is None:
        raise SystemExit(EXIT_API)
    hits = [n for n in nodes if value in (n.get("title") or "")]
    exact = [n for n in hits if (n.get("title") or "") == value]
    chosen = exact or hits
    if not chosen:
        print(f"no {tree} node matching {value!r} — try --find-courts to list them",
              file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    if len(chosen) > 1:
        print(f"{value!r} matches {len(chosen)} {tree} nodes; pass one nodeId to --{tree}:",
              file=sys.stderr)
        for n in chosen[:20]:
            print(f'  {n.get("nHits"):>7}  {n.get("title")}  {n.get("nodeId")}', file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    node = chosen[0]
    print(f'resolved {value!r} -> {node.get("title")} ({node.get("nHits")} hits)', file=sys.stderr)
    return node.get("nodeId")


def doc_summary(doc):
    af = doc.get("additionalFields") or {}
    return " | ".join(x for x in (
        af.get("judgmentDate"), af.get("documentNumber"), af.get("courtText"),
        clean(doc.get("title"))[:70],
    ) if x)


def doc_fragment(doc, width):
    units = ((doc.get("panorama") or {}).get("hitUnits") or [])[:2]
    frag = " ‖ ".join(clean(u.get("unitContent")) for u in units)
    return frag.replace("\n", " ")[:width]


def search(sess, body_of, limit, max_docs, out_path, frag, count_only):
    status, probe = sess.post(WKINFO_API_SEARCH, body_of(1, 0))
    if status != 200 or "searchMetadata" not in probe:
        print(f"search status {status}: {json.dumps(probe, ensure_ascii=False)[:400]}",
              file=sys.stderr)
        return EXIT_API
    meta = probe["searchMetadata"]
    total = meta.get("docCount", 0)
    limit_count = meta.get("limitCount", 5000)
    print(f"mode={sess.mode} count={total} limitCount={limit_count}", file=sys.stderr)
    if count_only:
        print(total)
        return EXIT_OK

    cap = min(total, limit_count, max_docs or total)
    all_docs, offset = [], 0
    while offset < cap:
        status, data = sess.post(WKINFO_API_SEARCH, body_of(min(limit, cap - offset), offset))
        if status != 200:
            print(f"search status {status}: {json.dumps(data, ensure_ascii=False)[:400]}",
                  file=sys.stderr)
            return EXIT_API
        docs = data.get("documentList") or []
        if not docs:
            break
        all_docs.extend(docs)
        offset += len(docs)
        print(f"offset={offset}/{cap}", file=sys.stderr)

    all_docs = all_docs[:cap]
    payload = "\n".join(json.dumps(d, ensure_ascii=False) for d in all_docs) + "\n"
    written = common.write_payload(out_path, payload)
    print(f"wrote {len(all_docs)} docs -> {written}", file=sys.stderr)
    for i, d in enumerate(all_docs, 1):
        print(f"{i:>3}  {doc_summary(d)}", file=sys.stderr)
        if frag:
            print(f"     {doc_fragment(d, frag)}", file=sys.stderr)
    return EXIT_OK


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_argument_group("what to search (pick one)")
    mode.add_argument("--search", help="fulltext keywords; leave geography OUT, use --court")
    mode.add_argument("--title", help="title-only keywords")
    mode.add_argument("--reason", help="本院认为 + 同段 keywords, space-separated")
    mode.add_argument("--query-string", help="raw queryString override, e.g. simple:((...))")
    ap.add_argument("--span", type=int, default=10000, help="同段 span for --reason (default 10000)")

    flt = ap.add_argument_group("filters")
    flt.add_argument("--court", action="append", default=[],
                     help="court name (resolved at runtime) or a raw treeNodeId; repeatable")
    flt.add_argument("--cause", action="append", default=[],
                     help="案由 name or raw treeNodeId; repeatable")
    flt.add_argument("--since", help="裁判日期 from, dotted: 2021.08.26")
    flt.add_argument("--until", help="裁判日期 to, dotted (default 2099.12.31)")
    flt.add_argument("--drop-notices", action="store_true",
                     help=f"add filterQuery {WKINFO_FQ_DROP_NOTICES}")
    flt.add_argument("--filter-query", action="append", default=[], help="raw filterQuery; repeatable")

    ap.add_argument("--index", default="law.case", help="law.case (judgments) or law.legislation")
    ap.add_argument("--limit", type=int, default=100, help="per-request page size (100 is the cap)")
    ap.add_argument("--max", type=int, help="stop after this many documents")
    ap.add_argument("--count-only", action="store_true",
                    help="print the hit count and stop — use it to compare candidate queries")
    ap.add_argument("--frag", type=int, nargs="?", const=240,
                    help="print the matched paragraph per hit (default 240 chars)")
    ap.add_argument("--find-courts", help="regex over court-tree titles; lists nodeIds and stops")
    ap.add_argument("--find-nodes", nargs=2, metavar=("TREE", "REGEX"),
                    help="same, for any facet tree (causeOfAction, typeOfDecision, ...)")
    ap.add_argument("--out", help="output JSONL path (default: auto under the state dir)")
    args = ap.parse_args()

    modes = [args.search, args.title, args.reason, args.query_string]
    if sum(x is not None for x in modes) != 1:
        ap.error("pick exactly one of --search / --title / --reason / --query-string")

    advanced = False
    if args.query_string:
        query_string = args.query_string
        advanced = query_string.startswith("judgedReasonDatabase")
    elif args.reason:
        query_string = wkinfo_reason_query_string(args.reason, args.span)
        advanced = True          # the backtick span operator is only parsed in advanced mode
    elif args.title:
        query_string = wkinfo_title_query_string(args.title)
    else:
        query_string = wkinfo_api_query_string(args.search)

    filter_queries = list(args.filter_query)
    if args.drop_notices:
        filter_queries.append(WKINFO_FQ_DROP_NOTICES)
    until = args.until or "2099.12.31"
    filter_dates = wkinfo_date_filters(args.since, until) if args.since else []

    provider = get_provider("wkinfo")
    out_path = (pathlib.Path(args.out) if args.out
                else common.default_out_path("wkinfo", query_string, "jsonl"))

    with sync_playwright() as p:
        sess = Session(p, provider)
        try:
            def body_of(limit, offset, nodes=()):
                return wkinfo_build_api_body(
                    query_string, list(nodes), limit, offset, args.index,
                    filter_dates=filter_dates, filter_queries=filter_queries,
                    advanced=advanced,
                )

            if args.find_courts or args.find_nodes:
                tree, pattern = (args.find_nodes if args.find_nodes
                                 else ("court", args.find_courts))
                return find_nodes(sess, body_of(1, 0), tree, pattern)

            nodes = [resolve_node(sess, body_of(1, 0), "court", c) for c in args.court]
            nodes += [resolve_node(sess, body_of(1, 0), "causeOfAction", c) for c in args.cause]
            if args.since:
                nodes.append(wkinfo_date_node(args.since, until))

            return search(sess, lambda l, o: body_of(l, o, nodes),
                          args.limit, args.max, out_path, args.frag, args.count_only)
        finally:
            sess.close()


if __name__ == "__main__":
    raise SystemExit(main())
