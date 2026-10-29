---
name: pkulaw-browser
description: "Search 北大法宝/PKULAW, 威科先行/WKINFO and 人民法院案例库/RMFYALK (rmfyalk.court.gov.cn — 指导性案例 and 参考案例) through a real browser and return compact, structured results. Use when a Chinese legal database is part of the task — case libraries (PKULAW `pfnl`, WKINFO `judgment-documents`), law libraries (PKULAW `law`/`chl`, WKINFO `legislation`), or the Supreme People's Court's own 指导性案例/参考案例 by 入库编号, 案号, 裁判理由 or 裁判要旨. Covers 类案检索 with precision filters: 同段检索 (same-paragraph), 本院认为 court-reasoning-only search, and filtering by 法院地域/案由/裁判日期. Scripts do the reading; screenshot-driven browsing is forbidden here."
---

# PKULAW / WKINFO / 人民法院案例库 Browser

Read these databases with scripts, not with eyes. Every script takes
`--provider pkulaw` (default), `--provider wkinfo` or `--provider rmfyalk`; the
provider decides the search URL scheme, the library names, the failure
signatures, the saved session path, and whether headless is allowed.
人民法院案例库 is read by its own script, `rmfyalk_api.py` — see its section.

Run the scripts from `scripts/`. All state is absolute, under
`~/.agents/state/pkulaw-browser/`, so the working directory does not matter.

## Hard discipline

**Never drive these sites with computer-use, claude-in-chrome, or any
screenshot/click tool.** Not as a fallback, not "just to check", not when a
script fails. A single visual browsing session costs more tokens than a hundred
script runs, and every site here is fully readable through the DOM or its API.

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

For 人民法院案例库, L0/L1 are `rmfyalk_api.py`. It attaches when the debug port
is up and otherwise runs headless, by itself. L2 is
`pkulaw_chrome.py --login rmfyalk`. L3 and L4 do not apply: there is no page
text to copy.

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
about a **week**; RMFYALK's token about an **hour** after the last request
(≤4h in all), though `rmfyalk_api.py` re-mints it by itself while the SSO
cookie lives.

The doctor also names the Chromium build drift: the Python Playwright package
pins one build (1223 for 1.60.0) while a `playwright` CLI on PATH may have
installed another (1228). Every launch now falls back to the installed build
(`common.with_chromium_fallback`) instead of dying with "Executable doesn't
exist"; the stderr line `chromium: pinned build … missing, using installed …` is
that fallback, not an error.

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

## Five ways these sites fail, and how to tell them apart

Four of the five return an HTTP 200 with real-looking content. Do not describe
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
5. **JSON-level gate** (RMFYALK, exit 3) — every API call answers HTTP 200 with
   `{"code":401,"msg":"未登录"}`: no redirect, no HTML, nothing a page check
   sees. Only the JSON `code` tells you. `rmfyalk_api.py` checks it on every
   call. A filter the API does not understand is a quieter version of the same
   thing: it is ignored, not refused — see the RMFYALK section.

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

人民法院案例库 (RMFYALK) is a different kind of source, not a third copy of the
same judgments: ~5,600 cases the Supreme People's Court selected, edited and
issued as 指导性案例 or 参考案例, each with a 裁判要旨/裁判要点 written for
reuse. Go there when the question is "is there an authoritative case on this",
when the user cites a 入库编号 or 指导性案例 number, or before relying on a
PKULAW/WKINFO judgment, to check whether the SPC has issued a case on the same
point. It has no statutes and no ordinary judgments.

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

## 人民法院案例库 (RMFYALK) — `rmfyalk_api.py`

`rmfyalk.court.gov.cn` is jQuery pages over a JSON API (`/cpws_al_api/api/`).
The pages carry nothing worth reading, so this provider has no DOM path:
`pkulaw_playwright.py` refuses it and names `rmfyalk_api.py`. Headless works,
logged in or out; the 奇安信 cloud WAF in front (WZWS) did not object.
Connections do **time out intermittently**. After a burst of test calls, roughly
one connect in three hung for several minutes, with no error page. Whether that
is the WAF throttling or the network path was not established. The script backs
off and retries; if it still exits on "the site stopped answering", wait
rather than retry.

**Login.** OAuth through 人民法院互联网统一账户 (`account.court.gov.cn`): 手机号
+密码, 支付宝 or 钉钉, always by the user:

```bash
python3 pkulaw_chrome.py --login rmfyalk      # user logs in by hand
python3 pkulaw_chrome.py --save-state rmfyalk # snapshot; headless from then on
```

The login is the JWT cookie `faxin-cpws-al-token`. Its cookie expiry is 1h and
every call slides it; the JWT itself expires ~4h after issue. `HOLDONKEY` on
`account.court.gov.cn` is the SSO session. While it lives, `rmfyalk_api.py`
replays the site's own OAuth redirect on a 401, gets a fresh token with no
password, headless, and saves it back. So a dead token is exit-3-worthy only
when that fails too. `--refresh` does just that step and prints both expiries.
How long `HOLDONKEY` lasts server-side is not measured yet. Saved state keeps
only `court.gov.cn` cookies, mode 600.

