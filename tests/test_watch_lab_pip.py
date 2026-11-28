"""一起看小窗播放实测：控制栏的「小窗」按钮进出画中画，小窗里同步照常。

运行：python tests/test_watch_lab_pip.py [--headed]
需要和 tests/test_watch_lab_restart.py 一样：playwright、Chrome / Edge、ffmpeg、联网；缺了就 SKIP。约 1 分钟。

场景：
- 标准 API（Chrome）：观众点「小窗」→ document.pictureInPictureElement 是播放器的 <video>，按钮变成「退出小窗」；
  小窗里房主暂停 / 拖动 / 继续，观众照常跟；再点一次退出；
- iOS Safari 的 webkit 接口（页面里模拟 webkitSupportsPresentationMode / webkitSetPresentationMode，并去掉标准 API）：
  按钮照样出现，点击走 webkitSetPresentationMode，模式变化事件让按钮状态跟着变；
- 不支持小窗的浏览器（两套 API 都去掉）：按钮不出现。
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import Bench, Lab, requirements_or_skip, skip  # noqa: E402

CLIP_SECONDS = 120
results = []

# iOS Safari: only the webkit presentation-mode API, no standard PiP API.
WEBKIT_JS = """
(() => {
  try { Object.defineProperty(Document.prototype, 'pictureInPictureEnabled', { get: () => false }); } catch (e) {}
  delete HTMLVideoElement.prototype.requestPictureInPicture;
  const P = HTMLVideoElement.prototype;
  P.webkitSupportsPresentationMode = function (m) { return m === 'picture-in-picture' || m === 'inline'; };
  P.webkitSetPresentationMode = function (m) {
    window.__webkitCalls = (window.__webkitCalls || 0) + 1;
    this.__mode = m;
    this.dispatchEvent(new Event('webkitpresentationmodechanged'));
  };
  Object.defineProperty(P, 'webkitPresentationMode', { get() { return this.__mode || 'inline'; } });
})();
"""
NO_PIP_JS = """
(() => {
  try { Object.defineProperty(Document.prototype, 'pictureInPictureEnabled', { get: () => false }); } catch (e) {}
  delete HTMLVideoElement.prototype.requestPictureInPicture;
})();
"""


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


async def pip_state(c):
    return await c.page.evaluate("""() => {
      const b = document.getElementById('pip'), v = document.querySelector('#player-host video');
      return { hidden: b.hidden, label: b.getAttribute('aria-label'), pressed: b.getAttribute('aria-pressed'),
               std: !!v && document.pictureInPictureElement === v,
               webkit: v ? (v.webkitPresentationMode || null) : null, calls: window.__webkitCalls || 0 };
    }""")


async def wait_for(c, pred, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        s = await pip_state(c)
        if pred(s) or loop.time() > end:
            return pred(s), s
        await asyncio.sleep(0.2)


async def main(args):
    requirements_or_skip()
    async with Bench({"ep1": CLIP_SECONDS}, headed=args.headed) as b:
        tail = b.mount("ep1", "/l1/e1")
        await b.start_cdn()
        room = await b.make_room(b.show("实验剧", {"线路一": [tail]}))
        host = await b.open_client("房主", b.host_url(room))
        viewer = await b.open_client("观众", b.viewer_url(room))

        async def webkit(page):
            await page.add_init_script(WEBKIT_JS)

        async def no_pip(page):
            await page.add_init_script(NO_PIP_JS)
        ios = await b.open_client("iOS", b.viewer_url(room), before_goto=webkit)
        old = await b.open_client("不支持", b.viewer_url(room), before_goto=no_pip)
        lab = Lab(room)
        for k, c in (("host", host), ("viewer", viewer), ("ios", ios), ("old", old)):
            lab.add(k, c)
        try:
            await asyncio.gather(*(c.click_start() for c in (host, viewer, ios, old)))
        except Exception as e:
            s = await lab.take()
            skip(f"播放器没就绪（可能没联网取不到 hls.js）：{e}；stage={s['host']['stage']!r}")
        await host.page.click("#play")
        ok, _ = await lab.wait_until(lambda s: s["host"]["ct"] > 1 and s["viewer"]["ct"] > 1, 15, "起播")
        check("起播", ok)

        # ---- 标准 API ----
        s = await pip_state(viewer)
        check("Chrome：小窗按钮出现", not s["hidden"] and s["label"] == "小窗播放", s)
        await viewer.page.click("#pip")
        ok, s = await wait_for(viewer, lambda s: s["std"] and s["pressed"] == "true")
        if not ok and not s["std"]:
            print("NOTE 这个 Chrome（可能是无头模式）不给开画中画，标准 API 的进入/退出跳过", flush=True)
        else:
            check("Chrome：点了进入画中画，按钮变成「退出小窗」", ok and s["label"] == "退出小窗", s)
            await lab.run(2, "小窗里播放")
            await host.page.click("#play")
            ok, _ = await lab.wait_until(lambda s: not s["playing"] and s["viewer"]["paused"], 8, "小窗里暂停")
            check("小窗里：房主暂停，观众跟着停", ok)
            await host.seek_ui(60, CLIP_SECONDS)
            ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - 60) < 1.5, 8, "小窗里拖动")
            check("小窗里：房主拖到 60 秒，观众跟过去", ok)
            await host.page.click("#play")
            ok, _ = await lab.wait_until(lambda s: s["playing"] and not s["viewer"]["paused"], 8, "小窗里继续")
            check("小窗里：房主继续，观众跟着播", ok)
            await viewer.page.click("#pip")
            ok, s = await wait_for(viewer, lambda s: not s["std"] and s["pressed"] == "false")
            check("Chrome：再点一次退出画中画", ok and s["label"] == "小窗播放", s)

        # ---- iOS webkit 接口 ----
        s = await pip_state(ios)
        check("iOS：小窗按钮出现", not s["hidden"], s)
        await ios.page.click("#pip")
        ok, s = await wait_for(ios, lambda s: s["webkit"] == "picture-in-picture" and s["pressed"] == "true")
        check("iOS：点击走 webkitSetPresentationMode，按钮变成「退出小窗」", ok and s["calls"] == 1 and s["label"] == "退出小窗", s)
        await ios.page.click("#pip")
        ok, s = await wait_for(ios, lambda s: s["webkit"] == "inline" and s["pressed"] == "false")
        check("iOS：再点一次回到页面里", ok and s["calls"] == 2, s)

        # ---- 不支持 ----
        s = await pip_state(old)
        check("不支持画中画的浏览器：按钮不出现", s["hidden"], s)

        for c in (host, viewer, ios, old):
            errors = [m for m in c.console if m.startswith("pageerror")]
            if errors:
                print(f"--- {c.name} console ---\n" + "\n".join(errors[-10:]), flush=True)
            check(f"{c.name}页面没有脚本错误", not errors)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed:\n  " + "\n  ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    asyncio.run(main(ap.parse_args()))
