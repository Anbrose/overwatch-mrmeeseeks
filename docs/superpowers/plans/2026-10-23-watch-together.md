# 一起看（同步播放房间）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 频道里 `@mrmeeseeks 一起看 <B站/YouTube链接>` 开一个房间，bot 发一张会自动更新的卡片；大家点链接进入 `https://watch.pigdogie.com.au/r/<房间号>` 的深色网页播放器，播放/暂停/进度同步，房主在网页里给人开关控制权、换视频。

**Architecture:**
- **房间逻辑**：`watch_rooms.py` 是纯逻辑（不依赖 discord 和网络），管成员、房主交接、权限、播放状态、空房关闭、B 站房间上限。通过 `listeners` 回调把变化告诉网页服务和 bot。
- **视频来源**：`watch_sources.py` 解析链接、取标题；B 站用 `SESSDATA` 调播放地址接口，解析出 DASH 视频/音频流。解析函数是纯函数，用 fixture 测。
- **网页服务**：`watch_server.py` 用 aiohttp（discord.py 已经依赖它，不用新加）提供页面、WebSocket 同步协议、B 站转流（生成 MPD + 带 Range 的代理）。
- **网页**：`server/watch_web/` 下的静态 HTML/JS/CSS，YouTube 用 IFrame API，B 站用 dash.js 播放我们生成的 MPD。
- **Discord**：`watch_ui.py` 负责卡片文字、"我是房主"持久按钮和节流编辑；`bot.py` 加指令和 `main()` 接线。

**Tech Stack:** Python 3.12（生产镜像）、aiohttp（`aiohttp.web` + `aiohttp.test_utils`）、discord.py ≥ 2.6（`DynamicItem`）、浏览器端原生 JS + dash.js 4.7.4（cdnjs）+ YouTube IFrame API。测试沿用仓库风格：`python tests/test_xxx.py`，自带 `check()`；**所有测试都在生产镜像里跑**（本机 Python 3.13 装不上 rapidocr）。

**Spec:** `docs/superpowers/specs/2026-10-23-watch-together-design.md`

**分支：** `watch-together`（从 `main` c4406ff 拉出，已包含 spec 提交）。

## Global Constraints

- 测试命令：先 `docker build -q -t mrmeeseeks:test . >/dev/null`，再 `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/<file>.py`。下文写作 `RUN tests/<file>.py`。
- 指令：`一起看` 和 `watch` 都触发，加进 `bot.ARG_COMMANDS`。
- 只接受 B 站和 YouTube：B 站 `bilibili.com/video/BV...`（含 `?p=`）、`b23.tv/<短码>`；YouTube `youtube.com/watch?v=`、`youtu.be/`、`youtube.com/shorts/`。其他链接拒绝。
- 进房默认**只能看**；只有房主能开关别人的控制权、换视频；控制权 = 播放、暂停、拖进度条。
- 房主交接：房间里没有房主时，交给**当前在房间里最早进来**的人。发起人**第一次**打开房主链接时拿回房主身份；房主链接用过一次之后作废（再打开就是普通观众）。原房主离开后回来不会拿回。
- 断线宽限 **30 秒**（按 `viewer_id`，同一浏览器多个标签页算一个人）。空房保留 **600 秒**后关闭。
- B 站画质上限 `WATCH_MAX_QUALITY`（`1080`/`720`/`480`，默认 `1080`，对应 qn 80/64/32）。同时正在播 B 站的房间最多 `WATCH_MAX_BILI_ROOMS`（默认 2），超出时拒绝并提示 `B 站房间已满（最多 N 个），请稍后再试或换 YouTube 视频`。
- 大陆限定等放不了的视频，提示 `这个视频在海外服务器上无法播放`。
- 卡片每条**最多每 15 秒编辑一次**，期间的变化合并成一次延后编辑；发送都用 `allowed_mentions=AllowedMentions.none()`。
- 卡片和网页文字用中文。网页深色。普通观众看不到名单和人数；只有房主有"🔑 权限"面板。
- 新 `.env` 项：`WATCH_PUBLIC_URL`（不设则功能关闭，指令回复 `一起看功能没有启用。`）、`WATCH_HOST`（默认 `0.0.0.0`）、`WATCH_PORT`（默认 `8080`）、`BILIBILI_SESSDATA`、`WATCH_MAX_QUALITY`、`WATCH_MAX_BILI_ROOMS`。
- `BILIBILI_SESSDATA` 绝不进仓库、日志或测试 fixture。
- 部署前先 `git fetch origin && git merge origin/main`（`deploy.sh` 是 `rsync --delete`）。部署本身要用户确认后再做。

## Review Focus

