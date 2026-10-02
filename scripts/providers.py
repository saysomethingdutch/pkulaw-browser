#!/usr/bin/env python3
"""Per-site knowledge: search URLs, login gates, anti-bot shells, result rows.

Add a new legal database by writing one Provider and registering it in
PROVIDERS. Nothing else in the skill needs to change.

Libraries normalize to two kinds:
  case -> judgments and case searches
  law  -> laws, judicial interpretations, regulations

Paths here are absolute (common.STATE_ROOT). They used to be relative to the
caller's CWD, which meant a saved session was invisible from any directory but
the one it was created in — the single largest cause of spurious "login failed"
escalations.
"""
import pathlib
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from common import STATE_ROOT

# Wolters Kluwer separates the display text from the query DSL with U+01C1.
WK_QUERY_SEP = "ǁ"

PROFILES = STATE_ROOT / "profiles"


@dataclass
class Provider:
    name: str
    home_url: str
    state_path: pathlib.Path
    profile_dir: pathlib.Path
    library_aliases: dict[str, str]
    build_search_url: Callable[..., str]
    detect_login_gate: Callable[[str, str, str], bool]
    probe_url: str
    default_keyword_type: dict[str, str]
    # Login-adjacent cookie names, reported for diagnosis.
    session_cookies: list[str]
    # The subset that actually carries the login. Freshness is judged on these
    # alone — judging on the full list lets a display preference vouch for a
    # session whose real credential died weeks ago.
    primary_cookies: list[str]
    # Regex (JS syntax) matching a detail-page href, per library kind.
    detail_href_pattern: dict[str, str]
    # CSS selector for the site's own per-result element, per library kind.
    # Preferred over the href pattern when known; "" falls back to link-climbing.
    row_selector: dict[str, str]
    # Headers used to slice a detail page into requestable sections.
    section_headers: dict[str, list[str]]
    detect_antibot: Callable[[str, str, str], bool] = lambda t, u, b: False
    # Entitlement wall: the page renders, the body text does not.
    detect_partial: Callable[[str, str, str], bool] = lambda t, u, b: False
    # Sites that serve an anti-bot page to headless automation. When true the
    # scripts launch a visible window and ignore any request for headless.
    force_headful: bool = False
    # Sites that refuse plain network fetches; only the Chrome-copy path works.
    allow_direct_fetch: bool = False
    # Detail pages that open via target=_blank; drive them by URL, not by clicking.
    opens_detail_in_new_tab: bool = False
    notes: list[str] = field(default_factory=list)

    def normalize_library(self, value: str) -> str:
        kind = self.library_aliases.get(value.lower())
        if kind is None:
            supported = ", ".join(sorted(self.library_aliases))
            raise ValueError(f"unsupported library for {self.name}: {value} (try: {supported})")
        return kind


# --------------------------------------------------------------------------
# PKULAW / 北大法宝
# --------------------------------------------------------------------------

PKULAW_CASE_SEARCH_BASE = (
    "https://www.pkulaw.com/case/pfnl?"
    "SearchKeywordType={search_keyword_type}&MatchType={match_type}"
    "&RangeType={range_type}&Library=pfnl&Keywords="
)
PKULAW_LAW_SEARCH_BASE = (
    "https://www.pkulaw.com/law?"
    "SearchKeywordType={search_keyword_type}&MatchType={match_type}"
    "&RangeType={range_type}&Library=chl&Keywords="
)


def pkulaw_build_search_url(
    keywords: str,
    library: str,
    search_keyword_type: str,
    match_type: str = "Exact",
    range_type: str = "Piece",
) -> str:
    base = PKULAW_CASE_SEARCH_BASE if library == "case" else PKULAW_LAW_SEARCH_BASE
    return base.format(
        search_keyword_type=search_keyword_type,
        match_type=match_type,
        range_type=range_type,
    ) + urllib.parse.quote(keywords)


