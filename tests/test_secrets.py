import stat

from deploy.secrets import (
    merge_secrets,
    missing_secrets,
    read_env_file,
    write_env_file,
)


def test_reading_a_missing_file_gives_an_empty_mapping(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_round_trips_values(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"A": "1", "B": "two"})
    assert read_env_file(path) == {"A": "1", "B": "two"}


def test_env_file_is_owner_read_write_only(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"SECRET": "hunter2"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_values_containing_spaces_survive(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"MSG": "hello there"})
    assert read_env_file(path)["MSG"] == "hello there"


def test_comments_and_blank_lines_are_ignored_on_read(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("# a comment\n\nA=1\n")
    assert read_env_file(path) == {"A": "1"}


def test_missing_secrets_lists_only_the_absent_ones(tmp_path):
    declared = {"A": "desc a", "B": "desc b"}
    assert missing_secrets(declared, {"A": "set"}) == ["B"]


def test_nothing_missing_when_all_are_present():
    assert missing_secrets({"A": "d"}, {"A": "v"}) == []


def test_merge_adds_without_dropping_existing_values(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"OLD": "keep"})
    merge_secrets(path, {"NEW": "added"})
    assert read_env_file(path) == {"OLD": "keep", "NEW": "added"}


def test_merge_keeps_permissions_tight(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"OLD": "keep"})
    merge_secrets(path, {"NEW": "added"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
