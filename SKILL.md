---
name: pkulaw-browser
description: Search 北大法宝/PKULAW and 威科先行/WKINFO through a real browser and return compact, structured results. Use when a Chinese legal database is part of the task — case libraries (PKULAW `pfnl`, WKINFO `judgment-documents`) or law libraries (PKULAW `law`/`chl`, WKINFO `legislation`). Covers 类案检索 with precision filters: 同段检索 (same-paragraph), 本院认为 court-reasoning-only search, and filtering by 法院地域/案由/裁判日期. Scripts do the reading; screenshot-driven browsing is forbidden here.
---

# PKULAW / WKINFO Browser

Read either database with scripts, not with eyes. Every script takes
`--provider pkulaw` (default) or `--provider wkinfo`; the provider decides the
search URL scheme, the library names, the four failure signatures, the saved
session path, and whether headless is allowed.

Run the scripts from `scripts/`. All state is absolute, under
`~/.agents/state/pkulaw-browser/`, so the working directory does not matter.

## Hard discipline

**Never drive these two sites with computer-use, claude-in-chrome, or any
screenshot/click tool.** Not as a fallback, not "just to check", not when a
script fails. A single visual browsing session costs more tokens than a hundred
script runs, and both sites are fully readable through the DOM.

The ladder, in order. Each level is a script:

| L | What | When |
|---|---|---|
| L0 | `pkulaw_playwright.py` with the saved session | default |
| L1 | `pkulaw_playwright.py --browser attach` | user's logged-in Chrome is running |
| L2 | `pkulaw_chrome.py --login <provider>` | not logged in — hand the window to the user |
| L3 | `seed_cookies_from_chrome.py` | that Chrome cannot be opened (needs the user + a Keychain prompt) |
| L4 | `pkulaw_fetch.py --chrome-copy` | the page exists only in the user's own tab |

**Then stop.** If L4 fails, report to the user: what you tried, the exit code,
and the exact one-line command they need to run. Do not improvise a fifth level.

Two budget rules:

- More than **3 tool calls spent on login** in one task → stop and ask.
- Never re-run a failed command unchanged. The exit code says what to change.

## Start with the preflight

```bash
python3 pkulaw_doctor.py
```

Files and one TCP probe, no browser — cheap enough that there is never a reason
to skip it and guess. It prints, per provider, whether the saved session is
`fresh` / `suspect` / `stale`, when each login cookie dies, and the single next
command to run. `--live` additionally loads the provider's probe URL.

Session lifetimes are short, which is why sessions go stale silently: WKINFO's
`connect.sid` expires about a **day** after issue and its `autologin` family
about a **week**.

## Exit codes

Branch on these instead of reading the prose.

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok | continue |
| 1 | error | read the message |
| 2 | login-gate, session looked fresh | `pkulaw_check_login.py` — something else is wrong |
| 3 | session stale or missing | `pkulaw_chrome.py --login <provider>` |
| 4 | browser could not start | install Chromium, or start the attach Chrome |
| 5 | anti-bot shell | you ran PKULAW headless; use `--browser attach` |
| 6 | validation failed | wrong URL, or `--expect` too strict |
| 7 | content truncated | metadata came back, full text was withheld — log in |

## Login: attach, don't copy

The firm intranet in front of WKINFO (`home.zhonglun.com`) answers
`WWW-Authenticate: Negotiate, NTLM`, and there is no Kerberos ticket cache, so
the user authenticates by typing a domain password into Chrome's **native** auth
dialog. Playwright cannot show that dialog — it supports HTTP Basic only. This
is a real limitation and no script here will ever work around it.

So don't reproduce the SSO. Attach to a browser that already did it:

```bash
python3 pkulaw_chrome.py --login wkinfo
```

That opens a plain Google Chrome with a dedicated profile and a debugging port.
The user logs in by hand — the native dialog behaves normally, because they
launched it and nothing set an automation flag. Every later command adds
`--browser attach` (or just `--browser auto`, which attaches when the port is
up). **No cookies are copied, nothing is decrypted, no Keychain or DPAPI prompt,
no admin rights.** The login stays inside Chrome's own profile; the scripts
drive that Chrome over `127.0.0.1`. This works the same on macOS and Windows —
unlike cookie copying, which needs platform-specific decryption and is the step
that has been observed to fail on Windows.

It must be a dedicated profile: Chrome ≥ 136 refuses `--remote-debugging-port`
on the Default one.

Then snapshot it, so the login outlives the window:

```bash
python3 pkulaw_chrome.py --save-state wkinfo
```

