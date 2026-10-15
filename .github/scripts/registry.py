import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

CHECKSUM = re.compile(r"[0-9a-fA-F]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_METADATA_BYTES = 4 * 1024 * 1024


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class Publication:
    record: dict
    binding: dict
    is_new: bool
    package_path: str


def valid_component(value, allow_plus=False):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255:
        return False
    if ".." in value or "@{" in value or value.endswith((".", " ")):
        return False
    stem = value.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"(?:COM|LPT)[1-9¹²³]", stem
    ):
        return False
    for char in value:
        if char.isascii():
            if not (char.isalnum() or char in "_-.@" or allow_plus and char == "+"):
                return False
        elif unicodedata.category(char) == "Cc" or char in "/\\?#%":
            return False
    return True


def parse_json(content):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RegistryError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    if len(content.encode("utf-8")) > MAX_METADATA_BYTES:
        raise RegistryError("registry metadata exceeds 4 MiB")

    def invalid_constant(value):
        raise RegistryError(f"invalid JSON constant: {value}")

    return json.loads(
        content, object_pairs_hook=unique_keys, parse_constant=invalid_constant
    )


def records(content):
    result = []
    versions = set()
    for line in content.split("\n"):
        if not line.strip():
            continue
        record = parse_json(line)
        if not isinstance(record, dict) or not isinstance(record.get("vers"), str):
            raise RegistryError("each registry record must be an object with a version")
        if record["vers"] in versions:
            raise RegistryError("duplicate pod version")
        versions.add(record["vers"])
        result.append(record)
    return result


def ownership(content):
    binding = parse_json(content)
    if not isinstance(binding, dict) or set(binding) != {"repository_id", "repository"}:
        raise RegistryError("ownership record requires repository_id and repository")
    if type(binding["repository_id"]) is not int or binding["repository_id"] <= 0:
        raise RegistryError("repository_id must be a positive integer")
    if not isinstance(binding["repository"], str) or not REPOSITORY.fullmatch(
        binding["repository"]
    ):
        raise RegistryError("repository must be a GitHub owner/repository name")
    return binding


def release_location(url):
    if not isinstance(url, str):
        raise RegistryError("release asset URL must be a string")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise RegistryError("assets must use GitHub HTTPS release download URLs")
    parts = parsed.path.split("/")
    if len(parts) != 7 or parts[3:5] != ["releases", "download"]:
        raise RegistryError("invalid GitHub release asset URL")
    repository = "/".join(parts[1:3])
    if not REPOSITORY.fullmatch(repository) or not parts[5] or not parts[6]:
        raise RegistryError("invalid GitHub release asset path")
    return repository, unquote(parts[5]), unquote(parts[6])


def validate_record(record, name):
    if record.get("name") != name:
        raise RegistryError("record name must match pod filename")
    if not valid_component(record.get("vers"), allow_plus=True):
        raise RegistryError("invalid pod version")
    if type(record.get("yanked", False)) is not bool or record.get("yanked", False):
        raise RegistryError("new versions must not be yanked")
    if record.get("olive_req") is not None and not isinstance(record["olive_req"], str):
        raise RegistryError("olive_req must be a string or null")
    if not isinstance(record.get("deps"), list):
        raise RegistryError("deps must be an array")
    dependencies = set()
    for dependency in record["deps"]:
        if not isinstance(dependency, dict):
            raise RegistryError("each dependency must be an object")
        dep_name = dependency.get("name")
        requirement = dependency.get("req")
        if not valid_component(dep_name):
            raise RegistryError("invalid dependency name")
        if (
            dep_name in dependencies
            or not isinstance(requirement, str)
            or not requirement.strip()
        ):
            raise RegistryError("duplicate dependency or missing version requirement")
        dependencies.add(dep_name)
    for asset in assets(record):
        if not isinstance(asset.get("cksum"), str) or not CHECKSUM.fullmatch(
            asset["cksum"]
        ):
            raise RegistryError("asset checksum must contain 64 hexadecimal characters")
        release_location(asset.get("url"))


def assets(record):
    yield {"url": record.get("dl"), "cksum": record.get("cksum"), "archive": True}
    native = record.get("native")
    if native is None:
        return
    if not isinstance(native, dict) or not valid_component(native.get("lib")):
        raise RegistryError("native metadata requires a valid library name")
    artifacts = native.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RegistryError("native artifacts must be an object")
    for target, artifact in artifacts.items():
        if not isinstance(target, str) or not isinstance(artifact, dict):
            raise RegistryError("invalid native artifact")
        for item in [artifact] + (
            [artifact["implib"]] if artifact.get("implib") is not None else []
        ):
            if not isinstance(item, dict) or not valid_component(item.get("file")):
                raise RegistryError("native artifact requires a valid filename")
            yield {"url": item.get("url"), "cksum": item.get("cksum"), "archive": False}


def is_pod_path(path):
    prefix, separator, name = path.partition("/")
    return bool(separator and valid_component(name) and prefix == name[:2])


def publication(changed, read_base, read_head):
    package_paths = [path for path in changed if not path.startswith("owners/")]
    if len(package_paths) != 1:
        raise RegistryError("publishing PR must change exactly one pod file")
    path = package_paths[0]
    name = path.partition("/")[2]
    if not is_pod_path(path):
        raise RegistryError(
            "pod file must use its name's first two characters as directory"
        )
    previous = read_base(path)
    current = read_head(path)
    if current is None:
        raise RegistryError("publishing PR must not delete pod files")
    if previous is not None and not current.startswith(previous):
        raise RegistryError("publishing PR must preserve existing registry bytes")
    base_records = records(previous or "")
    head_records = records(current)
    if (
        len(head_records) != len(base_records) + 1
        or head_records[: len(base_records)] != base_records
    ):
        raise RegistryError(
            "publishing PR must append one version without changing existing records"
        )
    new_record = head_records[-1]
    validate_record(new_record, name)
    owner_path = f"owners/{name}.json"
    is_new = previous is None
    expected = {path, owner_path} if is_new else {path}
    if set(changed) != expected:
        raise RegistryError(
            "publishing PR contains unrelated changes or missing ownership registration"
        )
    binding_text = read_head(owner_path) if is_new else read_base(owner_path)
    if binding_text is None:
        raise RegistryError("pod requires a reviewed repository ownership record")
    if is_new and read_base(owner_path) is not None:
        raise RegistryError("pod ownership is already registered")
    return Publication(new_record, ownership(binding_text), is_new, path)