1. **长视频看到一半 B 站播放地址过期**（拖进度条时 CDN 返回 403）→ 代理自动重新获取地址并重试一次，观众只感到一次短暂缓冲。测试在 Task 4。
2. **房主同时开了两个标签页、关掉一个** → 仍是房主，人数不变。测试在 Task 1。
3. **B 站房间已满时房主把 YouTube 换成 B 站**（或换视频时链接放不了）→ 拒绝并提示，原视频照常播放，状态不变。测试在 Task 1（满）和 Task 4（放不了）。
4. **恶意或损坏的 WebSocket 消息**（不是 JSON、`position` 为负数/NaN/超过时长、没权限的人发控制、普通观众发 `grant`）→ 忽略或夹到合法范围，连接不断，状态不被破坏。测试在 Task 4。
5. **@ 时链接前后夹着别的字**（`一起看 这个超好笑 https://b23.tv/xxxx 哈哈`）→ 取第一个合法链接；没有合法链接时回复用法。测试在 Task 2。

---

### Task 0: 服务器实测 B 站播放（关卡，不写产品代码）

spec 要求动工前确认这台海外服务器能拿到 B 站 1080p 播放地址。测不通就**停下来找用户**，不要继续后面的任务。

**Files:**
- Create（脚本放 scratchpad，不进仓库）：`<scratchpad>/bili_probe.sh`
- Create: `tests/fixtures/watch/bili_view.json`、`tests/fixtures/watch/bili_playurl_dash.json`、`tests/fixtures/watch/bili_playurl_region.json`

- [ ] **Step 1: 向用户要 SESSDATA 的使用许可和一个普通视频的 BV 号**（用户已在对话里提供过 SESSDATA；用之前确认可以在服务器上用 ssh 跑一次性测试）。

- [ ] **Step 2: 写探测脚本**：从 stdin 读 SESSDATA（不放在命令行参数里），对给定 BV 依次请求：
  - `GET https://api.bilibili.com/x/web-interface/view?bvid=<BV>` → 取 `data.title`、`data.pages[p-1].cid`、`data.duration`
  - `GET https://api.bilibili.com/x/player/playurl?bvid=<BV>&cid=<cid>&qn=80&fnval=16&fourk=0`（带 `Cookie: SESSDATA=...`、`Referer: https://www.bilibili.com`、桌面 UA）→ 打印 `data.accept_quality`、`data.dash.video[*].id/codecid`
  - 对一个 `data.dash.video[0].baseUrl` 发 `Range: bytes=0-1023`，打印 HTTP 状态码。
  如果 `x/player/playurl` 返回 `-352`/`-403` 之类风控错误，改用 `x/player/wbi/playurl`（需要 WBI 签名），把结论记到 Step 4。

- [ ] **Step 3: 在服务器上运行**：`ssh -i ~/.ssh/jobseeker_deploy deploy@5.223.69.203 'bash -s' < bili_probe.sh`（SESSDATA 通过 heredoc 传）。同时跑 `which caddy; systemctl is-active caddy; vnstat --oneline 2>/dev/null` 记录 Caddy 和流量情况。再用一个已知大陆限定的视频（如番剧正片）跑一次。

Expected：普通视频 `accept_quality` 含 `80`，`dash.video` 里有 `id=80` 且 `codecid=7`（AVC），Range 请求返回 `206`。

- [ ] **Step 4: 保存 fixture**：把两次的 view / playurl 返回存进上面三个 fixture 文件，把 `baseUrl`/`backupUrl` 的查询参数换成 `?deadline=1792800000&x=1`（原参数里有签名，不进仓库），确认文件里没有 SESSDATA。在 plan 末尾"实测记录"一节写下：用的是哪个接口、是否需要 WBI、Caddy 是否已装、大陆限定视频返回的 `code`/`message`。

- [ ] **Step 5: 判定**：普通视频拿不到 qn 80 或 CDN 不是 206 → 停，带着输出找用户。否则 commit fixture：

```bash
git add tests/fixtures/watch/
git commit -m "Add Bilibili API fixtures for watch-together"
```

---

### Task 1: 房间逻辑 `watch_rooms.py`

**Files:**
- Create: `server/watch_rooms.py`
- Test: `tests/test_watch_rooms.py`

