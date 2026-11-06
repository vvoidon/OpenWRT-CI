#!/usr/bin/env python3
"""Retry a first-attempt workflow only for GitHub's explicit runner disconnect error."""
import json
import os
from pathlib import Path
import urllib.request

RUNNER_DISCONNECTED = "The hosted runner lost communication with the server."


def pages(request, path, key=None):
    result = []
    page = 1
    while True:
        data = request(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items = data[key] if key else data
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def recover(event_run, request):
    if event_run["run_attempt"] != 1:
        return False
    path = f'/actions/runs/{int(event_run["id"])}'
    current = request(path)
    # A manual rerun may already have started since this completion event.
    if (current["run_attempt"] != 1 or current["status"] != "completed"
            or current["conclusion"] != "failure"):
        return False
    jobs = pages(request, path + "/jobs?filter=latest", "jobs")
    if any(job["conclusion"] not in {"success", "skipped", "failure"} for job in jobs):
        return False
    failed = [job for job in jobs if job["conclusion"] == "failure"]
    if not failed:
        return False
    for job in failed:
        annotations = pages(request, f'/check-runs/{int(job["id"])}/annotations')
        errors = [item for item in annotations if item["annotation_level"] == "failure"]
        if not errors or any(not item["message"].startswith(RUNNER_DISCONNECTED) for item in errors):
            return False
    request(path + "/rerun-failed-jobs", method="POST")
    print(f'Requested one recovery attempt for workflow run {current["id"]}')
    return True


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    base = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"]
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def request(path, method="GET"):
        query = urllib.request.Request(base + path, headers=headers, method=method,
                                       data=b"{}" if method == "POST" else None)
        with urllib.request.urlopen(query, timeout=30) as response:
            data = response.read()
        return json.loads(data) if data else None

    if not recover(event["workflow_run"], request):
        print("No runner-disconnect recovery required")


if __name__ == "__main__":
    main()
