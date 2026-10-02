# pkulaw-browser 安装与使用（Windows）

用脚本读北大法宝和威科先行，把结果压缩成结构化数据再交给 AI。
**不要让 AI 用截图点击的方式操作这两个网站**——又慢又贵，而且这两个站的内容用脚本全都读得到。

---

## 一、一次性安装

需要 Python 3.10 以上、Google Chrome。在 PowerShell 里：

```powershell
pip install playwright
playwright install chromium
```

把 `pkulaw-browser-skill` 整个文件夹放到你放 skill 的地方，例如：

```
C:\Users\<你的用户名>\.agents\skills\pkulaw-browser\
```

以下命令都在 `scripts` 目录里执行：

```powershell
cd C:\Users\<你的用户名>\.agents\skills\pkulaw-browser\scripts
```

登录态和抓取结果会存在 `C:\Users\<你的用户名>\.agents\state\pkulaw-browser\`，
跟你在哪个项目目录下运行无关。

---

## 二、先体检

任何任务开始前先跑这条。它只读文件、探一个端口，不开浏览器，很便宜：

```powershell
python pkulaw_doctor.py
```

它会告诉你每个网站的登录态是 `fresh`（能用）/ `suspect`（一半 cookie 过期了，不确定）/
`stale`（过期了），以及**下一条该运行什么命令**。照着它说的做就行。

---

## 三、登录（关键一步）

威科先行走律所内网（`home.zhonglun.com`）的一键登录。那是 Windows 集成认证（NTLM），
**自动化浏览器弹不出那个原生密码框**，所以不能让脚本去登。

解决办法是：脚本起一个**普通的 Chrome**，你自己在里面登，脚本事后接管它。

```powershell
python pkulaw_chrome.py --login wkinfo
```

会弹出一个 Chrome 窗口打开内网首页。**你在这个窗口里点威科先行的一键登录**，
跟平时一样操作（该输密码就输，脚本不碰你的密码）。

登好之后，把登录态存下来：

```powershell
python pkulaw_chrome.py --save-state wkinfo
```

存完之后**就可以关掉那个 Chrome 窗口了**，后面的检索不需要它。

```powershell
python pkulaw_chrome.py --stop
```

北大法宝不需要这一套，它可以直接 CARSI 登录：

```powershell
python pkulaw_login.py --provider pkulaw
```

> **为什么不是"从我的 Chrome 里复制 cookie"**
> 那条路要从 Chrome 外面读加密的 cookie 数据库，得问 Windows 要解密密钥（DPAPI），
> 麻烦且容易失败。上面这条路**完全不涉及解密**：cookie 是 Chrome 自己交出来的，
> 它内存里本来就是明文。`seed_cookies_from_chrome.py` 那条老路只在实在打不开 Chrome 时才用。

登录态大概能撑一周（威科的 `connect.sid` 只有一天，但 `autologin` 有七天）。
过期了 `pkulaw_doctor.py` 会告诉你，重复上面两条命令即可。

---

## 四、日常检索：用最省的那种形式

**结果的正文写进文件，屏幕上只打印十来行摘要。** 别把整页塞进 AI 的上下文。

### 1. 找案子（列表）

```powershell
python pkulaw_playwright.py --provider wkinfo --library judgment-documents `
  --search "（2021）京01民终9024号" --format rows
```

每条结果一行 JSON，含标题、网址、案号、法院、日期、文书类型。实测比整页文本省 79%。

> **注意 `module` 字段**：`judgment-documents` 是判决书本身，`case-analysis` 是别人写的
> 案例评析/法官评案。**它们的案号是一样的**，光看案号分不出来，引用时别搞混。

### 2. 读某一篇的关键部分

```powershell
python pkulaw_playwright.py "https://law.wkinfo.com.cn/judgment-documents/detail/xxxxx" `
  --fields 本院认为,裁判结果