**Interfaces:**
- Produces:
  ```python
  GRACE_SECONDS = 30; EMPTY_TTL_SECONDS = 600
  class RoomsFull(Exception): limit: int
  @dataclass class Video: source: str  # "bilibili" | "youtube"
                          id: str; page: int; title: str; duration: float | None; url: str
  @dataclass class Member: viewer_id: str; name: str; joined_at: float; can_control: bool = False
                           connections: int = 0; gone_since: float | None = None
  @dataclass class Playback: playing: bool; position: float; at: float
      def position_at(self, now: float, duration: float | None) -> float   # 播放中按时间推进，夹到 [0, duration]
  @dataclass class Room: room_id: str; channel_id: int; creator_id: int; creator_name: str; owner_key: str
                         video: Video; playback: Playback; members: dict[str, Member]
                         host_id: str | None = None; key_used: bool = False; message_id: int | None = None
                         empty_since: float | None = None; closed: bool = False; version: int = 0   # 每次换视频 +1
      def present(self) -> list[Member]          # connections > 0 或在宽限期内，按 joined_at 排序
      def viewer_count(self) -> int
      def host_name(self) -> str                 # 房主是用过房主链接的人 / 没房主 → creator_name；否则房主随机名字
  class RoomManager:
      def __init__(self, clock: Callable[[], float] = time.time, max_bili_rooms: int = 2)
      listeners: list[Callable[[Room, str], None]]   # kind ∈ "members" "playback" "video" "permissions" "host" "closed"
      def create(self, channel_id, creator_id, creator_name, video: Video) -> Room      # 暂停在 0；B 站满时 raise RoomsFull
      def get(self, room_id: str) -> Room | None                                          # 已关闭的返回 None
      def connect(self, room_id, viewer_id: str | None, key: str | None) -> Member        # viewer_id 为 None/未知则新建
      def disconnect(self, room_id, viewer_id) -> None
      def control(self, room_id, viewer_id, action: str, position: float | None) -> bool  # "play"|"pause"|"seek"
      def set_permission(self, room_id, actor_id, target_id, allowed: bool) -> bool
      def change_video(self, room_id, actor_id, video: Video) -> bool                     # 非房主 False；B 站满 raise RoomsFull
      def tick(self) -> None                       # 清理超过宽限的成员、交接房主、空房到期关闭
      def active(self) -> list[Room]
  def random_name(taken: set[str], rng: random.Random | None = None) -> str              # "快乐的企鹅"，房间内不重复
  ```
- 房间号：6 位，字符集 `23456789abcdefghjkmnpqrstuvwxyz`，用 `secrets`。`owner_key = secrets.token_urlsafe(16)`，`viewer_id = secrets.token_urlsafe(12)`。
- 每次状态变化后调用所有 `listeners`；某个 listener 抛异常时记日志，不影响其他 listener。

- [ ] **Step 1: 写失败的测试**（假时钟 `now = [1000.0]`，`clock=lambda: now[0]`）：

```python
def test_defaults_and_permissions():
    m, r = mk()                                   # create 一个 YouTube 房间
    check("新房间暂停在 0", r.playback.playing is False and r.playback.position == 0)
    a = m.connect(r.room_id, None, None)
    check("第一个进来的人暂时当房主", r.host_id == a.viewer_id)
    b = m.connect(r.room_id, None, None)
    check("默认没有控制权", b.can_control is False and not m.control(r.room_id, b.viewer_id, "play", None))
    check("普通观众不能给权限", not m.set_permission(r.room_id, b.viewer_id, b.viewer_id, True))
    check("房主给权限", m.set_permission(r.room_id, a.viewer_id, b.viewer_id, True) and b.can_control)
    check("有权限能播放", m.control(r.room_id, b.viewer_id, "play", None) and r.playback.playing)
    check("有权限的人不能换视频", not m.change_video(r.room_id, b.viewer_id, yt("x2")))

def test_owner_key_claim_once():
    m, r = mk(); a = m.connect(r.room_id, None, None)
    o = m.connect(r.room_id, None, r.owner_key)
    check("房主链接拿回房主", r.host_id == o.viewer_id and r.key_used)
    m.disconnect(r.room_id, o.viewer_id); now[0] += 31; m.tick()
    check("房主离开后交给最早进来的人", r.host_id == a.viewer_id)
    o2 = m.connect(r.room_id, None, r.owner_key)
    check("用过的房主链接作废", r.host_id == a.viewer_id and o2.viewer_id != a.viewer_id)

def test_grace_and_tabs():
    m, r = mk(); a = m.connect(r.room_id, None, None); m.connect(r.room_id, a.viewer_id, None)   # 两个标签页
    m.disconnect(r.room_id, a.viewer_id)
    check("关一个标签页仍在、仍是房主", r.viewer_count() == 1 and r.host_id == a.viewer_id)
    m.disconnect(r.room_id, a.viewer_id); now[0] += 29; m.tick()
    check("宽限期内还算在房间", r.viewer_count() == 1)
    again = m.connect(r.room_id, a.viewer_id, None)
    check("宽限期内回来保留名字和身份", again.name == a.name and r.host_id == a.viewer_id)

def test_empty_room_closes():
    m, r = mk(); a = m.connect(r.room_id, None, None); m.disconnect(r.room_id, a.viewer_id)
    now[0] += 30 + 599; m.tick(); check("不到 10 分钟还在", m.get(r.room_id) is not None)
    now[0] += 2; m.tick(); check("空房 10 分钟后关闭", m.get(r.room_id) is None and r.closed)
    check("关闭时通知 listener", ("closed", r.room_id) in events)

def test_bili_limit():
    m = RoomManager(clock, max_bili_rooms=1); m.create(1, 9, "小明", bili("BV1"))
    check("B 站满了拒绝新房间", raises(RoomsFull, lambda: m.create(1, 9, "小明", bili("BV2"))))
    r = m.create(1, 9, "小明", yt("a")); h = m.connect(r.room_id, None, r.owner_key)
    check("满了不能换成 B 站且原视频不变",
          raises(RoomsFull, lambda: m.change_video(r.room_id, h.viewer_id, bili("BV3"))) and r.video.id == "a")

def test_playback_math_and_seek_clamp():
    # play 后 now += 10 → position_at == 10；seek -5 → 0；seek 超过 duration → duration；NaN → 拒绝(False)
    # change_video 后：暂停在 0、version + 1、所有人控制权保留
```

