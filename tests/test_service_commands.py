"""Exercise the --svc-* verbs without a systemd session.

The verbs are deliberately split into policy (``plan_*`` returns the steps) and
effect (``execute`` performs them), so every decision here can be asserted by
argv instead of by poking a live user manager: what gets written, in which
order, what is *not* called, and what happens when the answer is no.

The last tests drive the real ``main()`` argparse wiring, so a flag that is
renamed or made mutually exclusive is caught here rather than by a user.
"""

import os
import sys
from pathlib import Path

import pytest

from pwr_tray import service
from pwr_tray.Utils import SingleInstance

DESKTOP = 'niri-wayland'
SCRIPT = '/usr/local/bin/pwr-tray'


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
    return {
        'HOME': str(tmp_path),
        'XDG_CONFIG_HOME': str(tmp_path),
        'XDG_RUNTIME_DIR': '/run/user/1000',
        'WAYLAND_DISPLAY': 'wayland-0',
        'DISPLAY': ':0',
        'PATH': '/usr/local/bin:/usr/bin:/bin',
    }


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
    kwargs.setdefault('script', SCRIPT)
    kwargs.setdefault('systemd', True)
    return service.dispatch(verb, DESKTOP, env=env, run=run, **kwargs)


def test_install_writes_the_unit_reloads_and_enables(env, unit_path):
    run = FakeSystemctl()
    assert run_verb('install', env, run) == service.OK

    text = unit_path.read_text(encoding='utf-8')
    assert f'ExecStart={SCRIPT} --de {DESKTOP}' in text
    assert 'Environment=WAYLAND_DISPLAY=wayland-0' in text
    # DISPLAY is not pinned by a Wayland unit, so it must not be injected
    assert 'Environment=DISPLAY=' not in text
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'enable', service.UNIT_NAME),
    ]


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


def test_start_enables_and_starts(env, unit_path):
    run = FakeSystemctl()
    assert run_verb('start', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'enable', service.UNIT_NAME),
        ('systemctl', '--user', 'start', service.UNIT_NAME),
    ]


def test_start_when_already_active_starts_nothing(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('start', env, run, echo=output.append) == service.OK
    assert run.mutating() == []
    assert any('already running' in line for line in output), output


def test_start_restarts_an_active_unit_that_changed(env, unit_path):
    """`start` on an active unit does nothing, so a rewrite needs a restart.

    This is the case that left a service running an executable from an old
    project virtualenv after the unit had been re-rendered: the file changed,
    the running process did not.
    """
    run_verb('install', env, FakeSystemctl(enabled=True))
    edited = unit_path.read_text(encoding='utf-8').replace(
        f'ExecStart={SCRIPT}', 'ExecStart=/snap/bin/pwr-tray')
    unit_path.write_text(edited, encoding='utf-8')

    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('start', env, run, yes=True) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'restart', service.UNIT_NAME),
    ]
    assert f'ExecStart={SCRIPT}' in unit_path.read_text(encoding='utf-8')


def test_restart_installs_enables_and_restarts(env, unit_path):
    run = FakeSystemctl()
    assert run_verb('restart', env, run) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'enable', service.UNIT_NAME),
        ('systemctl', '--user', 'restart', service.UNIT_NAME),
    ]


def test_restart_applies_a_rewritten_unit(env, unit_path):
    run_verb('install', env, FakeSystemctl(enabled=True))
    unit_path.write_text('stale\n', encoding='utf-8')

    output = []
    run = FakeSystemctl(enabled=True, active=True)
    assert run_verb('restart', env, run, echo=output.append, yes=True) == service.OK
    assert run.mutating() == [
        ('systemctl', '--user', 'daemon-reload'),
        ('systemctl', '--user', 'restart', service.UNIT_NAME),
    ]
    assert any('restarts now' in line for line in output), output
    assert f'ExecStart={SCRIPT}' in unit_path.read_text(encoding='utf-8')


def test_restart_refuses_while_the_applet_runs_outside_systemd(env, unit_path):
    """A restart would start a unit that exits 3 against the lock."""
    run = FakeSystemctl()
    with pytest.raises(service.ServiceError, match='outside systemd') as excinfo:
        run_verb('restart', env, run, holder=4242)
    assert '4242' in str(excinfo.value)
    assert run.mutating() == []
    assert not unit_path.exists()


def test_start_refuses_while_the_applet_runs_outside_systemd(env, unit_path):
    """The unit would exit 3 against the lock, so say so instead of failing."""
    run = FakeSystemctl()
    with pytest.raises(service.ServiceError) as excinfo:
        run_verb('start', env, run, holder=4242)
    assert 'outside systemd' in str(excinfo.value)
    assert '4242' in str(excinfo.value)
    assert run.mutating() == []
    assert not unit_path.exists()


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
    with pytest.raises(service.ServiceError) as excinfo:
        run_verb('start', env, FakeSystemctl(fail=('start',)))
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


def test_status_names_the_executable_the_unit_runs(env, unit_path):
    """Which executable the unit names decides which code a restart loads."""
    run_verb('install', env, FakeSystemctl(enabled=True))

    output = []
    run_verb('status', env, FakeSystemctl(enabled=True), echo=output.append)
    assert f'runs: {SCRIPT}' in '\n'.join(output), output


def test_status_points_out_a_unit_that_runs_another_pwr_tray(env, unit_path, tmp_path):
    """A unit installed from an old virtualenv must not look current."""
    run_verb('install', env, FakeSystemctl(enabled=True))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    other = bindir / 'pwr-tray'
    other.write_text('#!/bin/sh\n', encoding='utf-8')
    other.chmod(0o755)

    output = []
    run_verb('status', dict(env, PATH=f'{bindir}:/usr/bin:/bin'),
             FakeSystemctl(enabled=True), echo=output.append)
    joined = '\n'.join(output)
    assert str(other) in joined
    assert 'install and restart' in joined


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


def test_unknown_desktop_lists_what_is_shipped():
    with pytest.raises(service.ServiceError) as excinfo:
        service.template_for('plan9-x11')
    message = str(excinfo.value)
    assert 'plan9-x11' in message
    assert DESKTOP in message                # the alternatives are named


def test_executable_with_spaces_is_quoted(env):
    text = service.read_unit(service.template_for(DESKTOP))
    rendered = service.render(text, DESKTOP, '/opt/my tools/pwr-tray', env)
    assert 'ExecStart="/opt/my tools/pwr-tray" --de niri-wayland' in rendered
    assert service.installed_executable(rendered) == '/opt/my tools/pwr-tray'


def test_installed_executable_of_a_unit_without_exec_start_is_none():
    assert service.installed_executable('[Service]\nType=simple\n') is None


def test_missing_executable_is_explained(env):
    env = dict(env, PATH='/nonexistent')
    with pytest.raises(service.ServiceError, match='not on \\$PATH'):
        service.executable_path(env)


def test_dispatch_rejects_an_unknown_verb(env):
    with pytest.raises(service.ServiceError, match='unknown service verb'):
        run_verb('reload', env, FakeSystemctl())


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
