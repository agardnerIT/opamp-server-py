"""The weaver projection (#135): deterministic, attributable, and honest about limits.

Weaver itself runs in CI (it is a Rust binary in a container, not a Python dependency), so
what is tested here is the **generator**: that it is idempotent, that a malformed key fails
in our own suite rather than only in a red CI job, and that the committed projection is not
stale. `test_a_malformed_key_fails` is #135's "assert the failure rather than trusting the
tool".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "semconv_project.py"


@pytest.fixture(scope="module")
def project():
    spec = importlib.util.spec_from_file_location("semconv_project", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generator_is_idempotent(project):
    """Two runs must produce byte-identical files: CI diffs this output."""
    first = project.render_registry(project.load_profiles())
    second = project.render_registry(project.load_profiles())

    assert first == second
    # And the order profiles were listed in cannot matter.
    reversed_output = project.render_registry(list(reversed(project.load_profiles())))
    assert reversed_output == first


def test_the_committed_projection_is_not_stale(project):
    assert project.stale() == [], (
        "run `python scripts/semconv_project.py` and commit the result"
    )


def test_the_registry_pins_its_schema_and_format(project):
    registry = project.render_registry(project.load_profiles())

    assert "file_format: definition_manifest/2.0" in registry["manifest.yaml"]
    assert project.SCHEMA_URL in registry["manifest.yaml"]
    assert project.SEMCONV_VERSION in registry["manifest.yaml"]


def test_only_the_parts_weaver_can_judge_are_projected(project):
    """A document shape in the registry would be a claim weaver cannot check."""
    registry = project.render_registry(project.load_profiles())
    everything = "\n".join(registry.values())

    for key in ("attribute_group", "type: metric"):
        assert key in everything
    # Not projectable, and the whole point of the split: the loader owns these.
    for key in ("panels:", "config_formats:", "structure:", "snippet:"):
        assert key not in everything


def test_every_profile_with_identity_evidence_gets_a_group(project):
    from server import profiles

    groups = project.identity_attributes(profiles.PROFILES)
    named = {group["id"].rsplit(".", 1)[-1] for group in groups}

    for profile in profiles.PROFILES:
        if profile.identity_match_any:
            assert profile.name in named, f"{profile.name} has rules but no group"


def test_telemetry_is_projected_only_where_a_profile_declares_it(project):
    from server import profiles

    metrics = project.telemetry_metrics(profiles.PROFILES)
    declared = {profile.name for profile in profiles.PROFILES if profile.telemetry}

    projected = {group["id"].split(".")[3] for group in metrics}
    assert projected == declared
    # `generic` has no telemetry and must not appear: an empty metric group would read as
    # "reports nothing" rather than "has no such concept" (#120's lesson).
    assert "generic" not in projected


# ------------------------------------------- asserting the failure, not the tool


@pytest.mark.parametrize(
    "key,reason",
    [
        ("opamp.agent.otelcol.Service_Name", "lower case"),
        ("opamp.agent.otelcol.service-name", "underscores"),
        ("servicename", "namespaced"),
        ("opamp..otelcol", "namespaced"),
    ],
)
def test_a_malformed_key_fails_here_not_only_in_ci(project, key, reason):
    with pytest.raises(project.ProjectionError) as caught:
        project.check_key(key)
    assert reason in str(caught.value)


def test_a_duplicate_attribute_id_is_refused(project):
    from server import profiles

    duplicate = profiles.get("otelcol")
    with pytest.raises(project.ProjectionError) as caught:
        project.identity_attributes([duplicate, duplicate])
    assert "duplicate attribute id" in str(caught.value)


def test_the_generated_doc_says_what_the_check_does_not_prove(project):
    """Overclaiming is the risk this issue names: never "the dictionary is valid"."""
    doc = project.render_doc(project.load_profiles())

    assert "identity and telemetry projection" in doc
    assert "`server/profiles/schema.py`" in doc
    assert "nothing** about the rest of a profile" in doc
