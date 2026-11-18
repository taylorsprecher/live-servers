# MinIO server image (self-built from source)

The image behind the Zillion Storage service's `minio` backend:
**unmodified upstream MinIO, compiled by us from its archived source
at the pinned tag `RELEASE.2025-10-15T17-29-55Z`** (the final MinIO
community release, and the one carrying the October 2025 security
fixes), published as
`ghcr.io/taylorsprecher/minio:RELEASE.2025-10-15T17-29-55Z`.

## Why we build it ourselves

MinIO made its community edition source-only and withdrew the
official `minio/minio` container images — the Docker Hub repository
no longer exists, so the fleet's first MinIO deploy (2026-11-18)
failed at the image pull. Taylor's direction: build the image from
source ourselves rather than trusting a third-party rebuild. This
directory is that build. Nothing in the upstream code is patched or
altered; the Dockerfile only clones the tag, asserts the commit,
and compiles it the way upstream's own Makefile does (CGO disabled,
`-tags kqueue`, `-trimpath`, release version stamping mirroring
`buildscripts/gen-ldflags.go`).

## License (AGPLv3)

MinIO is licensed under the **GNU Affero General Public License
v3**. The corresponding source for this image is the upstream
repository at <https://github.com/minio/minio>, tag
`RELEASE.2025-10-15T17-29-55Z` (commit
`9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`) — the exact tree the
Dockerfile clones. The image itself also carries the license text
at `/licenses/LICENSE`. The server runs unmodified, as a separate
process the fleet talks to over the S3 API.

## How it is built and published

The **Build MinIO Image** workflow
(`.github/workflows/build-minio-image.yml`) is manual-dispatch only:
it builds this Dockerfile on a GitHub Actions runner (the fleet host
has ~1.9 GB RAM — far too small for a Go build of MinIO) and pushes
the result to GHCR with the workflow's `GITHUB_TOKEN`
(`packages: write`). Nothing deploys from that workflow; the normal
Deploy Zillion Storage run pulls the image like any other.

The GHCR package is **public**: the image contains only unmodified
open-source code (no credentials, no fleet configuration — root
credentials are generated into the host `.env` by
`ops/deploy-zillion.sh` and injected by compose at runtime), and a
public package lets the host pull it with no registry login.

## Repinning to another release

1. In `Dockerfile`, update `MINIO_TAG`, `MINIO_COMMIT`, and
   `MINIO_SHORT_COMMIT` (the tag's commit, from
   `git ls-remote https://github.com/minio/minio.git "refs/tags/<tag>"`),
   plus the three dated `-X` stamps (`cmd.Version` is the tag's
   timestamp in RFC3339 form; `cmd.ReleaseTag` is `RELEASE.` + the
   same timestamp with `-` instead of `:`).
2. Check the new tag's `go.mod`: if its `go`/`toolchain` lines move
   past 1.24.8, bump the builder image (`golang:1.24.8-bookworm`)
   to match.
3. Update the image tag in `docker-compose.yml` (the `minio`
   service) and the tag in `.github/workflows/build-minio-image.yml`
   to the same value.
4. Dispatch the Build MinIO Image workflow, make the new GHCR
   package version public if needed, then run Deploy Zillion
   Storage.
