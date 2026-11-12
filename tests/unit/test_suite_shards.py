"""CI slices of the suite (``pytest --shard I/N``) never lose or repeat a test."""

import subprocess
import sys
from pathlib import Path

import pytest

from tools import suite_shards

ROOT = Path(__file__).resolve().parents[2]
NODEIDS = [
    "tests/unit/test_a.py::test_one",
    "tests/unit/test_a.py::test_two[param]",
    "tests/unit/test_b.py::TestThing::test_method",
    "tests/integration/test_c.py::test_one",
    "tests/integration/test_d.py::test_one",
    "tests/contrib/calendar/test_e.py::test_one",
    "tests/accounts/test_f.py::test_one",
    "tests/accounts/test_f.py::test_two",
]


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7, 50])
def test_slices_are_disjoint_and_together_are_the_whole_suite(total):
    slices = [suite_shards.split_shard(NODEIDS, index, total)[0] for index in range(1, total + 1)]
    assert sorted(nodeid for chunk in slices for nodeid in chunk) == sorted(NODEIDS)
    assert sum(len(chunk) for chunk in slices) == len(NODEIDS)
    for index in range(1, total + 1):
        selected, deselected = suite_shards.split_shard(NODEIDS, index, total)
        assert sorted(selected + deselected) == sorted(NODEIDS)
        assert selected == [nodeid for nodeid in NODEIDS if nodeid in selected], "Collection order is kept."


def test_a_file_is_never_split_and_slices_are_balanced_by_files():
    assignment = suite_shards.shard_files([suite_shards.file_of(nodeid) for nodeid in NODEIDS], 2)
    assert len(assignment) == 6
    assert sorted(assignment.values()) == [1, 1, 1, 2, 2, 2]
    selected, _ = suite_shards.split_shard(NODEIDS, assignment["tests/unit/test_a.py"], 2)
    assert {"tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_two[param]"} <= set(selected)


def test_slices_do_not_depend_on_collection_order_or_path_separator():
    windows = [nodeid.replace("/", "\\", 2) for nodeid in reversed(NODEIDS)]
    for index in (1, 2, 3):
        expected = {suite_shards.file_of(nodeid) for nodeid in suite_shards.split_shard(NODEIDS, index, 3)[0]}
        assert {suite_shards.file_of(nodeid) for nodeid in suite_shards.split_shard(windows, index, 3)[0]} == expected


def test_one_slice_is_the_whole_suite():
    assert suite_shards.split_shard(NODEIDS, 1, 1) == (NODEIDS, [])


@pytest.mark.parametrize("value", ["", "1", "0/2", "3/2", "1/0", "a/b", "1/2/3", "-1/2", " 1/2", "1/2000", None])
def test_malformed_shard_is_refused(value):
    with pytest.raises(ValueError, match="--shard expects I/N"):
        suite_shards.parse_shard(value)


def test_shard_is_parsed():
    assert suite_shards.parse_shard("1/1") == (1, 1)
    assert suite_shards.parse_shard("2/3") == (2, 3)


FILES = [
    "tests/unit/test_suite_shards.py",
    "tests/unit/test_integration_database.py",
    "tests/unit/test_ratelimit.py",
    "tests/unit/test_http_revision.py",
]
COLLECT = [sys.executable, "-m", "pytest", *FILES, "--collect-only", "-q", "-p", "no:cacheprovider"]


def _collected(*arguments):
    """Test ids that a real pytest run of four files would execute."""
    completed = subprocess.run(
        [*COLLECT, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [line for line in completed.stdout.splitlines() if "::" in line]


def test_the_option_partitions_a_real_collection():
    whole = _collected()
    first, second = _collected("--shard", "1/2"), _collected("--shard", "2/2")
    assert first and second
    assert len({suite_shards.file_of(nodeid) for nodeid in first}) == 2
    assert not set(first) & set(second)
    assert sorted(first + second) == sorted(whole)


def test_the_option_refuses_a_malformed_value():
    completed = subprocess.run(
        [*COLLECT, "--shard", "3/2"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 4
    assert "--shard expects I/N" in completed.stderr
