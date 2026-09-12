from pathlib import Path

import pytest

from deploy.paths import MANAGED_HEADER, Paths
from deploy.reconcile import (
    ForeignFile,
    NginxTestFailed,
    ReloadFailed,
    apply_changes,
    plan_changes,
)
from deploy.runner import RecordingRunner

UNIT = MANAGED_HEADER + "\n[Service]\nEnvironment=PORT=8200\n"
CONF = MANAGED_HEADER + "\nlocation /x/ { proxy_pass http://127.0.0.1:8200/; }\n"


def desired(paths: Paths) -> dict:
    return {paths.unit_file("x"): UNIT, paths.nginx_file("x"): CONF}


def test_everything_is_a_change_when_nothing_exists(tmp_path):
    paths = Paths.under(tmp_path)
    changes = plan_changes(desired(paths))
    assert len(changes) == 2
    assert all(c.before is None for c in changes)


def test_applying_changes_writes_the_files(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    assert paths.unit_file("x").read_text() == UNIT
    assert paths.nginx_file("x").read_text() == CONF


def test_a_second_identical_run_plans_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    assert plan_changes(desired(paths)) == []


def test_a_second_identical_run_reloads_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(desired(paths)), runner=runner)
    assert actions == set()
    assert runner.calls == []


def test_changing_only_the_unit_does_not_reload_nginx(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"daemon-reload"}
    assert not runner.ran("nginx")


def test_changing_only_nginx_does_not_daemon_reload(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"nginx"}
    assert not runner.ran("daemon-reload")


def test_nginx_is_tested_before_it_is_reloaded(tmp_path):
    paths = Paths.under(tmp_path)
    runner = RecordingRunner()
    apply_changes(plan_changes(desired(paths)), runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any("nginx -t" in c for c in joined)
    assert joined.index(next(c for c in joined if "nginx -t" in c)) < joined.index(
        next(c for c in joined if "reload nginx" in c)
    )


def test_a_failed_nginx_test_restores_the_previous_content(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.nginx_file("x")] = MANAGED_HEADER + "\nthis is not nginx\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    assert paths.nginx_file("x").read_text() == CONF


def test_a_failed_nginx_test_does_not_reload(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.nginx_file("x")] = MANAGED_HEADER + "\nbroken\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    assert not runner.ran("reload nginx")


def test_a_file_without_the_managed_header_is_never_overwritten(tmp_path):
    paths = Paths.under(tmp_path)
    paths.units.mkdir(parents=True)
    paths.unit_file("x").write_text("[Service]\nExecStart=/hand/written\n")
    with pytest.raises(ForeignFile, match="x.service"):
        plan_changes(desired(paths))


def test_removal_is_planned_as_a_change_to_none(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changes = plan_changes({}, remove=[paths.unit_file("x"), paths.nginx_file("x")])
    assert {c.after for c in changes} == {None}
    apply_changes(changes, runner=RecordingRunner())
    assert not paths.unit_file("x").exists()


def test_dry_run_writes_nothing_and_runs_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    runner = RecordingRunner()
    apply_changes(plan_changes(desired(paths)), runner=runner, dry_run=True)
    assert not paths.unit_file("x").exists()
    assert runner.calls == []


def test_changing_only_nginx_in_a_root_path_containing_systemd(tmp_path):
    """Regression test for the substring-matching defect: classification must
    be based on file suffix, not on whether "systemd" or "nginx" appears
    anywhere in the path. Build a root whose path contains the literal
    substring "systemd" and confirm an nginx-only change still reloads only
    nginx, never daemon-reload.
    """
    root = tmp_path / "a-systemd-flavored-root"
    paths = Paths.under(root)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"nginx"}
    assert not runner.ran("daemon-reload")


def test_changing_only_a_unit_in_a_root_path_containing_nginx(tmp_path):
    """Mirror regression test: a root path containing the literal substring
    "nginx" must not cause a unit-only change to trigger an nginx reload.
    """
    root = tmp_path / "an-nginx-flavored-root"
    paths = Paths.under(root)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"daemon-reload"}
    assert not runner.ran("nginx")


# --- Fix round 1: failure-path correctness -------------------------------
#
# CRITICAL 1 regression: a failed `nginx -t` must roll back every changed
# file in the call, not only the nginx ones — otherwise a unit change left
# in its new state on disk becomes invisible to a later plan_changes (it
# already matches "desired"), so daemon-reload never runs for it and
# systemd silently keeps running the old unit forever.