另外：`random_name` 在 `taken` 包含所有组合时追加数字后缀，不死循环。

- [ ] **Step 2: 运行确认失败**：`RUN tests/test_watch_rooms.py` → `ModuleNotFoundError: watch_rooms`

- [ ] **Step 3: 实现 `server/watch_rooms.py`**，签名见 Interfaces。房主交接集中在一个私有方法 `_ensure_host(room)`，在 connect / tick 后调用：房主不在 `present()` 里就换成 `present()[0]`；房间空了就 `host_id = None`。形容词、动物各约 20 个，写在模块里。

- [ ] **Step 4: 运行确认通过**：`RUN tests/test_watch_rooms.py` → 全部 PASS

- [ ] **Step 5: Commit** `git add server/watch_rooms.py tests/test_watch_rooms.py && git commit -m "Add watch-together room state"`

---

### Task 2: 链接解析和视频来源 `watch_sources.py`

**Files:**
- Create: `server/watch_sources.py`
- Test: `tests/test_watch_sources.py`

**Interfaces:**
- Consumes: Task 1 的 `Video`；Task 0 的 fixture。
- Produces:
  ```python
  QUALITY_QN = {"1080": 80, "720": 64, "480": 32}
  class VideoUnavailable(Exception): message: str          # 给用户看的中文
  @dataclass class Link: source: str; id: str; page: int = 1; short: bool = False   # short=True 表示 b23.tv 未展开
  def find_link(text: str) -> Link | None                   # 取文本里第一个合法链接
  @dataclass class DashStreams: video_url: str; video_backup: list[str]; audio_url: str; audio_backup: list[str]
                                video_codecs: str; audio_codecs: str; width: int; height: int; frame_rate: str
                                video_bandwidth: int; audio_bandwidth: int; duration: float
                                video_init: str; video_index: str; audio_init: str; audio_index: str   # SegmentBase 范围
                                expires_at: float
  def parse_view(data: dict, page: int) -> tuple[str, int, float]          # (title, cid, duration)；code≠0 → VideoUnavailable
  def parse_playurl(data: dict, max_qn: int, now: float) -> DashStreams    # 选 ≤max_qn 的最高档，同档优先 codecid 7（AVC）
  class Sources:
      def __init__(self, sessdata: str | None, max_qn: int = 80, session_factory=...)
      async def resolve(self, text: str) -> Video               # 展开 b23.tv、取标题和时长；不合法 → VideoUnavailable
      async def bili_streams(self, video: Video) -> DashStreams # 每次调用都重新请求播放地址
  ```
- `expires_at`：取 `baseUrl` 里的 `deadline` 参数，没有就 `now + 1800`。
- 大陆限定 / 下架：view 或 playurl 的 `code` 非 0 时，`VideoUnavailable("这个视频在海外服务器上无法播放")`；Task 0 记录的具体 code 写进注释。
- YouTube 标题：`https://www.youtube.com/oembed?url=<watch url>&format=json` 的 `title`；oEmbed 失败（如禁止嵌入，返回 401/404）→ `VideoUnavailable("这个 YouTube 视频不允许嵌入播放")`。YouTube `duration=None`（播放器端上报，见 Task 4）。
- HTTP 用 aiohttp，UA 沿用 `live_watch.USER_AGENT`；B 站请求带 `Referer: https://www.bilibili.com`，有 SESSDATA 时带 Cookie。

- [ ] **Step 1: 写失败的测试**：

```python
def test_find_link():
    check("BV", find_link("一起看 https://www.bilibili.com/video/BV1xx411c7mD") == Link("bilibili", "BV1xx411c7mD"))
    check("分P", find_link("bilibili.com/video/BV1xx411c7mD?p=3&t=10").page == 3)
    check("短链", find_link("这个超好笑 https://b23.tv/abc123 哈哈") == Link("bilibili", "abc123", short=True))
    check("youtu.be", find_link("https://youtu.be/dQw4w9WgXcQ?si=x") == Link("youtube", "dQw4w9WgXcQ"))
    check("watch", find_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30").id == "dQw4w9WgXcQ")
    check("shorts", find_link("https://youtube.com/shorts/dQw4w9WgXcQ").id == "dQw4w9WgXcQ")
    check("其他网站拒绝", find_link("https://evil.example.com/video/BV1xx411c7mD") is None)
    check("没有链接", find_link("一起看") is None)

def test_parse_fixtures():
    title, cid, dur = parse_view(load("bili_view.json")["data"] ..., 1)   # 断言和 fixture 的真实值一致
    s = parse_playurl(load("bili_playurl_dash.json"), 80, now=0)
    check("1080p AVC", s.height == 1080 and s.video_codecs.startswith("avc1"))
    check("画质上限 720", parse_playurl(load("bili_playurl_dash.json"), 64, 0).height <= 720)
    check("有 SegmentBase", s.video_init and s.video_index)
    check("大陆限定报中文错误", raises_msg(VideoUnavailable, "这个视频在海外服务器上无法播放",
                                         lambda: parse_playurl(load("bili_playurl_region.json"), 80, 0)))
```

