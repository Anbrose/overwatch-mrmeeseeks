# 一起看按片名搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `@mrmeeseeks 一起看 <片名>` searches the 采集接口 sources, lets the requester pick from a Discord select menu, and plays the chosen show (HLS, browser-direct) in the existing synced watch room, with episode switching, source fallback, auto-next and per-channel "上次一起看到…" resume.

**Architecture:** Two new pure-ish modules: `vod_search.py` (search APIs → merged `Show` list) and `vod_playlist.py` (fetch + rewrite m3u8, strip ads, probe playability). `watch_rooms.py` gains a `vod` video kind and episode/resume/auto-next transitions; `watch_server.py` serves the rewritten playlist at `/vod/{room}/{version}/index.m3u8` and handles new WebSocket messages; `watch_progress.py` persists per-channel progress; `watch_ui.py`/`bot.py` add the search select menu; `watch_web/` adds an hls.js player and the episode/source/resume UI. Video segments never pass through our server.

**Tech Stack:** Python 3.12, discord.py ≥ 2.6, aiohttp ≥ 3.9, hls.js 1.5.20 (cdnjs), plain-script tests run in Docker.

**Spec:** `docs/superpowers/specs/2026-10-24-watch-search-design.md` (builds on `docs/superpowers/specs/2026-10-23-watch-together-design.md`)

## Global Constraints

- Run every test file with: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/<file>.py` — exit code 0 and no `FAIL` lines. Tests never touch the network (fake sessions / local aiohttp servers, fixtures in `tests/fixtures/watch/`).
- Test style: plain scripts with the `check(name, cond)` helper and `if __name__ == "__main__":` runner, like `tests/test_watch_sources.py`.
- No new Python dependencies. Browser libs only from cdnjs with an exact version: `https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.20/hls.min.js`.
- Only `https://` episode URLs whose path ends in `.m3u8` count as playable. Our server fetches only the current episode's m3u8 URL for the room's current source, and the variant URL named inside it — never a URL taken from a client.
- Source list env: `WATCH_VOD_SOURCES`, comma-separated `名字=接口地址`. Unset → default `创享影视=https://ys.cxkjg.cn/api/provide/vod,1080资源=https://api.1080zyku.com/inc/apijson.php,量子=https://cj.lziapi.com/api.php/provide/vod`. Set to empty string → search disabled.
- Search: query `?ac=detail&wd=<片名>`, follow redirects, 8 s timeout per source, at most 10 results, query length 1–50 characters.
- User-facing copy (exact):
  - `没找到「{query}」`
  - `资源站暂时无法访问，请稍后再试`
  - `按片名搜索没有启用，请贴 B 站或 YouTube 链接`
  - `只有发起人能选`
  - `选择已过期`（选单 2 分钟超时后消息改成这个）
  - `已过期，请重新搜索`（bot 重启后点旧选单）
  - `已选择：{title}`
  - `这一集暂时没有能播的片源`
  - `视频加载失败`（观众端）
  - `正在准备…`
  - `换一个片源`
  - search header `🔎 「{query}」的搜索结果（只有 {mention} 能选，2 分钟内有效）`
  - option label `{title} · {year} · {type_name} · {n} 集`（缺 year 或 type_name 就省掉那一段）, option description `上次看到第 {n} 集`
  - room title `{show title} · 第 {n} 集`（只有 1 集的条目就是 `{show title}`）
  - card source label `片源：{source name}`
  - resume line `上次一起看到 第 {n} 集 {m:ss 或 h:mm:ss}（{M}月{D}日）`
  - `WATCH_USAGE = "用法：@mrmeeseeks 一起看 <B站或YouTube链接 或 片名>"`
- Ad stripping, playability probe, progress rules, auto-next precondition, cross-source mapping by list index, duration from the rewritten playlist: exactly as in the spec's “我做的决定”.
- Existing behaviour for B 站/YouTube links must not change (all existing watch tests keep passing).

## Review Focus

1. A source answers with HTML, a Cloudflare page, a 403, or truncated JSON → that source is skipped/treated as unplayable, nothing raises to the user except the copy above. (Tests in Task 1 and Task 2.)
2. The host clicks two episodes quickly, or a manual switch races auto-next → exactly one switch wins, the stale one is dropped by version check, nobody ends up on a mixed state. (Test in Task 5.)
3. The select menu is used twice (double click, or after the room already exists) → only one room is created. (Test in Task 6.)
4. Playlists using relative paths, root-relative paths, protocol-relative URLs, query strings, `EXT-X-KEY URI=` and `EXT-X-MAP URI=` → every URI in the output is absolute `https://`. (Test in Task 2.)
5. `watch_progress.json` missing, empty or corrupted → store starts empty, bot keeps working. (Test in Task 4.)

---

### Task 1: Search the 采集接口 sources (`vod_search.py`)