def test_a_failed_nginx_test_rolls_back_all_changed_files_not_only_nginx(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    broken[paths.nginx_file("x")] = MANAGED_HEADER + "\nthis is not nginx\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    assert paths.unit_file("x").read_text() == UNIT
    assert paths.nginx_file("x").read_text() == CONF


def test_after_a_failed_nginx_test_replanning_reports_both_files_again(tmp_path):
    """The divergence must not be able to hide: once the failed apply has
    rolled everything back, plan_changes against the same (still broken)
    desired state must report both files as changes again, not just the
    nginx one."""
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    broken[paths.nginx_file("x")] = MANAGED_HEADER + "\nthis is not nginx\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    changes = plan_changes(broken)
    assert {c.path for c in changes} == {paths.unit_file("x"), paths.nginx_file("x")}


# CRITICAL 2 regression: daemon-reload failing must roll everything back
# (it happens before the user-visible nginx reload, so it is still safe to
# undo) and must never let nginx get reloaded; and a failure reloading
# nginx itself — the last, hardest-to-undo step — must surface as a domain
# exception rather than a raw CalledProcessError.


def test_a_failed_daemon_reload_rolls_back_everything_and_never_reloads_nginx(
    tmp_path,
):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner(results={"daemon-reload": 1})
    with pytest.raises(ReloadFailed):
        apply_changes(plan_changes(changed), runner=runner)
    assert paths.unit_file("x").read_text() == UNIT
    assert paths.nginx_file("x").read_text() == CONF
    assert not runner.ran("reload nginx")


def test_a_failed_nginx_reload_raises_reload_failed(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner(results={"reload nginx": 1})
    with pytest.raises(ReloadFailed):
        apply_changes(plan_changes(changed), runner=runner)


def test_reload_order_is_nginx_test_then_daemon_reload_then_nginx_reload(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner()
    apply_changes(plan_changes(changed), runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    test_i = joined.index(next(c for c in joined if "nginx -t" in c))
    daemon_i = joined.index(next(c for c in joined if "daemon-reload" in c))
    reload_i = joined.index(next(c for c in joined if "reload nginx" in c))
    assert test_i < daemon_i < reload_i


# IMPORTANT 3 regression: a write failing partway through the batch must
# roll back the writes already made in that call, not leave them on disk.


def test_a_write_failure_mid_loop_rolls_back_the_writes_already_made(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    changed[paths.nginx_file("x")] = CONF.replace("8200", "8201")
    # plan_changes sorts by path, and .../etc/deploy/systemd/... sorts before
    # .../etc/nginx/deploy.d/..., so the unit write is applied first and
    # should succeed; then the nginx write must fail and force a rollback of
    # the already-applied unit write. Force that failure in a way that is
    # not just permission bits (which root would bypass): replace the nginx
    # target with a directory, so writing to it raises IsADirectoryError
    # regardless of who runs the tests.
    changes = plan_changes(changed)
    paths.nginx_file("x").unlink()
    paths.nginx_file("x").mkdir()
    with pytest.raises(OSError):
        apply_changes(changes, runner=RecordingRunner())
    assert paths.unit_file("x").read_text() == UNIT


def test_removal_of_a_file_without_the_managed_header_is_refused(tmp_path):
    paths = Paths.under(tmp_path)
    paths.units.mkdir(parents=True)
    paths.unit_file("x").write_text("[Service]\nExecStart=/hand/written\n")
    with pytest.raises(ForeignFile, match="x.service"):
        plan_changes({}, remove=[paths.unit_file("x")])


# --- Fix round 2: _rollback must itself be fault-tolerant -----------------
#
# Important: _rollback previously had no per-file error handling, so a
# single un-restorable file would (a) abandon restoring the rest, leaving
# the half-restored split state Critical 1's fix exists to eliminate, and
# (b) let its own exception escape in place of the real NginxTestFailed /
# ReloadFailed / OSError, misleading the operator about what went wrong.


class SaboteurRunner(RecordingRunner):
    """A RecordingRunner that, at the moment `nginx -t` is invoked, replaces
    `sabotage_path` with a directory — so that when `apply_changes` later
    tries to roll it back, the restore of that one file fails with
    IsADirectoryError while every other file remains a normal, restorable
    file. Lets a test simulate "the rollback itself partially fails"
    deterministically and independent of whether the tests run as root."""

    def __init__(self, sabotage_path: Path, **kwargs):
        super().__init__(**kwargs)
        self.sabotage_path = sabotage_path

    def run(self, argv, **kwargs):
        if "nginx -t" in " ".join(argv):
            self.sabotage_path.unlink()
            self.sabotage_path.mkdir()
        return super().run(argv, **kwargs)


def _setup_unrestorable_rollback_scenario(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.unit_file("x")] = UNIT.replace("8200", "8201")
    broken[paths.nginx_file("x")] = MANAGED_HEADER + "\nthis is not nginx\n"
    runner = SaboteurRunner(paths.unit_file("x"), results={"nginx -t": 1})
    return paths, broken, runner


def test_a_rollback_failure_does_not_abandon_restoring_other_files(tmp_path):
    paths, broken, runner = _setup_unrestorable_rollback_scenario(tmp_path)
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    # The unit file could not be restored (sabotaged into a directory), but
    # that must not stop the nginx file — which could be restored — from
    # actually being restored.
    assert paths.nginx_file("x").read_text() == CONF


def test_a_rollback_failure_does_not_replace_the_original_exception(tmp_path):
    paths, broken, runner = _setup_unrestorable_rollback_scenario(tmp_path)
    with pytest.raises(NginxTestFailed) as excinfo:
        apply_changes(plan_changes(broken), runner=runner)
    # The operator must still learn that nginx rejected the config, not
    # merely that some unrelated restore failed.
    assert "nginx -t failed" in str(excinfo.value)
    # ...and, since it needs a human anyway, the primary error should also
    # name the path that could not be restored.
    assert str(paths.unit_file("x")) in str(excinfo.value)


def test_a_rollback_failure_names_the_unrestorable_path_on_stderr(tmp_path, capsys):
    paths, broken, runner = _setup_unrestorable_rollback_scenario(tmp_path)
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert str(paths.unit_file("x")) in captured.err
