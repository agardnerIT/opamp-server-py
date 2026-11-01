#!/usr/bin/env bash
# Regenerate the checked-in Python protobuf stubs from proto/*.proto.
#
# The generated files (`proto/*_pb2.py`, `proto/*_pb2.pyi`) are committed, so the
# only way to know they match the `.proto` is to regenerate and see no diff.
#
# The protoc version is pinned because it becomes the *minimum* protobuf runtime:
# the generated code calls ValidateProtobufRuntimeVersion() at import time and
# raises on any older runtime. Bumping the version here means bumping the
# `protobuf` floor in requirements.txt / pyproject.toml at the same time.
set -euo pipefail

PROTOC_VERSION="34.1"
PROTOC="${PROTOC:-protoc}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v "$PROTOC" >/dev/null 2>&1; then
    cat >&2 <<EOF
protoc not found. Install version ${PROTOC_VERSION}:

  macOS (arm64):
    curl -sSL -o /tmp/protoc.zip \\
      https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/protoc-${PROTOC_VERSION}-osx-aarch_64.zip
    unzip -o /tmp/protoc.zip -d /tmp/protoc && export PATH="/tmp/protoc/bin:\$PATH"

  Linux (x86_64): replace osx-aarch_64 with linux-x86_64.
  Or: pip install protobuf==7.${PROTOC_VERSION#*.} (ships no protoc; use the
  grpcio-tools bundle only if its protoc is ${PROTOC_VERSION} too).
EOF
    exit 1
fi

found="$("$PROTOC" --version | awk '{print $2}')"
if [ "$found" != "$PROTOC_VERSION" ]; then
    echo "protoc $found found, $PROTOC_VERSION expected." >&2
    echo "Regenerating with another version rewrites the runtime check in every stub." >&2
    echo "Override deliberately with PROTOC=... only if you also bump the protobuf floor." >&2
    exit 1
fi

cd "$ROOT"

# `-I .` and the `import "proto/anyvalue.proto"` line inside opamp.proto are what
# make the stubs import each other as `from proto import anyvalue_pb2`. That keeps
# the repository's flat proto/ layout while every call site keeps using the
# `proto.opamp_pb2` package path. Upstream (opamp-spec v0.20.0) uses
# `import "opamp/v1/anyvalue.proto"`; that line is the *only* intentional
# difference from https://github.com/open-telemetry/opamp-spec/blob/v0.20.0/proto/opamp/v1/opamp.proto
"$PROTOC" -I . --python_out=. --pyi_out=. proto/opamp.proto proto/anyvalue.proto

echo "Regenerated with protoc ${found}:"
git --no-pager diff --stat -- proto/ || true
echo
echo "Next: 'python -m pytest tests/test_proto_sync.py' — it pins every field number."
