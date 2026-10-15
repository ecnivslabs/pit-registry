import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

from registry import (
    ownership,
    parse_json,
    publication,
    release_location,
    valid_component,
)


def record(version="1.0.0"):
    return {
        "name": "tokenizer",
        "vers": version,
        "deps": [],
        "cksum": "a" * 64,
        "dl": f"https://github.com/ecnivslabs/tokenizer/releases/download/tokenizer-{version}/tokenizer.pit.zst",
        "yanked": False,
    }


def binding():
    return {"repository_id": 42, "repository": "ecnivslabs/tokenizer"}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.base = {
            "to/tokenizer": json.dumps(record()),
            "owners/tokenizer.json": json.dumps(binding()),
        }
        self.head = copy.deepcopy(self.base)
        self.head["to/tokenizer"] += "\n" + json.dumps(record("1.1.0"))

    def validate(self, changed=None):
        return publication(changed or ["to/tokenizer"], self.base.get, self.head.get)

    def test_existing_pod_uses_trusted_base_binding(self):
        self.head["owners/tokenizer.json"] = json.dumps(
            {"repository_id": 666, "repository": "attacker/repo"}
        )
        candidate = self.validate()
        self.assertEqual(candidate.binding, binding())
        self.assertFalse(candidate.is_new)

    def test_existing_pod_cannot_change_ownership(self):
        with self.assertRaisesRegex(ValueError, "unrelated changes"):
            self.validate(["to/tokenizer", "owners/tokenizer.json"])

    def test_workflow_and_readme_changes_cannot_be_auto_merged(self):
        for extra in [
            ".github/workflows/validate.yml",
            "README.md",
            ".gitignore",
            "LICENSE",
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.validate(["to/tokenizer", extra])

    def test_new_registration_requires_ownership_record(self):
        self.base.clear()
        self.head["to/tokenizer"] = json.dumps(record())
        with self.assertRaises(ValueError):
            self.validate()
        candidate = self.validate(["to/tokenizer", "owners/tokenizer.json"])
        self.assertTrue(candidate.is_new)

    def test_missing_migration_does_not_fall_back_to_email_or_username(self):
        del self.base["owners/tokenizer.json"]
        with self.assertRaisesRegex(ValueError, "reviewed repository"):
            self.validate()

    def test_reserved_binding_cannot_be_replaced_by_new_registration(self):
        del self.base["to/tokenizer"]
        self.head["to/tokenizer"] = json.dumps(record())
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.validate(["to/tokenizer", "owners/tokenizer.json"])

    def test_versions_cannot_be_modified_deleted_reordered_or_duplicated(self):
        original = record()
        altered = dict(original, cksum="b" * 64)
        variants = [
            [altered, record("1.1.0")],
            [dict(original, yanked=0), record("1.1.0")],
            [record("1.1.0")],
            [record("1.1.0"), original],
            [original, original],
            [original, record("1.1.0"), record("1.2.0")],
        ]
        for records in variants:
            self.head["to/tokenizer"] = "\n".join(map(json.dumps, records))
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.validate()

    def test_deletion_and_invalid_paths_fail(self):
        del self.head["to/tokenizer"]
        with self.assertRaises(ValueError):
            self.validate()
        for path in [
            "to/../tokenizer",
            "wrong/tokenizer",
            "to/tokenizer/more",
            "to/CON",
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([path])

    def test_bad_fields_do_not_create_unreadable_client_records(self):
        for field, values in {
            "olive_req": [True, {}, 1],
            "deps": [None, {}],
            "cksum": [None, "abc", "g" * 64],
            "vers": [False, "../bad"],
            "dl": [None, "http://github.com/a/b/releases/download/v/file"],
            "yanked": [True, "false"],
            "name": ["another"],
        }.items():
            for value in values:
                malformed = dict(record("1.1.0"), **{field: value})
                self.head["to/tokenizer"] = (
                    json.dumps(record()) + "\n" + json.dumps(malformed)
                )
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.validate()

    def test_native_import_library_checksums_and_urls_are_validated(self):
        native = {
            "lib": "tokenizer",
            "artifacts": {
                "windows-x86_64": {
                    "file": "tokenizer.dll",
                    "url": record()["dl"],
                    "cksum": "b" * 64,
                    "implib": {
                        "file": "tokenizer.lib",
                        "url": "https://evil.test/lib",
                        "cksum": "c" * 64,
                    },
                }
            },
        }
        new = dict(record("1.1.0"), native=native)
        self.head["to/tokenizer"] = json.dumps(record()) + "\n" + json.dumps(new)
        with self.assertRaises(ValueError):
            self.validate()

    def test_single_character_and_unicode_pod_names_match_client(self):
        for name in ["a", "日本語", "my.pod", ".my-pod", "é\u2028x"]:
            path = f"{name[:2]}/{name}"
            head = {
                path: json.dumps(dict(record(), name=name), ensure_ascii=False),
                f"owners/{name}.json": json.dumps(binding()),
            }
            self.assertTrue(publication(list(head), {}.get, head.get).is_new)


class MetadataTests(unittest.TestCase):
    def test_duplicate_keys_and_non_json_constants_fail(self):
        for content in [
            '{"repository_id":1,"repository_id":2}',
            '{"value":NaN}',
            '{"value":Infinity}',
        ]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_json(content)

    def test_binding_requires_stable_positive_integer_id(self):
        for value in [True, False, 0, -1, "42", 4.2]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ownership(json.dumps(dict(binding(), repository_id=value)))

    def test_windows_reserved_names_match_client(self):
        for name in ["CON", "aux.txt", "LPT9", "COM¹", "LPT²", "COM³"]:
            self.assertFalse(valid_component(name), name)

    def test_release_urls_cannot_inject_hosts_queries_or_paths(self):
        valid = record()["dl"]
        for url in [
            valid.replace("https:", "http:"),
            valid.replace("github.com", "github.com.evil.test"),
            valid.replace("github.com", "user@github.com"),
            valid + "?x=1",
            valid + "#fragment",
            valid.replace("github.com", "github.com:443"),
            valid + "/extra",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                release_location(url)
        self.assertEqual(
            release_location(valid),
            ("ecnivslabs/tokenizer", "tokenizer-1.0.0", "tokenizer.pit.zst"),
        )

    def test_migrated_pods_keep_all_published_versions_untouched(self):
        root = Path(__file__).resolve().parents[1]
        expected = {"term": 1338177599, "tokenizer": 1363689157}
        for name, repository_id in expected.items():
            metadata = ownership((root / "owners" / f"{name}.json").read_text())
            self.assertEqual(metadata["repository_id"], repository_id)
            index = root / name[:2] / name
            import subprocess

            historical = subprocess.run(
                ["git", "show", f"HEAD:{name[:2]}/{name}"],
                cwd=root,
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(index.read_bytes(), historical)


if __name__ == "__main__":
    unittest.main()
