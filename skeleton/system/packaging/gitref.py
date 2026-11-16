"""Export the committed tree of an app at a git revision (#519), for build and doctor.

``git archive`` honours ``export-ignore``: the package holds exactly what is committed and
marked for export, never an uncommitted file of the working directory. ``git`` runs with an
argument list (no shell), the revision is checked against an allowlist, and the tar is unpacked
into a private temporary directory with ``tarfile``'s ``data`` filter, refusing any link or
special file outright.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .errors import _fail

GIT_REF = re.compile(r"[A-Za-z0-9._/@][A-Za-z0-9._/@-]{0,199}\Z")
_TIMEOUT_S = 120


def _git(root: Path, *arguments: str, stdout=None) -> str:
    executable = shutil.which("git")
    if executable is None:
        _fail("git is not available on PATH; --git-ref needs it.")
    try:
        result = subprocess.run(  # noqa: S603 - git from PATH, argument list, allowlisted revision, no shell
            [executable, "-C", str(root), *arguments],
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(f"git could not run: {exc.__class__.__name__}.")
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", "replace").strip() if stdout is None else "ok"


def _safe_members(archive: tarfile.TarFile):
    for member in archive.getmembers():
        if not (member.isfile() or member.isdir()):
            _fail(f"Git tree contains a link or special file: {member.name}")
        parts = member.name.replace("\\", "/").split("/")
        if member.name.startswith(("/", "\\")) or ":" in parts[0] or ".." in parts:
            _fail(f"Git tree contains a path outside the destination: {member.name}")
        yield member


def export_git_tree(root, ref, destination) -> str:
    """Unpack the tree of ``root`` (a directory inside a git repository) at ``ref`` into ``destination``.

    ``root`` may be a subdirectory of the repository: only that subtree is exported, with its
    own ``.gitattributes``. Returns the full commit hash.
    """
    root = Path(root).resolve()
    destination = Path(destination)
    if not isinstance(ref, str) or not GIT_REF.fullmatch(ref):
        _fail("Invalid --git-ref: use letters, digits and . _ / @ - only, never starting with a hyphen.")
    if not root.is_dir():
        _fail(f"Application root is not a directory: {root}")
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        _fail(f"Application root is not inside a git repository: {root}")
    commit = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        _fail(f"Unknown git revision: {ref}")
    prefix = _git(root, "rev-parse", "--show-prefix").rstrip("/")
    tree = f"{commit}:{prefix}" if prefix else commit
    # From a subdirectory git archive keeps only that directory of the tree-ish: run at the top level.
    top = Path(_git(root, "rev-parse", "--show-toplevel") or root)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skeleton-git-archive-") as scratch:
        tar_path = Path(scratch) / "tree.tar"
        with tar_path.open("xb") as handle:
            if not _git(top, "archive", "--format=tar", tree, stdout=handle):
                _fail(f"git archive failed for {ref}.")
        try:
            with tarfile.open(tar_path, mode="r:") as archive:
                archive.extractall(destination, members=list(_safe_members(archive)), filter="data")
        except (tarfile.TarError, OSError) as exc:
            _fail(f"Git tree could not be extracted: {exc}")
    return commit


@contextlib.contextmanager
def exported_git_tree(root, ref):
    """Yield ``(path, commit)`` of a private temporary export, removed on exit."""
    with tempfile.TemporaryDirectory(prefix="skeleton-git-ref-") as directory:
        # Same directory name as the checkout: the package name of an app repository.
        target = Path(directory) / Path(root).resolve().name
        commit = export_git_tree(root, ref, target)
        yield target, commit