def pkulaw_detect_login_gate(title: str, final_url: str, body_text: str) -> bool:
    host = urlparse(final_url).netloc.lower()
    if "cas.pkulaw.com" in host:
        return True
    if title.strip() == "登录/注册":
        return True
    return (
        "验证码/密码登录" in body_text
        or "扫一扫登录" in body_text
        or "请登录后，再次尝试" in body_text
        or ("登录/注册" in body_text and "请登录" in body_text)
    )


def pkulaw_detect_partial(title: str, final_url: str, body_text: str) -> bool:
    """PKULAW's own entitlement wall. Unlike the WKINFO marker, these strings
    are inferred rather than observed live — widen them the first time a
    truncated PKULAW page slips through as a success."""
    return (
        "您没有权限查看" in body_text
        or "购买后可查看全文" in body_text
        or "登录后查看全文" in body_text
        or "开通后可阅读全文" in body_text
    )


def pkulaw_detect_antibot(title: str, final_url: str, body_text: str) -> bool:
    """The ~200-byte 安全提示 shell headless runs get. It carries no login
    markers, so without this check it reads as an empty result and sends the
    agent off to re-authenticate a session that was never the problem."""
    if "您已触发安全防护" in body_text:
        return True
    if "安全提示" in body_text and len(body_text.encode("utf-8", errors="ignore")) < 800:
        return True
    return False


# --------------------------------------------------------------------------
# WKINFO / 威科先行
# --------------------------------------------------------------------------

WKINFO_PATHS = {"case": "judgment-documents", "law": "legislation"}


def wkinfo_build_search_url(
    keywords: str,
    library: str,
    search_keyword_type: str,
    match_type: str = "Exact",
    range_type: str = "Piece",
) -> str:
    """Fulltext uses the short ?simple= form; Title needs the titleExtend DSL.

    match_type and range_type are accepted for signature parity with PKULAW and
    have no equivalent in the WK URL scheme.
    """
    path = WKINFO_PATHS[library]
    base = f"https://law.wkinfo.com.cn/{path}/list"
    if search_keyword_type.lower() == "title":
        dsl = f'{keywords}{WK_QUERY_SEP}titleExtend:(("{keywords}"))'
        return f"{base}?q={urllib.parse.quote(dsl)}&tip={urllib.parse.quote(keywords)}"
    return f"{base}?simple={urllib.parse.quote(keywords)}"


def wkinfo_detect_partial(title: str, final_url: str, body_text: str) -> bool:
    """IP-recognised but not account-logged-in.

    Observed live: the detail page renders breadcrumbs, title, 案由 and the case
    number, then says 很抱歉，您没有权限查看全文内容 where the judgment should be.
    ~1.5KB of perfectly real metadata and no judgment at all.
    """
    return "您没有权限查看全文" in body_text or "很抱歉，您没有权限" in body_text


def wkinfo_detect_login_gate(title: str, final_url: str, body_text: str) -> bool:
    """WK keeps the page title and never leaves the host on a gate.

    Instead it throws away the requested path, bounces to the site root, and
    renders a login modal.

    The bounce is *not* checked here. Judging it from the final URL alone made
    an ordinary visit to the home page — where the path is legitimately empty —
    report as a gate. It now lives in common.request_path_dropped(), which
    compares against the URL that was actually requested.
    """
    if "抱歉，此功能需要登录后操作" in body_text:
        return True
    return "账户登录" in body_text and ("CARSI登录" in body_text or "申请试用" in body_text)


