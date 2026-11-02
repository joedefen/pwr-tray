"""Exercise the --svc-* verbs without a systemd session.

The verbs are deliberately split into policy (``plan_*`` returns the steps) and
effect (``execute`` performs them), so every decision here can be asserted by
argv instead of by poking a live user manager: what gets written, in which
order, what is *not* called, and what happens when the answer is no.

The last tests drive the real ``main()`` argparse wiring, so a flag that is
renamed or made mutually exclusive is caught here rather than by a user.
"""

import configparser
import os
import sys
from pathlib import Path

import pytest

from pwr_tray import service
from pwr_tray.IniTool import parse_service_allowed
from pwr_tray.Utils import SingleInstance

DESKTOP = 'niri-wayland'
#: What the shipped unit runs, verbatim: the launcher, never an install.
LAUNCHER = '%h/.local/bin/pwr-tray'


class FakeSystemctl:
    """Records each systemctl call and answers the state queries."""

    QUERIES = ('is-enabled', 'is-active', 'is-failed')

    def __init__(self, enabled=False, active=False, failed=False, fail=()):
        self.enabled = enabled
        self.active = active
        self.failed = failed
        self.fail = set(fail)
        self.calls = []

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        assert argv[:2] == ('systemctl', '--user'), argv
        verb = argv[2]
        if verb in self.fail:
            return 1, 'Job failed.'
        if verb == 'is-enabled':
            return (0, 'enabled') if self.enabled else (1, 'disabled')
        if verb == 'is-active':
            return (0, 'active') if self.active else (3, 'inactive')
        if verb == 'is-failed':
            return (0, 'failed') if self.failed else (1, 'active')
        return 0, ''

    def verbs(self):
        return [argv[2] for argv in self.calls]

    def mutating(self):
        """Calls that change state, i.e. everything but the state queries."""
        return [argv for argv in self.calls if argv[2] not in self.QUERIES]


@pytest.fixture
def env(tmp_path):
    """A throw-away profile that already has the launcher the units run."""
    launcher = tmp_path / '.local' / 'bin' / 'pwr-tray'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    launcher.chmod(0o755)
    return {
        'HOME': str(tmp_path),
        'XDG_CONFIG_HOME': str(tmp_path),
        'XDG_RUNTIME_DIR': '/run/user/1000',
        'WAYLAND_DISPLAY': 'wayland-0',
        'DISPLAY': ':0',
        'PATH': '/usr/local/bin:/usr/bin:/bin',
    }


def launcher_of(env):
    return Path(env['HOME']) / '.local' / 'bin' / 'pwr-tray'


def remove_launcher(env):
    """Delete the launcher, leaving an otherwise usable profile behind."""
    launcher_of(env).unlink()


@pytest.fixture
def log_dir(env):
    """The applet's log directory, which the shipped units write into and
    systemd will not create for them."""
    return Path(env['HOME']) / '.config' / 'pwr-tray'


@pytest.fixture
def unit_path(env):
    return Path(env['XDG_CONFIG_HOME']) / 'systemd' / 'user' / service.UNIT_NAME


def run_verb(verb, env, run, **kwargs):
    """Call dispatch() with the interactive and machine bits stubbed out."""
    kwargs.setdefault('echo', lambda line: None)
    kwargs.setdefault('ask', lambda question: False)
    kwargs.setdefault('holder', None)
    kwargs.setdefault('systemd', True)
    return service.dispatch(verb, DESKTOP, env=env, run=run, **kwargs)


def config_of(env):
    """The config.ini that holds [ServiceAllowed] for this profile.

    ``$HOME/.config`` and not ``$XDG_CONFIG_HOME``: that is where ``IniTool``
    (the reader the applet uses) looks, so a verb must write there too.
    """
    return Path(env['HOME']) / '.config' / 'pwr-tray' / 'config.ini'


