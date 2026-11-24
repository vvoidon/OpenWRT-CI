#!/usr/bin/env python3
"""Wait for Pages to serve the same signed indices that passed build validation."""
import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def fetch(url):
    # Probe the new deployment, not a previous build cached under a fixed URL.
    url += ("&" if "?" in url else "?") + "verify=" + str(time.time_ns())
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def verify(expected, base):
    for attempt in range(6):
        try:
            actual = json.loads(fetch(base.rstrip("/") + "/repositories.json"))
            if actual != expected:
                raise ValueError("Pages still serves another build")
            if coverage_hash := expected.get("source_coverage_sha256"):
                state = fetch(base.rstrip("/") + "/source-state.txt")
                coverage = fetch(base.rstrip("/") + "/source-coverage.json")
                if (hashlib.sha256(state).hexdigest() != expected["fingerprint"] or
                        hashlib.sha256(coverage).hexdigest() != coverage_hash):
                    raise ValueError("Published source state or coverage mismatch")
            public = fetch(base.rstrip("/") + "/public-key.pem")
            if hashlib.sha256(public).hexdigest() != expected["public_key_sha256"]:
                raise ValueError("Published signing key mismatch")
            if policy_hash := expected.get("repository_policy_sha256"):
                policy = fetch(base.rstrip("/") + "/repository-policy.json")
                if hashlib.sha256(policy).hexdigest() != policy_hash:
                    raise ValueError("Published repository policy mismatch")
            for repository in expected["repositories"]:
                data = fetch(repository["url"])
                if hashlib.sha256(data).hexdigest() != repository["sha256"]:
                    raise ValueError("Published index mismatch: " + repository["url"])
            print("Published repositories match validated build " + expected["fingerprint"])
            return
        except (urllib.error.URLError, OSError, ValueError) as error:
            if attempt == 5:
                raise
            print(f"Waiting for Pages ({attempt + 1}/6): {error}")
            time.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("base_url")
    args = parser.parse_args()
    verify(json.loads(args.metadata.read_text()), args.base_url)