`resolve` 的 b23.tv 展开：用假的 session（返回 302 `Location`）测，不联网。

- [ ] **Step 2: 运行确认失败**：`RUN tests/test_watch_sources.py` → `ModuleNotFoundError`
- [ ] **Step 3: 实现 `server/watch_sources.py`**，签名见 Interfaces。
- [ ] **Step 4: 运行确认通过**：`RUN tests/test_watch_sources.py` → 全部 PASS
- [ ] **Step 5: Commit** `git commit -m "Add watch-together link parsing and Bilibili stream resolution"`

---

### Task 3: 网页播放器 `server/watch_web/`

先做网页，Task 4 的服务器测试会用到这些静态文件的路径。

**Files:**
- Create: `server/watch_web/index.html`、`server/watch_web/player.js`、`server/watch_web/style.css`

**Interfaces（和 Task 4 的 WebSocket 协议，两边必须一致）：**
- 页面从 `location.pathname` 取房间号，从 `?k=` 取房主密钥（连接成功后用 `history.replaceState` 从地址栏去掉）；`localStorage["watch_viewer_id"]` 存 `viewer_id`，读写包在 try/catch 里。
- 连接 `wss://<host>/ws/<room_id>`（http 页面用 `ws://`）。
- 客户端 → 服务器：
  - `{"type":"hello","viewer_id":str|null,"key":str|null}`
  - `{"type":"ping","t":<performance.now()>}`（连上后每 10 秒一次）
  - `{"type":"control","action":"play"|"pause"|"seek","position":float}`
  - `{"type":"duration","value":float}`（YouTube 播放器加载后上报一次）
  - `{"type":"grant","target":viewer_id,"allowed":bool}`
  - `{"type":"change_video","url":str}`
- 服务器 → 客户端：
  - `{"type":"welcome","viewer_id","name","is_host","can_control"}`
  - `{"type":"pong","t","server_time"}`
  - `{"type":"state","video":{"source","id","title","duration","version","mpd"},"playing","position","server_time"}`。`mpd` 只对 B 站有值，是 `/bili/<room>/<version>/manifest.mpd`
  - `{"type":"you","is_host","can_control"}`
  - `{"type":"members","members":[{"viewer_id","name","can_control"}]}`（只发给房主，不含房主自己）
  - `{"type":"error","message"}`；`{"type":"ended"}`

**页面行为（照 spec "网页"一节和 `player-v2.html` 草图）：**
- 深色：背景 `#0f1115`，文字 `#e6e6e6`，强调色 `#7c8cff`；颜色定义成 `:root` 变量。手机宽度可用。
- 标题一行 → 视频区（YouTube: IFrame API `controls=0, disablekb=1`；B 站: `<video>` + `dashjs.MediaPlayer().initialize(video, mpd, false)`）→ 自绘控件：播放/暂停、进度条、时间、音量、全屏。没有控制权时播放/暂停和进度条禁用（灰色）。右下角小字 `你是「<name>」`。
- 第一次进房显示遮罩 `点击开始观看`，点击后才开始播放（满足浏览器自动播放限制）。
- 房主：右上角 `🔑 权限` 按钮，展开面板：标题 `谁能控制播放`、每人一个开关、一个链接输入框和 `换视频` 按钮。
- **同步**：用 ping/pong 的往返时间估算 `offset = server_time + rtt/2 - 本地时间`，取最近 5 次中 rtt 最小的那次。期望位置 `= position + (playing ? (now_server - server_time) : 0)`。每 1 秒比较一次：
  - 差距 > 2 秒，或者处于暂停状态：直接跳到期望位置。
  - 0.3–2 秒：B 站把 `playbackRate` 设为 1.05 或 0.95，YouTube 设为 1.25 或 0.75（YouTube 只支持这几档速度），追上后恢复 1。
  - 播放/暂停状态不一致：照服务器的来。
- 只有本人亲手操作才发 `control`。服务器下发状态导致的播放器事件不能再发回去，否则会形成回声循环。
- 收到 `ended`：视频区换成 `房间已结束`。WebSocket 断开时 2 秒后自动重连，发同一个 `viewer_id`。

- [ ] **Step 1: 写三个文件。**
- [ ] **Step 2: 本地手动看样子**：`python -m http.server -d server/watch_web 8099`，打开 `http://localhost:8099/index.html`。没有服务器时页面显示 `连接中…`，不能报 JS 错误。深色布局和草图一致。（完整联调在 Task 4 Step 6。）
- [ ] **Step 3: Commit** `git commit -m "Add watch-together web player"`

---

### Task 4: 网页服务 `watch_server.py`

