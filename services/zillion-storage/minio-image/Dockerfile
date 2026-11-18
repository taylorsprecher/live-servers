# Self-built MinIO server image — unmodified upstream MinIO compiled
# from its archived source at the pinned release tag below. MinIO
# withdrew its official container images (community edition is now
# source-only), so the fleet builds its own. See README.md in this
# directory for the full story, the AGPLv3 note, and repin steps.
#
# The build below encodes exactly the steps proven by hand against
# this tag on 2026-11-18 (go1.24.8, CGO_ENABLED=0, upstream's
# -tags kqueue / -trimpath, and release version stamping mirroring
# buildscripts/gen-ldflags.go). Nothing is configured or credentialed
# in the image: root credentials arrive from the host .env at runtime
# via compose, exactly as they did for the official image.

FROM golang:1.24.8-bookworm AS build

# Pinned upstream release. MINIO_COMMIT is the commit the tag points
# at (verified with git ls-remote); the clone step asserts it so a
# moved or re-created tag fails the build instead of silently
# shipping different code.
ARG MINIO_TAG=RELEASE.2025-10-15T17-29-55Z
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
ARG MINIO_SHORT_COMMIT=9e49d5e7a648

ENV CGO_ENABLED=0
ENV GOTOOLCHAIN=local

WORKDIR /src

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${MINIO_TAG}" https://github.com/minio/minio.git . \
 && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"

# Version stamping: upstream's Makefile generates -X ldflags with
# buildscripts/gen-ldflags.go, which needs a full git checkout and
# stamps cmd.Version as the commit time in RFC3339 form and
# cmd.ReleaseTag as "RELEASE." + that time with dashes. Those exact
# values for this tag are reproduced here directly.
RUN LDFLAGS="-s -w \
      -X github.com/minio/minio/cmd.Version=2025-10-15T17:29:55Z \
      -X github.com/minio/minio/cmd.CopyrightYear=2025 \
      -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2025-10-15T17-29-55Z \
      -X github.com/minio/minio/cmd.CommitID=${MINIO_COMMIT} \
      -X github.com/minio/minio/cmd.ShortCommitID=${MINIO_SHORT_COMMIT} \
      -X github.com/minio/minio/cmd.GOPATH=$(go env GOPATH) \
      -X github.com/minio/minio/cmd.GOROOT=$(go env GOROOT)" \
 && go build -tags kqueue -trimpath -buildvcs=false --ldflags "${LDFLAGS}" -o /out/minio .

FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/minio /usr/bin/minio
COPY --from=build /src/LICENSE /licenses/LICENSE

EXPOSE 9000 9001
VOLUME ["/data"]

ENTRYPOINT ["minio"]