This is what makes the whole thing cheap. After it, WKINFO queries run headless
from the saved session with no Chrome open at all — verified identical output to
the attached run. The pattern is **log in once by hand → snapshot → headless
from then on**, until the session ages out (`pkulaw_doctor.py` will say when).

PKULAW needs none of this — CARSI is a normal web form, so
`pkulaw_login.py --provider pkulaw` is enough.

**Never type the user's domain password into any prompt on their behalf.** Hand
them the window.

## Spend the fewest tokens that answer the question

The payload goes to a **file**; stdout gets a ~12-line digest. Pick the smallest
form that works:

```bash
# 1. A result list — ~30 tokens per hit, structured JSONL
python3 pkulaw_playwright.py --provider wkinfo --library judgment-documents \
  --search '（2021）京01民终9024号' --format rows --browser auto
```
```bash
# 2. Named sections of one document — often <10% of a judgment
python3 pkulaw_playwright.py 'https://law.wkinfo.com.cn/judgment-documents/detail/...' \
  --fields 本院认为,裁判结果 --browser auto
```
```bash
# 3. Many documents, one browser launch, one summary table
python3 pkulaw_playwright.py --provider wkinfo --urls-file urls.txt \
  --fields 本院认为,裁判结果 --browser auto
```
```bash
# 4. Whole page to a file plus a digest (default)
python3 pkulaw_playwright.py --provider pkulaw --library pfnl \
  --search '（2026）京02民终43号' --browser auto --expect '北京市第二中级人民法院'
```

`--format text` dumps the whole page into the conversation. It is the last
resort, not the default.

Measured on real logged-in pages: a 20-hit result list is 24,227 bytes as text
and 5,298 as rows (79% less); one judgment is 48,871 bytes whole and 3,090 as
`本院认为` + `裁判结果` (94% less).

`--fields` takes the *longest* occurrence of each heading, because WKINFO
detail pages open with a navigation strip listing the section names verbatim —
first-match extraction returns a table of contents and calls it a success.

Row output is `{n, title, url, module, case_no, court, date, doc_type}` for case
libraries and `{n, title, url, module, doc_no, date, status}` for law libraries.
Fields appear only when they were found — an absent `court` means absent, not
unknown.

**Read `module` and `doc_type` before citing anything.** A WKINFO case search
mixes corpora: `module: judgment-documents` is the judgment itself, while
`module: case-analysis` is commentary about it (`doc_type: 典型案例`,
`法官评案`). All three carry the same `case_no`, so the case number alone will
not tell them apart. A search for one case number legitimately returns the
judgment plus two commentaries on it.

## Four ways these sites fail, and how to tell them apart

Three of the four return an HTTP 200 with real-looking content. Do not describe
any of them as "no results".

1. **Anti-bot shell** (PKULAW, exit 5) — ~200 bytes of `安全提示` /
   `您已触发安全防护`. Means something ran headless. PKULAW is **always headful**;
   the scripts force it regardless of flags.
2. **Login gate** (exit 2/3) — PKULAW redirects to `cas.pkulaw.com` and retitles
   the page `登录/注册`. WKINFO keeps host *and* title, silently discards the
   requested path, bounces to the site root, and shows
   `抱歉，此功能需要登录后操作！`.
3. **Query or path dropped** (exit 3) — WKINFO answers an unauthenticated
   search by throwing the search terms away and serving a generic list of recent
   judgments; deep links get the path stripped and land on the site root.
   Correct host, plenty of bytes, real judgments, wrong ones. Caught by
   `common.classify_navigation`, which compares the requested URL against the
   final one. **Any new caller must use that function rather than
   `classify_page`** — the URL-level tells are invisible in the page itself.
4. **Entitlement stub** (exit 7) — WKINFO recognises the institution by IP
   (`cupl_IP` in the page header), renders title, 案由 and case number, then puts
   `很抱歉，您没有权限查看全文内容` where the judgment should be. About 1.5KB of
   genuine metadata and no judgment. The metadata is still written to the file;
   the exit code tells you the text is missing.

## Picking a provider and library

Libraries normalize to `case` and `law`; aliases match what each site shows.

| Kind | PKULAW | WKINFO |
|---|---|---|
| `case` — judgments | `pfnl` | `judgment-documents` (or `case`) |
| `law` — laws, interpretations, regulations | `law`, `chl` | `legislation` (or `law`) |

Do not search a case library for statutes, or a law library for judgments.

PKULAW has deeper 法规 coverage and 法宝引证; WKINFO has richer case metadata
(案件历程, 争议焦点, 本案评析) and returns full judgment text more easily. If the
user names one, use it. If one gates or comes up dry, the other is a legitimate
second try — separate corpora.