**Files:**
- Create: `server/watch_server.py`
- Test: `tests/test_watch_server.py`

**Interfaces:**
- Consumes: Task 1 `RoomManager`/`Room`/`RoomsFull`；Task 2 `Sources`/`DashStreams`/`VideoUnavailable`；Task 3 协议。
- Produces:
  ```python
  class WatchServer:
      def __init__(self, rooms: RoomManager, sources: Sources, host: str, port: int,
                   web_dir: Path = Path(__file__).parent / "watch_web")
      app: aiohttp.web.Application
      async def start(self) -> None; async def close(self) -> None
      async def run(self) -> None        # 每 5 秒 rooms.tick()；每天 UTC 0 点记一次 B 站转流字节数（log.info）
  def build_mpd(s: DashStreams, room_id: str, version: int) -> str
  ```
- 路由：
  - `GET /r/{room_id}` → `index.html`；房间不存在时返回 404 页面，文字 `房间不存在或已经结束`。
  - `GET /static/{name}` → `watch_web` 下的文件，只允许文件名白名单。
  - `GET /ws/{room_id}` → WebSocket。
  - `GET /bili/{room_id}/{version}/manifest.mpd` 和 `GET /bili/{room_id}/{version}/{video|audio}` → 转流。
- **转流**：每个 `(room_id, version)` 缓存一份 `DashStreams`。代理只请求缓存里的 `video_url`/`audio_url`/backup，转发客户端的 `Range` 头，回传状态码、`Content-Range`、`Content-Length`、`Content-Type`。上游返回 403，或者已经过了 `expires_at - 60` 时，调用 `sources.bili_streams` 重新取地址并重试一次。房间不存在、`version` 不是当前版本、`kind` 不在 {video, audio} 时一律返回 404。服务器绝不请求任何客户端提供的 URL。
- **WebSocket 处理**：第一条消息必须是 `hello`（10 秒超时）→ `rooms.connect`。之后：
  - 非 JSON、类型未知、字段类型不对：忽略。
  - `position` 必须是有限数，否则忽略。
  - `duration` 只在 `video.duration is None` 时接受。
  - `change_video`：先 `sources.resolve`，`VideoUnavailable` 或 `RoomsFull` 时只给发起人回 `error`，消息内容见 Global Constraints。
  - 断开时 `rooms.disconnect`。
- 在 `rooms.listeners` 里登记一个回调，按变化类型推送：
  - `playback`/`video`：给房间所有人发 `state`。
  - `permissions`/`host`：给受影响的人发 `you`，给房主发 `members`。
  - `members`：给房主发 `members`。
  - `closed`：给所有人发 `ended` 并关闭连接。

- [ ] **Step 1: 写失败的测试**（`aiohttp.test_utils.TestServer` + `TestClient`。上游 B 站用另一个本地 aiohttp 假服务器模拟，`Sources` 用假实现）：

```python
async def test_ws_flow():
    # 两个客户端 hello；房主 grant；被授权者 control play → 两边都收到 state(playing=True)
    # 无权限者 control pause → 状态不变、连接仍开着
    check("非 JSON 被忽略", ...)                 # 发 "garbage" 后再 ping 仍收到 pong
    check("NaN/负数 position", ...)              # {"position": "NaN"} 忽略；-5 → 0
    check("普通观众发 grant 无效", ...)
    check("members 只发给房主", ...)

async def test_change_video_errors():
    check("放不了的视频只回给发起人且原视频不变", ...)   # 假 Sources.resolve raise VideoUnavailable
    check("B 站满了提示", ...)

async def test_bili_proxy():
    check("MPD 指向本服务器", "/bili/" + rid + "/0/video" in mpd and "SegmentBase" in mpd)
    check("Range 透传 206", resp.status == 206 and resp.headers["Content-Range"].startswith("bytes 0-1023/"))
    check("上游 403 后重新取地址重试一次", resp.status == 206 and fake_sources.calls == 2)
    check("旧版本号 404", (await get(f"/bili/{rid}/99/video")).status == 404)
    check("未知 kind 404", (await get(f"/bili/{rid}/0/..%2Fetc")).status == 404)
    check("关闭的房间 404", ...)

async def test_pages():
    check("房间页 200", ...); check("不存在的房间 404 文案", "房间不存在或已经结束" in text)
    check("static 白名单外 404", (await get("/static/../watch_server.py")).status == 404)
```

- [ ] **Step 2: 运行确认失败**：`RUN tests/test_watch_server.py`
- [ ] **Step 3: 实现 `server/watch_server.py`**。`build_mpd` 生成一个静态 MPD，包含两个 AdaptationSet（video、audio），各有一个 Representation，带 `BaseURL` 和 `SegmentBase indexRange` / `Initialization range`，`mediaPresentationDuration` 取 `duration`。
- [ ] **Step 4: 运行确认通过**：`RUN tests/test_watch_server.py` → 全部 PASS
- [ ] **Step 5: Commit** `git commit -m "Add watch-together web server with sync protocol and Bilibili proxy"`
- [ ] **Step 6: 本地联调（不连 Discord）**：写一个 scratchpad 脚本，启动 `WatchServer`，建一个 YouTube 房间和一个 B 站房间（B 站需要 SESSDATA，从环境变量读），打印两个房间链接和房主链接。用两个浏览器窗口打开：确认房主给权限后另一边能控制，暂停后另一边 1 秒内停下，拖进度条两边一致，换视频两边一起切换。如果本机连不上 B 站 CDN，只验证 YouTube，B 站留到部署后验证。把结果记到 plan 末尾"实测记录"。