**Narrow, then fetch.** Count first, then rows, then sections for survivors:

```bash
python3 rmfyalk_api.py --search '劳动关系' --count-only           # 140 (指导性案例 15, 参考案例 125)
python3 rmfyalk_api.py --reason '事实劳动关系' --lib ck --court 北京市 --frag
python3 rmfyalk_api.py --no 2024-12-3-007-006 --fields 裁判要旨,裁判理由
python3 rmfyalk_api.py --case-no '（2022）京02行终909号' --fields all
python3 rmfyalk_api.py --search 劳动关系 --find-nodes court 北京
```

- Term flags, each repeatable and all ANDed (the site's 结果中检索): `--search`
  全文, `--title`, `--reason` 裁判理由, `--facts` 基本案情, `--keyword` the
  case's own tags, `--no` 入库编号. `--fuzzy` switches 精确 → 模糊. It is a big
  switch: 劳动关系 goes from 140 to 2,732.
- `--lib zdx|ck|all`: 指导性案例, 参考案例, both.
- Facet filters `--court --cause --level --procedure --year --tag` take a name,
  resolved against that facet *under the current query* (only nodes with hits),
  or a raw key. A parent covers its children: `--court 北京市` (key `02`) is
  every Beijing court. An ambiguous name is listed, not guessed. **One value
  per facet**: a comma-joined pair came back as neither OR nor AND.
- `--case-no` is a 高级检索 field and runs alone, with no term flags (the builder
  enforces it). Sent the general way, it is **silently ignored**: 5,622 of
  5,622 came back. Substrings work: `京02行终` → 6.
- `--sort relevance|date-desc|date-asc|no-desc|no-asc`. Pages of up to 100;
  `--max` defaults to 50.

**Output.** One JSON line per case: `no` (入库编号), `type`
(指导性案例/参考案例), `title`, `case_no`, `court`, `date`, `infos`, `holding`
(the 裁判要旨/裁判要点, already in the list response, so triage is free),
`status` (raw `cpws_al_status`; only `01` seen, meaning unverified), `id`, and
with `--fields` a `sections` map. The map is keyed by the heading the page
shows, which changes with the corpus and with 执行/调解 cases: 裁判要点 vs
裁判要旨, 相关法条 vs 关联索引, 执行理由, 调解指引. Plus `keywords` and
`text_adjusted` when the SPC has revised the text.

**Before citing.**

- **Name the corpus.** 指导性案例 are 应当参照; 参考案例 are 参考. A row's `type`
  says which. Never call a 参考案例 a 指导性案例.
- **Cite by 入库编号**, never by `id`: ids are re-encrypted on every response
  (old ones still resolve), so they cannot dedupe or identify anything.
- **关联索引 carries the 审级 history**, e.g. 一审：…（2021）京0101行初862号 /
  二审：…. These are the underlying judgments; the 裁判理由 here is the SPC's
  edited text of the 生效裁判, not a verbatim judgment. Quote the judgment
  itself from PKULAW/WKINFO by those case numbers.
- `text_adjusted` means the text was revised after first issue. Quote the
  current text and say so.

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

Do not fetch any of these sites over plain HTTP. `pkulaw_fetch.py` refuses to
run without `--chrome-copy`. Direct requests and headless automation reliably
trip anti-bot defenses. The API scripts (`pkulaw_api.py`, `rmfyalk_api.py`)
send their requests from a Playwright browser context, which shares the
session's cookie jar, never from a bare HTTP client.

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
- `rmfyalk_api.py` — 人民法院案例库: 全文/标题/裁判理由/基本案情/入库编号/案号
  search, 指导性/参考 split, facet filters, counts, sections per case, silent
  token refresh. The only reader for that site.
- `pkulaw_login.py` — one-off manual login in a dedicated Playwright profile
  (PKULAW/CARSI; cannot complete an NTLM intranet login).
- `pkulaw_check_login.py` — definitive live check of one saved session.
- `pkulaw_fetch.py` — clipboard copy from the user's visible Chrome tab, incl.
  `view-source:`. L4.
- `seed_cookies_from_chrome.py` — copies named-domain cookies out of the user's
  real Chrome profile via `browser_cookie3`. Needs the user and a Keychain
  prompt; macOS-specific. Prefer `pkulaw_chrome.py --login`.
- `common.py` — browser lifecycle, exit codes, row and section extraction.
- `providers.py` — per-site URLs, the detectors, cookie names, the RMFYALK
  body builder and section labels. **Add a new database here**: define a
  `Provider`, register it in `PROVIDERS`. If its pages are empty shells over
  an API, set `api_script` and write that script the way `rmfyalk_api.py` is
  written.

The `pkulaw_*` filenames predate WKINFO and RMFYALK support and are not
PKULAW-only.
