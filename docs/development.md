# Development

```bash
pip install -r requirements.txt
pip install -e ".[dev]"
pytest tests/ -v                                   # logging defaults to JSON in CI
LOG_FORMAT=text pytest tests/ -v                   # readable local output
pip install -e ".[dev,observability]" && OPAMP_TRACING_ENABLED=true pytest tests/ -v
```

## Manual setup

Server and console by hand, without containers:

```bash
# Server
python -m venv ./venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --port 4320

# Console (web/) — its own toolchain, its own lockfile
cd web && npm ci && npm run dev      # 8501, proxies /api -> 127.0.0.1:4320

# CLI + MCP entry points
pip install -e .            # installs opampctl + opamp-mcp
pip install -e ".[mcp]"     # adds the mcp dependency
```

The console needs Node 22+ (`web/package.json`); it has no Python dependency and no
`pip` extra — `ui/`, `.streamlit/` and `requirements-ui.txt` were retired at the
stage-10 cutover (see `new-ui-plans.md`).

## Developing with Docker (local source, hot reload)

`docker-compose.yml` references **published images** (`ghcr.io/…`). Those
services have no `build:` section, so `docker compose up --build` has nothing to
build — Compose pulls the pinned image again and your local changes never reach the
container. That is also why the sidebar keeps showing the old version: the version is
`server/version.py` (kept in sync with `pyproject.toml` by `tests/test_version.py`),
not a build stamp, so `0.1.0` means "this checkout has not bumped the version", not
"the image is stale".

Use the development overlay, which builds the local Dockerfiles and bind-mounts the
source with autoreload:

```bash
# One command, every time during development
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build

# If you want plain `docker compose up` to use the overlay for this shell:
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
docker compose up --build
```

The overlay tags the local builds `opamp-server-py-server:dev` and
`opamp-server-py-ui:dev`, so it does not overwrite the pinned release images. It
also:

- bind-mounts `server/`, `proto/`, `policies/` and `collector/` into the server
  container, so **editing an existing Python file hot-reloads** (uvicorn `--reload`);
- builds the console from `web/` into a static bundle behind nginx. There is no hot
  reload for it and there cannot be: `Dockerfile.ui` runs `npm run build`, so an edit
  needs `docker compose build ui`. For an edit/refresh loop use vite instead — on the
  host (`cd web && npm run dev`), or `docker compose --profile console up console`
  (vite in a container, port 8502);
- keeps the `opamp-data` named volume at `/app/data`, so the database is not reset by
  a rebuild.

Things that still need a rebuild:

- **Anything under `web/`** — the container ships a compiled bundle. `docker compose
  build ui && docker compose up -d ui`, or use vite.
- **A new Python module** is hot-reloaded like an edited one, but a **new Python
  package** must be added to `requirements.txt` and rebuilt; the bind mount only
  shadows source files.
- **Dependency or Dockerfile changes** (`requirements.txt`, `web/package*.json`,
  `Dockerfile.*`): rebuild with `--build` (no need for `--no-cache` unless the base
  image itself changed).

Verify the new surface is live:

```bash
curl --max-time 20 --connect-timeout 5 -s localhost:4320/openapi.json | grep -o '"/agents/query/schema"'
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f ui
```

`docker compose down` is not needed between code changes; `up -d --build` recreates
only what changed. Avoid `down -v` unless you intend to delete the `opamp-data`
volume (database and generated admin password).

## End-to-end with a real collector (#113)

Every other test asserts the server is self-consistent; this one runs the upstream
`opamp` extension against it. Needs Docker.