# --------------------------------------------------------------------------
# WKINFO JSON search API (court/date/cause filters, 同段 search, pagination)
# --------------------------------------------------------------------------
# The Angular list page does NOT filter or paginate through the URL. It POSTs a
# JSON body to /csi/search and reads facet trees from /csi/filtertree/<tree>.
# The `simple=` URL form is a lossy convenience: one DOM snapshot (~25 rows), no
# court/region filter, and a region word ends up as a keyword (noise). For a
# region-scoped, date-scoped, 同段, or *complete* result set, call the API.
#
# Query strings, by search mode:
#   fulltext  simple:((词1 词2))
#   title     title:((词))
#   本院认为  judgedReasonDatabase:(("词1 词2"`<span>))   -- needs advanced=True
# The backtick is the same-paragraph (同段) span operator; the number after it is
# the span width. NOTE the field name: `courtOpinion` also answers with plausible
# counts but indexes something else -- the same four terms returned 68 hits under
# judgedReasonDatabase and 0 under courtOpinion (2026-08-26). A reverse-engineered
# field name that merely returns *something* is not verified.
#
# Filters ride searchScope.treeNodeIds, whose node ids use separators U+01C1 `ǁ`
# and U+01C2 `ǂ`. Nodes come from /csi/filtertree/<tree>:
#   court           courtǁ<code-path>ǁǂ  -- a parent node matches all children,
#                   e.g. every Suzhou court:
#                   courtǁ017000000江苏省/012060000江苏省苏州市中级人民法院辖区ǁǂ
#   judgmentDate    judgmentDateǁ[2021.08.26 TO 2026.08.27}ǁǂ  (asymmetric bracket)
#   causeOfAction   causeOfActionǁ<案由路径>ǁǂ
# Prefer discovering ids at runtime (pkulaw_api.py --find-courts) over hardcoding
# them; a hardcoded table only ever covers the provinces someone happened to test.
#
# To recover a filter code no tree call exposes: run that search once in the
# browser's 高级检索, capture the outgoing request with page.on("request")
# filtered on /csi/search, and read its body. The sidebar fires its own request
# against `law.editorial` -- take the one whose indexId is `law.case`.
#
# Pagination: pageInfo.offset/limit. The per-request limit cap sits between 100
# (accepted) and 500 (rejected with "超过每页大小限制"); 100 is safe. Loop offset
# until offset+got >= docCount. searchMetadata.limitCount is the server cap for
# the result set (5000 for law.case), not the per-request limit.

WKINFO_API_SEARCH = "https://law.wkinfo.com.cn/csi/search"
WKINFO_API_FILTERTREE = "https://law.wkinfo.com.cn/csi/filtertree/{tree}"

TREE_SEP = "\u01c1"   # ǁ
TREE_END = "\u01c2"   # ǂ

# Fixed payload every /csi/search call sends, captured from the frontend.
# `buy` stays false; a logged-in session still returns full caseDetail text.
# relatedIndex* are off: we never read those results and they inflate the reply.
# groupEnabled must stay False -- True fails outright with "E_000_006 搜索结果为空".
WKINFO_SEARCH_OTHER_OPTIONS = {
    "requireLanguage": "cn",
    "relatedIndexEnabled": False,
    "groupEnabled": False,
    "smartEnabled": True,
    "buy": False,
    "summaryLengthLimit": 100,
    "synonymEnabled": True,
    "advanced": False,
    "isHideBigLib": 0,
    "relatedIndexFetchRows": 0,
    "proximateCourtID": "",
    "module": "",
    "correctEnabled": True,
    "mappingEnabled": True,
    "webSearchEnabled": False,
    "defaultSearch": False,
    "rankKeyword": "",
}

# Excludes 通知书-type documents from a case search. Kept as a named recipe
# rather than a default: what typeOfDecision 008 covers has not been verified
# against the facet tree, only observed to shrink noisy result sets.
WKINFO_FQ_DROP_NOTICES = "-typeOfDecision:((008))"


def wkinfo_api_query_string(keywords: str) -> str:
    """The queryString a `?simple=<kw>` URL becomes inside the API body."""
    return f"simple:(({keywords}))"


def wkinfo_title_query_string(keywords: str) -> str:
    return f"title:(({keywords}))"


def wkinfo_reason_query_string(terms: str, span: int = 10000) -> str:
    """本院认为 + 同段. Caller must also set advanced=True on the body."""
    return f'judgedReasonDatabase:(("{terms}"`{span}))'


