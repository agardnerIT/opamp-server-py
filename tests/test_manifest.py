"""Tests for OCB manifest generation (server.manifest) and the /agent/{id}/manifest endpoint."""

import json
import base64

import pytest
import yaml
from fastapi.testclient import TestClient

from server.main import app
from server.state import AgentState, AGENT_REGISTRY
from server.manifest import (
    generate_manifest,
    generate_ocb_command,
    validate_manifest_version,
    DEFAULT_VERSION,
)

# --- fixtures -----------------------------------------------------------------

# Components an agent reports via OpAMP "available_components" (JSON shape after
# MessageToDict), incl. "code.namespace" metadata entries used for versions.
AVAILABLE_COMPONENTS = {
    "components": {
        "receivers": {
            "subComponentMap": {
                "otlp": {
                    "metadata": [{
                        "key": "code.namespace",
                        "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/otlpreceiver v0.98.0"},
                    }],
                },
                "jaeger": {
                    "metadata": [{
                        "key": "code.namespace",
                        "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/jaegerreceiver v0.99.0"},
                    }],
                },
            },
        },
        "processors": {
            "subComponentMap": {
                "batch": {
                    "metadata": [{
                        "key": "code.namespace",
                        "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector/processor/batchprocessor v0.98.0"},
                    }],
                },
            },
        },
        "exporters": {
            "subComponentMap": {
                "debug": {
                    "metadata": [{
                        "key": "code.namespace",
                        "value": {"stringValue": "github.com/open-telemetry/opentelemetry-collector/exporter/debugexporter v0.98.0"},
                    }],
                },
            },
        },
    },
}

# Effective collector config marking otlp + batch + debug as IN USE (jaeger unused).
COLLECTOR_YAML = """
service:
  extensions: []
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
"""


def effective_config_json() -> str:
    body = base64.b64encode(COLLECTOR_YAML.encode()).decode()
    return json.dumps({"configMap": {"configMap": {"": {"body": body}}}})


def register_agent(agent_id, available_components=None, effective_config=None):
    AGENT_REGISTRY.register(
        agent_id,
        AgentState(
            instance_uid=f"manifest-test-{agent_id}".encode(),
            agent_id=agent_id,
            available_components=available_components or {},
            effective_config=effective_config,
        ),
    )
    return agent_id


@pytest.fixture
def client():
    yield TestClient(app)
    for agent_id in list(AGENT_REGISTRY._agents.keys()):
        AGENT_REGISTRY.remove(agent_id)


# --- unit: generate_manifest --------------------------------------------------

class TestGenerateManifest:
    def test_includes_used_components_with_versions(self):
        comps = {
            "receiver": [{"id": "otlp", "version": "0.98.0", "used": True}],
            "exporter": [{"id": "debug", "version": "0.98.0", "used": True}],
        }
        manifest = generate_manifest(comps)
        assert "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/otlpreceiver v0.98.0" in manifest
        assert "github.com/open-telemetry/opentelemetry-collector/exporter/debugexporter v0.98.0" in manifest

    def test_excludes_unused_components(self):
        comps = {
            "receiver": [
                {"id": "otlp", "version": "0.98.0", "used": True},
                {"id": "jaeger", "version": "0.99.0", "used": False},
            ],
        }
        manifest = generate_manifest(comps)
        assert "otlpreceiver" in manifest
        assert "jaegerreceiver" not in manifest

    def test_emits_expected_sections_and_sorted_dedup(self):
        comps = {
            "receiver": [{"id": "otlp", "version": "", "used": True}],
            "processor": [{"id": "batch", "version": "", "used": True}],
            "exporter": [{"id": "debug", "version": "", "used": True}],
            "extension": [],
            "connector": [],
        }
        manifest = generate_manifest(comps)
        assert manifest.startswith("dist:")
        assert "otel_col_version: 0.98.0" in manifest
        assert manifest.index("receivers:") > manifest.index("exporters:")
        assert manifest.index("processors:") > manifest.index("receivers:")

    def test_output_is_valid_yaml(self):
        comps = {"receiver": [{"id": "otlp", "version": "0.98.0", "used": True}]}
        parsed = yaml.safe_load(generate_manifest(comps))
        assert parsed["dist"]["name"] == "otelcol-slim"
        assert parsed["receivers"] == [
            {"gomod": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/otlpreceiver v0.98.0"}
        ]

    def test_respects_custom_version(self):
        comps = {"receiver": [{"id": "otlp", "version": "0.98.0", "used": True}]}
        manifest = generate_manifest(comps, version="2.3.4")
        assert "  version: 2.3.4" in manifest


class TestGenerateOcbCommand:
    def test_default_version(self):
        assert generate_ocb_command() == "ocb build --config manifest.yaml --version 1.0.0"

    def test_custom_version(self):
        assert generate_ocb_command("2.3.4") == "ocb build --config manifest.yaml --version 2.3.4"


class TestValidateManifestVersion:
    @pytest.mark.parametrize("version", ["1.0.0", "0.98.0", "2.3.4-rc.1", "1.2.3+build.5"])
    def test_accepts_semver(self, version):
        assert validate_manifest_version(version) == version

    @pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "latest", "1.0.0\n  evil: x", "1.0.0-", None])
    def test_rejects_non_semver(self, version):
        with pytest.raises(ValueError):
            validate_manifest_version(version)


