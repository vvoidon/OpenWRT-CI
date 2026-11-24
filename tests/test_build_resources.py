import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from build_resources import Limits, MIB, plan_resources, run_bounded, scope_command


class BuildResourcesTests(unittest.TestCase):
    def fixture(self, base, memory, current, quota):
        proc, cgroups = base / "proc", base / "cgroups"
        (proc / "self").mkdir(parents=True)
        (proc / "meminfo").write_text("MemTotal: 16777216 kB\nMemAvailable: 12582912 kB\n")
        (proc / "self/cgroup").write_text("0::/runner/step\n")
        parent = cgroups / "runner"
        (parent / "step").mkdir(parents=True)
        (parent / "memory.max").write_text(str(memory))
        (parent / "memory.current").write_text(str(current))
        (parent / "memory.stat").write_text("inactive_file 0\n")
        (parent / "cpu.max").write_text(quota)
        (parent / "step/memory.max").write_text("max")
        (parent / "step/cpu.max").write_text("max 100000")
        return proc, cgroups

    def test_parent_limits_and_runner_reserve_bound_parallelism(self):
        with tempfile.TemporaryDirectory() as temporary, patch("os.sched_getaffinity", return_value=set(range(14))):
            proc, cgroups = self.fixture(Path(temporary), 8192 * MIB, 2048 * MIB, "400000 100000")
            limits = plan_resources(proc, cgroups)
            self.assertEqual(limits, Limits(3, 5120 * MIB, 300))

    def test_cpu_quota_and_low_memory_prevent_nproc_oversubscription(self):
        with tempfile.TemporaryDirectory() as temporary, patch("os.sched_getaffinity", return_value=set(range(14))):
            proc, cgroups = self.fixture(Path(temporary), 512 * MIB, 150 * MIB, "200000 100000")
            limits = plan_resources(proc, cgroups)
            self.assertEqual(limits, Limits(1, 234 * MIB, 100))
            command = scope_command(limits, ["make", "-j14", "world"])
            self.assertIn("-j1", command)
            self.assertNotIn("-j14", command)
            self.assertIn("--property=OOMPolicy=kill", command)
            self.assertIn(f"--property=MemoryMax={234 * MIB}", command)
            self.assertIn(f"--uid={os.getuid()}", command)
            serial = scope_command(Limits(3, 2048 * MIB, 300), ["make", "-j1"])
            self.assertIn("-j1", serial)

    def test_insufficient_headroom_fails_before_starting_build(self):
        with tempfile.TemporaryDirectory() as temporary, patch("os.sched_getaffinity", return_value={0, 1}):
            proc, cgroups = self.fixture(Path(temporary), 512 * MIB, 400 * MIB, "200000 100000")
            with self.assertRaisesRegex(RuntimeError, "runner headroom"):
                plan_resources(proc, cgroups)

    def test_scope_failure_is_reported_instead_of_swallowed(self):
        with patch("build_resources.plan_resources", return_value=Limits(1, 256 * MIB, 100)), \
                patch("build_resources.subprocess.run", return_value=subprocess.CompletedProcess([], -9)):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                run_bounded(["make", "-j14"])
            self.assertEqual(error.exception.returncode, -9)