def test_install_writes_the_unit_reloads_and_enables(env, unit_path):
    run = FakeSystemctl()
    assert run_verb('install', env, run) == service.OK

    text = unit_path.read_text(encoding='utf-8')
    assert f'ExecStart={LAUNCHER}' in text
    # A constant: no desktop, no session snapshot, nothing substituted.
    assert '--de' not in text
    assert 'Environment=WAYLAND_DISPLAY=' not in text
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'enable', service.UNIT_NAME),
    ]


def test_install_allows_the_desktop_it_ran_in(env, unit_path):
    """The one thing an install records is *this desktop* = True."""
    run_verb('install', env, FakeSystemctl())

    text = config_of(env).read_text(encoding='utf-8')
    allowed = parse_service_allowed(text)
    assert allowed[DESKTOP] is True
    assert set(allowed) == set(service.known_desktops())   # seeded complete
    assert sum(allowed.values()) == 1

    # A brand-new file must still be a config.ini the applet can read: the
    # defaults, with the section appended, not a file made only of the section.
    parser = configparser.ConfigParser()
    parser.read_string(text)
    assert parser['Settings']['debug_mode'] == 'False'
    assert parser['LoBattery']['power_down'] == 'True'


def test_install_keeps_the_hand_written_config_around(env, unit_path):
    """config.ini is meant to be edited by hand: no rewrite, no lost comment."""
    config_of(env).parent.mkdir(parents=True)
    config_of(env).write_text(
        '[Settings]\n# mine\ndebug_mode = True\n', encoding='utf-8')

    run_verb('install', env, FakeSystemctl())
    text = config_of(env).read_text(encoding='utf-8')
    assert '# mine' in text
    assert 'debug_mode = True' in text
    assert parse_service_allowed(text)[DESKTOP] is True


def test_second_install_does_not_rewrite_the_config(env, unit_path):
    run_verb('install', env, FakeSystemctl())
    stamp = config_of(env).stat().st_mtime_ns

    run_verb('install', env, FakeSystemctl(enabled=True))
    assert config_of(env).stat().st_mtime_ns == stamp


def test_install_does_not_start_anything(env, unit_path):
    run = FakeSystemctl()
    run_verb('install', env, run)
    assert 'start' not in run.verbs()


def test_second_install_is_a_no_op(env, unit_path):
    run_verb('install', env, FakeSystemctl())
    stamp = unit_path.stat().st_mtime_ns

    output = []
    run = FakeSystemctl(enabled=True)
    assert run_verb('install', env, run, echo=output.append) == service.OK

    assert run.mutating() == []                      # no reload, no enable
    assert unit_path.stat().st_mtime_ns == stamp     # file not rewritten
    assert any('already up to date' in line for line in output), output


def test_install_asks_before_replacing_an_edited_unit(env, unit_path):
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text('hand edited\n', encoding='utf-8')

    asked = []

    def ask(question):
        asked.append(question)
        return False

    run = FakeSystemctl()
    assert run_verb('install', env, run, ask=ask) == service.FAILED

    assert asked and 'differs' in asked[0]
    assert unit_path.read_text(encoding='utf-8') == 'hand edited\n'
    assert run.mutating() == []                # nothing written, nothing enabled


def test_force_replaces_an_edited_unit_without_asking(env, unit_path):
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text('hand edited\n', encoding='utf-8')

    def ask(question):
        raise AssertionError('--force must not prompt')

    run = FakeSystemctl()
    assert run_verb('install', env, run, ask=ask, force=True) == service.OK
    assert 'hand edited' not in unit_path.read_text(encoding='utf-8')


def test_install_prints_the_difference_before_asking(env, unit_path):
    """A real edit is shown line by line, so the user can judge the change."""
    run_verb('install', env, FakeSystemctl())
    edited = unit_path.read_text(encoding='utf-8').replace('RestartSec=5', 'RestartSec=9')
    unit_path.write_text(edited, encoding='utf-8')

    output = []
    run_verb('install', env, FakeSystemctl(), echo=output.append,
             ask=lambda question: False)
    joined = '\n'.join(output)
    assert '- RestartSec=9' in joined
    assert '+ RestartSec=5' in joined
    assert 'lines differ' not in joined


