"""Deterministic slices of the test suite for parallel CI jobs (``pytest --shard I/N``).

The slices are made of whole test files and are balanced by cost (#757): a test that asks for
``skeleton_db`` recreates the schema and costs seconds, a test without a database costs
milliseconds, so a file weighs what its tests cost and each file goes, heaviest first, to the
lightest slice so far. No list of files or of timings is kept anywhere: the cost comes from the
collection itself, and a new test file, or a new package with tests, joins a slice by itself.
For a given set of collected tests, the N slices are disjoint and together are the whole suite.
"""

import re

_SHARD = re.compile(r"([1-9][0-9]{0,2})/([1-9][0-9]{0,2})")
# Cost of a test that recreates schema skeleton, in units of a test without a database. In the CI
# of 2026-11-23 a database test took 2 to 3 s of a worker; a test without one, from milliseconds
# to the 2 s of the few that start a process. The exact ratio matters little: the database tests
# are over 95% of the load of the suite either way.
DATABASE_COST = 25


def parse_shard(value):
    """``"2/3"`` gives ``(2, 3)``: the second of three slices."""
    match = _SHARD.fullmatch(value or "")
    if not match or int(match[1]) > int(match[2]):
        raise ValueError(f"--shard expects I/N with 1 <= I <= N, for example 1/2; got {value!r}.")
    return int(match[1]), int(match[2])


def file_of(nodeid):
    """Test file of a pytest node id, with forward slashes on every platform."""
    return nodeid.split("::", 1)[0].replace("\\", "/")


def shard_files(files, total, weights=None):
    """Slice number (1..total) of each test file; ``weights`` maps a file to its cost (default 1).

    Heaviest file first, each to the lightest slice so far (ties: the path, then the lowest
    slice), so the result depends only on the set of files and their weights. Two slices never
    differ by more than the heaviest file.
    """
    weights = weights or {}
    load = [0] * total
    assignment = {}
    for path in sorted(set(files), key=lambda path: (-weights.get(path, 1), path)):
        lightest = min(range(total), key=lambda position: (load[position], position))
        assignment[path] = lightest + 1
        load[lightest] += weights.get(path, 1)
    return assignment


def file_weights(nodeids, costs=None):
    """Weight of each test file: the sum of the costs of its tests (``costs``: node id to cost, default 1)."""
    costs = costs or {}
    weights = {}
    for nodeid in nodeids:
        path = file_of(nodeid)
        weights[path] = weights.get(path, 0) + costs.get(nodeid, 1)
    return weights


def split_shard(nodeids, index, total, costs=None):
    """Node ids kept by slice ``index`` of ``total`` and the ones left to the other slices, in order."""
    weights = file_weights(nodeids, costs)
    assignment = shard_files(weights, total, weights)
    selected, deselected = [], []
    for nodeid in nodeids:
        (selected if assignment[file_of(nodeid)] == index else deselected).append(nodeid)
    return selected, deselected
