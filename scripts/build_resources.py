#!/usr/bin/env python3
"""Reserve resources for the Actions runner and isolate memory-heavy build processes."""
import argparse
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import subprocess

MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True)
class Limits:
    jobs: int
    memory: int
    cpu_quota: int


def plan_resources(proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup")):
    memory = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", (proc / "meminfo").read_text(), re.M))
    total, available = (int(memory[key]) * 1024 for key in ("MemTotal", "MemAvailable"))
    cpus = float(len(os.sched_getaffinity(0)))
    membership = next(line[3:] for line in (proc / "self/cgroup").read_text().splitlines() if line.startswith("0::"))
    base = cgroups.resolve()
    directory = (base / membership.lstrip("/")).resolve()
    if not directory.is_relative_to(base):
        raise ValueError("Invalid cgroup membership")
    # A child can have no limit while its parent still limits the entire runner.
    while True:
        if (directory / "cpu.max").is_file():
            quota, period = (directory / "cpu.max").read_text().split()
            if quota != "max":
                cpus = min(cpus, int(quota) / int(period))
        if (directory / "memory.max").is_file():
            limit = (directory / "memory.max").read_text().strip()
            if limit != "max":
                current = int((directory / "memory.current").read_text())
                stat = dict(re.findall(r"^(\w+) (\d+)$", (directory / "memory.stat").read_text(), re.M))
                reclaimable = min(current, int(stat.get("inactive_file", 0)))
                total = min(total, int(limit))
                available = min(available, max(0, int(limit) - current + reclaimable))
        if directory == base:
            break
        directory = directory.parent
    reserve = min(2 * GIB, max(128 * MIB, total // 8))
    budget = available - reserve
    if budget < 128 * MIB:
        raise RuntimeError("Insufficient free memory to build while preserving runner headroom")
    # Keep a CPU available to the runner when more than one CPU is allocated.
    cpu_jobs = max(1, math.floor(cpus - 1))
    jobs = min(cpu_jobs, max(1, budget // (1536 * MIB)))
    return Limits(jobs, budget, max(1, int(min(cpus, jobs) * 100)))


def scope_command(limits, command):
    command = list(map(str, command))
    if Path(command[0]).name == "make":
        command = [f"-j{min(int(arg[2:]), limits.jobs)}" if re.fullmatch(r"-j\d+", arg) else arg
                   for arg in command]
    return ["sudo", "-n", "-E", "systemd-run", "--scope", "--quiet", "--collect", "--expand-environment=no",
            f"--uid={os.getuid()}", f"--gid={os.getgid()}", "--nice=10",
            "--property=CPUWeight=20", f"--property=CPUQuota={limits.cpu_quota}%",
            f"--property=MemoryMax={limits.memory}", "--property=MemorySwapMax=0",
            "--property=OOMPolicy=kill", "--", "env", f"HOME={os.environ['HOME']}",
            f"PATH={os.environ['PATH']}", f"BUILD_JOBS={limits.jobs}", f"GOMAXPROCS={limits.jobs}",
            f"CARGO_BUILD_JOBS={limits.jobs}", *command]


def run_bounded(command, cwd=None, check=True):
    limits = plan_resources()
    print(f"Build scope: {limits.jobs} jobs, {limits.memory // MIB} MiB RAM, CPU quota {limits.cpu_quota}%", flush=True)
    result = subprocess.run(scope_command(limits, command), cwd=cwd)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("env")
    sub.add_parser("run").add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == "env":
        print(f"BUILD_JOBS={plan_resources().jobs}")
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires a command")
        result = run_bounded(command, check=False)
        raise SystemExit(result.returncode if result.returncode >= 0 else 128 - result.returncode)


if __name__ == "__main__":
    main()