def test_start_starts_an_installed_enabled_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=True)
    assert run_verb('start', env, run) == service.OK
    assert run.mutating() == [('systemctl', '--user', 'start', service.UNIT_NAME)]


def test_start_enables_a_disabled_installed_unit_and_starts(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=False)
    assert run_verb('start', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'enable', service.UNIT_NAME),
        ('systemctl', '--user', 'start', service.UNIT_NAME),
    ]


def test_start_does_not_install(env, unit_path):
    """Installing is an explicit --svc-install, never a side effect of start."""
    run = FakeSystemctl()
    with pytest.raises(service.ServiceError, match='--svc-install'):
        run_verb('start', env, run)
    assert not unit_path.exists()
    assert run.mutating() == []


def test_start_when_already_active_starts_nothing(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('start', env, run, echo=output.append) == service.OK
    assert run.mutating() == []
    assert any('already running' in line for line in output), output


def test_start_leaves_an_active_unit_alone(env, unit_path):
    """`start` never restarts; `--svc-restart` is the verb for new code."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    edited = unit_path.read_text(encoding='utf-8').replace(
        'RestartSec=5', 'RestartSec=9')
    unit_path.write_text(edited, encoding='utf-8')

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('start', env, run, echo=output.append) == service.OK
    assert run.mutating() == []
    assert any('already running' in line for line in output), output
    assert 'RestartSec=9' in unit_path.read_text(encoding='utf-8')


def test_start_refuses_a_unit_that_runs_another_pwr_tray(env, unit_path):
    """An older unit that baked in an absolute path is not started silently."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    edited = unit_path.read_text(encoding='utf-8').replace(
        f'ExecStart={LAUNCHER}', 'ExecStart=/snap/bin/pwr-tray')
    unit_path.write_text(edited, encoding='utf-8')

    run = FakeSystemctl(enabled=True, active=True)
    with pytest.raises(service.ServiceError, match='--svc-install') as excinfo:
        run_verb('start', env, run)
    assert '/snap/bin/pwr-tray' in str(excinfo.value)
    assert run.mutating() == []
    assert 'ExecStart=/snap/bin/pwr-tray' in unit_path.read_text(encoding='utf-8')


def test_restart_restarts_an_installed_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('restart', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'restart', service.UNIT_NAME)]


def test_restart_does_not_install(env, unit_path):
    """Installing is an explicit --svc-install, never a side effect of restart."""
    run = FakeSystemctl()
    with pytest.raises(service.ServiceError, match='--svc-install'):
        run_verb('restart', env, run)
    assert not unit_path.exists()
    assert run.mutating() == []


def test_restart_does_not_rewrite_a_changed_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    unit_path.write_text('stale\n', encoding='utf-8')

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('restart', env, run, echo=output.append) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'restart', service.UNIT_NAME)]
    assert any('differs from the shipped unit' in line for line in output), output
    assert unit_path.read_text(encoding='utf-8') == 'stale\n'


