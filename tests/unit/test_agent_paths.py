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


# --------------------------------------------------------------------------- chosen target (#653)

PUBLISHED = [entry("0.3.1"), entry("0.4.0"), entry("0.4.2"), entry("0.5.0")]


def test_reachable_targets_are_the_newest_of_each_line():
    from deployment_agent.paths import reachable_targets

    targets = reachable_targets("0.3.0", PUBLISHED, enforce_lines=True)
    assert [target["version"] for target in targets] == ["0.3.1", "0.4.2", "0.5.0"]
    chosen = targets[-1]["steps"]
    assert [step["version"] for step in chosen] == ["0.4.2", "0.5.0"]
    full = component_path("django-skeleton", "core", "0.3.0", PUBLISHED, enforce_lines=True, with_targets=True)
    assert full["targets"] == targets and "targets" not in path("0.3.0", PUBLISHED)


def test_steps_to_a_target(subtests):
    from deployment_agent.paths import steps_to

    for case, installed, entries, target, expected in (
        ("patch skipped", "0.3.0", PUBLISHED, "0.4.2", ["0.4.2"]),
        ("older patch of a line", "0.3.0", PUBLISHED, "0.4.0", ["0.4.0"]),
        ("same line", "0.3.0", PUBLISHED, "0.3.1", ["0.3.1"]),
        ("missing intermediate line", "0.3.0", [entry("0.3.1"), entry("0.5.0")], "0.5.0", None),
        ("not published", "0.3.0", PUBLISHED, "0.4.1", None),
        ("installed already", "0.5.0", PUBLISHED, "0.5.0", None),
        ("downgrade", "0.5.0", PUBLISHED, "0.4.2", None),
        ("incompatible target", "0.3.0", [entry("0.3.1", compatible=False)], "0.3.1", None),
        ("not a version", "0.3.0", PUBLISHED, "latest", None),
    ):
        with subtests.test(case=case):
            steps = steps_to(installed, entries, target, enforce_lines=True)
            assert (None if steps is None else [step["version"] for step in steps]) == expected


def test_jumps_allowed_reach_a_target_in_one_step():
    from deployment_agent.paths import steps_to

    entries = [entry("0.3.1"), entry("0.5.0", from_versions=">=0.3.0,<=0.5.0")]
    assert steps_to("0.3.0", entries, "0.5.0", enforce_lines=True) is None
    assert [step["version"] for step in steps_to("0.3.0", entries, "0.5.0", enforce_lines=False)] == ["0.5.0"]