**Search URL schemes.** PKULAW uses a long query string (`SearchKeywordType`,
`MatchType`, `RangeType`, `Library`, `Keywords`). WKINFO is terser: fulltext is
`/{library}/list?simple=<kw>`; title-only needs a small DSL,
`/{library}/list?q=<kw>ǁtitleExtend:(("<kw>"))&tip=<kw>`, separator U+01C1. Both
are built by `providers.py` — don't hand-assemble them. `--match-type` and
`--range-type` are PKULAW-only. These URL schemes carry the WKINFO **law**
library fine; for the **case** library they cannot filter or scope at all — see
the API section below.

WKINFO opens detail pages with `target=_blank`, so drive detail pages by URL
rather than clicking through a list.

## The JSON search API — filters, 同段, pagination, full text

For the WKINFO **case** library the URL search is a lossy convenience: one DOM
snapshot (~25 rows, relevance-ranked) and no filter of any kind. Use
`pkulaw_api.py`, which posts the JSON the Angular list page itself posts to
`POST /csi/search`. Four things the URL cannot express, all of which matter:

| Need | URL | API |
|---|---|---|
| Scope to a region / court | `fq=jurisdiction…` silently returns **0** | `searchScope.treeNodeIds` |
| Search only 本院认为 | no parameter | `judgedReasonDatabase` |
| Same-paragraph (同段) | no parameter | backtick span operator |
| Compare candidate queries cheaply | must render a page | `searchMetadata.docCount` |
| Read past row 25 | impossible | `pageInfo.offset` |

**Narrow before you fetch.** `--count-only` reports the hit count without
pulling a single row. Run it over several phrasings first and keep the one that
collapsed: on one task 分立+退休+确认劳动关系 went 158,470 → 17 that way.

```bash
python3 pkulaw_api.py --reason '劳动关系 确认 存在 注销' --court 北京市 --count-only
```

```bash
python3 pkulaw_api.py --reason '劳动关系 确认 存在 注销' \
  --court 北京市 --cause 劳动争议 --since 2021.08.26 --until 2026.08.26 --frag
```

- Pick one of `--search` (fulltext), `--title`, `--reason` (本院认为 + 同段), or
  `--query-string` (raw). Geography, 案由 and dates go in the filter flags, never
  in the keywords.
- `--court 北京市` resolves the name against the live court tree; an ambiguous
  name is reported with its candidates rather than guessed. `--find-courts
  '苏州|昆山|常熟'` lists node ids and hit counts; pick the parent 中级人民法院辖区
  node to cover every court under it. `--find-nodes causeOfAction '劳动'` does the
  same for any other facet tree. **Don't hardcode a code table** — it only ever
  covers the regions someone happened to test.
- Pagination is automatic (`pageInfo.offset` loop). Per-request `--limit` must
  stay ≤100; 500 is rejected with "超过每页大小限制". `searchMetadata.limitCount`
  (5000 for `law.case`) is the server's cap on the whole result set.
- Each returned document already carries structured fields, so there is usually
  no separate detail fetch: `additionalFields.courtText` (审理法院),
  `.documentNumber` (案号), `.courtOpinion` (本院认为), `.court1Judgement`
  (一审法院认为), `.caseDetail` (全文). `module` still matters — a case search
  mixes `judgment-documents` and `case-analysis` that share `case_no`, so read
  `docId`/title before citing.
- **Triage without opening documents.** Each result carries
  `panorama.hitUnits[].unitContent` — the matched paragraph itself. That is
  enough to sort dozens of hits into keep/discard; `--frag` prints it. Fetch
  full text only for survivors.

The API rides the same Playwright browser context as every other script here
(shared cookie jar) — not a plain HTTP fetch, so the no-direct-fetch rule holds.
It attaches to the debuggable Chrome when one is up, else runs headless on the
saved state.

Field details, for when the script needs extending:

- `judgedReasonDatabase:(("词1 词2 词3"` + backtick + `10000))` is 本院认为 +
  同段; the number after the backtick is the span. **Not `courtOpinion`** — that
  name also answers with plausible counts, but indexes something else: the same
  four terms give 68 hits under `judgedReasonDatabase` and 0 under
  `courtOpinion`. A reverse-engineered field name that merely returns something
  is not verified.
- `treeNodeIds` separators are U+01C1 `ǁ` and U+01C2 `ǂ`:
  `courtǁ003000000北京市ǁǂ`, `judgmentDateǁ[2021.08.26 TO 2026.08.27}ǁǂ`
  (note the asymmetric bracket), `causeOfActionǁ…ǁǂ`. Builders live in
  `providers.py`; don't hand-assemble them.
