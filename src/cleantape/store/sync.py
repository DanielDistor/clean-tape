"""Sync the state directory with the orphan ``data`` branch.

The branch holds exactly one commit: each push replaces it (force-push of a fresh orphan
commit), so git history never grows however many runs there are.

Auth: locally the user's git credentials are used. In GitHub Actions, ``actions/checkout``
stores an auth header in the main repo's local git config; we copy it into the state repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cleantape.logging_setup import get_logger

log = get_logger("sync")
BRANCH = "data"
AUTH_KEY = "http.https://github.com/.extraheader"


class SyncError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and p.returncode != 0:
        raise SyncError(f"git {' '.join(args[:3])} failed: {p.stderr.strip()[:400]}")
    return p


def origin_url(repo: Path) -> str:
    return _git(["remote", "get-url", "origin"], repo).stdout.strip()


def _prepare(repo: Path, state_dir: Path) -> None:
    """Make ``state_dir`` its own git repo pointing at the main repo's origin."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if not (state_dir / ".git").exists():
        _git(["init", "-q"], state_dir)
        _git(["remote", "add", "origin", origin_url(repo)], state_dir)
    header = _git(["config", "--get", AUTH_KEY], repo, check=False).stdout.strip()
    if header:
        _git(["config", AUTH_KEY, header], state_dir)


def branch_exists(repo: Path) -> bool:
    p = _git(["ls-remote", "--heads", "origin", BRANCH], repo, check=False)
    return p.returncode == 0 and bool(p.stdout.strip())


def pull(repo: Path, state_dir: Path) -> None:
    """Replace ``state_dir`` contents with the remote ``data`` branch."""
    if not branch_exists(repo):
        raise SyncError(
            f"remote branch '{BRANCH}' does not exist. Bootstrap it: "
            "`make bootstrap` then `scripts/push_state.sh` (see docs/RUNBOOK.md)"
        )
    if state_dir.exists():
        shutil.rmtree(state_dir)
    _prepare(repo, state_dir)
    _git(["fetch", "-q", "--depth", "1", "origin", BRANCH], state_dir)
    _git(["checkout", "-q", "-B", BRANCH, "FETCH_HEAD"], state_dir)
    log.info("pulled data branch into %s", state_dir)


def push(repo: Path, state_dir: Path, message: str) -> None:
    """Force-push ``state_dir`` as a single orphan commit on ``data``."""
    if not (state_dir / "curated" / "observations.parquet").exists():
        raise SyncError(f"refusing to push: {state_dir} has no observations (empty state)")
    _prepare(repo, state_dir)
    _git(["checkout", "-q", "--orphan", "tmp-state"], state_dir, check=False)
    _git(["add", "-A", "."], state_dir)
    ident = [
        "-c",
        "user.name=clean-tape-bot",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
    ]
    _git([*ident, "commit", "-q", "-m", message, "--allow-empty"], state_dir)
    _git(["branch", "-M", BRANCH], state_dir)
    _git(["push", "-q", "-f", "origin", f"{BRANCH}:{BRANCH}"], state_dir)
    log.info("pushed state to origin/%s (single commit)", BRANCH)
