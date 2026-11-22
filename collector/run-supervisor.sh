#!/usr/bin/env bash
# Run a supervisor-managed collector against the local OpAMP server.
#
# Why a supervisor: `extension/opampextension` never advertises
# AcceptsRemoteConfig, so a bare collector can be reported to but never take a
# pushed config. The OpAMP Supervisor (cmd/opampsupervisor) is the agent that
# can — it receives the config, restarts the child collector and auto-reverts
# on failure (docs/remote-config.md).
#
# The supervisor binary is NOT in the otelcol-contrib Docker image (the image
# ships only otelcol-contrib), so build it once:
#   git clone --depth 1 --branch v0.165.0 \
#     https://github.com/open-telemetry/opentelemetry-collector-contrib.git /tmp/contrib
#   cd /tmp/contrib/cmd/opampsupervisor
#   GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o ./opampsupervisor .
#
# Then run this script from the repo root:
#   OPAMP_SERVER_TOKEN=opamp_agent_... ./collector/run-supervisor.sh

set -euo pipefail
cd "$(dirname "$0")/.."

: "${OPAMP_SERVER_TOKEN:?export OPAMP_SERVER_TOKEN first}"
OPAMP_SERVER_HOST="${OPAMP_SERVER_HOST:-host.docker.internal}"
SUPERVISOR_BIN="${SUPERVISOR_BIN:-$PWD/.supervisor/opampsupervisor}"
[ -f "$SUPERVISOR_BIN" ] || { echo "supervisor binary not found at $SUPERVISOR_BIN — build it (see header)"; exit 1; }

# The token is a secret: substitute it into a gitignored copy, never commit one.
# (Everything under .supervisor/ is gitignored; it is also where the built
# binary lives, because Docker Desktop cannot bind-mount files from /tmp.)
sed -e "s/\${OPAMP_SERVER_TOKEN}/$OPAMP_SERVER_TOKEN/g" \
    -e "s/\${OPAMP_SERVER_HOST}/$OPAMP_SERVER_HOST/g" \
  collector/supervisor.yaml > .supervisor/supervisor.yaml
chmod 600 .supervisor/supervisor.yaml

# --rm on purpose? No: keep the container so a crash can be inspected with
# docker logs; remove it by hand once you are done.
docker run -d --name collector-supervisor \
  -e OPAMP_SERVER_TOKEN="$OPAMP_SERVER_TOKEN" \
  -p 4347:4347 -p 4348:4348 \
  -v "$PWD/collector/collector-base.yaml:/etc/otelcol-contrib/collector-base.yaml:ro" \
  -v "$PWD/collector/file.log:/var/log/collector/file.log:ro" \
  -v "$PWD/collector/supervisor-state:/var/lib/otelcol/supervisor" \
  -v "$PWD/.supervisor/supervisor.yaml:/etc/opamp-supervisor/supervisor.yaml:ro" \
  -v "$PWD/.supervisor:/opamp:ro" \
  --entrypoint /opamp/opampsupervisor \
  otel/opentelemetry-collector-contrib:0.165.0 \
  --config=/etc/opamp-supervisor/supervisor.yaml