def test_restart_refuses_a_unit_that_runs_another_pwr_tray(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    edited = unit_path.read_text(encoding='utf-8').replace(
        f'ExecStart={LAUNCHER}', 'ExecStart=/snap/bin/pwr-tray')
    unit_path.write_text(edited, encoding='utf-8')

    run = FakeSystemctl(enabled=True, active=True)
    with pytest.raises(service.ServiceError, match='--svc-install') as excinfo:
        run_verb('restart', env, run)
    assert '/snap/bin/pwr-tray' in str(excinfo.value)
    assert run.mutating() == []


def test_restart_refuses_while_the_applet_runs_outside_systemd(env, unit_path):
    """A restart would start a unit that exits 3 against the lock."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    run = FakeSystemctl(enabled=True)
    with pytest.raises(service.ServiceError, match='outside systemd') as excinfo:
        run_verb('restart', env, run, holder=4242)
    assert '4242' in str(excinfo.value)
    assert run.mutating() == []


def test_start_refuses_while_the_applet_runs_outside_systemd(env, unit_path):
    """The unit would exit 3 against the lock, so say so instead of failing."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    run = FakeSystemctl(enabled=True)
    with pytest.raises(service.ServiceError) as excinfo:
        run_verb('start', env, run, holder=4242)
    assert 'outside systemd' in str(excinfo.value)
    assert '4242' in str(excinfo.value)
    assert run.mutating() == []


def test_install_only_warns_about_a_running_applet(env, unit_path):
    output = []
    run = FakeSystemctl()
    assert run_verb('install', env, run, echo=output.append, holder=4242) == service.OK
    assert unit_path.is_file()
    assert any('keeps running' in line for line in output), output


def test_install_does_not_warn_when_the_unit_is_the_one_running(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run_verb('install', env, FakeSystemctl(enabled=True, active=True),
             echo=output.append, holder=4242)
    assert not any('outside systemd' in line for line in output), output


def test_applet_holder_follows_the_single_instance_lock(tmp_path):
    """A shell whose command line mentions pwr-tray must not look like one."""
    assert service.applet_holder(tmp_path) is None
    assert not (tmp_path / 'pwr-tray.lock').exists()   # probing creates nothing

    lock = SingleInstance(str(tmp_path / 'pwr-tray.lock'))
    assert lock.acquire()
    try:
        assert service.applet_holder(tmp_path) == os.getpid()
    finally:
        lock.release()
    assert service.applet_holder(tmp_path) is None


def test_install_prints_each_step_once(env, unit_path):
    output = []
    run_verb('install', env, FakeSystemctl(), echo=output.append)
    assert sum('reload the unit files' in line for line in output) == 1, output


def test_install_creates_the_directory_the_unit_logs_into(env, unit_path, log_dir):
    """systemd refuses to start a unit whose log directory is missing.

    It fails every process with EXIT_STDOUT (209) before ExecStart= is reached,
    so a unit that was installed but never started would fail forever.
    """
    assert not log_dir.exists()

    output = []
    assert run_verb('install', env, FakeSystemctl(), echo=output.append) == service.OK
    assert log_dir.is_dir()
    assert any(f'create {log_dir}' in line for line in output), output


def test_install_does_not_recreate_an_existing_directory(env, unit_path, log_dir):
    log_dir.mkdir(parents=True)

    output = []
    run_verb('install', env, FakeSystemctl(), echo=output.append)
    assert not any(line.startswith('create ') for line in output), output


def test_start_clears_a_failed_unit_before_starting(env, unit_path):
    """After the start rate limiter trips, `start` alone is refused as too soon."""
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=True, failed=True)
    assert run_verb('start', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'reset-failed', service.UNIT_NAME),
        ('systemctl', '--user', 'start', service.UNIT_NAME),
    ]


