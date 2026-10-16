"""Replay local edits without requiring the user to commit their checkout."""
import subprocess
from pathlib import Path

import pytest

from self_heal.repository import CandidateRepository, PatchRejected


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def test_snapshot_preserves_exact_source_and_leaves_index_branch_and_secrets_alone(tmp_path):
    git(tmp_path, "init", "-q")
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "agent.py").write_text("VALUE = 1\n")
    (harness / "removed.py").write_text("OLD = True\n")
    (tmp_path / "README.md").write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@local.invalid", "commit", "-qm", "baseline")
    parent = git(tmp_path, "rev-parse", "HEAD")
    (harness / "agent.py").write_text("VALUE = 2\n")
    git(tmp_path, "add", "harness/agent.py")
    (harness / "agent.py").write_text("VALUE = 3\n")
    (tmp_path / "README.md").write_text("local unrelated edit\n")
    (tmp_path / ".env").write_text("SECRET=do-not-snapshot\n")
    (harness / "private.json").write_text('{"credential": "secret"}')
    files = {"agent.py": "VALUE = 3\n", "new_tool.py": "TOOL = True\n"}
    status = git(tmp_path, "status", "--porcelain", "--untracked-files=all")
    staged = git(tmp_path, "diff", "--cached")
    repo = CandidateRepository(tmp_path)
    commit = repo.snapshot_harness(files)
    assert commit != parent
    assert repo.snapshot_harness(files) == commit
    assert git(tmp_path, "rev-parse", "HEAD") == parent
    assert git(tmp_path, "diff", "--cached") == staged
    assert git(tmp_path, "status", "--porcelain", "--untracked-files=all") == status
    assert git(tmp_path, "show", commit + ":README.md") == "original"
    tree_paths = git(tmp_path, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert tree_paths == ["README.md", "harness/agent.py", "harness/new_tool.py"]
    (harness / "agent.py").write_text("VALUE = 999\n")
    pinned = repo.create_worktree(commit)
    assert (pinned / "harness/agent.py").read_text() == files["agent.py"]
    assert not git(pinned, "status", "--porcelain", "--untracked-files=all")


@pytest.mark.parametrize("files", [{}, {"../.env": "secret"}, {"private.json": "secret"}, {"tool.py": None}])
def test_snapshot_rejects_non_source_paths(tmp_path, files):
    with pytest.raises(PatchRejected, match="Invalid harness"):
        CandidateRepository(tmp_path).snapshot_harness(files)