---

### Task 5: Discord 接入 `watch_ui.py` + `bot.py`

**Files:**
- Create: `server/watch_ui.py`
- Modify: `server/bot.py`（`ARG_COMMANDS`、`HELP_TEXT`、`MeeseeksBot.__init__`/`setup_hook`/`on_ready`/`on_message`、新增 `_watch`、`main()`）
- Modify: `server/.env.example`
- Test: `tests/test_watch_bot.py`

**Interfaces:**
- Consumes: Task 1 `RoomManager`；Task 2 `Sources`/`VideoUnavailable`；Task 4 `WatchServer`。
- Produces:
  ```python
  # watch_ui.py
  MIN_EDIT_INTERVAL = 15.0
  def card_text(room: Room, now: float, ended: bool = False, reason: str = "") -> str
  class HostButton(discord.ui.DynamicItem[discord.ui.Button], template=r"watch:host:(?P<room>[23456789a-z]{6})")
      handler: "WatchUI | None" = None
  def build_view(room: Room, public_url: str) -> discord.ui.View   # [进入房间](链接按钮) + [我是房主]
  class WatchUI:
      def __init__(self, rooms: RoomManager, public_url: str, cards: CardStore)
      async def on_host(self, interaction, room_id: str) -> None     # 发起人 → ephemeral 房主链接；否则 "你不是这个房间的房主。"
      def on_change(self, room: Room, kind: str) -> None             # 登记到 rooms.listeners；节流安排编辑
      edit: Callable[[int, int, str, discord.ui.View | None], Awaitable[None]]   # bot 注入 (channel_id, message_id, text, view)
  class CardStore:            # STATE_DIR/watch_cards.json：{"cards": [[channel_id, message_id], ...]}
      def __init__(self, path: Path); def add(self, channel_id, message_id); def remove(...); def all(self) -> list[tuple[int, int]]
  ```
- 卡片文字（`card_text`），进度用 `m:ss` 或 `h:mm:ss`，没有时长时不显示 `/ 总长`：
  ```
  🎬 **一起看**
  正在播放：**<title>**（B 站|YouTube）
  ▶ 播放中 12:34 / 45:10 · 👥 3 人在看        （暂停时：⏸ 已暂停 …）
  房主：<room.host_name()>
  ```
  结束时：`🎬 **一起看** · 已结束` + 换行 + `最后播放：**<title>**`，有 reason 时再加一行（如 `（服务器重启）`），并去掉按钮。
- 房主链接：`<public_url>/r/<room_id>?k=<owner_key>`。如果 `room.key_used`，回复 `房主链接已经用过了，房主身份现在在房间里的「<host_name>」手上。`
- 节流：同一房间距上次编辑不到 15 秒时，安排一次延后编辑，期间再来的变化合并进去。`closed` 立即编辑，并从 `CardStore` 删除。
- `bot._watch(message, arg)`：
  - 没有启用：回复 `一起看功能没有启用。`
  - 用 `find_link` 找不到链接：回复 `用法：@mrmeeseeks 一起看 <B站或YouTube链接>`
  - 否则依次：`sources.resolve` → `rooms.create(channel.id, author.id, author.display_name, video)` → `message.reply(card_text, view=build_view)` → 记下 `room.message_id`，写入 `CardStore`。
  - `VideoUnavailable` / `RoomsFull` 时回复对应中文提示。
- `on_ready`（只跑一次）：把 `CardStore` 里遗留的卡片都编辑成"已结束（服务器重启）"并清空。消息找不到时跳过。
- `main()`：只有设置了 `WATCH_PUBLIC_URL` 才创建 `RoomManager`、`Sources(BILIBILI_SESSDATA, QUALITY_QN[WATCH_MAX_QUALITY])`、`WatchServer` 和 `WatchUI`，然后在 `async with bot` 里 `await watch.start()`，把 `watch.run()` 加进 `refreshers`，`finally` 里 `await watch.close()`。`SESSDATA` 为空时打一条 warning（`B 站将使用未登录画质`），**不打印它的值**。
- `HELP_TEXT` 加一行：``"`@mrmeeseeks 一起看 <Bilibili or YouTube link>` (or `watch`) Open a watch-together room with synced playback\n"``。

- [ ] **Step 1: 写失败的测试**（沿用 `test_prediction_bot.py` 的假 Discord 对象写法）：

