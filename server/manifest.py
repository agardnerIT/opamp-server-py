"""
OpenTelemetry Collector Builder (OCB) manifest generation.

Pure module (no Streamlit / server dependencies) shared by the FastAPI server
and the Streamlit UI so both can never drift.

Generates a minimal `manifest.yaml` for a slim collector build containing ONLY
the components an agent actually uses. See:
- OCB: https://opentelemetry.io/docs/collector/extend/ocb/
- OpAMP: https://opentelemetry.io/docs/specs/opamp/
"""

import re

COMPONENT_GOMOD_PATHS = {
    "receiver": {
        "otlp": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/otlpreceiver",
        "prometheus": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/prometheusreceiver",
        "hostmetrics": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/hostmetricsreceiver",
        "jaeger": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/jaegerreceiver",
        "zipkin": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/zipkinreceiver",
        "awscontainerinsightreceiver": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/awscontainerinsightreceiver",
        "awsxrayreceiver": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/awsxrayreceiver",
        "fluentforward": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/fluentforwardreceiver",
        "carbon": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/carbonreceiver",
        "statsd": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/statsdreceiver",
        "kafkareceiver": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/kafkareceiver",
        "mongodbatlas": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/mongodbatlasreceiver",
        "splunkhecreceiver": "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/splunkhecreceiver",
    },
    "processor": {
        "batch": "github.com/open-telemetry/opentelemetry-collector/processor/batchprocessor",
        "memory_limiter": "github.com/open-telemetry/opentelemetry-collector/processor/memorylimiterprocessor",
        "filter": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/filterprocessor",
        "attributes": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/attributesprocessor",
        "resource": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/resourceprocessor",
        "k8sattributes": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/k8sattributesprocessor",
        "cumulativetodelta": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/cumulativetodeltaprocessor",
        "deltatorate": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/deltatorateprocessor",
        "metricstransform": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/metricstransformprocessor",
        "groupbyattrs": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/groupbyattrsprocessor",
        "resourcedetection": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/resourcedetectionprocessor",
        "routing": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/routingprocessor",
        "tailsampling": "github.com/open-telemetry/opentelemetry-collector-contrib/processor/tailsamplingprocessor",
    },
    "exporter": {
        "otlp": "github.com/open-telemetry/opentelemetry-collector/exporter/otlpexporter",
        "otlphttp": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/otlphttpexporter",
        "prometheus": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/prometheusexporter",
        "prometheusremotewrite": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/prometheusremotewriteexporter",
        "debug": "github.com/open-telemetry/opentelemetry-collector/exporter/debugexporter",
        "jaeger": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/jaegerexporter",
        "zipkin": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/zipkinexporter",
        "awsxray": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/awsxrayexporter",
        "awsprometheus": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/awsprometheusexporter",
        "kafka": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/kafkaexporter",
        "loadbalancing": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/loadbalancingexporter",
        "datadog": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/datadogexporter",
        "honeycomb": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/honeycombexporter",
        "splunk": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/splunkhecexporter",
        "mongodbatlas": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/mongodbatlasexporter",
        "clickhouse": "github.com/open-telemetry/opentelemetry-collector-contrib/exporter/clickhouseexporter",
    },
    "extension": {
        "opamp": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/opampextension",
        "pprof": "github.com/open-telemetry/opentelemetry-collector/extension/ballastextension",
        "zpages": "github.com/open-telemetry/opentelemetry-collector/extension/zpagesextension",
        "health_check": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/healthcheckextension",
        "observability": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/observabilityextension",
        "basicauth": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/basicauthextension",
        "oauth2client": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/oauth2clientauthextension",
        "storage": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/storageextension",
        "awsproxy": "github.com/open-telemetry/opentelemetry-collector-contrib/extension/awsproxy",
    },
    "connector": {
        "metrics_transform": "github.com/open-telemetry/opentelemetry-collector-contrib/connector/metricstransformconnector",
        "servicegraph": "github.com/open-telemetry/opentelemetry-collector-contrib/connector/servicegraphconnector",
        "spanmetrics": "github.com/open-telemetry/opentelemetry-collector-contrib/connector/spanmetricsconnector",
    },
}

BASE_COLLECTOR_VERSION = "0.98.0"
DEFAULT_VERSION = "1.0.0"

# Strict semver (with optional -prerelease / +build) so a distro version can
# never smuggle arbitrary YAML into a generated manifest.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


def validate_manifest_version(version: str) -> str:
    """Validate an OCB distro version string, raising ValueError if not semver.

    The UI previously accepted free-text versions and interpolated them raw into
    generated YAML (an injection hazard). This is the single gate for user input
    before it reaches manifest_yaml / the OCB command.
    """
    if not isinstance(version, str) or not _SEMVER_RE.match(version):
        # Do not echo the offending input back.
        raise ValueError(
            f"Invalid manifest version: must be semver like '{DEFAULT_VERSION}'"
        )
    return version


def get_gomod_path(comp_type: str, comp_id: str) -> str:
    if comp_type in COMPONENT_GOMOD_PATHS:
        if comp_id in COMPONENT_GOMOD_PATHS[comp_type]:
            return COMPONENT_GOMOD_PATHS[comp_type][comp_id]
    return f"github.com/open-telemetry/opentelemetry-collector-contrib/{comp_type}/{comp_id}"


def generate_manifest(components: dict, version: str = DEFAULT_VERSION) -> str:
    receivers = []
    processors = []
    exporters = []
    extensions = []
    connectors = []

    for comp_type, comps in components.items():
        for comp in comps:
            if comp.get("used"):
                gomod = get_gomod_path(comp_type, comp["id"])
                comp_version = comp.get("version", "")
                gomod_with_version = f"{gomod} v{comp_version}" if comp_version else gomod
                if comp_type == "receiver":
                    receivers.append(gomod_with_version)
                elif comp_type == "processor":
                    processors.append(gomod_with_version)
                elif comp_type == "exporter":
                    exporters.append(gomod_with_version)
                elif comp_type == "extension":
                    extensions.append(gomod_with_version)
                elif comp_type == "connector":
                    connectors.append(gomod_with_version)

    lines = [
        f"dist:",
        f"  name: otelcol-slim",
        f"  description: OpenTelemetry Collector slim build",
        f"  output_path: ./otelcol",
        f"  version: {version}",
        f"  otel_col_version: {BASE_COLLECTOR_VERSION}",
        f"exporters:",
    ]

    for exp in sorted(set(exporters)):
        lines.append(f"  - gomod: {exp}")

    lines.append(f"extensions:")
    for ext in sorted(set(extensions)):
        lines.append(f"  - gomod: {ext}")

    lines.append(f"receivers:")
    for rec in sorted(set(receivers)):
        lines.append(f"  - gomod: {rec}")

    lines.append(f"processors:")
    for pro in sorted(set(processors)):
        lines.append(f"  - gomod: {pro}")

    if connectors:
        lines.append(f"connectors:")
        for con in sorted(set(connectors)):
            lines.append(f"  - gomod: {con}")

    return "\n".join(lines)


def generate_ocb_command(version: str = DEFAULT_VERSION) -> str:
    return f"ocb build --config manifest.yaml --version {version}"