import pytest

from npd.paths import Paths
from npd.static import live_target, publish, published_commit, published_paths


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


def test_rejects_live_name_as_a_real_directory(tmp_path):
    """FINDING 4: live name pre-existing as directory should raise with clear error."""
    paths = Paths.under(tmp_path)
    # Pre-create the live name as a real directory
    live_dir = paths.static / "boggle"
    live_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="is a real directory"):
        publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)


def test_rejects_static_dir_as_a_regular_file(tmp_path):
    """FINDING 4: paths.static as regular file should raise with clear error."""
    paths = Paths.under(tmp_path)
    # Pre-create paths.static as a regular file
    paths.static.parent.mkdir(parents=True, exist_ok=True)
    paths.static.write_text("not a directory")

    with pytest.raises(ValueError, match="is a regular file"):
        publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)


def test_published_paths_finds_the_live_link_and_every_build_dir(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v2"), name="boggle", commit="bbb", paths=paths)
    found = {p.name for p in published_paths("boggle", paths)}
    assert found == {"boggle", "boggle-aaa", "boggle-bbb"}


def test_published_paths_does_not_match_a_similarly_prefixed_app(tmp_path):
    """MINOR 7's care requirement: --purge must only ever touch paths for
    the exact app being removed, never a differently-named one that merely
    shares a prefix."""
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v1"), name="boggle-admin", commit="bbb", paths=paths)
    found = {p.name for p in published_paths("boggle", paths)}
    assert found == {"boggle", "boggle-aaa"}


def test_published_paths_is_empty_when_nothing_is_published(tmp_path):
    assert published_paths("boggle", Paths.under(tmp_path)) == []


def test_non_hex_commit_is_rejected_before_touching_the_filesystem(tmp_path):
    paths = Paths.under(tmp_path)
    with pytest.raises(ValueError, match="not a valid hex string"):
        publish(
            build(tmp_path, "v1"),
            name="boggle",
            commit="../../etc/evil",
            paths=paths,
        )
    assert not paths.static.exists()


def test_publishing_prunes_builds_older_than_the_previous_one(tmp_path):
    paths = Paths.under(tmp_path)
    for commit in ("aaa", "bbb", "ccc"):
        publish(build(tmp_path, commit), name="boggle", commit=commit, paths=paths)
    assert not (paths.static / "boggle-aaa").exists()
    assert (paths.static / "boggle-bbb" / "index.html").read_text() == "bbb"
    assert (paths.static / "boggle" / "index.html").read_text() == "ccc"


def test_republishing_the_live_commit_keeps_the_previous_build(tmp_path):
    paths = Paths.under(tmp_path)
    for commit in ("aaa", "bbb", "bbb"):
        publish(build(tmp_path, commit), name="boggle", commit=commit, paths=paths)
    assert (paths.static / "boggle-aaa").is_dir()


def test_pruning_leaves_a_sibling_apps_builds_alone(tmp_path):
    # "boggle-add" is itself valid hex after "boggle-", so this is the name
    # most likely to be mistaken for one of boggle's own builds.
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "sib"), name="boggle-add", commit="aaa", paths=paths)
    for commit in ("bbb", "ccc", "ddd"):
        publish(build(tmp_path, commit), name="boggle", commit=commit, paths=paths)
    assert (paths.static / "boggle-add-aaa").is_dir()
    assert (paths.static / "boggle-add" / "index.html").read_text() == "sib"


def test_published_commit_is_read_from_the_live_link(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="abc123", paths=paths)
    assert published_commit("boggle", paths) == "abc123"


def test_published_commit_is_none_when_nothing_is_published(tmp_path):
    assert published_commit("boggle", Paths.under(tmp_path)) is None