def test_a_failed_start_says_where_to_look(env, unit_path):
    """The one thing a failed start must not do is leave the user guessing."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    with pytest.raises(service.ServiceError) as excinfo:
        run_verb('start', env, FakeSystemctl(enabled=True, fail=('start',)))
    message = str(excinfo.value)
    assert 'start pwr-tray.service failed' in message
    assert 'systemctl --user status pwr-tray.service' in message


def test_stop_leaves_the_unit_enabled(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('stop', env, run) == service.OK
    assert run.mutating() == [('systemctl', '--user', 'stop', service.UNIT_NAME)]
    assert 'disable' not in run.verbs()


def test_stop_when_not_running_changes_nothing(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run = FakeSystemctl(enabled=True)
    assert run_verb('stop', env, run, echo=output.append) == service.OK
    assert run.mutating() == []
    assert any('not running' in line for line in output), output


def test_stop_without_an_installed_unit_is_an_error(env, unit_path):
    with pytest.raises(service.ServiceError, match='not installed'):
        run_verb('stop', env, FakeSystemctl())


def test_uninstall_stops_disables_and_removes(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    assert unit_path.is_file()

    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('uninstall', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'stop', service.UNIT_NAME),
        ('systemctl', '--user', 'disable', service.UNIT_NAME),
        ('systemctl', '--user', 'daemon-reload'),
    ]
    assert not unit_path.exists()
    assert unit_path.parent.is_dir()          # only the unit file is removed


def test_uninstall_refuses_a_hand_edited_unit(env, unit_path):
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text('mine\n', encoding='utf-8')

    with pytest.raises(service.ServiceError, match='--force'):
        run_verb('uninstall', env, FakeSystemctl())
    assert unit_path.read_text(encoding='utf-8') == 'mine\n'


def test_uninstall_of_a_missing_unit_is_a_no_op(env, unit_path):
    run = FakeSystemctl()
    output = []
    assert run_verb('uninstall', env, run, echo=output.append) == service.OK
    assert run.calls == []
    assert any('nothing to do' in line for line in output), output


def test_status_codes_are_a_contract(env, unit_path):
    """0 active, 1 installed but not running, 2 not installed."""
    assert run_verb('status', env, FakeSystemctl()) == service.STATUS_ABSENT == 2

    run_verb('install', env, FakeSystemctl(enabled=True))
    assert run_verb('status', env, FakeSystemctl(enabled=True)) == service.STATUS_INACTIVE == 1
    assert run_verb('status', env, FakeSystemctl(
        enabled=True, active=True)) == service.STATUS_ACTIVE == 0


def test_status_reports_a_failed_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    assert run_verb('status', env, FakeSystemctl(enabled=True, failed=True),
                    echo=output.append) == service.STATUS_INACTIVE
    joined = '\n'.join(output)
    assert 'failed' in joined
    assert 'journalctl --user' in joined


def test_only_file_targets_need_a_directory():
    """journal, null and friends name no path, so nothing must be created."""
    text = '[Service]\nStandardOutput=journal\nStandardError=null\n'
    assert service.output_dirs(text, {'HOME': '/home/x'}) == []


def test_unit_paths_expand_the_home_specifier():
    text = 'StandardOutput=append:%h/.local/log/app.log\n'
    assert service.output_dirs(text, {'HOME': '/home/x'}) == [Path('/home/x/.local/log')]


def test_a_present_log_directory_is_not_missing():
    text = 'StandardError=file:/var/log/pwr-tray.err\n'
    assert Path('/var/log').is_dir()
    assert service.missing_dirs(text, {}) == []


def test_status_reports_an_out_of_date_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl())
    unit_path.write_text('stale\n', encoding='utf-8')

    output = []
    run_verb('status', env, FakeSystemctl(), echo=output.append)
    assert any('differs from the shipped unit' in line for line in output), output


def test_status_names_the_launcher_the_unit_runs(env, unit_path):
    """The unit always names the launcher; `%h` is expanded for the reader."""
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run_verb('status', env, FakeSystemctl(enabled=True), echo=output.append)
    assert f'runs: {launcher_of(env)}' in '\n'.join(output), output


def test_status_points_out_a_unit_that_runs_another_pwr_tray(env, unit_path):
    """A unit installed by an older pwr-tray must not look current."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    edited = unit_path.read_text(encoding='utf-8').replace(
        f'ExecStart={LAUNCHER}', 'ExecStart=/opt/old-venv/bin/pwr-tray')
    unit_path.write_text(edited, encoding='utf-8')

    output = []
    run_verb('status', env, FakeSystemctl(enabled=True), echo=output.append)
    joined = '\n'.join(output)
    assert 'runs: /opt/old-venv/bin/pwr-tray' in joined
    assert f'the shipped unit runs {launcher_of(env)}' in joined


def test_verbs_refuse_to_run_without_systemd(env, unit_path):
    with pytest.raises(service.ServiceError, match='no systemd user manager'):
        run_verb('install', env, FakeSystemctl(), systemd=False)
    assert not unit_path.exists()


