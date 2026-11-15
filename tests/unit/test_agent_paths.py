"""Update paths of the agent (#493): one minor line at a time, patches may be skipped."""

import pytest

from deployment_agent.paths import MAX_STEPS, component_path


def entry(version, *, source="s", from_versions=None, compatible=True):
    major, minor, _ = version.split(".")[:3]
    previous = f"{major}.{int(minor) - 1}.0" if int(minor) else f"{int(major) - 1}.0.0" if int(major) else "0.0.0"
    return {
        "version": version,
        "source_id": source,
        "from_versions": from_versions or f">={previous},<={version}",
        "compatible": compatible,
    }


def path(installed, entries, *, enforce_lines=True):
    return component_path("django-skeleton", "core", installed, entries, enforce_lines=enforce_lines)


def test_core_path_takes_the_newest_patch_of_each_line():
    result = path("0.5.2", [entry("0.5.3"), entry("0.6.0"), entry("0.6.4"), entry("0.7.1")])
    assert result["steps"] == [{"version": "0.6.4", "source_id": "s"}, {"version": "0.7.1", "source_id": "s"}]
    assert (result["latest"], result["blocked"]) == ("0.7.1", None)
    assert (result["component_id"], result["kind"], result["installed"]) == ("django-skeleton", "core", "0.5.2")


def test_missing_line_blocks_after_the_reachable_steps():
    result = path("0.5.2", [entry("0.5.3"), entry("0.7.1")])
    assert result["steps"] == [{"version": "0.5.3", "source_id": "s"}]
    assert (result["latest"], result["blocked"]) == ("0.7.1", "missing_intermediate_release")


def test_only_incompatible_newer_entries_block_as_runtime():
    result = path("0.5.2", [entry("0.5.3", compatible=False), entry("0.6.0", compatible=False)])
    assert result["steps"] == []
    assert (result["latest"], result["blocked"]) == ("0.6.0", "incompatible_runtime")


def test_up_to_date_component_has_no_path():
    result = path("0.7.1", [entry("0.5.3"), entry("0.7.1")])
    assert (result["latest"], result["steps"], result["blocked"]) == (None, [], None)
    assert path("0.7.1", [])["latest"] is None


def test_wide_from_versions_still_stops_at_each_line_when_enforced():
    wide = [entry(version, from_versions=">=0.1.0") for version in ("0.6.2", "0.7.0", "0.8.3")]
    assert [step["version"] for step in path("0.5.2", wide)["steps"]] == ["0.6.2", "0.7.0", "0.8.3"]
    assert [step["version"] for step in path("0.5.2", wide, enforce_lines=False)["steps"]] == ["0.8.3"]


def test_app_paths_may_skip_lines():
    result = component_path(
        "org.example.notes", "app", "1.2.0", [entry("1.5.0", from_versions=">=1.0.0")], enforce_lines=False
    )
    assert result["steps"] == [{"version": "1.5.0", "source_id": "s"}] and result["blocked"] is None


def test_first_entry_of_a_repeated_version_is_the_one_offered():
    # evaluate() drops repeated versions keeping the first source; component_path keeps their order.
    result = path("0.5.2", [entry("0.6.0", source="first"), entry("0.6.0", source="second")])
    assert result["steps"] == [{"version": "0.6.0", "source_id": "first"}]


def test_prereleases_are_ordinary_steps():
    result = path("0.5.2", [entry("0.6.0rc1", from_versions=">=0.5.0")])
    assert result["steps"] == [{"version": "0.6.0rc1", "source_id": "s"}]


def test_steps_are_bounded():
    entries = [entry(f"0.{minor}.0", from_versions=">=0.0.0") for minor in range(1, MAX_STEPS + 5)]
    result = path("0.0.1", entries)
    assert len(result["steps"]) == MAX_STEPS
    assert result["blocked"] == "missing_intermediate_release"


@pytest.mark.parametrize("bad", [{"from_versions": "not a range"}, {"version": "x"}, {"from_versions": None}])
def test_malformed_entries_are_never_steps(bad):
    result = path("0.5.2", [{**entry("0.6.0"), **bad}])
    assert result["steps"] == []
