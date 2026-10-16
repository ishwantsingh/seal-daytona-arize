import subprocess

import pytest

from self_heal.repository import CandidateRepository, PatchRejected


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "tools.py").write_text("VALUE = 1\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "analyst.yaml").write_text("limit: 8\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@local.invalid", "commit", "-qm", "base")
    return CandidateRepository(tmp_path, tmp_path / "worktrees"), git(tmp_path, "rev-parse", "HEAD")


def test_candidate_patch_is_committed_in_isolated_worktree(tmp_path):
    repo, parent = repository(tmp_path)
    diff = """diff --git a/harness/tools.py b/harness/tools.py
--- a/harness/tools.py
+++ b/harness/tools.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    candidate = repo.apply_proposal(parent, diff)
    repo.inspect(candidate)
    assert candidate.candidate_commit != parent
    assert (candidate.worktree / "harness" / "tools.py").read_text() == "VALUE = 2\n"
    assert (tmp_path / "harness" / "tools.py").read_text() == "VALUE = 1\n"
    assert git(candidate.worktree, "rev-parse", "HEAD^") == parent


def test_standard_unified_patch_is_screened_and_applied(tmp_path):
    repo, parent = repository(tmp_path)
    diff = """--- a/harness/tools.py
+++ b/harness/tools.py
@@ -1,2 +1,2 @@
-VALUE = 1
+VALUE = 3
"""
    candidate = repo.apply_proposal(parent, diff)
    assert (candidate.worktree / "harness" / "tools.py").read_text() == "VALUE = 3\n"


@pytest.mark.parametrize("path,addition", [
    ("config/analyst.yaml", "limit: 100"),
    ("harness/tools.py", "ANSWER = 'small-inventory-v1'"),
    ("harness/../config/analyst.yaml", "limit: 100"),
])
def test_out_of_scope_and_fixture_patches_are_rejected(tmp_path, path, addition):
    repo, parent = repository(tmp_path)
    diff = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+{addition}\n"
    with pytest.raises(PatchRejected):
        repo.apply_proposal(parent, diff)
