"""服务器资源用量采样（给一起看的管理后台看）：只读 /proc、/sys/fs/cgroup 和磁盘用量，不需要额外依赖和权限。

容器里 /proc/stat、/proc/meminfo、/proc/loadavg 是宿主机的（整台服务器的 CPU、内存），
/proc/net/dev 是容器自己的网卡（也就是 bot 的流量，B 站转流都算在里面），
/sys/fs/cgroup/memory.current|max 是容器的内存和 compose 里的 mem_limit，/proc/self/status 是 bot 进程的 RSS。
读不到的项（比如在 Windows 上开发）就是 None，页面上显示「—」。
CPU 占用和网速是两次采样之间的差，第一次采样为 None。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _clk_tck() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return 100


class SystemStats:
    def __init__(self, proc: str | os.PathLike = "/proc", cgroup: str | os.PathLike = "/sys/fs/cgroup",
                 disk_path: str | os.PathLike = "/", clock: Callable[[], float] = time.monotonic):
        self.proc, self.cgroup, self.disk_path = Path(proc), Path(cgroup), disk_path
        self.clock = clock
        self._last: dict | None = None     # 上一次的原始计数：t、cpu (busy, total)、proc_ticks、net (rx, tx)

    # ---------------- 单项读取 ----------------
    def _cpu_counters(self) -> tuple[int, int] | None:
        """/proc/stat 第一行 -> (busy, total)，单位 jiffies。"""
        text = _read(self.proc / "stat")
        if not text:
            return None
        parts = text.split("\n", 1)[0].split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            nums = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)     # idle + iowait
        total = sum(nums)
        return total - idle, total

    def _process_ticks(self) -> int | None:
        """/proc/self/stat 的 utime + stime（jiffies）。进程名在括号里可能带空格，从最后一个 ')' 后面开始数。"""
        text = _read(self.proc / "self" / "stat")
        if not text:
            return None
        rest = text.rsplit(")", 1)[-1].split()
        try:
            return int(rest[11]) + int(rest[12])     # 原第 14、15 个字段（pid 和 comm 已去掉，state 是 rest[0]）
        except (IndexError, ValueError):
            return None

    def _net_counters(self) -> tuple[int, int] | None:
        """/proc/net/dev 除 lo 外所有网卡的 (rx_bytes, tx_bytes) 合计。"""
        text = _read(self.proc / "net" / "dev")
        if not text:
            return None
        rx = tx = 0
        found = False
        for line in text.splitlines()[2:]:
            name, sep, data = line.partition(":")
            if not sep or name.strip() == "lo":
                continue
            fields = data.split()
            try:
                rx += int(fields[0])
                tx += int(fields[8])
            except (IndexError, ValueError):
                continue
            found = True
        return (rx, tx) if found else None

    def _meminfo(self) -> dict | None:
        text = _read(self.proc / "meminfo")
        if not text:
            return None
        values = {}
        for line in text.splitlines():
            key, sep, rest = line.partition(":")
            if sep:
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key.strip()] = int(parts[0]) * 1024
        total, avail = values.get("MemTotal"), values.get("MemAvailable")
        if total is None or avail is None:
            return None
        return {"total": total, "available": avail, "used": max(0, total - avail)}

    def _load(self) -> list[float] | None:
        text = _read(self.proc / "loadavg")
        if not text:
            return None
        try:
            return [float(x) for x in text.split()[:3]]
        except ValueError:
            return None

    def _uptime(self) -> float | None:
        text = _read(self.proc / "uptime")
        if not text:
            return None
        try:
            return float(text.split()[0])
        except (IndexError, ValueError):
            return None

    def _process_rss(self) -> int | None:
        text = _read(self.proc / "self" / "status")
        if not text:
            return None
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
        return None

    def _container_mem(self) -> dict | None:
        """cgroup v2（memory.current / memory.max）或 v1（memory/memory.usage_in_bytes / memory.limit_in_bytes）。"""
        for cur_name, max_name in (("memory.current", "memory.max"),
                                   ("memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes")):
            cur = _read(self.cgroup / cur_name)
            if cur is None or not cur.strip().isdigit():
                continue
            limit_text = (_read(self.cgroup / max_name) or "").strip()
            limit = int(limit_text) if limit_text.isdigit() else None
            if limit is not None and limit >= 1 << 60:     # v1 没有限制时是一个极大的数
                limit = None
            return {"current": int(cur.strip()), "max": limit}
        return None

    def _disk(self) -> dict | None:
        try:
            u = shutil.disk_usage(self.disk_path)
        except OSError:
            return None
        return {"total": u.total, "used": u.used, "free": u.free}

    # ---------------- 对外 ----------------
    def sample(self) -> dict:
        now = self.clock()
        cpu, ticks, net = self._cpu_counters(), self._process_ticks(), self._net_counters()
        cpu_percent = process_cpu_percent = rx_rate = tx_rate = None
        last = self._last
        if last is not None and now > last["t"]:
            dt = now - last["t"]
            if cpu and last["cpu"]:
                dtotal = cpu[1] - last["cpu"][1]
                if dtotal > 0:
                    cpu_percent = max(0.0, min(100.0, 100.0 * (cpu[0] - last["cpu"][0]) / dtotal))
            if ticks is not None and last["ticks"] is not None:
                process_cpu_percent = max(0.0, 100.0 * (ticks - last["ticks"]) / _clk_tck() / dt)
            if net and last["net"]:
                rx_rate = max(0.0, (net[0] - last["net"][0]) / dt)
                tx_rate = max(0.0, (net[1] - last["net"][1]) / dt)
        self._last = {"t": now, "cpu": cpu, "ticks": ticks, "net": net}
        return {
            "cpu_percent": cpu_percent,
            "cpu_count": os.cpu_count(),
            "process_cpu_percent": process_cpu_percent,
            "load": self._load(),
            "mem": self._meminfo(),
            "container_mem": self._container_mem(),
            "process_rss": self._process_rss(),
            "disk": self._disk(),
            "net": ({"rx_bytes": net[0], "tx_bytes": net[1], "rx_rate": rx_rate, "tx_rate": tx_rate}
                    if net else None),
            "uptime": self._uptime(),
        }
