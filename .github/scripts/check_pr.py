import json
import os
import subprocess
import sys
from pathlib import Path

from github_api import GitHub, verify_publication
from registry import (
    MAX_METADATA_BYTES,
    REPOSITORY,
    SHA,
    RegistryError,
    is_pod_path,
    publication,
)


def git(*args):
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout


def read_blob(reference, path):
    entry = git("ls-tree", "-z", reference, "--", path)
    if not entry:
        return None
    metadata, actual_path = entry.rstrip(b"\0").split(b"\t", 1)
    mode, kind, object_id = metadata.decode("ascii").split()
    if mode != "100644" or kind != "blob" or actual_path.decode("utf-8") != path:
        raise RegistryError("registry metadata must be regular non-executable files")
    if int(git("cat-file", "-s", object_id)) > MAX_METADATA_BYTES:
        raise RegistryError("registry metadata exceeds 4 MiB")
    return git("cat-file", "blob", object_id).decode("utf-8")


def validate_identity(pr, repository, number, expected_head):
    if pr.get("number") != number or pr.get("state") != "open" or pr.get("draft"):
        raise RegistryError("PR must be open and ready for review")
    if pr.get("base", {}).get("repo", {}).get("full_name") != repository:
        raise RegistryError("PR targets a different registry")
    if pr["base"].get("ref") not in {"master", "main"}:
        raise RegistryError("PR targets an unsupported branch")
    for reference in [pr["base"].get("sha", ""), pr.get("head", {}).get("sha", "")]:
        if not isinstance(reference, str) or not SHA.fullmatch(reference):
            raise RegistryError("PR has invalid commit IDs")
    if pr["head"]["sha"] != expected_head:
        raise RegistryError(
            "PR head changed; latest event must validate the new commit"
        )
    author = pr.get("user", {})
    if type(author.get("id")) is not int or not isinstance(author.get("login"), str):
        raise RegistryError("PR has invalid author identity")


def process(github, event, repository):
    number = event["number"]
    if type(number) is not int or number <= 0 or not REPOSITORY.fullmatch(repository):
        raise RegistryError("invalid registry or PR number")
    path = f"repos/{repository}/pulls/{number}"
    expected_head = event["pull_request"]["head"]["sha"]
    pr = github.api(path)
    validate_identity(pr, repository, number, expected_head)
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    # Fetching objects as data avoids executing code or loading configuration from PRs.
    for reference in [base, head]:
        git("fetch", "--no-tags", "origin", reference)
    merge_base = git("merge-base", base, head).decode("ascii").strip()
    changed = (
        git("diff", "--name-only", "-z", merge_base, head)
        .decode("utf-8")
        .rstrip("\0")
        .split("\0")
    )
    if not any(is_pod_path(item) for item in changed):
        print(
            "No pod publication detected; maintainer review required for registry administration."
        )
        return
    candidate = publication(
        changed, lambda name: read_blob(base, name), lambda name: read_blob(head, name)
    )
    source = verify_publication(github, candidate, pr["user"])
    current = github.api(path)
    validate_identity(current, repository, number, expected_head)
    if current["base"]["sha"] != base:
        raise RegistryError(
            "registry base changed during validation; synchronize and retry publishing PR"
        )
    if candidate.is_new:
        github.api(
            f"repos/{repository}/issues/{number}/labels",
            method="POST",
            data={"labels": ["manual-review"]},
        )
        print(
            f"New pod {candidate.record['name']} verified against {source['full_name']}; maintainer review required."
        )
        return
    result = github.api(
        f"{path}/merge",
        method="PUT",
        data={"sha": head, "merge_method": "squash"},
    )
    if not isinstance(result, dict) or result.get("merged") is not True:
        raise RegistryError("GitHub refused to merge validated publishing PR")
    print(
        f"Merged {candidate.record['name']}@{candidate.record['vers']} from {source['full_name']}."
    )
    try:
        github.api(
            f"repos/{repository}/actions/workflows/index.yml/dispatches",
            method="POST",
            data={"ref": pr["base"]["ref"]},
        )
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Publishing PR was merged, but registry website index refresh failed: {error}"
        ) from error


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
        raise RegistryError(
            "publishing automation requires a trusted pull_request_target event"
        )
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    process(GitHub(), event, os.environ["GITHUB_REPOSITORY"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        sys.exit(1)
