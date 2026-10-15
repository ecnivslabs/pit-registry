import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

from check_pr import process, read_blob


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 12,
            "state": "open",
            "draft": False,
            "user": {"id": 7, "login": "publisher"},
            "base": {
                "sha": "a" * 40,
                "ref": "master",
                "repo": {"full_name": "owner/registry"},
            },
            "head": {"sha": "b" * 40},
        }
        self.event = {"number": 12, "pull_request": copy.deepcopy(self.pr)}
        old = {
            "name": "tokenizer",
            "vers": "1.0.0",
            "deps": [],
            "cksum": "a" * 64,
            "dl": "https://github.com/owner/project/releases/download/v1/file",
        }
        self.base = {
            "to/tokenizer": json.dumps(old),
            "owners/tokenizer.json": '{"repository_id":42,"repository":"owner/project"}',
        }
        self.head = dict(
            self.base,
            **{
                "to/tokenizer": json.dumps(old)
                + "\n"
                + json.dumps(dict(old, vers="1.1.0"))
            },
        )
        self.mutations = []
        self.reads = 0
        self.changed = b"to/tokenizer\0"

    def api(self, path, method="GET", data=None):
        if method == "GET":
            self.reads += 1
            return copy.deepcopy(self.pr)
        self.mutations.append((path, method, data))
        return {"merged": True}

    def git(self, *args):
        if args[0] == "merge-base":
            return ("a" * 40).encode()
        if args[0] == "diff":
            return self.changed
        return b""

    def run_process(self):
        with (
            patch("check_pr.git", side_effect=self.git),
            patch(
                "check_pr.read_blob",
                side_effect=lambda ref, path: (
                    self.base if ref == "a" * 40 else self.head
                ).get(path),
            ),
            patch(
                "check_pr.verify_publication",
                return_value={"full_name": "owner/project"},
            ),
            patch("builtins.print"),
        ):
            process(self, self.event, "owner/registry")

    def test_existing_pod_merge_is_pinned_to_validated_sha(self):
        self.run_process()
        self.assertEqual(
            self.mutations,
            [
                (
                    "repos/owner/registry/pulls/12/merge",
                    "PUT",
                    {"sha": "b" * 40, "merge_method": "squash"},
                ),
                (
                    "repos/owner/registry/actions/workflows/index.yml/dispatches",
                    "POST",
                    {"ref": "master"},
                ),
            ],
        )

    def test_index_dispatch_failure_reports_that_pr_was_already_merged(self):
        original_api = self.api

        def api(path, method="GET", data=None):
            if path.endswith("/dispatches"):
                raise RuntimeError("dispatch unavailable")
            return original_api(path, method, data)

        with (
            patch.object(self, "api", side_effect=api),
            self.assertRaisesRegex(RuntimeError, "was merged.*index refresh failed"),
        ):
            self.run_process()
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][1], "PUT")

    def test_new_pod_is_only_labeled_never_auto_merged(self):
        self.base.clear()
        self.head["to/tokenizer"] = json.dumps(
            json.loads(self.head["to/tokenizer"].splitlines()[-1])
        )
        self.changed = b"to/tokenizer\0owners/tokenizer.json\0"
        self.run_process()
        self.assertEqual([method for _, method, _ in self.mutations], ["POST"])
        self.assertTrue(self.mutations[0][0].endswith("/labels"))

    def test_changed_head_closed_draft_or_wrong_registry_cannot_merge(self):
        for mutate in [
            lambda pr: pr["head"].update(sha="c" * 40),
            lambda pr: pr.update(state="closed"),
            lambda pr: pr.update(draft=True),
            lambda pr: pr["base"]["repo"].update(full_name="attacker/registry"),
        ]:
            saved = copy.deepcopy(self.pr)
            mutate(self.pr)
            with self.assertRaises(ValueError):
                self.run_process()
            self.pr = saved
        self.assertEqual(self.mutations, [])

    def test_head_or_base_moving_after_verification_cannot_merge(self):
        original_api = self.api

        def api(path, method="GET", data=None):
            result = original_api(path, method, data)
            if method == "GET" and self.reads == 2:
                result["base"]["sha"] = "c" * 40
            return result

        with (
            patch.object(self, "api", side_effect=api),
            self.assertRaisesRegex(ValueError, "base changed"),
        ):
            self.run_process()
        self.assertEqual(self.mutations, [])

    def test_unrelated_workflow_changes_cannot_forge_merge_result(self):
        self.changed += b".github/workflows/validate.yml\0"
        with self.assertRaises(ValueError):
            self.run_process()
        self.assertEqual(self.mutations, [])

    def test_administration_pr_has_no_automatic_write(self):
        for changed in [
            b"owners/tokenizer.json\0.github/scripts/check_pr.py\0",
            b"README.md\0",
        ]:
            self.changed = changed
            self.run_process()
        self.assertEqual(self.mutations, [])

    def test_symlink_executable_and_oversized_blobs_are_rejected(self):
        for mode in [b"120000", b"100755"]:
            with (
                patch(
                    "check_pr.git",
                    return_value=mode + b" blob " + b"a" * 40 + b"\tto/tokenizer\0",
                ),
                self.assertRaises(ValueError),
            ):
                read_blob("a" * 40, "to/tokenizer")
        with (
            patch(
                "check_pr.git",
                side_effect=[
                    b"100644 blob " + b"a" * 40 + b"\tto/tokenizer\0",
                    b"999999999",
                ],
            ),
            self.assertRaises(ValueError),
        ):
            read_blob("a" * 40, "to/tokenizer")


if __name__ == "__main__":
    unittest.main()
