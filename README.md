# pit-registry

Package index for the [Olive programming language](https://github.com/ecnivslabs/olive).

## Format

Each package has a file at `{2-char-prefix}/{name}` (e.g., `ht/http`).
The file contains one JSON object per line, with one line per published version:

```json
{"name":"http","vers":"1.0.0","deps":[],"cksum":"blake3hex...","dl":"https://github.com/user/repo/releases/download/http-1.0.0/http.pit.zst","yanked":false}
```

## Fields

| Field | Description |
|-------|-------------|
| `name` | Package name |
| `vers` | Exact version string |
| `deps` | Array of `{"name":"..","req":".."}` |
| `cksum` | BLAKE3 hex digest of the `.pit.zst` archive |
| `dl` | Direct download URL of the `.pit.zst` asset |
| `yanked` | Set to `true` to warn users (version not deleted) |
| `olive_req` | Optional. Minimum Olive compiler version this version requires |

## Publishing

```sh
pit publish
```

Requires `GITHUB_TOKEN` with `repo` scope. Opens a PR automatically.

## Pod ownership

Each pod has a protected binding at `owners/{name}.json`:

```json
{"repository_id":1363689157,"repository":"ecnivslabs/tokenizer"}
```

The numeric GitHub repository ID determines ownership. The repository name is
descriptive. Renaming or transferring the repository preserves the binding.
Recreating a repository at an old URL does not acquire the pod.

Publishing requires the PR author's current source repository permission to be
`write` or `admin`. GitHub maps the `maintain` role to `write`. A personal
repository's owner is also recognized by their numeric GitHub account ID.
Organization membership alone does not grant publishing permission.

Existing pod PRs may only append one new version to one index file. They cannot
change ownership, existing versions, workflows, or other files. All source and
native download URLs must identify uploaded GitHub release assets in the bound
repository, and all asset checksums are verified before merging.

New pod PRs include their ownership binding and first index record. Successful
validation adds `manual-review`; a maintainer reviews the name and binding before
merging. Moving a pod to a different repository ID requires a separate,
maintainer-reviewed ownership change. Published version records remain intact.

The registered `tokenizer` pod is bound to `ecnivslabs/tokenizer` (ID 1363689157).
The registered `term` pod is bound to `vinceswu/term` (ID 1338177599).

## Workflow authentication

Permission checks for source repositories require the registry secret
`PIT_OWNERSHIP_TOKEN`. Use a fine-grained token with access to the source
repositories and their read-only Metadata permission, or a GitHub App installation
token with equivalent access. The workflow tries its default token when this
secret is absent, and blocks automatic publishing if permissions cannot be
verified. A registry-scoped default token does not grant access to source
repository permissions automatically. No publisher credential is sent in a PR.

Validation runs on `pull_request_target` using scripts checked out from the
trusted base commit. PR commits are fetched and inspected as Git objects only.
No PR code, hooks, configuration, or validation artifacts execute with write
credentials. The merge request specifies the exact validated PR head commit.
After an automatic merge, validation explicitly dispatches `index.yml` to refresh
the website index. GitHub does not trigger push workflows for changes made by the
default workflow token.

## Validation

Install the checksum dependency, then run the registry test suite:

```sh
python3 -m pip install blake3==1.0.8
python3 -m unittest discover -s tests
```

## Contributing

PRs are validated by CI before merge. Maintainers review and merge.