**Files:**
- Create: `server/vod_search.py`
- Test: `tests/test_vod_search.py`
- Fixtures (already captured, do not regenerate): `tests/fixtures/watch/vod_search_cxkjg.json` (aggregator, 10 sources per entry, names like `"绝命毒师  第五季"`), `vod_search_1080zyk.json` (one source, names like `"绝命毒师第一季"`), `vod_search_lzi.json` (电影解说 entries), `vod_search_empty.json`.

**Interfaces:**
- Produces:
  ```python
  DEFAULT_VOD_SOURCES = "创享影视=https://ys.cxkjg.cn/api/provide/vod,1080资源=https://api.1080zyku.com/inc/apijson.php,量子=https://cj.lziapi.com/api.php/provide/vod"
  MAX_RESULTS = 10
  class SearchUnavailable(Exception): message: str   # "资源站暂时无法访问，请稍后再试"
  @dataclass class Episode: label: str; url: str
  @dataclass class Line: name: str; episodes: list[Episode]          # one playable source
  @dataclass class Show:
      key: str                 # normalize_title(vod_name) + "|" + str(year or "")
      title: str               # vod_name with whitespace runs collapsed to one space, stripped
      year: int | None
      type_name: str
      lines: list[Line]        # config order, then API order
      @property episode_count -> int   # max(len(l.episodes))
  def parse_sources_env(raw: str | None) -> list[tuple[str, str]]
  def normalize_title(s: str) -> str          # NFKC, drop all whitespace, casefold
  def parse_detail(resp: dict, api_name: str) -> list[Show]
  def merge_shows(groups: list[list[Show]]) -> list[Show]
  def rank_shows(shows: list[Show], query: str) -> list[Show]
  class VodSearch:
      def __init__(self, sources: list[tuple[str, str]], session_factory=<aiohttp default>, timeout: float = 8.0)
      async def search(self, query: str) -> list[Show]   # ranked, ≤ MAX_RESULTS; [] if nothing; SearchUnavailable if every source failed
  ```

