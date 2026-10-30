"""Regression tests for audit finding P1-7 and residual finding R1.

P1-7: PyKill used to match only ``python <target>`` / ``python <target>.py``.
A restarted instance is ``python -m pwr_tray.main`` (``ps`` shows ``-m`` where
the script name used to be) and the development launcher was ``python
runner``, so neither could be found -- a second tray icon and a second
swayidle manager followed.

R1: the killer then ran *before* the lock, so a second launch never detected
the incumbent; it terminated it and took over.  With ``Restart=always``, two
supervisors could ping-pong forever.  These tests pin down command-line
matching, the guarded takeover, and the lock-first start-up order.
"""

import os
import signal
import types

import pytest

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


def test_matching_pids_finds_restarted_instance_only(monkeypatch):
    lines = [
        '    PID COMMAND\n',                                       # ps header
        '    123 /usr/bin/python3 -m pwr_tray.main\n',             # restarted
        '    456 /usr/bin/python3 -m pytest tests\n',              # unrelated
        f'    {os.getpid()} /usr/bin/python3 -m pwr_tray.main\n',  # ourselves
    ]
    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))

    assert PyKill.matching_pids(['pwr-tray']) == {
        123: '123 /usr/bin/python3 -m pwr_tray.main'}


def test_kill_pid_is_harmless_when_the_pid_is_no_longer_pwr_tray(monkeypatch):
    """A recorded pid may have been recycled: never signal on a name mismatch."""
    lines = ['    123 /usr/sbin/cupsd -l\n']
    killed = []
    monkeypatch.setattr(Utils.os, 'popen', lambda command: iter(lines))
    monkeypatch.setattr(Utils.os, 'kill',
                        lambda pid, sig: killed.append((pid, sig)))

    assert PyKill.kill_pid(123, ['pwr-tray']) is False
    assert killed == []


def test_kill_pid_refuses_its_own_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(Utils.os, 'kill',
                        lambda pid, sig: killed.append((pid, sig)))

    assert PyKill.kill_pid(os.getpid(), ['pwr-tray']) is False
    assert killed == []


def test_kill_pid_terminates_a_verified_instance(monkeypatch):
    state = {'alive': True}

    def fake_popen(command):
        if not state['alive']:
            return iter(['    PID COMMAND\n'])
        return iter(['    123 /usr/bin/python3 -m pwr_tray.main\n'])

    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGTERM:
            state['alive'] = False

    monkeypatch.setattr(Utils.os, 'popen', fake_popen)
    monkeypatch.setattr(Utils.os, 'kill', fake_kill)

    assert PyKill.kill_pid(123, ['pwr-tray'], grace=1.0, delay=0) is True
    assert killed == [(123, signal.SIGTERM)]


def test_kill_pid_escalates_to_sigkill_when_sigterm_is_ignored(monkeypatch):
    state = {'alive': True}
    killed = []

    def fake_popen(command):
        if not state['alive']:
            return iter(['    PID COMMAND\n'])
        return iter(['    123 /usr/bin/python3 -m pwr_tray.main\n'])

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == signal.SIGKILL:
            state['alive'] = False

    monkeypatch.setattr(Utils.os, 'popen', fake_popen)
    monkeypatch.setattr(Utils.os, 'kill', fake_kill)

    assert PyKill.kill_pid(123, ['pwr-tray'], grace=0, delay=0) is True
    assert killed == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


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


# --------------------------------------------------------------------------
# R1: the lock is taken first; killing is an explicit takeover
# --------------------------------------------------------------------------

class _FakeLock:
    """Stands in for SingleInstance without touching the filesystem."""

    def __init__(self, results, path='/tmp/pwr-tray-test/pwr-tray.lock'):
        self.path = path
        self.results = list(results)
        self.attempts = 0

    def acquire(self, retries=0, delay=0.2):
        self.attempts += 1
        return self.results.pop(0) if self.results else False

    def release(self):
        pass


def _install_fake_killer(monkeypatch, gone=True):
    """Replace main.PyKill with one that records every signal request."""
    import pwr_tray.main as main_module

    calls = []

    class FakeKill:
        @staticmethod
        def kill_pid(pid, targets, **kwargs):
            calls.append((pid, list(targets)))
            return gone

    monkeypatch.setattr(main_module, 'PyKill', FakeKill)
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    return calls


def _install_holder(monkeypatch, pid=4242):
    import pwr_tray.main as main_module

    monkeypatch.setattr(main_module.Utils.SingleInstance, 'read_pid',
                        staticmethod(lambda path: pid))


def test_plain_start_refuses_instead_of_killing_the_incumbent(monkeypatch):
    """The regression: without --replace, a second launch must not signal."""
    import pwr_tray.main as main_module

    lock = _FakeLock([False])
    calls = _install_fake_killer(monkeypatch)
    _install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock) is False
    assert calls == []            # the incumbent was left alone
    assert lock.attempts == 1     # and the failed start did not spin


def test_replace_kills_the_verified_holder_then_takes_the_lock(monkeypatch):
    import pwr_tray.main as main_module

    lock = _FakeLock([False, True])
    calls = _install_fake_killer(monkeypatch)
    _install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is True
    assert calls == [(4242, ['pwr-tray'])]
    assert lock.attempts == 2


def test_replace_does_not_kill_a_pid_that_is_not_pwr_tray(monkeypatch):
    import pwr_tray.main as main_module

    lock = _FakeLock([False, False])
    calls = _install_fake_killer(monkeypatch, gone=False)
    _install_holder(monkeypatch)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is False
    assert calls == [(4242, ['pwr-tray'])]   # asked, but no takeover happened
    assert lock.attempts == 1


def test_replace_without_a_recorded_holder_does_not_kill(monkeypatch):
    import pwr_tray.main as main_module

    lock = _FakeLock([False])
    calls = _install_fake_killer(monkeypatch)
    _install_holder(monkeypatch, pid=None)

    assert main_module.acquire_lock_or_take_over(lock, replace=True) is False
    assert calls == []


def test_main_exits_3_without_touching_processes_when_lock_is_held(monkeypatch):
    """main() must not build a killer before it knows it owns the lock."""
    import pwr_tray.main as main_module

    class FakeIniTool:
        folder = '/tmp/pwr-tray-test'
        ini_path = os.path.join(folder, 'config.ini')
        log_path = os.path.join(folder, 'debug.log')

        def __init__(self, paths_only=False):
            pass

        def update_config(self):
            return False

        def get_selectors(self):
            return []

    class RefusingLock:
        def __init__(self, path):
            self.path = path

        def acquire(self, retries=0, delay=0.2):
            return False

        def release(self):
            pass

        @staticmethod
        def read_pid(path):
            return 4242

    class NoKill:
        def __init__(self, *args, **kwargs):
            raise AssertionError('PyKill must not run before the lock is ours')

    reported = []
    fake_utils = types.SimpleNamespace(
        prt_path='',
        capture_console=lambda: None,
        error_print=reported.append,
        SingleInstance=RefusingLock)

    monkeypatch.setattr(main_module, 'IniTool', FakeIniTool)
    monkeypatch.setattr(main_module, 'Utils', fake_utils)
    monkeypatch.setattr(main_module, 'PyKill', NoKill)
    monkeypatch.setattr(main_module, 'prt', lambda *args, **kw: None)
    monkeypatch.setattr('sys.argv', ['pwr-tray'])

    with pytest.raises(SystemExit) as raised:
        main_module.main()

    assert raised.value.code == 3
    assert 'already running' in reported[0]
    assert '4242' in reported[0]
    assert '--replace' in reported[0]
