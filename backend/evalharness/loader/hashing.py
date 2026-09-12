"""Config hashing (SPEC 8.2).

The hash covers every file in a scenario directory so a run can prove which
definition produced it. Line endings are normalized, which matters because this
repo is developed on Windows and run in Linux containers.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

#: Artifacts that are not part of the scenario definition.
IGNORED_PARTS: frozenset[str] = frozenset({"__pycache__", ".pytest_cache", ".DS_Store"})


def _is_ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts) or path.name.endswith(".pyc")


def iter_scenario_files(directory: Path) -> list[Path]:
    """Every definition file in the directory, sorted by POSIX-style relative path."""
    files = [p for p in directory.rglob("*") if p.is_file() and not _is_ignored(p)]
    return sorted(files, key=lambda p: p.relative_to(directory).as_posix())


def normalize_bytes(raw: bytes) -> bytes:
    """CRLF/CR -> LF, and strip a UTF-8 BOM, so the hash is platform-independent."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def compute_config_hash(directory: Path) -> str:
    """SHA-256 over the canonicalized contents of every file in the directory.

    Both the relative path and the normalized content of each file feed the
    digest, so a rename changes the hash even when content is untouched.
    """
    digest = hashlib.sha256()
    for path in iter_scenario_files(directory):
        rel = path.relative_to(directory).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalize_bytes(path.read_bytes()))
        digest.update(b"\0")
    return digest.hexdigest()


#: Commit stamped into the image at build time (DECISIONS D41).
#:
#: A container has no .git and no git binary, so asking git inside one always
#: fails -- which made `git_commit` return "unknown" and, far worse, made
#: `is_dirty` return False through its "not a checkout" path. The dirty-tree
#: guard therefore could not fire on the API path, which is the path every
#: comparison run uses. The build stamps the answer in instead.
COMMIT_ENV = "EVALHARNESS_GIT_COMMIT"


def _stamped_commit() -> str | None:
    """The build-time commit, or None when nothing stamped one."""
    value = os.environ.get(COMMIT_ENV, "").strip()
    return value or None


def provenance_is_known(repo_root: Path) -> bool:
    """Whether the commit this code came from can be established at all.

    False means neither a stamp nor a usable git checkout. Callers must refuse
    to launch a comparison run rather than record "unknown" and continue: a run
    whose code cannot be identified is not comparable to one whose can (D41).
    """
    if _stamped_commit() is not None:
        return True
    try:
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def is_dirty(repo_root: Path) -> bool:
    """Whether the working tree has uncommitted changes.

    A comparison run records the commit it ran on so results stay traceable to a
    definition. From a dirty tree that record is a lie: the commit named does not
    describe the code that ran (DECISIONS D30).
    """
    stamped = _stamped_commit()
    if stamped is not None:
        # The image was built from a tree the builder described; trust that
        # rather than asking git inside a container that has none.
        return stamped.endswith("-dirty")
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False  # not a git checkout: nothing to be dirty about
    return bool(status)


def git_commit(repo_root: Path) -> str:
    """The current commit, or ``"<sha>-dirty"`` when the tree has changes (SPEC 8.2).

    Prefers the build-time stamp: inside a container there is no git to ask, and
    silently answering "unknown" is what let unidentifiable runs be recorded as
    if they were traceable (D41).
    """
    stamped = _stamped_commit()
    if stamped is not None:
        return stamped
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{sha}-dirty" if status else sha
