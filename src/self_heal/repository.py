"""Pin model-proposed harness edits without changing the operator checkout."""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class PatchRejected(ValueError):
    pass


_FORBIDDEN = re.compile(
    r"(?i)(eval-bulk|small-inventory|SKU-\d{3,}|case_[a-f0-9]{12,}|"
    r"from\s+evals\b|import\s+evals\b|\boracle\b|\bsqlite3\b|"
    r"os\.environ|subprocess|\bsocket\b|DAYTONA_API_KEY|OPENROUTER_API_KEY|PHOENIX_API_KEY)"
)


@dataclass(frozen=True)
class CandidateSource:
    parent_commit: str
    candidate_commit: str
    worktree: Path
    diff: str
    changed_paths: tuple[str, ...]


class CandidateRepository:
    def __init__(self, repository: Path, worktrees: Path | None = None) -> None:
        self.repository = repository.resolve()
        self.worktrees = (worktrees or self.repository / ".self-heal" / "worktrees").resolve()

    def _git(self, *args: str, cwd: Path | None = None, input: str | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.repository, input=input,
            text=True, capture_output=True, timeout=30, check=False,
        )
        if result.returncode:
            raise PatchRejected(f"Git operation failed: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    def resolve_commit(self, ref: str) -> str:
        if not ref or ref == "unknown":
            raise PatchRejected("Observed run has no pinned source commit")
        commit = self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise PatchRejected("Baseline commit is invalid")
        return commit

    def create_worktree(self, commit: str) -> Path:
        commit = self.resolve_commit(commit)
        self.worktrees.mkdir(parents=True, exist_ok=True)
        path = self.worktrees / uuid.uuid4().hex
        self._git("worktree", "add", "--detach", str(path), commit)
        return path

    def active_checkout(self, commit: str) -> Path:
        commit = self.resolve_commit(commit)
        path = self.repository / ".self-heal" / "active" / commit
        if path.exists():
            if path.is_symlink() or self._git("rev-parse", "HEAD", cwd=path) != commit:
                raise PatchRejected("Active checkout does not match its pinned commit")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", str(path), commit)
        return path

    def apply_proposal(self, parent: str, diff: str) -> CandidateSource:
        parent = self.resolve_commit(parent)
        if not isinstance(diff, str) or not diff.strip() or len(diff) > 120_000:
            raise PatchRejected("Proposal must contain a bounded unified diff")
        headers = re.findall(r"^diff --git a/(\S+) b/(\S+)$", diff, re.MULTILINE)
        if not headers:
            headers = re.findall(r"^--- a/(\S+)\n\+\+\+ b/(\S+)$", diff, re.MULTILINE)
        if not headers or any(a != b or not a.startswith("harness/") or not a.endswith(".py") for a, b in headers):
            raise PatchRejected("Only Python files under harness/ may change")
        if "GIT binary patch" in diff or re.search(r"^(?:old mode|new mode|deleted file mode|rename from|rename to)", diff, re.MULTILINE):
            raise PatchRejected("Binary, deletion, rename, and mode changes are forbidden")
        added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        if _FORBIDDEN.search(added):
            raise PatchRejected("Proposal contains fixture identifiers or protected access")
        path = self.create_worktree(parent)
        try:
            self._git("apply", "--check", "--index", "--recount", "-", cwd=path, input=diff)
            self._git("apply", "--index", "--recount", "-", cwd=path, input=diff)
            self._git("diff", "--cached", "--check", cwd=path)
            changes = self._git("diff", "--cached", "--name-status", cwd=path).splitlines()
            paths: list[str] = []
            for change in changes:
                status, name = change.split("\t", 1)
                target = path / name
                if status not in {"M", "A"} or not name.startswith("harness/") or not name.endswith(".py"):
                    raise PatchRejected("Patch changed an out-of-scope file")
                if target.is_symlink() or not target.resolve().is_relative_to((path / "harness").resolve()):
                    raise PatchRejected("Patch contains a path or symlink escape")
                paths.append(name)
            if not paths:
                raise PatchRejected("Proposal made no source change")
            self._git(
                "-c", "user.name=Self-Heal", "-c", "user.email=self-heal@local.invalid",
                "commit", "-m", "Self-Heal candidate", cwd=path,
            )
            candidate = self._git("rev-parse", "HEAD", cwd=path)
            return CandidateSource(parent, candidate, path, diff, tuple(paths))
        except Exception:
            self._git("worktree", "remove", "--force", str(path))
            raise

    def inspect(self, source: CandidateSource) -> None:
        if self._git("rev-parse", "HEAD", cwd=source.worktree) != source.candidate_commit:
            raise PatchRejected("Candidate checkout changed after commit")
        if self._git("rev-parse", "HEAD^", cwd=source.worktree) != source.parent_commit:
            raise PatchRejected("Candidate parent changed")
        if self._git("status", "--porcelain", "--untracked-files=all", cwd=source.worktree):
            raise PatchRejected("Candidate checkout is dirty")
        names = tuple(self._git("diff", "--name-only", "HEAD^", "HEAD", cwd=source.worktree).splitlines())
        if names != source.changed_paths or any(not p.startswith("harness/") for p in names):
            raise PatchRejected("Candidate commit differs from screened patch")
