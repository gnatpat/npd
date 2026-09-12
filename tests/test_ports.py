import pytest

from deploy.config import parse_config
from deploy.paths import Paths
from deploy.ports import PortExhausted, allocate_port, ports_in_use
from deploy.render import render_systemd_unit


def write_unit(paths: Paths, name: str, port: int) -> None:
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file(name).write_text(
        "# Managed by deploy — edits will be overwritten\n"
        "[Service]\n"
        f"Environment=PORT={port}\n"
    )


def test_no_units_means_nothing_in_use(tmp_path):
    assert ports_in_use(Paths.under(tmp_path)) == {}


def test_reads_ports_back_out_of_units(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    write_unit(paths, "pokemon", 8151)
    assert ports_in_use(paths) == {"blog": 8080, "pokemon": 8151}


def test_exclude_hides_one_apps_own_port(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    assert ports_in_use(paths, exclude="blog") == {}


def test_allocates_the_lowest_free_port_in_range(tmp_path):
    assert allocate_port(Paths.under(tmp_path), name="new") == 8200


def test_skips_ports_already_taken(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "a", 8200)
    write_unit(paths, "b", 8201)
    assert allocate_port(paths, name="new") == 8202


def test_ports_outside_the_range_do_not_consume_range_slots(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    assert allocate_port(paths, name="new") == 8200


def test_a_pinned_port_is_returned_unchanged(tmp_path):
    assert allocate_port(Paths.under(tmp_path), name="blog", pinned=8080) == 8080


def test_a_pinned_port_that_collides_is_rejected(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "other", 8151)
    with pytest.raises(ValueError, match="8151"):
        allocate_port(paths, name="pokemon", pinned=8151)


def test_an_app_may_keep_its_own_pinned_port_on_update(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "pokemon", 8151)
    assert allocate_port(paths, name="pokemon", pinned=8151) == 8151


def test_reallocating_an_existing_app_keeps_its_port(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "app", 8205)
    assert allocate_port(paths, name="app") == 8205


def test_exhausted_range_raises(tmp_path):
    paths = Paths.under(tmp_path)
    for i, port in enumerate(range(8200, 8300)):
        write_unit(paths, f"app{i}", port)
    with pytest.raises(PortExhausted):
        allocate_port(paths, name="new")


def test_ports_in_use_with_actual_render_systemd_unit_output(tmp_path):
    """Ensure regex matches quoted form from render_systemd_unit, not just hand-built fixtures."""
    paths = Paths.under(tmp_path)
    paths.units.mkdir(parents=True, exist_ok=True)

    # Create a minimal config that render_systemd_unit will accept
    toml_text = """
[app]
name = "web"

[service]
start = "python app.py"
"""

    config = parse_config(toml_text, repo_name="test")
    unit_text = render_systemd_unit(config, 8250, paths)
    paths.systemd_unit_file("web").write_text(unit_text)

    # Verify the regex matches the quoted form
    assert ports_in_use(paths) == {"web": 8250}


def test_skips_unreadable_units_and_still_reads_good_ones(tmp_path, capsys):
    """FINDING 3: unreadable file should not abort the scan."""
    paths = Paths.under(tmp_path)
    paths.units.mkdir(parents=True, exist_ok=True)

    # Write two good units
    write_unit(paths, "good1", 8200)
    write_unit(paths, "good2", 8201)

    # Create an unreadable file with invalid UTF-8 bytes
    bad_unit = paths.systemd_unit_file("bad")
    bad_unit.write_bytes(b"\xff\xfe invalid utf-8")

    # Should still read the good ports and skip the bad one with a warning
    result = ports_in_use(paths)
    assert result == {"good1": 8200, "good2": 8201}

    # Verify a warning was printed to stderr, not stdout (stdout is reserved
    # for machine-readable output elsewhere in the tool)
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert "bad" in captured.err
    assert captured.out == ""