# --- api: POST /agent/{id}/manifest --------------------------------------------

class TestManifestEndpoint:
    def test_generates_manifest_for_agent_with_components(self, client):
        agent_id = register_agent("manifest-ok", AVAILABLE_COMPONENTS, effective_config_json())

        resp = client.post(f"/agent/{agent_id}/manifest")
        assert resp.status_code == 200
        data = resp.json()

        assert data["collector_version"] == DEFAULT_VERSION
        assert data["ocb_command"] == f"ocb build --config manifest.yaml --version {DEFAULT_VERSION}"
        # used components present with versions
        assert "otlpreceiver v0.98.0" in data["manifest_yaml"]
        assert "batchprocessor v0.98.0" in data["manifest_yaml"]
        assert "debugexporter v0.98.0" in data["manifest_yaml"]
        # unused component excluded
        assert "jaegerreceiver" not in data["manifest_yaml"]
        # response is parseable YAML
        parsed = yaml.safe_load(data["manifest_yaml"])
        assert parsed["dist"]["name"] == "otelcol-slim"

    def test_honors_custom_version(self, client):
        agent_id = register_agent("manifest-version", AVAILABLE_COMPONENTS, effective_config_json())

        resp = client.post(f"/agent/{agent_id}/manifest", json={"version": "2.3.4"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["collector_version"] == "2.3.4"
        assert "  version: 2.3.4" in data["manifest_yaml"]
        assert data["ocb_command"] == "ocb build --config manifest.yaml --version 2.3.4"

    def test_unknown_agent_returns_404(self, client):
        resp = client.post("/agent/does-not-exist/manifest")
        assert resp.status_code == 404

    def test_agent_without_components_returns_409(self, client):
        agent_id = register_agent("manifest-empty", {})
        resp = client.post(f"/agent/{agent_id}/manifest")
        assert resp.status_code == 409

    def test_invalid_version_returns_422(self, client):
        agent_id = register_agent("manifest-badversion", AVAILABLE_COMPONENTS, effective_config_json())
        resp = client.post(f"/agent/{agent_id}/manifest", json={"version": "1.0"})
        assert resp.status_code == 422

    def test_yaml_injection_version_returns_422(self, client):
        agent_id = register_agent("manifest-inject", AVAILABLE_COMPONENTS, effective_config_json())
        resp = client.post(f"/agent/{agent_id}/manifest", json={"version": "1.0.0\n  evil: true"})
        assert resp.status_code == 422
        # the injection never reaches the generated manifest
        assert "manifest_yaml" not in resp.text
        assert "evil: true" not in resp.text