def test_a_failing_systemctl_call_is_reported(env, unit_path):
    run = FakeSystemctl(fail=('enable',))
    with pytest.raises(service.ServiceError) as excinfo:
        run_verb('install', env, run)
    assert 'enable pwr-tray.service failed' in str(excinfo.value)
    assert 'Job failed.' in str(excinfo.value)


def test_installed_executable_expands_the_home_specifier(env):
    """What the unit says and what the launcher is must compare equal."""
    text = service.shipped_unit_text()
    assert service.installed_executable(text, env) == str(launcher_of(env))


def test_installed_executable_of_a_unit_without_exec_start_is_none():
    assert service.installed_executable('[Service]\nType=simple\n') is None


def test_install_refuses_without_the_launcher(env, unit_path):
    """A unit that names a launcher nobody created cannot start."""
    remove_launcher(env)
    run = FakeSystemctl()

    with pytest.raises(service.ServiceError, match='does not exist') as excinfo:
        run_verb('install', env, run)
    assert 'pipx install --editable .' in str(excinfo.value)
    assert not unit_path.exists()
    assert run.mutating() == []


def test_start_and_restart_refuse_without_the_launcher(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    remove_launcher(env)

    for verb in ('start', 'restart'):
        run = FakeSystemctl(enabled=True)
        with pytest.raises(service.ServiceError, match='does not exist'):
            run_verb(verb, env, run)
        assert run.mutating() == []


def test_a_non_executable_launcher_is_refused(env, unit_path):
    launcher_of(env).chmod(0o644)
    with pytest.raises(service.ServiceError, match='not executable'):
        run_verb('install', env, FakeSystemctl())
    assert not unit_path.exists()


def test_a_dangling_launcher_symlink_is_refused(env, unit_path):
    link = launcher_of(env)
    link.unlink()
    link.symlink_to(Path(env['HOME']) / 'gone')
    with pytest.raises(service.ServiceError, match='dangling symlink'):
        run_verb('install', env, FakeSystemctl())


def test_stop_and_uninstall_still_work_without_the_launcher(env, unit_path):
    """Those verbs are needed exactly when the install is broken."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    remove_launcher(env)

    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('stop', env, run) == service.OK
    assert run_verb('uninstall', env, run) == service.OK
    assert not unit_path.exists()


def test_status_reports_a_missing_launcher(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    remove_launcher(env)

    output = []
    assert run_verb('status', env, FakeSystemctl(enabled=True),
                    echo=output.append) == service.STATUS_INACTIVE
    joined = '\n'.join(output)
    assert f'runs: {launcher_of(env)}' in joined
    assert 'cannot start' in joined


def test_install_names_the_launcher_it_will_run(env, unit_path):
    output = []
    run_verb('install', env, FakeSystemctl(), echo=output.append)
    assert f'the unit will run {launcher_of(env)}' in '\n'.join(output)


def test_dispatch_rejects_an_unknown_verb(env):
    with pytest.raises(service.ServiceError, match='unknown service verb'):
        run_verb('reload', env, FakeSystemctl())


def test_show_file_prints_the_unit_without_systemd_or_a_launcher(env, unit_path):
    """The starter unit is a dump, not an install: nothing has to be running."""
    remove_launcher(env)

    output = []
    assert run_verb('show-file', env, FakeSystemctl(), echo=output.append,
                    systemd=False) == service.OK
    text = '\n'.join(output)
    assert f'ExecStart={LAUNCHER}' in text
    assert '--de' not in text
    assert '[Install]' in text
    assert not unit_path.exists()


def cli_session(monkeypatch, tmp_path, argv):
    """Point the real CLI at a throw-away profile with no systemd manager.

    Desktop detection is given a one-entry config so this test exercises the
    argparse/dispatch wiring, not the YAML loading that other tests cover.
    """
    import pwr_tray.main as cli

    de_json = {'defaults': {}, 'session_types': {}, 'desktops': {DESKTOP: {}}}
    monkeypatch.setattr(cli, 'load_known_de_json', lambda: (de_json, 'test'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'niri')
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    monkeypatch.setenv('XDG_RUNTIME_DIR', '')
    monkeypatch.setattr(sys, 'argv', ['pwr-tray', *argv])
    return cli


def test_cli_status_reports_not_installed(monkeypatch, tmp_path, capsys):
    cli = cli_session(monkeypatch, tmp_path, ['--svc-status'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == service.STATUS_ABSENT
    assert 'not installed' in capsys.readouterr().out


def test_cli_install_without_systemd_changes_nothing(monkeypatch, tmp_path, capfd):
    """error_print() writes to the saved console fd, so capture the fd, not sys.err."""
    cli = cli_session(monkeypatch, tmp_path, ['--svc-install', '--yes'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert 'no systemd user manager' in capfd.readouterr().err
    assert not (Path(tmp_path) / 'systemd' / 'user' / service.UNIT_NAME).exists()


def test_cli_restart_verb_is_wired(monkeypatch, tmp_path, capfd):
    cli = cli_session(monkeypatch, tmp_path, ['--svc-restart', '--yes'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert 'no systemd user manager' in capfd.readouterr().err


def test_cli_rejects_two_verbs(monkeypatch, capsys):
    cli = cli_session(monkeypatch, Path('/tmp'), ['--svc-start', '--svc-stop'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert 'not allowed with' in capsys.readouterr().err


def test_cli_show_svc_file_writes_the_unit_to_stdout(monkeypatch, tmp_path, capsys):
    cli = cli_session(monkeypatch, tmp_path, ['--show-svc-file'])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == service.OK
    out = capsys.readouterr().out
    assert f'ExecStart={LAUNCHER}' in out
    assert '[Install]' in out


# --------------------------------------------------------------------------
# the allowed-desktop set
# --------------------------------------------------------------------------

def test_every_verb_reports_the_allowed_desktops(env, unit_path):
    output = []
    run_verb('install', env, FakeSystemctl(), echo=output.append)
    joined = '\n'.join(output)
    assert f'desktops allowed: {DESKTOP}' in joined
    assert f'this session: {DESKTOP} (allowed)' in joined


def test_start_refuses_a_desktop_that_is_not_allowed(env, unit_path):
    """Starting here would exit 0 at once, so say why instead."""
    run_verb('install', env, FakeSystemctl(enabled=True))

    run = FakeSystemctl(enabled=True)
    with pytest.raises(service.ServiceError, match='not allowed') as excinfo:
        service.dispatch('start', 'i3-x11', env=env, run=run,
                         echo=lambda line: None, systemd=True)
    assert '--svc-install' in str(excinfo.value)
    assert run.mutating() == []


def test_uninstall_leaves_the_unit_installed_for_another_desktop(env, unit_path):
    """Removing the unit is the last-desktop event, not the meaning of the verb."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    service.dispatch('install', 'i3-x11', env=env, run=FakeSystemctl(enabled=True),
                     echo=lambda line: None, systemd=True)

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('uninstall', env, run, echo=output.append) == service.OK

    assert unit_path.is_file()
    assert 'disable' not in run.verbs()
    assert run.mutating() == [('systemctl', '--user', 'stop', service.UNIT_NAME)]
    joined = '\n'.join(output)
    assert 'stays installed and enabled for: i3-x11' in joined
    assert parse_service_allowed(
        config_of(env).read_text(encoding='utf-8'))[DESKTOP] is False


def test_status_says_when_this_session_is_not_allowed(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    service.dispatch('status', 'i3-x11', env=env, run=FakeSystemctl(enabled=True),
                     echo=output.append)
    joined = '\n'.join(output)
    assert f'desktops allowed: {DESKTOP}' in joined
    assert 'this session: i3-x11 (not allowed' in joined
