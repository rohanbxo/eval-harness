"""Config hashing (SPEC 8.2).

The hash covers every file in a scenario directory so a run can prove which
definition produced it. Line endings are normalized, which matters because this
repo is developed on Windows and run in Linux containers.
"""

from __future__ import annotations

import hashlib
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


def is_dirty(repo_root: Path) -> bool:
    """Whether the working tree has uncommitted changes.

    A comparison run records the commit it ran on so results stay traceable to a
    definition. From a dirty tree that record is a lie: the commit named does not
    describe the code that ran (DECISIONS D30).
    """
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
    """The current commit, or ``"<sha>-dirty"`` when the tree has changes (SPEC 8.2)."""
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
