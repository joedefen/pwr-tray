"""Tests for debug.log rotation and the *Open Applet Log* menu action.

The contract pinned here:

- at most two files exist: ``debug.log`` and ``debug.log.old``;
- a rotation discards the previous ``.old`` and renames the current file, so
  the fresh log is created by the reopen that follows it;
- the cap is 2 MiB;
- ``prt()`` rotates the file it is appending to, not whatever stdout is -- a
  ``--stdout`` run must never rotate a log it is not writing;
- the viewer is started inside a terminal emulator (the applet has no tty of
  its own), pages the current log first, plus the rotated one if present, and
  is detached so an applet stop/restart does not kill it.
"""

import os
import subprocess
import sys
import types

import pytest

import pwr_tray.Utils as utils
from pwr_tray.Utils import LOG_OLD_SUFFIX, log_viewer_argv, rotate_log, terminal_argv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_NAME = f'debug.log{LOG_OLD_SUFFIX}'


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------

def test_cap_is_two_mebibytes():
    assert utils.prt_kb == 2048


def test_rotate_log_leaves_a_small_file_alone(tmp_path):
    log = tmp_path / 'debug.log'
    log.write_text('small', encoding='utf-8')

    assert rotate_log(str(log), max_bytes=1024) is False
    assert log.read_text(encoding='utf-8') == 'small'
    assert not (tmp_path / OLD_NAME).exists()


def test_rotate_log_moves_current_to_old(tmp_path):
    log = tmp_path / 'debug.log'
    log.write_text('x' * 2048, encoding='utf-8')

    assert rotate_log(str(log), max_bytes=1024) is True

    assert not log.exists()
    assert (tmp_path / OLD_NAME).read_text(encoding='utf-8') == 'x' * 2048


def test_rotate_log_keeps_only_one_generation(tmp_path):
    log = tmp_path / 'debug.log'
    log.write_text('first' * 512, encoding='utf-8')
    assert rotate_log(str(log), max_bytes=1024) is True

    log.write_text('second' * 512, encoding='utf-8')
    assert rotate_log(str(log), max_bytes=1024) is True

    # The current file is recreated by the caller's reopen, so right after a
    # rotation only .old is left -- and there is never a .old.old.
    log_files = sorted(p.name for p in tmp_path.iterdir()
                       if p.name.startswith('debug.log'))
    assert log_files == [OLD_NAME]
    old_text = (tmp_path / OLD_NAME).read_text(encoding='utf-8')
    assert 'second' in old_text
    assert 'first' not in old_text


def test_rotate_log_tolerates_missing_or_empty_path(tmp_path):
    assert rotate_log(str(tmp_path / 'nope.log'), max_bytes=10) is False
    assert rotate_log('') is False


def test_prt_rotates_the_log_it_is_writing(tmp_path):
    """Exercise prt()'s rotate-and-reopen path in a child process.

    It cannot run in-process: reopen() dup2()s the real fds 1 and 2 onto the
    log file, which would break pytest's capture.
    """
    log = tmp_path / 'debug.log'
    script = f"""\
import pwr_tray.Utils as U
U.prt_path = {str(log)!r}
U.prt_kb = 1                      # 1 KiB cap, so one pad write crosses it
U.prt('before-the-rotation', to_stdout=False)
with open(U.prt_path, 'a', encoding='utf-8') as handle:
    handle.write('pad ' + 'x' * 4096)
U.prt('after-the-rotation')
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                            env=dict(os.environ, PYTHONPATH=ROOT),
                            capture_output=True, text=True, check=False,
                            timeout=60)

    assert result.returncode == 0, result.stderr
    old = tmp_path / OLD_NAME
    assert old.exists() and log.exists()
    assert 'before-the-rotation' in old.read_text(encoding='utf-8')
    assert 'after-the-rotation' in log.read_text(encoding='utf-8')
    assert 'before-the-rotation' not in log.read_text(encoding='utf-8')


def test_prt_takes_over_from_a_foreign_redirect(tmp_path):
    """A unit redirecting stdout elsewhere must not hide the applet's log.

    This is the regression behind "--svc-* broke logging": the unit sent
    stdout to ~/.local/log while the menu tailed ~/.config/pwr-tray/debug.log,
    so the visible log froze at whatever it held before the unit was installed.
    """
    log = tmp_path / 'debug.log'
    foreign = tmp_path / 'pwr-tray.log'
    script = f"""\
