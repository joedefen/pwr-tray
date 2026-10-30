"""Regression tests for audit finding P1-7: one instance, always.

PyKill used to match only ``python <target>`` / ``python <target>.py``.  A
restarted instance is ``python -m pwr_tray.main`` (``ps`` shows ``-m`` where the
script name used to be) and the development launcher was ``python runner``, so
neither could be found -- a second tray icon and a second swayidle manager
followed.  These tests pin down the command-line matching, the runner's
canonical command line, and the flock-based SingleInstance backstop.
"""

import os
import signal

import pwr_tray.Utils as Utils
from pwr_tray.Utils import PyKill, SingleInstance, command_names, script_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def keys(*argv):
    """Comparable names for one process command line."""
    return {script_key(name) for name in command_names(list(argv))}


# --------------------------------------------------------------------------
# command-line recognition
# --------------------------------------------------------------------------

def test_restarted_instance_is_recognised():
    assert 'pwr-tray' in keys('/usr/bin/python3', '-m', 'pwr_tray.main')
    # interpreter options before -m must not hide the module
    assert 'pwr-tray' in keys('/usr/bin/python3.12', '-u', '-O',
                              '-m', 'pwr_tray.main', '--stdout')


def test_console_script_is_recognised():
    assert 'pwr-tray' in keys('/usr/bin/python3', '/home/u/.local/bin/pwr-tray')
    assert 'pwr-tray' in keys('/usr/bin/python3', 'pwr-tray')


def test_script_file_is_recognised_by_its_package_directory():
    # python pwr_tray/main.py -- only the directory names the app
    assert 'pwr-tray' in keys('/usr/bin/python3',
                              '/home/u/Projects/pwr-tray/pwr_tray/main.py')


def test_unrelated_processes_are_not_matched():
    assert 'pwr-tray' not in keys('/usr/bin/python3', '-m', 'pytest', 'tests')
    assert 'pwr-tray' not in keys('/usr/bin/python3', '-m', 'pwr_tool.main')
    assert 'pwr-tray' not in keys('/usr/bin/python3', '-c', 'import pwr_tray')
    assert 'pwr-tray' not in keys('/usr/bin/ssh', 'host')
    assert 'pwr-tray' not in keys()


def test_kill_script_kills_restarted_instance_only(monkeypatch):
    lines = [
        '    PID COMMAND\n',                                       # ps header
        '    123 /usr/bin/python3 -m pwr_tray.main\n',             # restarted
        '    456 /usr/bin/python3 -m pytest tests\n',              # unrelated
        f'    {os.getpid()} /usr/bin/python3 -m pwr_tray.main\n',  # ourselves
    ]
    killed = []
    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))
    monkeypatch.setattr(Utils.os, 'kill',
                        lambda pid, sig: killed.append((pid, sig)))

    alive = PyKill()._kill_script(['pwr-tray'], signal.SIGTERM)

    assert killed == [(123, signal.SIGTERM)]
    assert alive == {'pwr-tray': '123 /usr/bin/python3 -m pwr_tray.main'}


def test_kill_script_reports_processes_that_survived(monkeypatch):
    lines = ['    123 /usr/bin/python3 -m pwr_tray.main\n']

    def kill_ignored(pid, sig):
        raise OSError('no such process')

    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))
    monkeypatch.setattr(Utils.os, 'kill', kill_ignored)

    assert PyKill()._kill_script(['pwr-tray'], signal.SIGKILL) == {}


# --------------------------------------------------------------------------
# development launcher
# --------------------------------------------------------------------------

def test_runner_execs_the_canonical_command_line():
    """./runner must look like a restarted instance to ps (and to PyKill)."""
    with open(os.path.join(ROOT, 'runner'), encoding='utf-8') as handle:
        text = handle.read()
    assert "'-m', 'pwr_tray.main'" in text
    assert 'import runpy' not in text
    assert 'dwipe' not in text     # copy-paste leftover from another project


# --------------------------------------------------------------------------
# single-instance lock
# --------------------------------------------------------------------------

def test_second_instance_cannot_take_the_lock(tmp_path):
    path = str(tmp_path / 'pwr-tray.lock')
    first, second = SingleInstance(path), SingleInstance(path)
    assert first.acquire() is True
    try:
        assert SingleInstance.read_pid(path) == os.getpid()
        assert second.acquire() is False
    finally:
        first.release()
    # Once released (as restart_self does) the replacement may take it.
    assert second.acquire() is True
    second.release()
    assert SingleInstance.read_pid(path) == os.getpid()


def test_release_current_frees_the_lock(tmp_path):
    path = str(tmp_path / 'pwr-tray.lock')
    first = SingleInstance(path)
    assert first.acquire() is True
    try:
        assert SingleInstance.current is first
        SingleInstance.release_current()
        assert SingleInstance.current is None
        assert SingleInstance(path).acquire() is True
    finally:
        SingleInstance.release_current()


def test_lock_file_without_a_holder_does_not_block(tmp_path):
    """A crashed instance leaves a pid file, but the kernel dropped the lock."""
    path = tmp_path / 'pwr-tray.lock'
    path.write_text('999999\n', encoding='utf-8')
    lock = SingleInstance(str(path))
    assert lock.acquire() is True
    lock.release()
