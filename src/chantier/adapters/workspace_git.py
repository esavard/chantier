"""Git workspace: the factory works in its own clone, never in the deliverable's working copy.

Layout under the factory home:

    workspaces/<project>/mirror/            clone of the deliverable repo (factory-owned)
    workspaces/<project>/worktrees/<stem>/  one worktree per ticket, on branch chantier/<stem>

The deliverable repo only ever receives branches (and base-branch merges when
the merge gate is automatic). No side-car, no factory files in the deliverable.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections import defaultdict
from functools import wraps
from pathlib import Path

from chantier.domain.model import Ticket

# Agent tool artifacts that must never reach a deliverable.
EXCLUDES = (".chantier-prompt.md",)
IDENTITY = ("-c", "user.name=chantier", "-c", "user.email=chantier@localhost")


class GitError(RuntimeError):
    pass


def _serialized(method):
    """Mirror operations of one project run one at a time: the scheduler's workers and
    the sync loop share the mirror's index, refs and worktree list."""

    @wraps(method)
    def wrapper(self, project, *args, **kwargs):
        with self._locks[project]:
            return method(self, project, *args, **kwargs)

    return wrapper


class GitWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)

    # ------------------------------------------------------------------ paths

    def _mirror(self, project: str) -> Path:
        return self.root / project / "mirror"

    def _worktree(self, project: str, ticket: Ticket) -> Path:
        return self.root / project / "worktrees" / ticket.id.split("/", 1)[-1]

    # ------------------------------------------------------------------ base branch

    @_serialized
    def sync(self, project: str, repo: str, base_branch: str) -> Path:
        mirror = self._mirror(project)
        if not (mirror / ".git").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _git(mirror.parent, "clone", "--quiet", _resolve_repo(repo), mirror.name)
            exclude = mirror / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:
                fh.write("\n".join(EXCLUDES) + "\n")
        else:
            _git(mirror, "fetch", "--quiet", "--prune", "origin")
        if not _ok(mirror, "rev-parse", "--verify", "--quiet", f"origin/{base_branch}"):
            raise GitError(f"{project}: base branch '{base_branch}' not found on the remote (empty repo?)")
        _git(mirror, "checkout", "--quiet", "-B", base_branch, f"origin/{base_branch}")
        return mirror

    def read_base_file(self, project: str, base_branch: str, path: str) -> str | None:
        code, out = _run(self._mirror(project), "show", f"origin/{base_branch}:{path}")
        return out if code == 0 else None

    def list_base_files(self, project: str, base_branch: str, directory: str) -> list[str]:
        code, out = _run(self._mirror(project), "ls-tree", "--name-only", f"origin/{base_branch}",
                         directory.rstrip("/") + "/")
        return out.splitlines() if code == 0 else []

    # ------------------------------------------------------------------ ticket worktrees

    @_serialized
    def prepare_worktree(self, project: str, ticket: Ticket, base_branch: str, reset: bool = False) -> Path:
        mirror = self._mirror(project)
        path = self._worktree(project, ticket)
        branch = ticket.branch or f"chantier/{path.name}"
        if path.exists() and not (path / ".git").exists():
            shutil.rmtree(path)
        if not path.exists():
            _git(mirror, "worktree", "prune")
            path.parent.mkdir(parents=True, exist_ok=True)
            if _ok(mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
                _git(mirror, "worktree", "add", "--quiet", str(path), branch)
            else:
                _git(mirror, "worktree", "add", "--quiet", "-b", branch, str(path), f"origin/{base_branch}")
        if reset:
            _git(path, "reset", "--quiet", "--hard", f"origin/{base_branch}")
            _git(path, "clean", "-fdq")
        return path

    def commit_all(self, worktree: Path, message: str) -> bool:
        _git(worktree, "add", "-A")
        if _ok(worktree, "diff", "--cached", "--quiet"):
            return False
        _git(worktree, *IDENTITY, "commit", "--quiet", "--no-verify", "-m", message)
        return True

    def changed_files(self, worktree: Path, base_branch: str) -> list[str]:
        out = _git(worktree, "diff", "--name-only", f"origin/{base_branch}...HEAD")
        return [line for line in out.splitlines() if line]

    def diff(self, worktree: Path, base_branch: str) -> str:
        return _git(worktree, "diff", f"origin/{base_branch}...HEAD")

    def read_file(self, worktree: Path, path: str) -> str | None:
        file = worktree / path
        return file.read_text(encoding="utf-8") if file.is_file() else None

    @_serialized
    def deliver(self, project: str, worktree: Path, branch: str) -> None:
        _git(worktree, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")

    @_serialized
    def merge(self, project: str, branch: str, base_branch: str) -> None:
        mirror = self._mirror(project)
        _git(mirror, "fetch", "--quiet", "origin")
        if self.is_merged(project, branch, base_branch):
            return
        _git(mirror, "checkout", "--quiet", "-B", base_branch, f"origin/{base_branch}")
        _git(mirror, *IDENTITY, "merge", "--quiet", "--no-ff", "-m", f"Merge {branch} (chantier)", branch)
        try:
            _git(mirror, "push", "--quiet", "origin", base_branch)
        except GitError as exc:
            _git(mirror, "reset", "--quiet", "--hard", f"origin/{base_branch}")
            raise GitError(
                f"could not push {base_branch}: {exc}. If the deliverable is a local non-bare repo with "
                f"{base_branch} checked out, run `git config receive.denyCurrentBranch updateInstead` in it, "
                "or merge the branch yourself (chantier detects the merge)."
            ) from exc

    @_serialized
    def is_merged(self, project: str, branch: str, base_branch: str) -> bool:
        mirror = self._mirror(project)
        tip = f"refs/heads/{branch}"
        if not _ok(mirror, "rev-parse", "--verify", "--quiet", tip):
            tip = f"origin/{branch}"
            if not _ok(mirror, "rev-parse", "--verify", "--quiet", tip):
                return False
        return _ok(mirror, "merge-base", "--is-ancestor", tip, f"origin/{base_branch}")

    @_serialized
    def remove_worktree(self, project: str, ticket: Ticket) -> None:
        path = self._worktree(project, ticket)
        if path.exists():
            _run(self._mirror(project), "worktree", "remove", "--force", str(path))


def _resolve_repo(repo: str) -> str:
    candidate = Path(repo).expanduser()
    return str(candidate.resolve()) if candidate.exists() else repo


def _run(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def _git(cwd: Path, *args: str) -> str:
    code, out = _run(cwd, *args)
    if code != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {out.strip()}")
    return out


def _ok(cwd: Path, *args: str) -> bool:
    return _run(cwd, *args)[0] == 0
