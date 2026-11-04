# 一起看：投屏：设计

日期：2026-10-30

建立在「按片名搜索」（`2026-10-24-watch-search-design.md`）之上。

## 目标

想在电视上一起看。手机或电脑只当遥控器，电视自己放，房间同步照常。

## 用户确认的决策

- 做**原生投屏**（电视自己拉流），不是屏幕镜像。
- 只支持**按片名搜到的剧集**（vod 片源）。YouTube（iframe 不给流地址）和 B 站（走服务器 DASH 转流，电视端不支持 DASH、老 Chromecast 不认 HEVC）不能投，用户知晓。

## 我做的决定（用户可以改）

- **两种协议**：Chromecast 用 Google Cast 发送端 SDK（`cast.js`，懒加载，只在 Chrome 系浏览器里加载，房间第一次拿到 vod 状态时才加载）；AirPlay 用 `<video>` 自带的 Remote Playback API（Safari 13+），老 Safari 退回 `webkitShowPlaybackTargetPicker`。安卓 Chrome 两个都有，优先 Cast SDK（控制更完整）。iPhone 网页投不了 Chromecast（Cast 网页 SDK 不支持 iOS），只能 AirPlay。
- **电视端用 Google 默认媒体接收端**，不写自定义接收端：不用注册应用 id，HLS 原生支持。代价是没法从发送端调倍速，同步只能靠 seek。
- **同步策略换掉，不重写协议**：播放器适配层多一个 `remote()`，为真时 `syncTick` 不调倍速；播放中差距 > 1.5 秒跳到目标 + 0.5 秒（补 seek 往返），暂停时 > 1 秒重定位，两次跳转至少隔 3 秒，电视 seeking / buffering 时跳过。本地播放一个字节都没改。
- **Chromecast 是独立适配器**（`createCastPlayer`），没有 `<video>`；舞台上显示「正在投屏到 <设备名>」。位置用 SDK 的 `getEstimatedTime()`（上次状态 + 经过时间）。音量取电视当前的，不把页面的 100% 推过去。
- **AirPlay 不换适配器**：元素继续当播放器，只是同步策略切换。换集时如果正在 AirPlay，只换 `video.src` 不重建元素，否则会话会断。hls.js 路径（Chrome 桌面）不挂 AirPlay，MediaSource 投不出去。
- **谁都能投**：每个观众投自己的屏，互不影响；服务器不知道谁在投屏，也不需要知道。
- **Cast 会话连上就算「已开始观看」**：电视放不需要手势，跳过「点击开始观看」。会话结束（电视停了、用户点 📺、投屏失败）重建本地播放器；房主换成 B 站 / YouTube 时主动结束会话并提示。
- **服务器只改一处**：`/vod/…/index.m3u8` 加 `Access-Control-Allow-Origin: *`，因为接收端是 Google 的页面、跨域来取。片段本来就要求 CDN 放开 CORS（`probe` 已经检查 `*` 或本站 origin），所以 Chrome 上能播的线路，电视上一般也能播；只给本站 origin 放行的 CDN 电视会播不了，页面提示「投屏失败」并自动换回本地，换片源再试。`fmp4` 片段按播放列表里有没有 `#EXT-X-MAP` 判断，告诉接收端容器格式。
- **地区限制不变**：电视直接向服务器要列表，按电视的 IP 判断；家里同一个网络和手机一样。

## 补充：「在电视上看」入口（2026-10-30 当天追加）

用户的组合是 iPhone + Chromecast，Safari 投不了 Chromecast，于是补一条「电视自己进房间」的路：

- 房间页右上角「📺 在电视上看」弹出二维码（`qrcode-generator` 1.4.4，cdnjs 懒加载，内容是不带房主密钥的观众链接）和 6 位房间号。
- 网站首页 `/` 从 404 改成输房间号的表单（`join.html`，纯 HTML 表单，电视浏览器不需要 JS），`/j?c=` 规范化（去空格横线、转小写）后 303 到 `/r/`。房间号本来就用无歧义字母表（没有 0/1/i/l/o），直接当短码用，不另造编号。
- 电视是普通观众，任何片源都行；控制留在手机上。

## 已知限制

- 投屏时同步误差 1 到 2 秒，比本地大。
- AirPlay 没有「断开」的程序接口，停止要再点 📺 在系统选择器里选回自己的设备。
- 没有真机自动化测试：JS 只做语法检查；Python 测试覆盖静态文件白名单、房间页引用和 `/vod/` 的 CORS 头。Chromecast 和 Apple TV 需要上线后真机验证。

## 实现

`server/watch_web/cast.js`（新）、`player.js`（远端同步、AirPlay 钩子、Cast 适配器、📺 按钮）、`index.html`（按钮、脚本）、`style.css`（点亮态）、`watch_server.py`（`cast.js` 入白名单、`/vod/` CORS）、`tests/test_watch_server.py`。