```python
def test_parse_and_route():
    check("中文指令", bot.parse_command("一起看 https://youtu.be/dQw4w9WgXcQ") == ("一起看", "https://youtu.be/dQw4w9WgXcQ"))
    check("watch 指令", bot.parse_command("watch https://youtu.be/x")[0] == "watch")

def test_card_text():
    check("播放中", "▶ 播放中 0:10 / 3:15 · 👥 2 人在看" in card_text(room_playing_10s_of_195, now))
    check("暂停", "⏸ 已暂停" in ...); check("无时长不显示总长", " / " not in ...)
    check("结束文案", "已结束" in card_text(r, now, ended=True, reason="服务器重启") and "（服务器重启）" in ...)

def test_watch_command():
    check("没启用", reply == "一起看功能没有启用。")
    check("无链接给用法", reply.startswith("用法："))
    check("建房发卡片并记录", fake_channel.sent and card_store.all() == [(555, msg_id)])
    check("放不了提示", "这个视频在海外服务器上无法播放" in reply)

def test_host_button():
    check("发起人拿到 ephemeral 房主链接", sent.ephemeral and f"/r/{rid}?k=" in sent.content)
    check("别人点提示不是房主", sent.content == "你不是这个房间的房主。")
    check("链接用过后提示", "房主链接已经用过了" in sent.content)

def test_throttle_and_close():
    # 连续 3 次 members 变化 → 只立即编辑 1 次，15 秒后再编辑 1 次；closed 立即编辑且去掉按钮、从 CardStore 删除

def test_stale_cards_on_ready():
    # CardStore 里有 2 条 → on_ready 后两条都编辑成含"服务器重启"，CardStore 清空；第二次 on_ready 不再编辑
```

- [ ] **Step 2: 运行确认失败**：`RUN tests/test_watch_bot.py`
- [ ] **Step 3: 实现 `server/watch_ui.py` 并修改 `server/bot.py`、`server/.env.example`**（新配置项都加上中文注释，照现有风格）。
- [ ] **Step 4: 运行确认通过**：`RUN tests/test_watch_bot.py`，再跑全部测试确认没有破坏现有功能：
  `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test sh -c 'pip install -q -r client/requirements.txt >/dev/null 2>&1; for t in tests/test_*.py; do printf "%s: " $t; python $t 2>/dev/null | tail -1; done'`
  预期：每个文件最后一行都是全部通过。
- [ ] **Step 5: Commit** `git commit -m "Add watch-together Discord command, room card and host button"`

---

### Task 6: 部署配置、文档和上线

**Files:**
- Modify: `compose.yml`、`deploy/Caddyfile`、`README.md`、`docs/ARCHITECTURE.md`

- [ ] **Step 1: `compose.yml`**：加 `WATCH_HOST: 0.0.0.0`、`WATCH_PORT: "8080"`，端口 `"127.0.0.1:8080:8080"`，现有 8765 不变。
- [ ] **Step 2: `deploy/Caddyfile`**：加一段
  ```
  watch.pigdogie.com.au {
  	reverse_proxy 127.0.0.1:8080 {
  		flush_interval -1
  	}
  }
  ```
  （Caddy 的 `reverse_proxy` 默认就支持 WebSocket。`flush_interval -1` 让视频流边收边发。）
- [ ] **Step 3: 文档**：在 README 的功能列表和配置说明里加上"一起看"（指令、`.env` 项、DNS 和 Caddy 步骤、SESSDATA 怎么取、流量提醒）。在 `docs/ARCHITECTURE.md` 的模块表里加 `watch_rooms.py`、`watch_sources.py`、`watch_server.py`、`watch_ui.py`、`watch_web/` 五行，再加一节"一起看 WebSocket 协议"（照搬 Task 3 的消息表）。
- [ ] **Step 4: 全量测试**（同 Task 5 Step 4 的命令）→ 全部通过。Commit：`git commit -m "Document and configure watch-together deployment"`
- [ ] **Step 5: 和用户确认上线**。请用户：
  - 在域名后台加 A 记录 `watch.pigdogie.com.au → 5.223.69.203`；
  - 同意在服务器 `/opt/mrmeeseeks/.env` 里写入 `WATCH_PUBLIC_URL=https://watch.pigdogie.com.au` 和 `BILIBILI_SESSDATA`。
  按 Task 0 的记录装好或配置 Caddy（需要 sudo，先征得同意）。
- [ ] **Step 6: 部署**：`git fetch origin && git merge origin/main`，跑一遍全量测试，然后执行 `deploy/deploy.sh`。确认日志里有 watch server 的监听信息。
- [ ] **Step 7: 线上验收（和用户一起）**：
  - 频道里分别用 B 站和 YouTube 链接 `@mrmeeseeks 一起看`；
  - 两台设备（至少一台手机）进入房间；
  - 测试房主链接、给权限、同步暂停和拖动、换视频、房主离开后的交接、卡片人数更新、关闭所有页面 10 分钟后卡片显示"已结束"；
  - 测一个大陆限定视频的提示。
  结果记到"实测记录"。

---

## 实测记录

（Task 0、Task 4 Step 6、Task 6 Step 7 填写。）
