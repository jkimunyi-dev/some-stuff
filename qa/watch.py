"""Live progress of a running Kaggle kernel from its execution log (no secrets needed).

`kaggle kernels logs -f` replays the whole log of a running session, then streams. We read it for a few seconds and parse
the runner's progress lines:
    === pack 9/26 rc=0 in 201s; 20/50 done; lost 0          (isolated-pack parent)
    --- pack 1/1 done (2 questions); 20/50 answered so far  (child)
    [60/600] df8a0d48 urban_greenery op=None {...} err=...  (per question, only when the runner does not filter it)

    python -m qa.watch devkimunyi/municipal-qa-batch [devkimunyi/municipal-qa-fast ...] [--seconds 40]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

PACK = re.compile(r"=== pack (\d+)/(\d+) rc=(-?\d+) in (\d+)s; (\d+)/(\d+) done; lost (\d+)")
CHILD = re.compile(r"--- pack \d+/\d+ done \((\d+) questions\); (\d+)/(\d+) answered so far")
CMD = re.compile(r"\$ cd .*qa\.submit .*")
COMMIT = re.compile(r"^([0-9a-f]{7}) (.{0,70})", re.M)


def read_log(kernel: str, seconds: int) -> str:
    try:
        r = subprocess.run([sys.executable, "-m", "kaggle", "kernels", "logs", "-f", kernel], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=seconds)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:  # expected: -f never ends while the kernel runs
        out = e.stdout or b""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else out


def status(kernel: str) -> str:
    r = subprocess.run([sys.executable, "-m", "kaggle", "kernels", "status", kernel], capture_output=True, text=True)
    return (r.stdout.strip().splitlines() or ["?"])[-1].split("status")[-1].strip(' "')


def summarize(kernel: str, log: str) -> str:
    lines = [f"## {kernel}  [{status(kernel)}]"]
    if m := COMMIT.search(log):
        lines.append(f"commit {m.group(1)} {m.group(2)}")
    packs = PACK.findall(log)
    if packs:
        i, n, rc, _, done, total, lost = packs[-1]
        secs = [int(p[3]) for p in packs]
        el = sum(secs)
        rate = int(done) / el if el else 0
        eta = (int(total) - int(done)) / rate / 60 if rate else float("nan")
        bad = sum(1 for p in packs if p[2] != "0")
        lines.append(f"packs {i}/{n} | questions {done}/{total} ({100 * int(done) / int(total):.0f}%) | lost {sum(int(p[6]) for p in packs)}"
                     f" | crashed packs {bad} | answering time {el / 60:.1f} min | ETA ~{eta:.0f} min (at {60 * rate:.2f} q/min)")
    elif kids := CHILD.findall(log):
        _, done, total = kids[-1]
        lines.append(f"questions {done}/{total} (no per-pack timings)")
    elif CMD.search(log):
        lines.append("answering started, but no progress lines yet: either the first pack is still running, or this runner"
                     " buffers its output (old commits pipe through `tail`, which prints only at the end)")
    else:
        tail = [l for l in log.strip().splitlines() if l.strip()][-3:]
        lines.append("setting up: " + " | ".join(t[:100] for t in tail))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kernels", nargs="+")
    ap.add_argument("--seconds", type=int, default=40, help="how long to read each log stream")
    a = ap.parse_args()
    print(time.strftime("%Y-%m-%d %H:%M:%S local"))
    for k in a.kernels:
        print(summarize(k, read_log(k, a.seconds)))


if __name__ == "__main__":
    main()
