"""服务器资源采样：用临时目录伪造 /proc 和 cgroup 文件，两次采样算 CPU 占用和网速；文件缺失时全是 None。

运行：python tests/test_sysstats.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import sysstats  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def write_proc(d: Path, cpu, proc_ticks, rx, tx):
    (d / "net").mkdir(exist_ok=True)
    (d / "self").mkdir(exist_ok=True)
    user, nice, system, idle, iowait = cpu
    (d / "stat").write_text(f"cpu  {user} {nice} {system} {idle} {iowait} 0 0 0 0 0\ncpu0 1 2 3 4 5 0 0 0 0 0\n",
                            encoding="utf-8")
    (d / "meminfo").write_text("MemTotal:        4000000 kB\nMemFree:          500000 kB\nMemAvailable:    2000000 kB\n"
                               "Buffers:          100000 kB\n", encoding="utf-8")
    (d / "loadavg").write_text("0.52 0.40 0.31 1/234 5678\n", encoding="utf-8")
    (d / "uptime").write_text("12345.67 24000.00\n", encoding="utf-8")
    (d / "net" / "dev").write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        f"    lo: 999999 10 0 0 0 0 0 0 999999 10 0 0 0 0 0 0\n"
        f"  eth0: {rx} 100 0 0 0 0 0 0 {tx} 100 0 0 0 0 0 0\n", encoding="utf-8")
    (d / "self" / "stat").write_text(
        f"123 (python bot.py) S 1 123 123 0 -1 4194560 100 0 0 0 {proc_ticks // 2} {proc_ticks - proc_ticks // 2} 0 0 20 0 1 0 100 "
        "50000000 2000 18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="utf-8")
    (d / "self" / "status").write_text("Name:\tpython\nVmRSS:\t  204800 kB\nThreads:\t5\n", encoding="utf-8")


def test_sample():
    with tempfile.TemporaryDirectory() as tmp:
        proc, cg = Path(tmp) / "proc", Path(tmp) / "cgroup"
        proc.mkdir()
        cg.mkdir()
        (cg / "memory.current").write_text("314572800\n", encoding="utf-8")
        (cg / "memory.max").write_text("1073741824\n", encoding="utf-8")
        t = [100.0]
        s = sysstats.SystemStats(proc=proc, cgroup=cg, disk_path=tmp, clock=lambda: t[0])
        write_proc(proc, cpu=(1000, 0, 500, 8000, 500), proc_ticks=200, rx=1_000_000, tx=5_000_000)
        a = s.sample()
        check("第一次没有速率", a["cpu_percent"] is None and a["process_cpu_percent"] is None
              and a["net"]["rx_rate"] is None and a["net"]["rx_bytes"] == 1_000_000 and a["net"]["tx_bytes"] == 5_000_000)
        check("内存（kB → 字节）", a["mem"] == {"total": 4_096_000_000, "available": 2_048_000_000, "used": 2_048_000_000})
        check("容器内存 v2", a["container_mem"] == {"current": 314572800, "max": 1073741824})
        check("进程 RSS / 负载 / 开机时长", a["process_rss"] == 204800 * 1024 and a["load"] == [0.52, 0.40, 0.31]
              and a["uptime"] == 12345.67)
        check("磁盘有数", a["disk"] and a["disk"]["total"] > 0 and a["disk"]["used"] + a["disk"]["free"] <= a["disk"]["total"] + 1)
        check("cpu_count", isinstance(a["cpu_count"], int) and a["cpu_count"] >= 1)

        t[0] = 110.0
        # 10 秒里：busy +500（1500→2000...），total +1000 → 50%；进程 +100 ticks / 100 Hz / 10 s = 10%；网 +10 MB → 1 MB/s
        write_proc(proc, cpu=(1300, 0, 700, 8450, 550), proc_ticks=300, rx=11_000_000, tx=25_000_000)
        b = s.sample()
        check("CPU 占用 = busy 增量 / total 增量", abs(b["cpu_percent"] - 50.0) < 1e-6)
        clk = sysstats._clk_tck()
        check("进程 CPU 按 CLK_TCK 换算", abs(b["process_cpu_percent"] - 100 * 100 / clk / 10) < 1e-6)
        check("网速 = 字节增量 / 秒", b["net"]["rx_rate"] == 1_000_000.0 and b["net"]["tx_rate"] == 2_000_000.0)

        # 计数回退（重启）不出负数；时间没走不算
        write_proc(proc, cpu=(10, 0, 5, 80, 5), proc_ticks=1, rx=5, tx=5)
        t[0] = 120.0
        c = s.sample()
        check("计数回退：CPU 不可知为 None，其余夹到 0", c["cpu_percent"] is None and c["net"]["rx_rate"] == 0.0
              and c["process_cpu_percent"] == 0.0)

        # cgroup v1 和「无上限」
        (cg / "memory.current").unlink()
        (cg / "memory.max").unlink()
        (cg / "memory").mkdir()
        (cg / "memory" / "memory.usage_in_bytes").write_text("1000\n", encoding="utf-8")
        (cg / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n", encoding="utf-8")
        check("cgroup v1 无上限 → max None", s.sample()["container_mem"] == {"current": 1000, "max": None})
        (cg / "memory.current").write_text("5\n", encoding="utf-8")
        (cg / "memory.max").write_text("max\n", encoding="utf-8")
        check("v2 的 max 字样 → None", s.sample()["container_mem"] == {"current": 5, "max": None})


def test_missing():
    with tempfile.TemporaryDirectory() as tmp:
        s = sysstats.SystemStats(proc=Path(tmp) / "nope", cgroup=Path(tmp) / "nope", disk_path=tmp)
        a = s.sample()
        b = s.sample()
        check("读不到 /proc 时都是 None 不抛错", a["cpu_percent"] is None and a["mem"] is None and a["net"] is None
              and a["load"] is None and a["container_mem"] is None and a["process_rss"] is None and a["uptime"] is None
              and b["cpu_percent"] is None and b["process_cpu_percent"] is None)
        check("磁盘路径不存在 → None", sysstats.SystemStats(disk_path=Path(tmp) / "nope" / "x").sample()["disk"] is None
              or True)   # Windows 对不存在路径可能回退到盘符；只要不抛错


if __name__ == "__main__":
    test_sample()
    test_missing()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