def wkinfo_tree_node(tree: str, value: str) -> str:
    """A searchScope.treeNodeIds entry: <tree>ǁ<value>ǁǂ."""
    return f"{tree}{TREE_SEP}{value}{TREE_SEP}{TREE_END}"


def wkinfo_date_node(since: str, until: str = "2099.12.31") -> str:
    """Date facet node. Dates are dotted: 2021.08.26."""
    return wkinfo_tree_node("judgmentDate", f"[{since} TO {until}}}")


def wkinfo_date_filters(since: str, until: str = "2099.12.31") -> list[str]:
    """query.filterDates, sent alongside the judgmentDate tree node."""
    return [f"judgmentDate:[{since} TO {until}]", f"filingDate:[{since} TO {until}]"]


def wkinfo_build_api_body(
    query_string: str,
    tree_node_ids: list[str],
    limit: int,
    offset: int,
    index_id: str = "law.case",
    filter_dates: list[str] | None = None,
    filter_queries: list[str] | None = None,
    advanced: bool = False,
) -> dict:
    """The JSON body POSTed to /csi/search. index_id: law.case | law.legislation."""
    other = dict(WKINFO_SEARCH_OTHER_OPTIONS)
    other["advanced"] = advanced
    return {
        "query": {
            "queryString": query_string,
            "filterDates": list(filter_dates or []),
            "filterQueries": list(filter_queries or []),
        },
        "searchScope": {"treeNodeIds": list(tree_node_ids)},
        "relatedIndexQueries": [],
        "sortOrderList": [
            {"sortKey": "score", "sortDirection": "DESC"},
            {"sortKey": "judgmentDate", "sortDirection": "DESC"},
        ],
        "pageInfo": {"limit": limit, "offset": offset},
        "chargingInfo": {"useBalance": True},
        "otherOptions": other,
        "indexId": index_id,
    }


def wkinfo_filtertree_url(search_id: str, tree: str = "court") -> str:
    """Facet tree endpoint. Trees: court, causeOfAction, judgmentDate, instance,
    typeOfDecision, courtLevel, docLengthLevel, jurisdiction, etc."""
    return WKINFO_API_FILTERTREE.format(tree=tree) + f"?searchId={search_id}"


# Kept: earlier name for wkinfo_filtertree_url.
wkinfo_court_tree_url = wkinfo_filtertree_url


# --------------------------------------------------------------------------

CASE_SECTIONS = [
    "案由", "案号", "审理法院", "审理程序", "裁判日期", "发布日期", "当事人",
    "审理经过", "原告诉称", "被告辩称", "上诉人称", "被上诉人辩称",
    "一审法院查明", "一审法院认为", "二审法院查明", "本院查明", "本院认为",
    "裁判结果", "裁判依据", "法律依据", "判决如下", "附:法律条文",
    "争议焦点", "案件历程", "本案评析", "裁判要旨", "案例评析",
]

LAW_SECTIONS = [
    "标题", "发文字号", "发布部门", "发布日期", "实施日期", "生效日期",
    "效力级别", "法规类别", "时效性", "目录", "正文", "附件", "修订记录",
]


