import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

from github_api import GitHub, HTTPSRedirects, verify_download, verify_publication
from registry import Publication

URL = "https://github.com/ecnivslabs/tokenizer/releases/download/v1/tokenizer.pit.zst"
AUTHOR = {"id": 7, "login": "publisher"}


class FakeGitHub(GitHub):
    def __init__(self):
        super().__init__()
        self.responses = {
            "repositories/42": {
                "id": 42,
                "full_name": "ecnivslabs/tokenizer",
                "owner": {"type": "Organization", "id": 99},
            },
            "repos/ecnivslabs/tokenizer": {
                "id": 42,
                "full_name": "ecnivslabs/tokenizer",
            },
            "repos/ecnivslabs/tokenizer/collaborators/publisher/permission": {
                "permission": "write",
                "user": AUTHOR,
            },
            "repos/ecnivslabs/tokenizer/releases/tags/v1": {"id": 10, "draft": False},
            "repos/ecnivslabs/tokenizer/releases/10/assets?per_page=100&page=1": [
                {
                    "name": "tokenizer.pit.zst",
                    "state": "uploaded",
                    "size": 3,
                    "browser_download_url": URL,
                },
            ],
        }

    def api(self, path, method="GET", data=None, ownership=False):
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


class GitHubOwnershipTests(unittest.TestCase):
    def candidate(self, **kwargs):
        return Publication(
            {"dl": URL, "cksum": "a" * 64, **kwargs},
            {"repository_id": 42, "repository": "previous-owner/tokenizer"},
            False,
            "to/tokenizer",
        )

    def test_transfer_uses_stable_id_and_current_canonical_permissions(self):
        downloads = []
        source = verify_publication(
            FakeGitHub(), self.candidate(), AUTHOR, lambda *args: downloads.append(args)
        )
        self.assertEqual(source["full_name"], "ecnivslabs/tokenizer")
        self.assertEqual(len(downloads), 1)

    def test_personal_repository_owner_is_checked_by_account_id(self):
        repository = {"owner": {"type": "User", "id": AUTHOR["id"]}}
        FakeGitHub().authorize(repository, AUTHOR)

    def test_read_only_and_unverified_publishers_are_denied(self):
        path = "repos/ecnivslabs/tokenizer/collaborators/publisher/permission"
        for permission in ["read", "none", "triage", None]:
            github = FakeGitHub()
            github.responses[path] = {"permission": permission, "user": AUTHOR}
            with self.subTest(permission=permission), self.assertRaises(ValueError):
                verify_publication(github, self.candidate(), AUTHOR, lambda *args: None)

    def test_api_permission_failure_never_falls_back_to_old_publisher(self):
        github = FakeGitHub()
        github.responses[
            "repos/ecnivslabs/tokenizer/collaborators/publisher/permission"
        ] = RuntimeError("forbidden")
        with self.assertRaisesRegex(RuntimeError, "PIT_OWNERSHIP_TOKEN"):
            verify_publication(github, self.candidate(), AUTHOR, lambda *args: None)

    def test_permission_response_requires_matching_numeric_account_id(self):
        github = FakeGitHub()
        github.responses[
            "repos/ecnivslabs/tokenizer/collaborators/publisher/permission"
        ]["user"] = {"id": 8}
        with self.assertRaisesRegex(ValueError, "account ID"):
            verify_publication(github, self.candidate(), AUTHOR, lambda *args: None)

    def test_recreated_repository_name_cannot_take_over_pod(self):
        github = FakeGitHub()
        github.responses["repos/ecnivslabs/tokenizer"]["id"] = 666
        with self.assertRaisesRegex(ValueError, "different repository ID"):
            verify_publication(github, self.candidate(), AUTHOR, lambda *args: None)

    def test_release_must_be_published_uploaded_and_have_matching_asset_url(self):
        for field, value in [
            ("draft", True),
            ("state", "new"),
            ("browser_download_url", URL.replace("ecnivslabs", "attacker")),
        ]:
            github = FakeGitHub()
            target = (
                github.responses["repos/ecnivslabs/tokenizer/releases/tags/v1"]
                if field == "draft"
                else github.responses[
                    "repos/ecnivslabs/tokenizer/releases/10/assets?per_page=100&page=1"
                ][0]
            )
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_publication(github, self.candidate(), AUTHOR, lambda *args: None)

    def test_native_artifact_cannot_come_from_other_repository(self):
        github = FakeGitHub()
        github.responses["repos/attacker/project"] = {
            "id": 666,
            "full_name": "attacker/project",
        }
        native = {
            "lib": "tokenizer",
            "artifacts": {
                "linux-x86_64": {
                    "url": URL.replace("ecnivslabs/tokenizer", "attacker/project"),
                    "cksum": "b" * 64,
                    "file": "libtokenizer.so",
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "different repository ID"):
            verify_publication(
                github, self.candidate(native=native), AUTHOR, lambda *args: None
            )

    def test_new_binding_requires_canonical_name(self):
        candidate = self.candidate()
        candidate = Publication(
            candidate.record, candidate.binding, True, candidate.package_path
        )
        with self.assertRaisesRegex(ValueError, "canonical repository name"):
            verify_publication(FakeGitHub(), candidate, AUTHOR, lambda *args: None)

    def test_permission_revoked_during_download_blocks_publication(self):
        github = FakeGitHub()

        def revoke(*args):
            github.responses[
                "repos/ecnivslabs/tokenizer/collaborators/publisher/permission"
            ]["permission"] = "read"

        with self.assertRaisesRegex(ValueError, "must have write"):
            verify_publication(github, self.candidate(), AUTHOR, revoke)


class DownloadTests(unittest.TestCase):
    def test_known_blake3_vector_and_size_are_verified(self):
        # BLAKE3's standard empty-input test vector.
        checksum = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
        asset = {"size": 0, "browser_download_url": URL}
        for expected, size in [(checksum, 0), ("0" * 64, 0), (checksum, 1)]:
            with patch("github_api.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(b"")
                if expected == checksum and size == 0:
                    verify_download(dict(asset, size=size), expected, True)
                else:
                    with self.assertRaises(ValueError):
                        verify_download(dict(asset, size=size), expected, True)

    def test_oversized_and_truncated_downloads_fail(self):
        asset = {"size": 1, "browser_download_url": URL}
        with patch("github_api.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"too large")
            with self.assertRaisesRegex(ValueError, "declared size"):
                verify_download(asset, "a" * 64, True)
        with self.assertRaisesRegex(ValueError, "allowed size"):
            verify_download(dict(asset, size=10 * 1024 * 1024 + 1), "a" * 64, True)

    def test_redirect_cannot_downgrade_https(self):
        handler = HTTPSRedirects()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            handler.redirect_request(
                Request(URL), None, 302, "Found", {}, "http://evil.test/archive"
            )


if __name__ == "__main__":
    unittest.main()
