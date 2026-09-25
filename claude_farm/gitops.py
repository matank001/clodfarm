"""Git isolation: every task works in its own worktree and branch.

Top-level tasks branch from main. When one finishes, its branch is rebased onto
main and fast-forwarded in, so main only moves forward; with an ``origin``, main
is pushed after each merge.

Sub-tasks branch from their parent's branch and never touch main. Their branches
are left for the parent, which is resumed and merges them itself (it is an agent:
it resolves conflicts with the intent of both sides in view). Branches that main
already contains are deleted when a top-level task lands.
"""

from __future__ import annotations

import os
import subprocess
import threading

_lock = threading.Lock()  # one merge at a time per farm


class GitError(RuntimeError):
    pass


def git(repo: str, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def is_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def main_branch(repo: str) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def has_origin(repo: str) -> bool:
    return "origin" in git(repo, "remote", check=False).split()


def ensure_repo(repo: str, url: str | None):
    """Clone ``url`` into ``repo`` on first start, or create an empty repo."""
    if is_repo(repo):
        return
    os.makedirs(os.path.dirname(repo), exist_ok=True)
    if url:
        subprocess.run(["git", "clone", url, repo], check=True)
    else:
        os.makedirs(repo, exist_ok=True)
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "README.md"), "w") as f:
            f.write("# workspace\n\nManaged by claude-farm. Put your MISSION.md here.\n")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=claude-farm", "-c", "user.email=claude-farm@localhost",
            "commit", "-qm", "init workspace")


def worktree_for(repo: str, task_id: str, base: str | None = None) -> tuple[str, str]:
    """Create (or reuse, for a resumed task) the task's worktree, branched from ``base``
    (the parent's branch for a sub-task) or from main."""
    path = os.path.join(os.path.dirname(repo), ".worktrees", task_id)
    branch = f"farm/{task_id}"
    if os.path.isdir(path):
        return path, branch
    with _lock:
        if base is None or not git(repo, "branch", "--list", base):
            base = main_branch(repo)
            if has_origin(repo):
                git(repo, "pull", "--rebase", "-q", "origin", base, check=False)
        exists = git(repo, "branch", "--list", branch)
        if exists:
            git(repo, "worktree", "add", "-q", path, branch)
        else:
            git(repo, "worktree", "add", "-q", "-b", branch, path, base)
    return path, branch


def commit_leftovers(path: str, branch: str) -> bool:
    """Agents are told to commit; anything left over is committed for them."""
    if not git(path, "status", "--porcelain", check=False):
        return False
    git(path, "add", "-A")
    git(path, "-c", "user.name=claude-farm", "-c", "user.email=claude-farm@localhost",
        "commit", "-qm", f"{branch}: uncommitted work at end of run")
    return True


def ahead_of(repo: str, branch: str, base: str | None = None) -> int:
    base = base or main_branch(repo)
    out = git(repo, "rev-list", "--count", f"{base}..{branch}", check=False)
    return int(out) if out.isdigit() else 0


def rebase_onto_main(repo: str, path: str, branch: str):
    """Bring the task branch up to date with main (so a check sees what would land). Conflicts raise GitError."""
    commit_leftovers(path, branch)
    base = main_branch(repo)
    try:
        git(path, "rebase", "-q", base)
    except GitError as e:
        git(path, "rebase", "--abort", check=False)
        raise GitError(f"merge conflict rebasing {branch} onto {base}: {e}") from e


def run_check(path: str, cmd: str, timeout: int) -> tuple[bool, str]:
    """Run the project's check (tests, lint, build) in the worktree. Returns (passed, output tail)."""
    try:
        p = subprocess.run(["bash", "-lc", cmd], cwd=path, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        return False, f"(check timed out after {timeout}s)\n{out[-3000:]}"
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or ""))[-6000:]


def merge(repo: str, path: str, branch: str, push: bool = True) -> str:
    """Rebase the task branch onto main and fast-forward main. Returns a summary."""
    with _lock:
        base = main_branch(repo)
        commit_leftovers(path, branch)
        ahead = git(repo, "rev-list", "--count", f"{base}..{branch}")
        if ahead == "0":
            return "no commits"
        if has_origin(repo):
            git(repo, "pull", "--rebase", "-q", "origin", base, check=False)
        try:
            git(path, "rebase", "-q", base)
        except GitError as e:
            git(path, "rebase", "--abort", check=False)
            raise GitError(f"merge conflict rebasing {branch} onto {base}: {e}") from e
        git(repo, "merge", "--ff-only", "-q", branch)
        out = f"merged {ahead} commit(s) into {base}"
        if push and has_origin(repo):
            p = subprocess.run(["git", "-C", repo, "push", "-q", "origin", base], capture_output=True, text=True)
            out += "; pushed" if p.returncode == 0 else f"; push failed: {p.stderr.strip()[:200]}"
        return out


def remove_worktree(repo: str, path: str, branch: str | None = None):
    """Remove a finished task's worktree; delete its branch too when given."""
    with _lock:
        git(repo, "worktree", "remove", "--force", path, check=False)
        if branch:
            git(repo, "branch", "-D", branch, check=False)


def prune_merged(repo: str) -> int:
    """Delete claude-farm/* branches that main already contains and that no worktree uses."""
    with _lock:
        base = main_branch(repo)
        used = {l.split()[1].replace("refs/heads/", "") for l in git(repo, "worktree", "list", "--porcelain",
                check=False).splitlines() if l.startswith("branch ")}
        n = 0
        for b in git(repo, "branch", "--merged", base, "--format=%(refname:short)", check=False).split():
            if b.startswith("farm/") and b not in used:
                git(repo, "branch", "-d", b, check=False)
                n += 1
        return n