- `otherOptions.advanced` must be `true` for a `--reason` query — `pkulaw_api.py`
  sets it. `groupEnabled: true` fails outright with `E_000_006 搜索结果为空`.
- **To recover a filter code no facet tree exposes**: run that search once in the
  browser's 高级检索, capture the outgoing request with `page.on("request")`
  filtered on `/csi/search`, and read its body. The sidebar fires its own
  request against `law.editorial` — take the one whose `indexId` is `law.case`.

## Reading a hit without misquoting it

**Confirm which section a fragment sits in.** Fragments come from 「被告辩称」as
often as from the court. In one corpus (2026)京0117民初2726号 carries a
well-drafted passage on labour relations being non-transferable and
non-retroactive — it is the *defendant's pleading*, not the holding. Quoting it
as the court's view would misattribute it.

**「案件历程」showing 二审维持原判 does NOT mean the appellate court adopted the
lower court's reasoning.** It means the *outcome* stood. A judgment can be
upheld on entirely different grounds, and the appellate court may expressly
reject the very passage you wanted to cite. Verified example: (2015)忻民初字第580号
held a deceased worker's brother lacked standing and read 《劳动争议调解仲裁法》
第25条 narrowly; the 案件历程 says 二审维持原判 — but (2015)来民三终字第189号
**rejected** that holding ("上诉人姚某……是有权代替姚兰德申请仲裁确认劳动关系的
主体资格"), affirming only because the claim failed on the evidence. Contrast
(2018)豫1422民初3038号, whose appeal (2018)豫14民终4885号 *did* endorse the
standing holding. **Before citing a first-instance passage, pull the appellate
decision and read its 本院认为 — every time.** Search the appellate case number
with `--search`, or search the parties' names to find unlisted follow-ons; an
absent 案件历程 plus no name hits means "no appeal recorded in WKINFO", which is
not the same as "no appeal".

**WKINFO detail pages lazy-load the judgment body.** Until it loads, the DOM
holds only the navigation strip, which repeats 「本院认为 裁判结果 审判人员」
verbatim — so section extraction returns the table of contents and reports
success. `pkulaw_playwright.py` scrolls before extracting (`--scrolls`, 6 by
default when reading a document, 0 for `--format rows`). If a section still
comes back suspiciously short, raise `--scrolls` or `--wait-ms` before
concluding the document lacks it.

## Search discipline

- Narrowest stable identifier first: case number, then exact title, then court
  plus cause of action.
- Where names are anonymized, confirm identity from metadata: court, date,
  charge/cause, result, issuing authority.
- If the first provider already answers the question, stop there.
- Do not broaden to outside-web search after either database exposed the content.

## No direct fetch

Do not fetch either site over plain HTTP. `pkulaw_fetch.py` refuses to run
without `--chrome-copy`. Direct requests and headless automation reliably trip
anti-bot defenses.

## Scripts

- `pkulaw_doctor.py` — preflight: session freshness, attach status, environment.
  **Run first.**
- `pkulaw_chrome.py` — start/stop/inspect the debuggable Chrome; `--login`
  walks the user through the manual step; `--save-state` snapshots its cookies.
- `pkulaw_playwright.py` — the extractor: search, rows, sections, batch.
- `pkulaw_api.py` — the JSON search API: 本院认为/同段 search, court/案由/date
  filters, hit-count probing, pagination, structured full text. **Prefer this
  over `pkulaw_playwright.py --search` for any WKINFO case task that needs a
  filter, 同段, or more than the first 25 rows.**
- `pkulaw_login.py` — one-off manual login in a dedicated Playwright profile
  (PKULAW/CARSI; cannot complete an NTLM intranet login).
- `pkulaw_check_login.py` — definitive live check of one saved session.
- `pkulaw_fetch.py` — clipboard copy from the user's visible Chrome tab, incl.
  `view-source:`. L4.
- `seed_cookies_from_chrome.py` — copies named-domain cookies out of the user's
  real Chrome profile via `browser_cookie3`. Needs the user and a Keychain
  prompt; macOS-specific. Prefer `pkulaw_chrome.py --login`.
- `common.py` — browser lifecycle, exit codes, row and section extraction.
- `providers.py` — per-site URLs, the four detectors, cookie names. **Add a new
  database here**: define a `Provider`, register it in `PROVIDERS`, done.

The `pkulaw_*` filenames predate WKINFO support and are not PKULAW-only.
