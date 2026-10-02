"""英雄数据缓存/刷新/名字解析测试。不联网：刷新用假的 fetch 函数。

运行：python tests/test_herodata.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from herodata import HeroStore, load_aliases  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def snapshot(names, with_weapons=None):
    with_weapons = names if with_weapons is None else with_weapons
    return {n: {"name": n, "weapons": [{"name": "gun"}] if n in with_weapons else [], "patches": []}
            for n in names}


NAMES = ["Cassidy", "Tracer", "Sojourn", "Soldier: 76", "Sombra", "Lúcio", "Reinhardt", "Mei", "Mercy", "Moira"]


def test_resolve():
    store = HeroStore("unused.json", load_aliases())
    store.heroes = snapshot(NAMES)
    name = lambda q: (lambda r: r["name"] if isinstance(r, dict) else r)(store.resolve(q))  # noqa: E731
    check("精确名，忽略大小写", name("cassidy") == "Cassidy")
    check("中文别名", name("麦克雷") == "Cassidy" and name("猎空") == "Tracer")
    check("英文别名", name("McCree") == "Cassidy")
    check("忽略标点：Soldier 76", name("soldier 76") == "Soldier: 76")
    check("数字别名 76", name("76") == "Soldier: 76")
    check("忽略重音：lucio", name("lucio") == "Lúcio")
    check("单字中文别名：美", name("美") == "Mei")
    check("唯一前缀：tr", name("tr") == "Tracer")
    check("歧义前缀返回候选", name("so") == ["Sojourn", "Soldier: 76", "Sombra"])
    check("拼写错误返回候选而不是猜", name("reinhart") == ["Reinhardt"])
    check("单字母不自动匹配", isinstance(store.resolve("m"), list))
    check("完全不认识返回空列表", name("xyz") == [])
    check("空字符串返回空列表", name("  ") == [])


def test_aliases_file():
    aliases = load_aliases()
    check("aliases.json 能读", "Cassidy" in aliases and "麦克雷" in aliases["Cassidy"])
    flat = [a for v in aliases.values() for a in v]
    check("别名之间没有重复", len(flat) == len(set(flat)))


def test_accept():
    store = HeroStore("unused.json")
    check("空快照不接受", not store.accept({}))
    check("没有旧快照时，非空就接受", store.accept(snapshot(["A"])))
    store.heroes = snapshot([f"H{i}" for i in range(50)])
    check("英雄数降到 90% 以下不接受", not store.accept(snapshot([f"H{i}" for i in range(44)])))
    check("英雄数 90% 接受", store.accept(snapshot([f"H{i}" for i in range(45)])))
    names = [f"H{i}" for i in range(50)]
    check("有武器的英雄占比掉 20 个百分点不接受", not store.accept(snapshot(names, names[:40])))
    check("占比掉 10 个百分点以内接受", store.accept(snapshot(names, names[:46])))


def test_refresh_and_cache():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "heroes.json")
        store = HeroStore(path)
        check("没有缓存文件时 load 返回 False", store.load() is False and not store.ready)

        async def good():
            return snapshot(["Cassidy", "Tracer"])

        async def broken():
            raise RuntimeError("wiki down")

        async def tiny():
            return snapshot(["Cassidy"])

        check("刷新成功", asyncio.run(store.refresh(good)) is True and store.ready)
        check("写入了缓存文件，含 fetched_at", os.path.exists(path) and store.fetched_at)
        check("目录里没有残留临时文件", os.listdir(os.path.dirname(path)) == ["heroes.json"])
        check("抓取异常时保留旧快照", asyncio.run(store.refresh(broken)) is False and len(store.heroes) == 2)
        check("异常快照被拒绝", asyncio.run(store.refresh(tiny)) is False and len(store.heroes) == 2)

        fresh = HeroStore(path)
        check("新进程能从缓存加载", fresh.load() is True and set(fresh.heroes) == {"Cassidy", "Tracer"}
              and fresh.fetched_at == store.fetched_at)

        blocker = os.path.join(d, "blocker")
        open(blocker, "w").close()
        unwritable = HeroStore(os.path.join(blocker, "heroes.json"))  # 父路径是文件，写缓存必然失败
        check("缓存写不进去时仍然更新内存数据", asyncio.run(unwritable.refresh(good)) is True and unwritable.ready)

        with open(path, "w") as f:
            f.write("{not json")
        check("缓存文件损坏时 load 返回 False", HeroStore(path).load() is False)
        with open(path, "w") as f:
            json.dump({"unexpected": True}, f)
        check("缓存格式不对时 load 返回 False", HeroStore(path).load() is False)


if __name__ == "__main__":
    test_resolve()
    test_aliases_file()
    test_accept()
    test_refresh_and_cache()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
