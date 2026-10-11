"""赛区映射测试：Liquipedia 赛事路径 -> 赛区、赛区 -> 频道的存储与解析。

运行：python tests/test_regions.py
不联网，不需要 Discord。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import regions as rg  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def test_candidates():
    c = rg.region_candidates
    check("韩国联赛 -> korea 优先、asia 兜底",
          c("Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1") == ["korea", "asia"])
    check("日本联赛", c("Overwatch Champions Series/2026/Asia/Stage 3/Japan/Regular Season") == ["japan", "asia"])
    check("中国赛区 -> cn", c("Overwatch Champions Series/2026/China/Stage 3/Regular Season") == ["cn"])
    check("EMEA", c("Overwatch Champions Series/2026/EMEA/Stage 3/Playoffs") == ["emea"])
    check("北美 -> na", c("Overwatch Champions Series/2026/North America/Stage 3/Regular Season") == ["na"])
    check("世界总决赛 -> world", c("Overwatch Champions Series/2026/World Finals") == ["world"])
    check("季中赛 -> world", c("Overwatch Champions Series/2026/Midseason Championship/Playoffs") == ["world"])
    check("世界杯 -> owwc, world", c("Overwatch World Cup/2026/Group Stage") == ["owwc", "world"])
    check("大小写和锚点不影响", c("overwatch champions series/2026/asia/stage 3/korea/x#Week 2") == ["korea", "asia"])
    check("空路径 -> world", c("") == ["world"])

    check("解析顺序：具体赛区绑定了就用它", rg.resolve({"korea": 1, "asia": 2}, ["korea", "asia"]) == 1)
    check("具体赛区没绑就用上级", rg.resolve({"asia": 2}, ["korea", "asia"]) == 2)
    check("都没绑返回 None", rg.resolve({"na": 3}, ["korea", "asia"]) is None)


def test_normalize():
    check("归一化：去空格、小写、去 #", rg.normalize_region(" EMEA ") == "emea" and rg.normalize_region("#cn") == "cn")
    for bad in ("", "e m e a", "太长太长太长", "a" * 30):
        try:
            rg.normalize_region(bad)
            check(f"非法赛区名 {bad!r} 抛错", False)
        except rg.RegionError:
            check(f"非法赛区名 {bad!r} 抛错", True)


def test_store():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "regions.json")
        store = rg.RegionStore(path)
        store.load()
        check("文件不存在 -> 空映射", store.channels == {})
        store.set("cn", 111)
        store.set("emea", 222)
        check("自动建目录并落盘", os.path.exists(path) and json.load(open(path, encoding="utf-8")) == {"channels": {"cn": 111, "emea": 222}})
        check("get / resolve", store.get("cn") == 111 and store.get("na") is None
              and store.resolve(["korea", "asia"]) is None and store.resolve(["cn"]) == 111)

        again = rg.RegionStore(path)
        again.load()
        check("重新加载一致", again.channels == {"cn": 111, "emea": 222})
        check("删除存在/不存在的赛区", again.remove("emea") and not again.remove("emea")
              and json.load(open(path, encoding="utf-8"))["channels"] == {"cn": 111})

        with open(path, "w") as f:
            f.write("{broken")
        broken = rg.RegionStore(path)
        broken.load()
        check("文件损坏时用空映射、不抛错", broken.channels == {})

        blocker = os.path.join(d, "blocker")
        open(blocker, "w").close()
        unwritable = rg.RegionStore(os.path.join(blocker, "regions.json"))
        unwritable.set("na", 5)
        check("写不进去时不抛异常、内存里照常生效", unwritable.get("na") == 5)

    mem = rg.RegionStore(None)
    mem.set("asia", 9)
    check("path=None 只存内存", mem.get("asia") == 9)


if __name__ == "__main__":
    import logging
    logging.disable(logging.ERROR)
    test_candidates()
    test_normalize()
    test_store()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