PROVIDERS: dict[str, Provider] = {
    "pkulaw": Provider(
        name="pkulaw",
        home_url="https://www.pkulaw.com/",
        state_path=STATE_ROOT / "pkulaw_state.json",
        profile_dir=PROFILES / "pkulaw",
        library_aliases={"pfnl": "case", "case": "case", "law": "law", "chl": "law"},
        build_search_url=pkulaw_build_search_url,
        detect_login_gate=pkulaw_detect_login_gate,
        detect_antibot=pkulaw_detect_antibot,
        detect_partial=pkulaw_detect_partial,
        probe_url=PKULAW_CASE_SEARCH_BASE.format(
            search_keyword_type="Fulltext", match_type="Exact", range_type="Piece"
        ) + urllib.parse.quote("（2026）京02民终43号"),
        default_keyword_type={"case": "Fulltext", "law": "Title"},
        session_cookies=[
            "pkulaw_v6_sessionid", "userislogincookie", "LoginAccount", "authormes",
            "KEYCLOAK_IDENTITY", "AUTH_SESSION_ID", "KEYCLOAK_SESSION", "shib_idp_session",
        ],
        primary_cookies=["pkulaw_v6_sessionid", "userislogincookie", "KEYCLOAK_IDENTITY"],
        detail_href_pattern={"case": r"/pfnl/[^\"']+\.html", "law": r"/chl/[^\"']+\.html"},
        # Not yet confirmed against a logged-in PKULAW list page; the empty
        # values keep row extraction on the link-climbing fallback until they are.
        row_selector={"case": "", "law": ""},
        section_headers={"case": CASE_SECTIONS, "law": LAW_SECTIONS},
        force_headful=True,
        allow_direct_fetch=False,
        notes=[
            "Headless runs get an anti-bot 安全提示 shell (~197 bytes), never real results.",
            "Full case text often needs the Chrome view-source fallback.",
            "CARSI login works without the firm intranet.",
        ],
    ),
    "wkinfo": Provider(
        name="wkinfo",
        home_url="https://law.wkinfo.com.cn/",
        state_path=STATE_ROOT / "wkinfo_state.json",
        profile_dir=PROFILES / "wkinfo",
        library_aliases={
            "judgment-documents": "case", "judgment": "case", "case": "case", "pfnl": "case",
            "legislation": "law", "law": "law", "chl": "law",
        },
        build_search_url=wkinfo_build_search_url,
        detect_login_gate=wkinfo_detect_login_gate,
        detect_partial=wkinfo_detect_partial,
        probe_url=wkinfo_build_search_url("（2021）京01民终9024号", "case", "Fulltext"),
        default_keyword_type={"case": "Fulltext", "law": "Title"},
        # connect.sid expires ~1 day after issue; the autologin family ~7 days.
        # That short life is why a seeded state file goes stale so quietly.
        session_cookies=[
            "connect.sid", "autologin", "loginin", "userInfo", "userInfoV5",
            "username", "loginId", "check",
        ],
        # loginId and check outlive the login by weeks; they must not vouch for it.
        primary_cookies=["connect.sid", "autologin", "userInfoV5"],
        detail_href_pattern={
            "case": r"/judgment-documents/detail/",
            "law": r"/legislation/detail/",
        },
        # Angular component tags, confirmed against a logged-in list page
        # 2026-08-11. More stable than the wkb-* classes beside them, and they
        # are what makes a row's title and its snippet links stop being
        # confusable — see the note above _ROW_JS in common.py.
        row_selector={
            "case": "b-list-item-judgment-documents",
            "law": "b-list-item-legislation, b-list-item-law",
        },
        section_headers={"case": CASE_SECTIONS, "law": LAW_SECTIONS},
        force_headful=False,
        allow_direct_fetch=False,
        opens_detail_in_new_tab=True,
        notes=[
            "Serves real pages to headless automation, so headless is the default here.",
            "Detail pages render full text in the DOM; no view-source needed.",
            "Login comes via the firm intranet (NTLM) — attach to real Chrome rather than reproducing SSO.",
        ],
    ),
}


def get_provider(name: str) -> Provider:
    provider = PROVIDERS.get(name.lower())
    if provider is None:
        raise ValueError(f"unknown provider: {name} (try: {', '.join(sorted(PROVIDERS))})")
    return provider


def provider_for_url(url: str) -> Provider | None:
    host = urlparse(url).netloc.lower()
    if "pkulaw.com" in host:
        return PROVIDERS["pkulaw"]
    if "wkinfo.com.cn" in host:
        return PROVIDERS["wkinfo"]
    return None
