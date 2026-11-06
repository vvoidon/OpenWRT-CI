import sys
from pathlib import Path
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from retry_failed_runner import recover


class RunnerRecoveryTests(unittest.TestCase):
    def api(self, run=None, annotations=None, jobs=None):
        calls = []
        run = run or {"id": 42, "run_attempt": 1, "status": "completed", "conclusion": "failure"}
        jobs = jobs or [{"id": 7, "conclusion": "failure"}]
        if annotations is None:
            annotations = [{"annotation_level": "failure", "message":
                "The hosted runner lost communication with the server. Anything in your workflow that "
                "terminates the runner process, starves it for CPU/Memory, or blocks its network access can cause this error."}]

        def request(path, method="GET"):
            calls.append((path, method))
            if method == "POST":
                return None
            if path == "/actions/runs/42":
                return run
            if path.startswith("/actions/runs/42/jobs?"):
                return {"jobs": jobs}
            if path.startswith("/check-runs/7/annotations?"):
                return annotations
            raise AssertionError(path)
        return request, calls

    def test_real_runner_disconnect_annotation_retries_failed_jobs_once(self):
        request, calls = self.api()
        self.assertTrue(recover({"id": 42, "run_attempt": 1}, request))
        self.assertEqual(calls[-1], ("/actions/runs/42/rerun-failed-jobs", "POST"))

    def test_compiler_errors_and_mixed_failures_do_not_retry(self):
        error = {"annotation_level": "failure", "message": "Process completed with exit code 2."}
        for annotations in ([error], [], [error, {"annotation_level": "failure", "message":
                "The hosted runner lost communication with the server."}]):
            with self.subTest(annotations=annotations):
                request, calls = self.api(annotations=annotations)
                self.assertFalse(recover({"id": 42, "run_attempt": 1}, request))
                self.assertFalse(any(method == "POST" for _, method in calls))

    def test_already_retried_active_or_successful_runs_do_not_retry(self):
        for changes in ({"run_attempt": 2}, {"status": "in_progress", "conclusion": None},
                        {"conclusion": "success"}, {"conclusion": "cancelled"}):
            with self.subTest(changes=changes):
                request, calls = self.api(run={"id": 42, "run_attempt": 1, "status": "completed",
                                              "conclusion": "failure", **changes})
                self.assertFalse(recover({"id": 42, "run_attempt": 1}, request))
                self.assertFalse(any(method == "POST" for _, method in calls))
        request, calls = self.api()
        self.assertFalse(recover({"id": 42, "run_attempt": 2}, request))
        self.assertFalse(calls)

    def test_other_failed_or_timed_out_jobs_prevent_retry(self):
        for job in ({"id": 8, "conclusion": "failure"}, {"id": 8, "conclusion": "timed_out"}):
            request, calls = self.api(jobs=[job, {"id": 7, "conclusion": "failure"}])
            if job["conclusion"] == "failure":
                def mixed(path, method="GET"):
                    if path.startswith("/check-runs/8/annotations?"):
                        return [{"annotation_level": "failure", "message": "Build failed"}]
                    return request(path, method)
                query = mixed
            else:
                query = request
            self.assertFalse(recover({"id": 42, "run_attempt": 1}, query))
            self.assertFalse(any(method == "POST" for _, method in calls))
