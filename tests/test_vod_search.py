"""一起看：按片名搜索资源站测试。

运行：python tests/test_vod_search.py
不联网：HTTP 用假的 session，数据来自 tests/fixtures/watch/vod_search_*.json。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import vod_search as vs  # noqa: E402
from vod_search import Episode, SearchUnavailable, VodSearch  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def load(name):
    with open(os.path.join(ROOT, "tests", "fixtures", "watch", f"vod_search_{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def entry(name="片", year="2020", play_from="a", play_url="第1集$https://x/1.m3u8", type_name="剧"):
    return {"vod_name": name, "vod_year": year, "type_name": type_name, "vod_play_from": play_from, "vod_play_url": play_url}


def test_parse_sources_env():
    d = vs.parse_sources_env(None)
    check("默认 3 个", len(d) == 3 and d[0] == ("创享影视", "https://ys.cxkjg.cn/api/provide/vod")
          and [n for n, _ in d] == ["创享影视", "1080资源", "量子"])
    check("空串禁用", vs.parse_sources_env("") == [])
    check("trim", vs.parse_sources_env("甲=https://a/x, 乙=https://b/y") == [("甲", "https://a/x"), ("乙", "https://b/y")])
    check("跳过坏项", vs.parse_sources_env("无等号,丙=ftp://c/z,丁=javascript:alert(1),=https://e/f,戊=http://g/h")
          == [("戊", "http://g/h")])


def test_parse_detail_aggregator():
    shows = vs.parse_detail(load("cxkjg"), "创享影视")
    s = shows[0]
    check("3 条", len(shows) == 3)
    check("标题空白折叠", s.title == "绝命毒师 第五季")
    check("年份/类型/集数", s.year == 2012 and s.type_name == "欧美剧" and s.episode_count == 16)
    check("key", s.key == "绝命毒师第五季|2012")
    check("线路名", s.lines[0].name == "创享影视/金鹰" and s.lines[1].name == "创享影视/闪电")
    check("首集", s.lines[0].episodes[0] == Episode("第01集", "https://hd.kuktxu.com/play/zbqxRqya/index.m3u8"))
    one = vs.parse_detail(load("1080zyk"), "1080资源")[0]
    check("单线路用接口名", one.lines[0].name == "1080资源")


def test_parse_detail_filters():
    play_url = "$$$".join([
        "第1集$https://x/share/abc",
        "第1集$https://x/a.m3u8#第2集$https://x/b.m3u8#",
        "第1集$http://x/a.m3u8",
    ])
    shows = vs.parse_detail({"list": [entry(play_from="web$$$m3u8$$$plain", play_url=play_url)]}, "站")
    check("只留 https m3u8 线路", len(shows) == 1 and [l.name for l in shows[0].lines] == ["站/m3u8"])
    check("尾部 # 忽略", shows[0].episode_count == 2)
    check("没有可播线路整条丢弃", vs.parse_detail({"list": [entry(play_url="第1集$https://x/share/abc")]}, "站") == [])
    nolabel = vs.parse_detail({"list": [entry(play_url="https://x/a.m3u8#https://x/b.m3u8")]}, "站")[0]
    check("无 $ 用 第{n}集", [e.label for e in nolabel.lines[0].episodes] == ["第01集", "第02集"]
          and nolabel.lines[0].episodes[1].url == "https://x/b.m3u8")
    check("标签里带 $ 按最后一个切", vs.parse_detail({"list": [entry(play_url="a$b$https://x/a.m3u8")]}, "站")[0]
          .lines[0].episodes[0] == Episode("a$b", "https://x/a.m3u8"))


def test_parse_detail_bad_input():
    check("空 dict", vs.parse_detail({}, "站") == [])
    check("list 不是列表", vs.parse_detail({"list": "x"}, "站") == [])
    check("不是 dict", vs.parse_detail([], "站") == [])
    bad = entry()
    del bad["vod_play_url"]
    check("缺 vod_play_url 跳过", vs.parse_detail({"list": [bad, "x", entry(name="好")]}, "站")[0].title == "好"
          and len(vs.parse_detail({"list": [bad, "x", entry(name="好")]}, "站")) == 1)
    check("年份非数字", vs.parse_detail({"list": [entry(year="未知")]}, "站")[0].year is None)
    check("年份取前 4 位数字", [vs.parse_detail({"list": [entry(year=y)]}, "站")[0].year
                              for y in ("2012.0", "2012-2013", 2012, "约1999年")] == [2012, 2012, 2012, 1999])
    check("年份不足 4 位", vs.parse_detail({"list": [entry(year="12")]}, "站")[0].year is None)
    check("年份缺失", vs.parse_detail({"list": [entry(year=None)]}, "站")[0].year is None)


def merged_all():
    return vs.merge_shows([vs.parse_detail(load("cxkjg"), "创享影视"), vs.parse_detail(load("1080zyk"), "1080资源"),
                           vs.parse_detail(load("lzi"), "量子")])


def test_merge():
    a, b = vs.parse_detail(load("cxkjg"), "创享影视"), vs.parse_detail(load("1080zyk"), "1080资源")
    n_a = len(a[0].lines)
    m = vs.merge_shows([a, b])
    s5 = [s for s in m if s.key == "绝命毒师第五季|2012"]
    check("同名同年合并", len(s5) == 1 and s5[0].title == "绝命毒师 第五季")
    names = [l.name for l in s5[0].lines]
    check("创享影视线路在前", names[:n_a] == [l.name for l in a[0].lines] and names[n_a:] == ["1080资源"])
    check("只在一边的保留", len(m) == 5 and any(s.title == "绝命毒师第一季" for s in m))
    check("不改入参", len(a[0].lines) == n_a)
    x = vs.parse_detail({"list": [entry(name="同名", year="2000")]}, "甲")
    y = vs.parse_detail({"list": [entry(name="同名", year="2001")]}, "乙")
    check("同名不同年不合并", len(vs.merge_shows([x, y])) == 2)
    p1 = vs.parse_detail({"list": [dict(entry(name="海报"), vod_pic=" https://img.example.com/a.jpg ")]}, "甲")
    p2 = vs.parse_detail({"list": [dict(entry(name="海报"), vod_pic="http://img.example.com/b.jpg")]}, "乙")
    p3 = vs.parse_detail({"list": [dict(entry(name="海报"), vod_pic=123)]}, "丙")
    check("海报只认 https、去空白", p1[0].pic == "https://img.example.com/a.jpg" and p2[0].pic is None
          and p3[0].pic is None and vs.parse_detail({"list": [entry()]}, "甲")[0].pic is None)
    check("合并时补上海报", vs.merge_shows([p2, p1])[0].pic == "https://img.example.com/a.jpg"
          and vs.merge_shows([p1, p2])[0].pic == "https://img.example.com/a.jpg")


def test_rank():
    r = vs.rank_shows(merged_all(), "绝命毒师")
    titles = [s.title for s in r]
    check("全部在", len(r) == 8)
    seasons = [i for i, s in enumerate(r) if s.type_name != "电影解说"]
    check("正片按年份倒序", [r[i].year for i in seasons] == [2012, 2011, 2010, 2009, 2008])
    jieshuo = [i for i, s in enumerate(r) if s.type_name == "电影解说"]
    check("电影解说在最后", len(jieshuo) == 3 and min(jieshuo) > max(seasons))
    exact = vs.rank_shows(vs.merge_shows([vs.parse_detail({"list": [
        entry(name="别的 绝命毒师", year="2030"), entry(name="绝命毒师 外传", year="2001"),
        entry(name="绝命 毒师", year="1999"), entry(name="绝命毒师续", year="2010")]}, "站")]), "绝命毒师")
    check("相等 > 开头 > 其他", [s.title for s in exact] == ["绝命 毒师", "绝命毒师续", "绝命毒师 外传", "别的 绝命毒师"])
    longer = vs.rank_shows(vs.merge_shows([vs.parse_detail({"list": [
        entry(name="绝命毒师 前传特别篇", year="2030"), entry(name="绝命毒师二", year="2001"),
        entry(name="绝命毒师三", year="2005"), entry(name="绝命毒师无年份", year=None)]}, "站")]), "绝命毒师")
    check("开头档：短的在前，同长度年份倒序", [s.title for s in longer] == ["绝命毒师三", "绝命毒师二", "绝命毒师无年份", "绝命毒师 前传特别篇"])
    nulls = vs.rank_shows(vs.merge_shows([vs.parse_detail({"list": [
        entry(name="甲", year=None), entry(name="乙", year="2001")]}, "站")]), "x")
    check("无年份排最后", [s.title for s in nulls] == ["乙", "甲"])
    check("规范化", vs.normalize_title(" ＡＢ c　D ") == "abcd")


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status, self._text = status, text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for key, val in self.routes:
            if key in url:
                return val(url) if callable(val) else val
        return FakeResp(404, "")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def raw(name):
    with open(os.path.join(ROOT, "tests", "fixtures", "watch", f"vod_search_{name}.json"), encoding="utf-8") as f:
        return f.read()


def test_search_fake_session():
    async def run():
        def boom(url):
            raise asyncio.TimeoutError()
        fs = FakeSession([("one.test", FakeResp(200, raw("cxkjg"))),
                          ("two.test", FakeResp(200, "<html>Just a moment...</html>")),
                          ("three.test", boom)])
        srch = VodSearch([("一", "https://one.test/vod"), ("二", "https://two.test/vod"), ("三", "https://three.test/vod")],
                         session_factory=lambda: fs)
        out = await srch.search("绝命毒师")
        check("只剩第一个站的结果", [s.year for s in out] == [2012, 2011, 2010] and out[0].lines[0].name == "一/金鹰")
        one = [kw for u, kw in fs.calls if "one.test" in u][0]
        check("参数 ac=detail&wd", one["params"] == {"ac": "detail", "wd": "绝命毒师"} and one.get("allow_redirects") is True)
        check("三个站都查了", len(fs.calls) == 3)
        many = {"list": [entry(name=f"片{i}", year=str(2000 + i)) for i in range(15)]}
        out2 = await VodSearch([("甲", "https://a.test/x")], session_factory=lambda: FakeSession([("a.test", FakeResp(200, json.dumps(many)))])).search("片")
        check("最多 10 条", len(out2) == 10 and out2[0].year == 2009)
        slow = VodSearch([("甲", "https://a.test/x"), ("慢", "https://slow.test/x")], session_factory=lambda: FakeSession(
            [("a.test", FakeResp(200, raw("cxkjg"))), ("slow.test", lambda u: _Hang())]), timeout=0.05)
        check("超时的站被跳过", len(await slow.search("绝命毒师")) == 3)
    asyncio.run(run())


class _Hang:
    async def __aenter__(self):
        await asyncio.sleep(10)

    async def __aexit__(self, *a):
        return False


class _BadEnter:
    async def __aenter__(self):
        raise OSError("enter failed")

    async def __aexit__(self, *a):
        return False


def test_search_all_fail():
    async def run():
        def boom(url):
            raise OSError("down")
        srch = VodSearch([("甲", "https://a.test/x"), ("乙", "https://b.test/x"), ("丙", "https://c.test/x")],
                         session_factory=lambda: FakeSession([("a.test", boom), ("b.test", FakeResp(503, "x")),
                                                              ("c.test", FakeResp(200, '{"list": [{"vod'))]))
        try:
            await srch.search("x")
            check("全部失败抛 SearchUnavailable", False)
        except SearchUnavailable as e:
            check("全部失败抛 SearchUnavailable", e.message == "资源站暂时无法访问，请稍后再试")
        empty = VodSearch([("甲", "https://a.test/x"), ("乙", "https://b.test/x")],
                          session_factory=lambda: FakeSession([("a.test", FakeResp(200, raw("empty"))), ("b.test", FakeResp(200, raw("empty")))]))
        check("都没结果返回 []", await empty.search("zzz") == [])
        class BadFactory:
            def __call__(self):
                raise RuntimeError("no session")
        for fac in (BadFactory(), lambda: _BadEnter()):
            try:
                await VodSearch([("甲", "https://a.test/x")], session_factory=fac).search("x")
                check("session 出错抛 SearchUnavailable", False)
            except SearchUnavailable as e:
                check("session 出错抛 SearchUnavailable", e.message == "资源站暂时无法访问，请稍后再试")
        check("没有配置源返回 []", await VodSearch([]).search("x") == [])
    asyncio.run(run())


if __name__ == "__main__":
    test_parse_sources_env()
    test_parse_detail_aggregator()
    test_parse_detail_filters()
    test_parse_detail_bad_input()
    test_merge()
    test_rank()
    test_search_fake_session()
    test_search_all_fail()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
