import pytest

from deploy.paths import Paths
from deploy.static import live_target, publish


def build(tmp_path, content: str):
    src = tmp_path / "build-output"
    src.mkdir(exist_ok=True)
    (src / "index.html").write_text(content)
    return src


def test_publish_creates_a_versioned_directory(tmp_path):
    paths = Paths.under(tmp_path)
    target = publish(build(tmp_path, "v1"), name="boggle", commit="abc123", paths=paths)
    assert target.name == "boggle-abc123"
    assert (target / "index.html").read_text() == "v1"


def test_publish_points_the_live_symlink_at_the_new_build(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="abc123", paths=paths)
    live = paths.static / "boggle"
    assert live.is_symlink()
    assert (live / "index.html").read_text() == "v1"


def test_publishing_again_swaps_the_symlink(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v2"), name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v2"


def test_the_previous_build_is_retained_for_rollback(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v2"), name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle-aaa" / "index.html").read_text() == "v1"


def test_live_target_reports_the_current_commit(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    assert live_target("boggle", paths).name == "boggle-aaa"


def test_live_target_is_none_when_nothing_is_published(tmp_path):
    assert live_target("boggle", Paths.under(tmp_path)) is None


def test_republishing_the_same_commit_replaces_the_directory(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v1-rebuilt"), name="boggle", commit="aaa", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v1-rebuilt"


def test_a_missing_build_output_is_an_error_and_leaves_live_alone(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    with pytest.raises(FileNotFoundError):
        publish(tmp_path / "nope", name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v1"
