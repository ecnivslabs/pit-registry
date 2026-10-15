import json
import os
import re
import subprocess
import urllib.request
from urllib.parse import quote, urlsplit

from registry import REPOSITORY, RegistryError, assets, release_location


class GitHub:
    def __init__(self):
        self.cache = {}

    def api(self, path, method="GET", data=None, ownership=False):
        token = os.environ.get("PIT_OWNERSHIP_TOKEN") if ownership else None
        environment = dict(os.environ)
        if token:
            environment["GH_TOKEN"] = token
        command = ["gh", "api", "--method", method, path]
        if data is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(
            command,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
            check=False,
        )
        if result.returncode:
            status = re.search(r"HTTP [0-9]{3}", result.stderr)
            detail = status.group() if status else f"exit status {result.returncode}"
            raise RuntimeError(f"GitHub API failed ({detail}): {method} {path}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def get(self, path):
        if path not in self.cache:
            self.cache[path] = self.api(path)
        return self.cache[path]

    def repository(self, path, fresh=False):
        repository = self.api(path) if fresh else self.get(path)
        if not isinstance(repository, dict) or type(repository.get("id")) is not int:
            raise RegistryError("GitHub returned invalid repository metadata")
        if not isinstance(repository.get("full_name"), str) or not REPOSITORY.fullmatch(
            repository["full_name"]
        ):
            raise RegistryError("GitHub returned invalid repository name")
        return repository

    def authorize(self, repository, author):
        if (
            repository.get("owner", {}).get("type") == "User"
            and repository["owner"].get("id") == author["id"]
        ):
            return
        path = f"repos/{repository['full_name']}/collaborators/{quote(author['login'], safe='')}/permission"
        try:
            permission = self.api(path, ownership=True)
        except RuntimeError as error:
            raise RuntimeError(
                "Cannot verify source repository permissions. Configure PIT_OWNERSHIP_TOKEN "
                f"with access to source repository metadata. Automatic publishing is blocked. {error}"
            ) from error
        if not isinstance(permission, dict) or permission.get("permission") not in {
            "write",
            "admin",
        }:
            raise RegistryError(
                "PR author must have write, maintain, or admin access to source repository"
            )
        if permission.get("user", {}).get("id") != author["id"]:
            raise RegistryError(
                "GitHub permission response does not match PR author's account ID"
            )

    def release_asset(self, url, repository_id):
        source, tag, filename = release_location(url)
        repository = self.repository(f"repos/{source}")
        if repository["id"] != repository_id:
            raise RegistryError("release asset belongs to a different repository ID")
        canonical = repository["full_name"]
        release = self.get(f"repos/{canonical}/releases/tags/{quote(tag, safe='')}")
        if (
            not isinstance(release, dict)
            or type(release.get("id")) is not int
            or release.get("draft")
        ):
            raise RegistryError("release must be published in registered repository")
        for page in range(1, 101):
            entries = self.get(
                f"repos/{canonical}/releases/{release['id']}/assets?per_page=100&page={page}"
            )
            if not isinstance(entries, list):
                raise RegistryError("GitHub returned invalid release assets")
            for asset in entries:
                if asset.get("name") == filename and asset.get("state") == "uploaded":
                    asset_source, asset_tag, asset_name = release_location(
                        asset.get("browser_download_url")
                    )
                    if (asset_source.lower(), asset_tag, asset_name) != (
                        canonical.lower(),
                        tag,
                        filename,
                    ):
                        raise RegistryError(
                            "release asset URL does not match registered repository"
                        )
                    return asset
            if len(entries) < 100:
                break
        raise RegistryError("download URL does not identify an uploaded release asset")


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise RegistryError("release asset redirected outside HTTPS")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def verify_download(asset, checksum, archive):
    import blake3

    maximum = 10 * 1024 * 1024 if archive else 512 * 1024 * 1024
    size = asset.get("size")
    if type(size) is not int or size < 0 or size > maximum:
        raise RegistryError("release asset exceeds allowed size")
    digest = blake3.blake3()
    downloaded = 0
    request = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": "pit-registry"}
    )
    with urllib.request.build_opener(HTTPSRedirects).open(
        request, timeout=60
    ) as response:
        while chunk := response.read(65536):
            downloaded += len(chunk)
            if downloaded > maximum or downloaded > size:
                raise RegistryError("release asset exceeds declared size")
            digest.update(chunk)
    if downloaded != size or digest.hexdigest().lower() != checksum.lower():
        raise RegistryError("release asset size or BLAKE3 checksum mismatch")


def verify_publication(github, publication, author, download=verify_download):
    repository_id = publication.binding["repository_id"]
    repository = github.repository(f"repositories/{repository_id}")
    if repository["id"] != repository_id:
        raise RegistryError("GitHub returned a different repository ID")
    if (
        publication.is_new
        and publication.binding["repository"].lower() != repository["full_name"].lower()
    ):
        raise RegistryError("new ownership record must use canonical repository name")
    github.authorize(repository, author)
    checked = set()
    for artifact in assets(publication.record):
        if (artifact["url"], artifact["cksum"]) in checked:
            continue
        asset = github.release_asset(artifact["url"], repository_id)
        download(asset, artifact["cksum"], artifact["archive"])
        checked.add((artifact["url"], artifact["cksum"]))
    repository = github.repository(f"repositories/{repository_id}", fresh=True)
    if repository["id"] != repository_id:
        raise RegistryError("repository identity changed during asset verification")
    github.authorize(repository, author)
    return repository