```

一篇判决书整页约 49KB，只取这两段约 3KB，省 94%，而且要的就是这两段。

### 3. 一次读很多篇

把网址一行一个存进 `urls.txt`：

```powershell
python pkulaw_playwright.py --provider wkinfo --urls-file urls.txt --fields 本院认为,裁判结果
```

只启动一次浏览器，逐条报状态和字节数。

### 4. 整页（少用）

```powershell
python pkulaw_playwright.py --provider pkulaw --library pfnl --search "（2026）京02民终43号"
```

默认就是整页写文件 + 打印摘要。加 `--format text` 才会把整页打到屏幕上，**那是最后手段**。

### 5. 威科类案检索：要过滤条件时用这个

上面第 1 条的 `--search` 走的是网址检索，对威科**判决库**有两个硬伤：只能看到第一屏
约 25 条，而且**任何过滤都失效**（网址上的地域参数会静默返回 0 条）。要按法院地域、
案由、裁判日期缩小，或者要"本院认为里同一段出现这几个词"，用 `pkulaw_api.py`：

```powershell
# 先只看命中量，比几种检索式哪个收得住（不取回结果，很便宜）
python pkulaw_api.py --reason "劳动关系 确认 存在 注销" --court 北京市 --count-only
```

```powershell
# 本院认为·同段·北京·近五年·劳动争议，并打印命中的那一段原文
python pkulaw_api.py --reason "劳动关系 确认 存在 注销" `
  --court 北京市 --cause 劳动争议 --since 2021.08.26 --until 2026.08.26 --frag
```

- `--reason` = 只在「本院认为」里检索，且要求这些词出现在同一段（同段检索）。
  `--search` 是全文，`--title` 是标题。
- `--court 北京市` 会去站点自己的法院树里查这个名字；重名会把候选列出来让你挑，
  不会瞎猜。不知道有哪些节点就先 `--find-courts "苏州|昆山|常熟" --search 关键词`。
- `--frag` 打印命中的那一段原文。**先靠这一段筛掉大部分，再去取全文**，别一上来就下载。
- 结果全文写进文件（默认在 `.agents\state\pkulaw-browser\out\`），屏幕上只出摘要。

> **看到一段好用的话，先确认它是谁说的。** 命中片段来自「被告辩称」的概率和来自法院
> 说理的一样高。另外，「案件历程」写着**二审维持原判，不等于二审采纳了一审的说理**——
> 维持的是结果。引一审的说理之前，必须把二审判决调出来读它的「本院认为」。

### 法宝检索要注意

北大法宝**必须开着可见窗口**，无头模式只会拿到反爬页面。脚本会自动强制，不用你操心，
但看到窗口弹出来是正常的。

---

## 五、退出码：出错时看这个

脚本失败时会打印 `exit N`，照着这张表处理，别原样重跑：

| 码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 成功 | — |
| 2 | 登录墙，但登录态看着是新的 | `python pkulaw_check_login.py --provider wkinfo` |
| 3 | 登录态过期或没有 | `python pkulaw_chrome.py --login wkinfo` |
| 4 | 浏览器起不来 | `playwright install chromium` |
| 5 | 撞上反爬页 | 法宝跑成无头了，加 `--browser attach` |
| 6 | 页面读到了但内容不对 | 检查网址，或放宽 `--expect` |
| 7 | **只拿到元数据，正文被网站扣住了** | 没登录到位，去登录 |

第 7 种最阴险：页面看起来完全正常，有标题有案号有法院，但正文位置写着
「很抱歉，您没有权限查看全文内容」。脚本会明确告诉你，不会假装成功。

---

## 六、给 AI 的硬规矩

如果你让 Claude Code / Codex 用这个 skill，把这段告诉它（`SKILL.md` 里已经写了）：

> 降级顺序：已存会话 → 挂载 Chrome → 手动登录 → cookie 备胎 →
> `pkulaw_fetch.py --chrome-copy` → **到此为止，停下来告诉用户该跑哪条命令**。
> 禁止使用 computer use / claude-in-chrome / 任何截图点击式浏览。
> 登录相关的尝试超过 3 次工具调用就停下来问人。失败的命令不许原样重跑。

这条规矩就是为了防止它一路 fallback 到视觉浏览、把 token 烧光。

---

## 七、卡住了怎么办

先跑 `python pkulaw_doctor.py --live`（这条会真的开浏览器加载一次，慢但准），
它会明确说出是登录问题、浏览器问题还是网站问题，并给出下一条命令。
