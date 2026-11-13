"""Pre-release ranges (#256): the answer does not depend on the installed packaging version."""

from pathlib import Path

import pytest

from skeleton.versioning import normalize_version, version_satisfies


@pytest.mark.parametrize(
    "version,specifier",
    [("0.6.0rc1", ">=0.4.0,<1.0.0"), ("1.2.0rc1", ">=1.0.0,<2.0.0"), ("0.6.0-rc.1", ">=0.4.0,<1.0.0")],
)
def test_prerelease_inside_range_satisfies(version, specifier):
    assert version_satisfies(version, specifier) is True


@pytest.mark.parametrize("specifier", ["<1.0.0", ">=1.0.0"])
def test_prerelease_of_the_exclusive_upper_bound_is_outside(specifier):
    assert version_satisfies("1.0.0rc1", specifier) is False


def test_final_versions_keep_their_meaning():
    assert version_satisfies("1.0.0", "<1.0.0") is False
    assert version_satisfies("0.10.0", ">=0.2.0,<1.0.0") is True


def test_invalid_specifier_raises_value_error():
    with pytest.raises(ValueError):
        version_satisfies("1.0.0", "not a range")


def test_semver_prerelease_and_nonlexical_ranges():
    assert normalize_version("1.2.3-rc.2") == "1.2.3rc2"
    assert version_satisfies("0.10.0", ">=0.2.0,<1.0.0")
    assert not version_satisfies("1.0.0", "<1.0.0")


def test_source_metadata_never_claims_an_unrelated_installed_distribution(monkeypatch):
    import skeleton.versioning as versioning

    class UnrelatedDistribution:
        version = "9.9.9"

        def locate_file(self, path):
            return Path("/unrelated/site-packages") / path

    monkeypatch.setattr(versioning.metadata, "distribution", lambda _: UnrelatedDistribution())
    assert versioning.core_version()["origin"] == "source"
    assert versioning.core_version()["version"] != "9.9.9"