import pwr_tray.Utils as U
U.prt_path = {str(log)!r}
U.prt('first-line')
U.prt('second-line')
"""
    with open(foreign, 'w', encoding='utf-8') as handle:
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                env=dict(os.environ, PYTHONPATH=ROOT),
                                stdout=handle, stderr=handle, check=False,
                                timeout=60)

    assert result.returncode == 0, foreign.read_text(encoding='utf-8')
    assert 'first-line' in log.read_text(encoding='utf-8')
    assert 'second-line' in log.read_text(encoding='utf-8')


def test_prt_writes_through_a_redirect_already_named_debug_log(tmp_path):
    """systemd `StandardOutput=append:debug.log` hands us the log itself.

    prt() must write through that descriptor rather than reopen it on every
    call -- and rotation must still work: the renamed inode becomes .old and
    the fresh debug.log is created by the reopen that follows.
    """
    log = tmp_path / 'debug.log'
    script = f"""\
import pwr_tray.Utils as U
U.prt_path = {str(log)!r}
U.prt_kb = 1
U.prt('first-line')
with open(U.prt_path, 'a', encoding='utf-8') as handle:
    handle.write('pad ' + 'x' * 4096)
U.prt('after-the-rotation')
"""
    with open(log, 'a', encoding='utf-8') as handle:
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                env=dict(os.environ, PYTHONPATH=ROOT),
                                stdout=handle, stderr=handle, check=False,
                                timeout=60)

    assert result.returncode == 0
    old = tmp_path / OLD_NAME
    assert 'first-line' in old.read_text(encoding='utf-8')
    assert 'after-the-rotation' in log.read_text(encoding='utf-8')
    assert 'after-the-rotation' not in old.read_text(encoding='utf-8')


# --------------------------------------------------------------------------
# terminal wrapper
# --------------------------------------------------------------------------

def test_terminal_argv_honours_the_configured_prefix():
    assert terminal_argv(['less', '+F'], 'xterm -maximized -e') == [
        'xterm', '-maximized', '-e', 'less', '+F']


def test_terminal_argv_autodetects_an_argv_style_terminal(monkeypatch):
    monkeypatch.setattr(
        utils.shutil, 'which',
        lambda name: '/usr/bin/gnome-terminal' if name == 'gnome-terminal'
        else None)

    assert terminal_argv(['less'], '') == [
        '/usr/bin/gnome-terminal', '--', 'less']


def test_terminal_argv_autodetects_a_string_style_terminal(monkeypatch):
    """xfce4-terminal and friends take the whole command line as one string."""
    monkeypatch.setattr(
        utils.shutil, 'which',
        lambda name: '/usr/bin/xfce4-terminal' if name == 'xfce4-terminal'
        else None)

    assert terminal_argv(['less', '+F', '/tmp/a b.log'], '') == [
        '/usr/bin/xfce4-terminal', '-e', "less +F '/tmp/a b.log'"]


def test_terminal_argv_without_any_terminal(monkeypatch):
    monkeypatch.setattr(utils.shutil, 'which', lambda name: None)

    assert terminal_argv(['less'], '') == []


# --------------------------------------------------------------------------
# less command line
# --------------------------------------------------------------------------

def test_log_viewer_pages_current_then_old(tmp_path, monkeypatch):
    log = tmp_path / 'debug.log'
    old = tmp_path / OLD_NAME
    log.write_text('current', encoding='utf-8')
    old.write_text('previous', encoding='utf-8')
    monkeypatch.setattr(utils, 'less_follow_name_supported', lambda: True)

    assert log_viewer_argv(str(log), str(old), 'xterm -e') == [
        'xterm', '-e', 'less', '--follow-name', '+F', str(log), str(old)]


def test_log_viewer_skips_absent_old_and_old_less(tmp_path, monkeypatch):
    log = tmp_path / 'debug.log'
    log.write_text('current', encoding='utf-8')
    monkeypatch.setattr(utils, 'less_follow_name_supported', lambda: False)

    assert log_viewer_argv(str(log), str(tmp_path / OLD_NAME), 'xterm -e') == [
        'xterm', '-e', 'less', '+F', str(log)]


# --------------------------------------------------------------------------
# the menu action
# --------------------------------------------------------------------------

def tray_stub(log, old, log_viewer='xterm -e'):
    """A tray with just the attributes open_log() reads."""
    return types.SimpleNamespace(
        ini_tool=types.SimpleNamespace(ini_path='/tmp/config.ini',
                                       log_path=str(log),
                                       log_old_path=str(old)),
        get_params=lambda: types.SimpleNamespace(log_viewer=log_viewer))


def test_opening_the_log_spawns_the_viewer_outside_our_process(
        monkeypatch, tmp_path):
    import pwr_tray.Actions as actions_module
    import pwr_tray.main as main_module

    log = tmp_path / 'debug.log'
    old = tmp_path / OLD_NAME
    log.write_text('current', encoding='utf-8')
    old.write_text('previous', encoding='utf-8')

    launched = []
    monkeypatch.setattr(main_module.PwrTray, 'singleton',
                        tray_stub(log, old), raising=False)
    # open_log() now lives in the action layer, so that is where its narration
    # is silenced -- and spawn_detached() is still the only launcher.
    monkeypatch.setattr(actions_module, 'prt', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        utils, 'spawn_detached',
        lambda argv, **kwargs: launched.append((argv, kwargs)))
    monkeypatch.setattr(utils, 'less_follow_name_supported', lambda: True)

    main_module.PwrTray.open_log(None)

    # quiet=True: prt() has pointed our fds 1/2 at debug.log, so a terminal
    # emulator spawned without redirection would inherit the log as its own
    # stdout and append its raw toolkit output (konsole's Qt palette dump)
    # where our Qt message handler cannot reach it.
    assert launched == [(
        ['xterm', '-e', 'less', '--follow-name', '+F', str(log), str(old)],
        {'quiet': True})]


def test_open_log_reports_when_no_terminal_exists(monkeypatch, tmp_path):
    import pwr_tray.Actions as actions_module
    import pwr_tray.main as main_module

    log = tmp_path / 'debug.log'
    log.write_text('current', encoding='utf-8')

    messages = []
    launched = []
    monkeypatch.setattr(main_module.PwrTray, 'singleton',
                        tray_stub(log, tmp_path / OLD_NAME, log_viewer=''),
                        raising=False)
    monkeypatch.setattr(actions_module, 'prt',
                        lambda *args, **kwargs: messages.append(
                            ' '.join(str(arg) for arg in args)))
    monkeypatch.setattr(utils, 'spawn_detached', launched.append)
    monkeypatch.setattr(utils.shutil, 'which', lambda name: None)

    main_module.PwrTray.open_log(None)

    assert launched == []
    assert any('no terminal emulator' in message for message in messages)


# --------------------------------------------------------------------------
# viewer detachment
# --------------------------------------------------------------------------

def test_spawn_detached_starts_a_new_session(monkeypatch):
    """setsid(2) via start_new_session: no controlling terminal, no SIGHUP."""
    calls = []
    monkeypatch.setattr(utils.shutil, 'which', lambda name, path=None: None)

    utils.spawn_detached(['less', '+F', '/tmp/x.log'],
                         spawn=lambda argv, **kwargs: calls.append((argv, kwargs)),
                         env={})

    assert calls == [(['less', '+F', '/tmp/x.log'],
                      {'start_new_session': True})]


def test_spawn_detached_escapes_the_unit_cgroup(monkeypatch):
    """systemd kills the whole cgroup on stop/restart, so the viewer must leave it."""
    calls = []
    monkeypatch.setattr(
        utils.shutil, 'which',
        lambda name, path=None: '/usr/bin/systemd-run' if name == 'systemd-run' else None)

    utils.spawn_detached(
        ['less', '+F', '/tmp/x.log'],
        spawn=lambda argv, **kwargs: calls.append((argv, kwargs)),
        env={'INVOCATION_ID': 'inv-1', 'PATH': '/usr/bin'})

    assert calls == [([
        '/usr/bin/systemd-run', '--user', '--scope', '--quiet', '--',
        'less', '+F', '/tmp/x.log'], {'start_new_session': True})]


def test_spawn_detached_ignores_an_empty_command():
    def spawn(*_args, **_kwargs):
        pytest.fail('nothing should be spawned')

    assert utils.spawn_detached([], spawn=spawn) is None


def test_spawn_detached_can_keep_a_child_out_of_our_log(monkeypatch):
    """quiet=True: the child must not inherit debug.log as its stdout."""
    calls = []
    monkeypatch.setattr(utils.shutil, 'which', lambda name, path=None: None)

    utils.spawn_detached(['konsole', '-e', 'less', '+F', '/tmp/x.log'],
                         spawn=lambda argv, **kwargs: calls.append((argv, kwargs)),
                         env={}, quiet=True)

    assert calls == [(
        ['konsole', '-e', 'less', '+F', '/tmp/x.log'],
        {'start_new_session': True,
         'stdin': utils.subprocess.DEVNULL,
         'stdout': utils.subprocess.DEVNULL,
         'stderr': utils.subprocess.DEVNULL})]


def test_spawn_detached_leaves_the_applet_s_own_streams_alone(monkeypatch):
    """The default spawn keeps the inherited fds: that output is the session log."""
    calls = []
    monkeypatch.setattr(utils.shutil, 'which', lambda name, path=None: None)

    utils.spawn_detached(['python3', '-m', 'pwr_tray.main'],
                         spawn=lambda argv, **kwargs: calls.append((argv, kwargs)),
                         env={})

    assert calls == [(['python3', '-m', 'pwr_tray.main'],
                      {'start_new_session': True})]
    assert 'stdout' not in calls[0][1]
    assert 'stderr' not in calls[0][1]
