"""Deterministic slices of the test suite for parallel CI jobs (``pytest --shard I/N``).

The slices are made of whole test files: sorted by path and dealt round-robin, so neighbours in a
directory land in different slices and every slice gets a similar mix. No list of files is kept
anywhere: a new test file, or a new package with tests, joins a slice by itself. For a given set
of collected files, the N slices are disjoint and together are the whole suite.
"""

import re

_SHARD = re.compile(r"([1-9][0-9]{0,2})/([1-9][0-9]{0,2})")


def parse_shard(value):
    """``"2/3"`` gives ``(2, 3)``: the second of three slices."""
    match = _SHARD.fullmatch(value or "")
    if not match or int(match[1]) > int(match[2]):
        raise ValueError(f"--shard expects I/N with 1 <= I <= N, for example 1/2; got {value!r}.")
    return int(match[1]), int(match[2])


def file_of(nodeid):
    """Test file of a pytest node id, with forward slashes on every platform."""
    return nodeid.split("::", 1)[0].replace("\\", "/")


def shard_files(files, total):
    """Slice number (1..total) of each test file."""
    return {path: position % total + 1 for position, path in enumerate(sorted(set(files)))}


def split_shard(nodeids, index, total):
    """Node ids kept by slice ``index`` of ``total`` and the ones left to the other slices, in order."""
    assignment = shard_files((file_of(nodeid) for nodeid in nodeids), total)
    selected, deselected = [], []
    for nodeid in nodeids:
        (selected if assignment[file_of(nodeid)] == index else deselected).append(nodeid)
    return selected, deselected