- [ ] **Step 1: Write the failing tests** in `tests/test_vod_search.py`:
  - `test_parse_sources_env`: `None` → 3 defaults in order (`("创享影视", "https://ys.cxkjg.cn/api/provide/vod")` first); `""` → `[]`; `"甲=https://a/x, 乙=https://b/y"` → two pairs, whitespace trimmed; entries without `=` or with a non-http(s) URL are skipped.
  - `test_parse_detail_aggregator`: on `vod_search_cxkjg.json` with api_name `"创享影视"`: 3 shows; first `title == "绝命毒师 第五季"`, `year == 2012`, `type_name == "欧美剧"`, `episode_count == 16`; line names are `"创享影视/金鹰"`, `"创享影视/闪电"`… (format `{api_name}/{play_from}` when an entry has more than one play_from, else just `api_name`); first episode of first line is `Episode("第01集", "https://hd.kuktxu.com/play/zbqxRqya/index.m3u8")`.
  - `test_parse_detail_filters`: build an entry inline whose `vod_play_from` is `"web$$$m3u8$$$plain"` with lines: page URLs (`https://x/share/abc`), https m3u8 URLs, and only `http://…m3u8` → only the middle line survives; an entry where no line survives is dropped entirely; empty segments from trailing `#` are ignored; an episode string without `$` uses the URL as its label fallback `第{n}集`.
  - `test_parse_detail_bad_input`: `{}` → `[]`; `{"list": "x"}` → `[]`; entries missing `vod_play_url` skipped; non-numeric `vod_year` → `year is None`.
  - `test_merge`: `vod_search_cxkjg.json` (创享影视) + `vod_search_1080zyk.json` (1080资源) merged → `绝命毒师 第五季` (2012) has lines from both, 创享影视 lines first, then `"1080资源"`; title kept from the first group; shows only in one group kept; same name different year not merged.
  - `test_rank`: query `"绝命毒师"` on merged cxkjg+zyk+lzi: shows whose normalized title equals the query first, then those starting with the query (sorted by year descending), then the rest (year descending); `"绝命毒师 第五季[电影解说]"` (type 电影解说, different normalized title) is present but after all `绝命毒师 第N季` entries.
  - `test_search_fake_session` (async, fake session like `tests/test_watch_sources.py`'s): three sources where #1 returns the cxkjg fixture, #2 returns HTML text with status 200, #3 raises `asyncio.TimeoutError` → returns ranked shows from #1 only, ≤ 10; request params include `ac=detail` and `wd=<query>`.
  - `test_search_all_fail`: every source errors → raises `SearchUnavailable` with `.message == "资源站暂时无法访问，请稍后再试"`; every source returns `vod_search_empty.json` → returns `[]` (no exception).

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_vod_search.py`
  Expected: `ModuleNotFoundError: No module named 'vod_search'`

- [ ] **Step 3: Implement `server/vod_search.py`** to the interface above. `vod_play_from` and `vod_play_url` split on `$$$` and zip; episodes split on `#`, each `label$url` split on the last `$`. Parse JSON from `await resp.text()` with `json.loads` (some APIs send `text/html` content-type), treat any non-dict / non-list `list` / HTTP ≥ 400 / exception as “this source failed”. Sources are queried concurrently with `asyncio.gather`, each under `asyncio.wait_for(..., timeout)`. Log one warning per failed source with the source name and exception type.

- [ ] **Step 4: Run tests to verify they pass** (same command). Expected: all PASS, exit 0.

- [ ] **Step 5: Commit**
  ```bash
  git add server/vod_search.py tests/test_vod_search.py tests/fixtures/watch/vod_search_*.json
  git commit -m "Search 采集接口 sources and merge results for 一起看"
  ```

### Task 2: Fetch, rewrite and probe m3u8 (`vod_playlist.py`)

**Files:**
- Create: `server/vod_playlist.py`
- Test: `tests/test_vod_playlist.py`
- Fixtures (already captured): `tests/fixtures/watch/vod_master.m3u8` (master with one relative variant `1000k/hls/mixed.m3u8`), `vod_variant_ads.m3u8` (VOD playlist, 9 discontinuity blocks; blocks of 4–5 segments totalling 16.47 s / 17.67 s appear twice each and are ads; relative segment names).

**Interfaces:**
- Consumes: `Show`, `Line`, `Episode` from Task 1.
- Produces:
  ```python
  AD_MAX_SECONDS = 30.0
  MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
  NO_PLAYABLE = "这一集暂时没有能播的片源"
  class PlaylistError(Exception): message: str
  @dataclass class Playlist: text: str; duration: float; first_segment: str | None; removed_blocks: int
  def pick_variant(text: str, base_url: str) -> str | None      # absolute URL of highest BANDWIDTH variant; None if not a master
  def rewrite_media(text: str, base_url: str) -> Playlist         # absolutize every URI, strip ads, duration = sum of kept EXTINF
  async def fetch_playlist(session, url: str) -> Playlist          # master → follow exactly one variant; PlaylistError on HTTP error / non-#EXTM3U / too big / no segments
  async def probe(session, playlist: Playlist, origin: str) -> bool   # Range bytes=0-1023 GET of first_segment with Origin header; True iff status in (200, 206) and ACAO is "*" or origin
  async def find_playable(session, show: Show, episode: int, start_line: int, origin: str) -> tuple[int, Playlist]
      # tries lines start_line, start_line+1, … wrapping once around; skips lines lacking that episode index; raises PlaylistError(NO_PLAYABLE)
  ```

- [ ] **Step 1: Write the failing tests** in `tests/test_vod_playlist.py`:
  - `test_pick_variant`: fixture master with base `https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/index.m3u8` → `"https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/mixed.m3u8"`; inline master with BANDWIDTH 800000 and 2000000 picks the 2000000 one; a media playlist → `None`.
  - `test_rewrite_ads`: fixture variant with base `https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/mixed.m3u8` → `removed_blocks == 4`; `abs(duration - 3486.48) < 0.5` (total 3554.75 minus 16.47·2 minus 17.67·2); output contains no line ending in `9abd76c66643cf6bbfbda0a81923d523.ts`; every non-comment line starts with `https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/`; `#EXTM3U` and `#EXT-X-ENDLIST` (if present in input) preserved; `first_segment` ends with `527ede660a2a7ccd1187bf31ca80a15e.ts`.
  - `test_rewrite_keeps_unsure`: inline playlist: long block (100 s) + one unique 15 s block in the same directory + long block → nothing removed (`removed_blocks == 0`); same but the 15 s block's segments are on `https://ads.example.com/` → removed; a 40 s block repeated twice → not removed (over `AD_MAX_SECONDS`).
  - `test_rewrite_uris`: inline playlist with segment lines `a.ts`, `/root/b.ts`, `//cdn.example.com/c.ts`, `https://x.example.com/d.ts?t=1`, plus `#EXT-X-KEY:METHOD=AES-128,URI="key.key"` and `#EXT-X-MAP:URI="init.mp4"`, base `https://h.example.com/p/q/index.m3u8` → `https://h.example.com/p/q/a.ts`, `https://h.example.com/root/b.ts`, `https://cdn.example.com/c.ts`, `https://x.example.com/d.ts?t=1`, `URI="https://h.example.com/p/q/key.key"`, `URI="https://h.example.com/p/q/init.mp4"`.
  - `test_fetch_playlist` (async, local `aiohttp` test server like `tests/test_watch_server.py`'s fake upstream): serves master at `/m/index.m3u8` and the ad fixture at `/m/1000k/hls/mixed.m3u8` → `Playlist.duration ≈ 3486.48`, URIs point at the test server; a URL returning an HTML 403 page → `PlaylistError`; a 200 body not starting with `#EXTM3U` → `PlaylistError`; a body larger than `MAX_PLAYLIST_BYTES` → `PlaylistError`; a master whose variant 404s → `PlaylistError`.
  - `test_probe`: segment handler returning 206 with `Access-Control-Allow-Origin: *` → True; 206 without ACAO → False; 403 → False; ACAO equal to origin → True; connection refused → False (no raise).
  - `test_find_playable`: a `Show` with 3 lines, line 0 has only 2 episodes, line 1's playlist 404s, line 2 is fine; `find_playable(show, 2, 0, origin)` → `(2, playlist)`; `find_playable(show, 0, 2, origin)` tries line 2 first; all bad → `PlaylistError` with message `"这一集暂时没有能播的片源"`.

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_vod_playlist.py`
  Expected: `ModuleNotFoundError: No module named 'vod_playlist'`

- [ ] **Step 3: Implement `server/vod_playlist.py`**. URL resolution with `urllib.parse.urljoin`, then force scheme to `https` only for protocol-relative inputs (`//…`); any resulting non-`https` segment makes `rewrite_media` raise `PlaylistError(NO_PLAYABLE)`. Ad rule (spec): split at `#EXT-X-DISCONTINUITY`; the main body is the block with the largest total duration; a block is an ad iff total < `AD_MAX_SECONDS` and (its EXTINF-value sequence occurs as a whole block more than once in the playlist, or the directory part (`scheme://host/path-without-filename`) of its first segment differs from the main body's). When removing a block, also drop the `#EXT-X-DISCONTINUITY` that opened it. Read bodies with a byte cap (`resp.content.read(MAX_PLAYLIST_BYTES + 1)`). Send a browser `User-Agent` (reuse `live_watch.USER_AGENT`).

- [ ] **Step 4: Run tests to verify they pass.** Expected: all PASS, exit 0.

- [ ] **Step 5: Commit**
  ```bash
  git add server/vod_playlist.py tests/test_vod_playlist.py tests/fixtures/watch/vod_*.m3u8
  git commit -m "Rewrite 采集接口 m3u8 playlists, strip inserted ads and probe playability"
  ```

### Task 3: `vod` videos in rooms (`watch_rooms.py`)

**Files:**
- Modify: `server/watch_rooms.py`
- Test: `tests/test_watch_rooms.py` (add tests; keep existing ones passing)

**Interfaces:**
- Consumes: `Show` (Task 1); `Progress` (Task 4 defines it — to avoid a dependency, type it here as `object | None`, the room only stores it).
- Produces:
  ```python
  @dataclass class VodInfo:
      show: Show
      episode: int            # 0-based index
      line: int               # index into show.lines
      resume: object | None = None   # Progress snapshot taken when the room was created (see Task 4); never updated by the room
  # Video gains:  vod: VodInfo | None = None      (source == "vod" iff vod is not None)
  def vod_title(show: Show, episode: int) -> str     # "绝命毒师 第一季 · 第 3 集"; show.title alone when episode_count == 1
  def vod_video(info: VodInfo, duration: float) -> Video
      # Video(source="vod", id=info.show.key, page=info.episode + 1, title=vod_title(...), duration=duration,
      #       url=info.show.lines[info.line].episodes[info.episode].url, vod=info)
  class RoomManager:
      def change_video(self, room_id, actor_id, video: Video, position: float = 0.0, playing: bool = False) -> bool   # host only (unchanged rule); new optional args
      def resume(self, room_id, actor_id, video: Video, position: float) -> bool   # host OR can_control; current video must be vod; starts paused at position
      def advance(self, room_id: str, expected_version: int, video: Video) -> bool   # no actor; only if room.version == expected_version; starts PLAYING at 0
  # tick(): for a vod room that is playing, has a duration, position_at(now) >= duration, has someone present,
  #         and room.finished_version != room.version → set room.finished_version = room.version and notify kind "finished".
  # Room gains field: finished_version: int | None = None
  ```
  All three switching methods share one private `_set_video(room, video, position, playing)` that bumps `version`, clamps position to `[0, duration]`, sets `Playback(playing, position, now)`, and notifies `"video"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_watch_rooms.py`, build a 2-line, 3-episode `Show` inline):
  - `test_vod_title`: 3-episode show `title="绝命毒师 第一季"`, episode 2 → `"绝命毒师 第一季 · 第 3 集"`; 1-episode show → its title.
  - `test_change_video_position`: host `change_video(..., vod_video(info, 3000.0), position=120.0)` → `version` +1, `playback.position == 120.0`, `playing is False`; position 5000 clamps to 3000; non-host → False.
  - `test_resume_permission`: viewer without control → False; after `set_permission` → True and playback paused at the given position; host → True; when the current video is YouTube → False even for host.
  - `test_advance_version_guard`: `advance(rid, room.version, v)` → True, `playing is True`, position 0; calling again with the old version → False and state unchanged.
  - `test_tick_finished`: vod room, host present, `control(play)` at position 2990 of a 3000 s video, clock +20 → `tick()` notifies `"finished"` exactly once (second `tick()` does not re-notify); after `advance`, a new end fires again.
  - `test_tick_finished_needs_people`: same but the only member disconnected past `GRACE_SECONDS` → no `"finished"`.
  - `test_tick_finished_not_for_youtube`: YouTube video at its end → no `"finished"`.

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_watch_rooms.py`
  Expected: FAIL / ImportError on `vod_title`.

- [ ] **Step 3: Implement** in `server/watch_rooms.py` (import `Show` from `vod_search`; update the module docstring's list of listener kinds to include `finished`).

- [ ] **Step 4: Run tests to verify they pass** (all old + new). Expected: all PASS.

- [ ] **Step 5: Commit**
  ```bash
  git add server/watch_rooms.py tests/test_watch_rooms.py
  git commit -m "Add vod videos, episode switching, resume and auto-next events to watch rooms"
  ```

### Task 4: Per-channel progress (`watch_progress.py`)

**Files:**
- Create: `server/watch_progress.py`
- Test: `tests/test_watch_progress.py`

**Interfaces:**
- Consumes: `RoomManager`, `Room`, `VodInfo` (Task 3).
- Produces:
  ```python
  MIN_POSITION = 30.0
  RECORD_INTERVAL = 30.0
  @dataclass class Progress: title: str; episode: int; position: float; updated_at: float   # episode 0-based
  class ProgressStore:
      def __init__(self, path: Path | None)          # STATE_DIR/watch_progress.json; None = memory only
      def get(self, channel_id: int, key: str) -> Progress | None
      def record(self, channel_id: int, key: str, p: Progress) -> bool   # False (no write) when p.position < MIN_POSITION; atomic write like CardStore
  class ProgressRecorder:
      def __init__(self, rooms: RoomManager, store: ProgressStore, interval: float = RECORD_INTERVAL)
          # appends self.on_event to rooms.listeners
      def on_event(self, room: Room, kind: str) -> None   # "playback" (any play/pause/seek) and "closed" → record now
      def tick(self) -> None                              # every vod room that is playing and whose last record is ≥ interval old → record
  def resume_line(p: Progress, show_episode_count: int) -> str
      # "上次一起看到 第 3 集 34:12（10月23日）"; month/day in Australia/Sydney time; when show_episode_count == 1: "上次一起看到 34:12（10月23日）"
  ```
  A record uses `room.channel_id`, `room.video.vod.show.key`, `room.video.vod.show.title`, `room.video.vod.episode`, `room.playback.position_at(now, duration)`, `updated_at=now`. Non-vod rooms are ignored.

- [ ] **Step 1: Write the failing tests** in `tests/test_watch_progress.py`:
  - `test_store_roundtrip`: record in a temp file, new `ProgressStore` on the same path returns equal `Progress`; channels are independent; second record on the same key replaces the first.
  - `test_store_min_position`: position 29.9 → `record` returns False and `get` is None.
  - `test_store_corrupt`: path missing → empty; file containing `""`, `"{"`, `"[]"`, `{"1": "x"}` → empty store, no exception, and a following `record` works.
  - `test_recorder_events`: vod room playing from 0, clock +100, host pauses → store has position 100; `"closed"` records final position; YouTube room → nothing recorded.
  - `test_recorder_tick_interval`: playing vod room, `tick()` at +31 s records, at +45 s does not, at +62 s records again.
  - `test_resume_line`: `Progress("绝命毒师 第一季", 2, 2052, <2026-10-23 20:00 Sydney>)`, count 7 → `"上次一起看到 第 3 集 34:12（10月23日）"`; position 3725 → `1:02:05`; count 1 → no `第 n 集`.

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_watch_progress.py`
  Expected: `ModuleNotFoundError: No module named 'watch_progress'`

- [ ] **Step 3: Implement `server/watch_progress.py`** (reuse `watch_ui.fmt_time` for `m:ss`/`h:mm:ss` only if it does not create an import cycle; otherwise copy the 4-line formatter). JSON shape: `{"<channel_id>": {"<key>": {"title", "episode", "position", "updated_at"}}}`.

- [ ] **Step 4: Run tests to verify they pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add server/watch_progress.py tests/test_watch_progress.py
  git commit -m "Record per-channel 一起看 progress for searched shows"
  ```

### Task 5: Serve playlists and vod controls (`watch_server.py`)

**Files:**
- Modify: `server/watch_server.py`
- Test: `tests/test_watch_server.py` (add tests)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  ```python
  class WatchServer:
      def __init__(self, rooms, sources, host, port, web_dir=..., origin: str = "",
                   progress: ProgressStore | None = None, recorder: ProgressRecorder | None = None)
          # origin = WATCH_PUBLIC_URL, used for probe(); recorder.tick() is called from run() every TICK_SECONDS
      async def prepare_vod(self, show: Show, episode: int, start_line: int = 0,
                            resume: object | None = None) -> tuple[Video, Playlist]
          # find_playable → VodInfo(show, episode, line, resume) → vod_video(info, playlist.duration); raises PlaylistError
      def attach_playlist(self, room: Room, playlist: Playlist) -> None   # cache under (room.room_id, room.version)
  # Route: GET /vod/{room_id}/{version}/index.m3u8 → cached Playlist.text,
  #        content-type "application/vnd.apple.mpegurl", Cache-Control no-store; 404 if room/version/source mismatch;
  #        cache miss (e.g. evicted) → fetch_playlist(room.video.url) once, cache, serve; PlaylistError → 502 with message.
  # state.video for vod adds: "hls": "/vod/<room>/<version>/index.m3u8", "episode": int, "episode_count": int,
  #        "line_name": str, "line_count": int, "show_title": str
  # state adds: "resume": {"episode": int, "position": float, "text": resume_line(...)} | null   (from room.video.vod.resume)
  # Client → server messages (all ignored when the room's current video is not vod):
  #   {"type": "episode", "index": int}   host only; 0 <= index < episode_count; starts paused at 0, keeps current line as start_line
  #   {"type": "next_line"}               host only; same episode, start_line = current line + 1, keeps current position (paused)
  #   {"type": "resume"}                  host or can_control; uses room.video.vod.resume; switches episode if needed (same line first), seeks to its position, paused
  # These run through the existing conn.busy single-flight (CHANGING_MSG when busy); PlaylistError → {"type": "error", "message": e.message}.
  # On rooms event "finished": spawn a task that, if episode + 1 < episode_count, prepare_vod(show, episode + 1, current line)
  #   then rooms.advance(room_id, version_at_start, video) and attach_playlist; failure → log + leave room at end.
  ```
  After every successful switch call `attach_playlist(room, playlist)` with the room's new version, and evict playlists of older versions (same place the existing code evicts `_streams`).

- [ ] **Step 1: Write the failing tests** (append; reuse the file's local fake-upstream pattern to serve the fixture master/variant and a segment with ACAO `*`):
  - `test_vod_playlist_route`: room created with `prepare_vod` + `attach_playlist` → `GET /vod/<id>/<ver>/index.m3u8` 200, content-type `application/vnd.apple.mpegurl`, body has no ad segments; wrong version → 404; YouTube room → 404.
  - `test_vod_state_message`: welcome `state.video` has `source == "vod"`, `hls` path with current version, `episode == 0`, `episode_count`, `line_name`, `line_count`; `state.resume` is null without a snapshot and `{"episode", "position", "text"}` with one.
  - `test_vod_episode_switch`: host sends `{"type":"episode","index":1}` → all clients get a `state` with `episode == 1` and the new version; non-host → ignored (no state change); index out of range or non-int → ignored.
  - `test_vod_next_line`: line 0 playable, line 1 playable; host `next_line` at position 300 → state shows `line_name` of line 1, position 300, paused; when every other line fails → host receives `{"type":"error","message":"这一集暂时没有能播的片源"}` and the room is unchanged.
  - `test_vod_resume`: viewer with control sends `resume` with snapshot `episode=2, position=2052` → state `episode == 2`, position 2052; viewer without control → ignored.
  - `test_vod_switch_race` (Review Focus 2): host sends two `episode` messages back to back → second gets `"正在换视频，请稍候"`; then simulate auto-next by firing `"finished"` while a host switch is in flight that bumps the version first → the auto-next `advance` returns False and the room ends on the host's chosen episode.
  - `test_vod_auto_next`: vod room playing near the end with a member present, advance the fake clock and call `rooms.tick()` → after the task runs, state has `episode + 1`, `playing is True`, position 0; on the last episode → stays put.
  - `test_recorder_ticks_from_run` is not needed — instead assert in `test_vod_state_message` that constructing `WatchServer(..., recorder=r)` does not register duplicate listeners (recorder registers itself; server only calls `tick`).

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_watch_server.py`
  Expected: new tests FAIL (`prepare_vod` missing / 404 on route).

- [ ] **Step 3: Implement** in `server/watch_server.py`. Use one shared `aiohttp.ClientSession` (the existing `_http()`; note it has `auto_decompress=False` — create a second lazily-made session with default decompression for playlist fetches, closed in `_cleanup`). Update the module docstring's route list and the protocol comment.

- [ ] **Step 4: Run tests to verify they pass** (old + new).

- [ ] **Step 5: Commit**
  ```bash
  git add server/watch_server.py tests/test_watch_server.py
  git commit -m "Serve rewritten vod playlists and handle episode, source and resume switches"
  ```

### Task 6: Discord search flow (`watch_ui.py`, `bot.py`)

**Files:**
- Modify: `server/watch_ui.py`, `server/bot.py`, `server/.env.example`
- Test: `tests/test_watch_bot.py` (add tests)

**Interfaces:**
- Consumes: `VodSearch`, `SearchUnavailable`, `Show` (Task 1); `PlaylistError` (Task 2); `WatchServer.prepare_vod/attach_playlist` (Task 5); `ProgressStore`, `Progress` (Task 4).
- Produces:
  ```python
  # watch_ui.py
  PICK_TEMPLATE = r"watch:pick:(?P<token>[A-Za-z0-9_-]{8,32})"
  PICK_TIMEOUT = 120.0
  @dataclass class PendingSearch: token: str; requester_id: int; query: str; shows: list[Show];
                                  channel_id: int; message_id: int | None; origin_message: discord.Message; created_at: float; done: bool = False
  def option_label(show: Show) -> str          # "绝命毒师 第一季 · 2008 · 欧美剧 · 7 集", truncated to 100 chars
  def search_header(query: str, mention: str) -> str
  class PickSelect(discord.ui.DynamicItem[discord.ui.Select], template=PICK_TEMPLATE)   # handler: WatchUI | None
  def build_pick_view(p: PendingSearch, progress: ProgressStore | None) -> discord.ui.View   # options value = str(index); description "上次看到第 n 集" when progress has the show for this channel
  class WatchUI:  # additions
      pending: dict[str, PendingSearch]
      def add_search(self, p: PendingSearch) -> None     # stores it and spawns the PICK_TIMEOUT expiry (edit message to "选择已过期", no view)
      async def on_pick(self, interaction: discord.Interaction, token: str) -> None
          # unknown token → ephemeral "已过期，请重新搜索"; user != requester → ephemeral "只有发起人能选";
          # p.done already True → ephemeral "已过期，请重新搜索"; else mark done FIRST (Review Focus 3), defer, then call self.create_vod_room(p, index)
      create_vod_room: Callable[[PendingSearch, int], Awaitable[None]] | None   # injected by bot
  # card_text: for vod videos the source part is "片源：{room.video.vod.show.lines[room.video.vod.line].name}"
  # bot.py
  WATCH_USAGE = "用法：@mrmeeseeks 一起看 <B站或YouTube链接 或 片名>"
  MeeseeksBot.__init__(..., vod_search: VodSearch | None = None, watch_server: WatchServer | None = None, progress: ProgressStore | None = None)
  async def _watch(self, message, arg)   # link → existing path; else search path
  async def _create_vod_room(self, p: PendingSearch, index: int) -> None
      # prepare_vod(show, 0, resume=progress.get(channel, show.key)) → rooms.create → attach_playlist → edit search message to "已选择：{show.title}" (no view)
      # → reply to p.origin_message with the existing room card + CardStore.add; PlaylistError → edit search message to e.message
  ```
  Search path in `_watch`: `vod_search is None` → reply `按片名搜索没有启用，请贴 B 站或 YouTube 链接`; `len(arg)` not in 1..50 → `WATCH_USAGE`; `SearchUnavailable` → its message; `[]` → `没找到「{arg}」`; else post `search_header(arg, message.author.mention)` with `build_pick_view`, `allowed_mentions=AllowedMentions.none()`, then `add_search`. `setup_hook` also registers `PickSelect` with `add_dynamic_items`. `main()` builds `VodSearch(parse_sources_env(os.environ.get("WATCH_VOD_SOURCES")))` (None when the parsed list is empty), `ProgressStore(STATE_DIR/"watch_progress.json")`, `ProgressRecorder`, and passes `origin=watch_url`, `progress`, `recorder` to `WatchServer`.

- [ ] **Step 1: Write the failing tests** (append; reuse the file's fake message/interaction helpers, fake `VodSearch` and fake `watch_server` objects):
  - `test_parse_watch_text`: `parse_command("一起看 绝命毒师")` → `("一起看", "绝命毒师")`; `"watch Breaking Bad"` → `("watch", "Breaking Bad")`.
  - `test_watch_search_disabled`, `test_watch_search_length` (51 chars → `WATCH_USAGE`), `test_watch_search_unavailable`, `test_watch_search_empty` (`没找到「不存在的剧」`).
  - `test_watch_search_posts_menu`: reply text equals `search_header("绝命毒师", "<@42>")`; view has one select with `custom_id` matching `PICK_TEMPLATE`, ≤ 10 options, first label `"绝命毒师 第一季 · 2008 · 欧美剧 · 7 集"`; with a stored progress for that show in that channel the option description is `"上次看到第 3 集"`.
  - `test_pick_wrong_user`: other user → ephemeral `只有发起人能选`, no room.
  - `test_pick_creates_room`: requester picks index 0 → one room exists with `video.source == "vod"`, search message edited to `已选择：绝命毒师 第一季` with no view, card replied to the origin message and stored in `CardStore`, card text contains `片源：`; the room's `video.vod.resume` equals the stored progress.
  - `test_pick_twice` (Review Focus 3): two `on_pick` calls without awaiting in between (use `asyncio.gather`) → exactly one room; the second gets `已过期，请重新搜索`.
  - `test_pick_unknown_token`: → ephemeral `已过期，请重新搜索`.
  - `test_pick_timeout`: with an injected fast sleep, after expiry the search message is edited to `选择已过期` and the token is removed.
  - `test_pick_unplayable`: `prepare_vod` raises `PlaylistError("这一集暂时没有能播的片源")` → search message edited to that text, no room.
  - `test_watch_link_unchanged`: a YouTube link still creates a room through `sources.resolve` (existing behaviour).

- [ ] **Step 2: Run tests to verify they fail**
  Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_watch_bot.py`
  Expected: new tests FAIL.

- [ ] **Step 3: Implement.** Update `HELP_TEXT`'s 一起看 line to mention 片名, the `bot.py` header comment line 17, and `.env.example` with a commented `WATCH_VOD_SOURCES=` entry explaining the format, the default, and that empty disables search.

- [ ] **Step 4: Run tests to verify they pass**, then run the whole suite (every `tests/test_*.py`) — all exit 0.

- [ ] **Step 5: Commit**
  ```bash
  git add server/watch_ui.py server/bot.py server/.env.example tests/test_watch_bot.py
  git commit -m "Search shows by name from Discord and let the requester pick one"
  ```

### Task 7: Web player for vod (`watch_web/`)

**Files:**
- Modify: `server/watch_web/index.html`, `server/watch_web/player.js`, `server/watch_web/style.css`

**Interfaces:**
- Consumes: the `state` fields and client messages defined in Task 5.
- Produces (UI contract, used by the manual acceptance test):
  - `createVodPlayer(hlsUrl)` adapter with the same shape as `createBiliPlayer` (play/pause/seek/getTime/getDuration/setRate/volume/ready/failed/destroy). Uses native HLS when `video.canPlayType("application/vnd.apple.mpegurl")` is truthy, else hls.js from `https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.20/hls.min.js`. Fatal hls.js errors / `video` `error` → stage message `视频加载失败` (local only; never sends anything to the server). `RATE_STEPS.vod = [0.95, 1.05]`.
  - Duration for vod comes only from `state.video.duration`; the page never sends `duration` for vod.
  - Under the title: `<div id="resume">` shows `state.resume.text` when `state.resume` is non-null and NOT (`state.video.episode === resume.episode` and `|position − resume.position| < 30`). Rendered as a `<button>` that sends `{"type":"resume"}` when `me.canControl`, otherwise as plain text.
  - In `#perm-panel` (host only), when the current video is vod: an episode list (`第 n 集` buttons, current one highlighted, click sends `{"type":"episode","index":i}`), and a `换一个片源` button labelled with `片源：{line_name}（共 {line_count} 个）` that sends `{"type":"next_line"}`. While waiting for the resulting `state` or `error`, the stage shows `正在准备…`.
  - All text set via `textContent`. Existing B 站/YouTube behaviour unchanged.

- [ ] **Step 1: Implement the adapter, resume line and panel controls** following the existing `createBiliPlayer`, `buildPlayer` and `renderControls` structure in `player.js`; add CSS for `#resume`, the episode grid (wraps, scrolls past ~3 rows) and the current-episode highlight, in the existing dark palette.

- [ ] **Step 2: Static check.** Run: `docker run --rm -v "$PWD":/repo -w /repo node:20-alpine node --check server/watch_web/player.js` — expected: no output, exit 0. Then run `tests/test_watch_server.py` (its page test serves the static files) — all PASS.

- [ ] **Step 3: Local browser smoke test** (no Discord needed): write a throwaway script in the scratchpad that starts `WatchServer` on `127.0.0.1:8080` with a room built via `prepare_vod` from the real `1080资源` search for `绝命毒师` (this one step uses the network), prints the room URL and owner key URL; open both in a browser and check: plays with hls.js (Chrome) and native (Safari), host sees the episode list and `换一个片源`, switching episode syncs the second tab, the resume line appears when a snapshot is injected. Record what was checked in the commit message body.

- [ ] **Step 4: Commit**
  ```bash
  git add server/watch_web/
  git commit -m "Play searched shows with hls.js and add episode, source and resume controls"
  ```

### Task 8: Docs

**Files:**
- Modify: `README.md`, `docs/ARCHITECTURE.md`

- [ ] **Step 1: Update docs.** README 一起看 section: `一起看 <片名>` usage, the select menu, episode list, resume line, `WATCH_VOD_SOURCES` (format, default, empty disables), and the note that videos load from third-party sources directly in the browser. `docs/ARCHITECTURE.md`: add `vod_search.py`, `vod_playlist.py`, `watch_progress.py` to the module list; extend the 一起看 WebSocket protocol section with the new `state` fields (`hls`, `episode`, `episode_count`, `line_name`, `line_count`, `show_title`, `resume`) and the `episode` / `next_line` / `resume` client messages; add the `/vod/{room}/{version}/index.m3u8` route.

- [ ] **Step 2: Run the full suite** (every `tests/test_*.py`) — all exit 0.

- [ ] **Step 3: Commit**
  ```bash
  git add README.md docs/ARCHITECTURE.md
  git commit -m "Document searching shows by name in 一起看"
  ```

Deployment (after the branch is merged, with the user's go-ahead): `git fetch origin && git merge origin/main`, full suite, `deploy/deploy.sh`; no new server config is required because `WATCH_VOD_SOURCES` has a default. Then a live acceptance test with the user: search `绝命毒师`, pick 第一季, play/pause/seek/switch episode/auto-next/换一个片源 on two devices including iPhone Safari, and the resume line the next day.
