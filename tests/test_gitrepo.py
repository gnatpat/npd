import subprocess

import pytest

from deploy.gitrepo import (
    DirtyRepo,
    clone,
    head_commit,
    is_dirty,
    pull_ff_only,
    remote_url,
    repo_url,
)
from deploy.runner import RealRunner

RUNNER = RealRunner()


def git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "one")
    return origin


def test_short_name_expands_to_the_github_account():
    assert repo_url("pokemon") == "git@github.com:gnatpat/pokemon.git"


def test_a_full_url_is_passed_through():
    url = "git@github.com:someone/else.git"
    assert repo_url(url) == url


def test_an_https_url_is_passed_through():
    url = "https://github.com/someone/else.git"
    assert repo_url(url) == url


def test_clone_then_read_back_the_remote(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert remote_url(dest, runner=RUNNER) == str(origin)


def test_head_commit_is_a_full_sha(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    sha = head_commit(dest, runner=RUNNER)
    assert len(sha) == 40


def test_a_clean_clone_is_not_dirty(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert is_dirty(dest, runner=RUNNER) is False


def test_an_edited_file_makes_the_repo_dirty(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    (dest / "README.md").write_text("edited\n")
    assert is_dirty(dest, runner=RUNNER) is True


def test_pull_reports_false_when_there_is_nothing_new(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert pull_ff_only(dest, runner=RUNNER) is False


def test_pull_reports_true_and_advances_when_upstream_moves(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    before = head_commit(dest, runner=RUNNER)
    (origin / "README.md").write_text("two\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "two")
    assert pull_ff_only(dest, runner=RUNNER) is True
    assert head_commit(dest, runner=RUNNER) != before


def test_pull_refuses_to_touch_a_dirty_tree(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    (dest / "README.md").write_text("local edit\n")
    with pytest.raises(DirtyRepo):
        pull_ff_only(dest, runner=RUNNER)
    assert (dest / "README.md").read_text() == "local edit\n"
