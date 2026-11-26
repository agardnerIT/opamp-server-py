"""The console is a static bundle behind nginx, on the product port (stage 10).

`new-ui-plans.md` stage 10 retired the Streamlit UI: `ui/`, `.streamlit/` and
`requirements-ui.txt` are gone, and 8501 — Streamlit's old port, the one the owner
asked for — now belongs to the React console. That hand-over is easy to undo by
accident, in ways no other test would notice:

* the compose port drifts back to `8501:8501` while the image listens on 80, and the
  container is "up" but unreachable;
* the console container grows an `env_file:` or a data volume, quietly putting the
  server's secrets and database inside the one service that only serves files;
* the nginx `/api` proxy loses its trailing slash, so every console request 404s (the
  FastAPI app serves `/auth/session`, not `/api/auth/session`);
* a Streamlit image reference survives a release, and `docker compose up` on a fresh
  clone resurrects the UI the project just deleted.

These are string-level assertions on purpose: they are cheap, they run offline, and
they encode the decisions rather than the mechanism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ships with the server dependencies")

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile.ui"
NGINX = REPO / "docker" / "ui.nginx.conf"

CONSOLE_PORT = "8501"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def ui_service() -> dict:
    return compose()["services"]["ui"]


def published_ports(service: dict) -> list[tuple[str, str]]:
    """`[(host_side, container_side)]`, tolerating both compose spellings."""
    found = []
    for entry in service.get("ports") or []:
        text = entry if isinstance(entry, str) else f"{entry.get('published')}:{entry.get('target')}"
        host, _, container = text.rpartition(":")
        found.append((host, container.split("/")[0]))
    return found


def test_the_product_port_is_published_to_the_nginx_port():
    assert published_ports(ui_service()) == [(f"127.0.0.1:{CONSOLE_PORT}", "80")], (
        "the console must publish 127.0.0.1:8501 -> 80 (nginx). A mismatch between "
        "the published port and the port nginx listens on is a container that runs "
        "and answers nothing"
    )


def test_the_console_container_holds_no_configuration_or_data():
    """No `env_file:`, no volumes: it needs neither, and both are ways to leak."""
    service = ui_service()
    assert not service.get("env_file"), (
        "the console container must not load `.env` — it is static files plus a "
        "proxy, and `.env` carries CONFIG_AT_REST_KEY"
    )
    assert not service.get("volumes"), (
        "the console container must not mount the database volume: it reads the "
        "fleet over HTTP, and a data mount here is #132's trap in another service"
    )


def instructions(path: Path) -> str:
    """The file with comment lines removed.

    History in a comment ("streamlit's old port") is documentation, not a build
    instruction; only a real directive can resurrect the thing this test forbids.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).lower()


def test_the_ui_image_is_not_a_streamlit_image_any_more():
    """The built `:dev` image and the released image both come from Dockerfile.ui."""
    dev = yaml.safe_load((REPO / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    build = dev["services"]["ui"]["build"]
    assert build["dockerfile"] == "Dockerfile.ui"
    image = instructions(DOCKERFILE)
    for gone in ("streamlit run", "_stcore", "requirements-ui.txt"):
        assert gone not in image, f"Dockerfile.ui still runs the Streamlit UI ({gone})"


def test_the_image_builds_the_console_and_serves_it_with_nginx():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "npm ci" in text, "the build must install from the committed lockfile"
    assert "npm run build" in text, "the bundle is built in the image, not committed"
    assert "FROM nginx" in text, "the runtime stage is nginx serving web/dist"
    assert "web/dist" in text
    assert "ui.nginx.conf" in text, "the checked-in nginx config must be the one shipped"


def test_api_requests_are_proxied_with_the_prefix_stripped():
    config = NGINX.read_text(encoding="utf-8")
    assert "location /api/" in config
    assert "proxy_pass http://server:4320/;" in config, (
        "the trailing slash is what strips /api — without it every console request "
        "reaches the server as /api/... and 404s"
    )


def test_deep_links_are_served_the_shell_but_the_shell_is_not_cached():
    config = NGINX.read_text(encoding="utf-8")
    assert "try_files $uri $uri/ /index.html;" in config, (
        "a bookmark to /agents must not 404 — the SPA routes client-side"
    )
    assert "Cache-Control \"no-store\"" in config, (
        "index.html names content-hashed assets; caching it pins browsers to a build "
        "that no longer exists after an upgrade"
    )


def test_nothing_in_the_tree_still_ships_streamlit():
    """A leftover Streamlit reference is a second UI nobody is maintaining."""
    offenders = []
    for name in ("ui", ".streamlit", "requirements-ui.txt"):
        if (REPO / name).exists():
            offenders.append(name)
    assert offenders == []

    for path in (REPO / "docker-compose.yml", REPO / "docker-compose.dev.yml"):
        assert "streamlit run" not in instructions(path), (
            f"{path.name} still starts Streamlit"
        )
        assert "requirements-ui" not in instructions(path), (
            f"{path.name} still installs the retired UI requirements"
        )

    pyproject = instructions(REPO / "pyproject.toml")
    assert "streamlit" not in pyproject, (
        "the `[ui]` extra installed Streamlit; the console ships as an image"
    )