The config carries a bearer header (#125), so the runbook is the mint + inject pair
from [agent-tokens.md](agent-tokens.md) — the same two steps the workflow does:

```bash
ADMIN_PASSWORD=… python -m uvicorn server.main:app --host 0.0.0.0 --port 4320 &
TOKEN=$(curl -sf -u "admin:$ADMIN_PASSWORD" -H 'Content-Type: application/json' \
  -d '{"label":"e2e","max_agents":5}' localhost:4320/agent-tokens \
  | python -c 'import json,sys; print(json.load(sys.stdin)["token"])')
docker run -d --name opamp-e2e \
  -e OPAMP_AGENT_TOKEN="$TOKEN" \
  -v "$PWD/collector/e2e.yaml:/etc/otelcol-contrib/config.yaml" \
  otel/opentelemetry-collector-contrib:0.140.0
python scripts/collector_e2e.py --server http://localhost:4320
```

It waits for its *own* agent by name (`e2e-collector`), then asserts the chain the
feature depends on: registered, healthy, a component list rather than just a hash
(the #77 handshake), and a `POST /agent/{id}/manifest` containing exactly the
components the pipeline uses, at the versions the collector reported. On Linux add
`--add-host=host.docker.internal:host-gateway` to the `docker run`; Docker Desktop
provides that name already.

Minting needs an admin credential: with no `ADMIN_PASSWORD` the server is read-only
and `POST /agent-tokens` answers 403. The same steps run on every PR in
[`.github/workflows/collector-e2e.yml`](../.github/workflows/collector-e2e.yml),
marked non-blocking while it stabilises.

## Protocol definitions (`proto/`)

The wire format lives in [`proto/opamp.proto`](../proto/opamp.proto) and
[`proto/anyvalue.proto`](../proto/anyvalue.proto), synced to
[opamp-spec **v0.20.0**](https://github.com/open-telemetry/opamp-spec/tree/v0.20.0/proto/opamp/v1).
The Python stubs (`proto/*_pb2.py`, `proto/*_pb2.pyi`) are generated and committed.

Two deliberate differences from upstream:

- **Flat layout.** Files stay in `proto/` rather than `proto/opamp/v1/`, so `opamp.proto`
  imports `"proto/anyvalue.proto"` instead of `"opamp/v1/anyvalue.proto"`. That is what
  makes the stubs import each other as `from proto import anyvalue_pb2` and every call
  site (`server/`, `cli/`, `tests/`) keep using the `proto.opamp_pb2` path. Nothing on the
  wire changes: protobuf does not transmit package, file or message names.
- **Generated with protoc 34.1**, so the stubs refuse to import on a protobuf runtime older
  than 7.34.1 (`requirements.txt` pins that floor). Regenerating with a newer protoc
  raises the floor — bump the requirement in the same commit.

Regenerate after editing a `.proto`:

```bash
./scripts/generate_proto.sh      # prints the expected protoc release if it is missing
git diff --stat -- proto/        # a clean tree means the stubs were not stale
python -m pytest tests/test_proto_sync.py
```

`tests/test_proto_sync.py` is the guard rail: it pins the proto package, every
pre-existing field number, the byte-for-byte encoding of a pre-v0.20.0 message, and the
messages v0.20.0 added (`AgentConfigObject.role`, `ComponentHealth.attributes`). A
renumbered field is invisible in review — protobuf happily parses the old bytes into the
new field — so the test is the only thing that catches it.

## Project structure

```
opamp-server-py/
├── server/          # FastAPI server (logging.py/tracing.py = observability, #108)
├── client/          # opamp_client — shared HTTP client library
├── cli/             # opampctl CLI (typer, JSON output)
├── mcp_server/      # FastMCP server (stdio/SSE) for AI agents
├── web/             # the console (React + Vite + Tailwind; built into the ui image)
├── docker/          # TLS terminator examples (#96) + ui.nginx.conf for the console
├── proto/           # Protobuf definitions
├── tests/           # Tests (fake_agent.py = scriptable OpAMP Supervisor, #107)
├── collector/       # Sample configs
└── data/            # SQLite DB (+ generated admin_password)
```

## Console gates

The console is a product artifact now, so its gates are not optional:

```bash
cd web
npm run build     # tsc -b (strict) + vite build
npm run test      # vitest (mock transport, no server needed)
npm run lint      # oxlint
npm run e2e       # Playwright against the mock dev server
npm run e2e:live  # Playwright live smoke against a throwaway server on :4330
```

CI runs `build`, `test` and `lint` on every push; `npm ci` is the install, so
`web/package-lock.json` must be committed with any dependency change. Screenshots for
docs live in `docs/screenshots/` and are regenerated with `web/tools/screenshot-*.mjs`
(commands in `web/README.md`).

## Release

Pushing a `vX.Y.Z` git tag publishes `X.Y.Z` and `X.Y` images and moves `latest`; a
prerelease tag such as `vX.Y.Z-rc1` publishes only `X.Y.Z-rc1` and leaves `latest`
alone. Tags are created with `python3 release.py --version vX.Y.Z`, which also prints
the release checklist.
