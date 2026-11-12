#!/usr/bin/env python3
"""Optional machine sampler: appends one JSON line per interval with load, CPU, memory, disk, net, GPU.

Run it next to a long autonomous run, then pass the file to `analyze.py --machine-log FILE`
to turn inferred environment signals into measured ones.

  python3 sample_machine.py --out samples.jsonl --interval 5 --duration 3600 [--path /]
Linux uses /proc; other systems get what os/shutil can provide. GPU only if nvidia-smi exists.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time


def read_cpu():
    try:
        with open("/proc/stat") as f:
            p = f.readline().split()[1:]
        v = list(map(int, p))
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)
    except (OSError, ValueError, IndexError):
        return None


def read_mem():
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                d[k] = int(rest.split()[0])
        return 100.0 * d["MemAvailable"] / d["MemTotal"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def read_net():
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in list(f)[2:]:
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                c = rest.split()
                rx += int(c[0])
                tx += int(c[8])
        return rx, tx
    except (OSError, ValueError, IndexError):
        return None


def read_gpu():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()[0]
        u, mu, mt = [float(x) for x in out.split(",")]
        return u, 100.0 * mu / mt if mt else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until interrupted")
    ap.add_argument("--path", default="/", help="filesystem to watch for free space")
    a = ap.parse_args(argv)
    prev_cpu, prev_net, prev_t = read_cpu(), read_net(), time.time()
    end = time.time() + a.duration if a.duration else None
    try:
        with open(a.out, "a", encoding="utf-8") as f:
            while end is None or time.time() < end:
                time.sleep(a.interval)
                now = time.time()
                row = {"t": round(now, 2)}
                try:
                    row["load1"] = os.getloadavg()[0]
                except (OSError, AttributeError):
                    pass
                cpu = read_cpu()
                if cpu and prev_cpu and cpu[1] > prev_cpu[1]:
                    row["cpu_pct"] = round(100.0 * (1 - (cpu[0] - prev_cpu[0]) / (cpu[1] - prev_cpu[1])), 1)
                prev_cpu = cpu
                m = read_mem()
                if m is not None:
                    row["mem_avail_pct"] = round(m, 1)
                try:
                    row["disk_free_gb"] = round(shutil.disk_usage(a.path).free / 1e9, 2)
                except OSError:
                    pass
                net = read_net()
                if net and prev_net and now > prev_t:
                    row["net_rx_kbps"] = round((net[0] - prev_net[0]) / 125.0 / (now - prev_t), 1)
                    row["net_tx_kbps"] = round((net[1] - prev_net[1]) / 125.0 / (now - prev_t), 1)
                prev_net, prev_t = net, now
                g = read_gpu()
                if g:
                    row["gpu_util"] = g[0]
                    if g[1] is not None:
                        row["gpu_mem_pct"] = round(g[1], 1)
                f.write(json.dumps(row) + "\n")
                f.flush()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
