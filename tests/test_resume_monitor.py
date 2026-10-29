"""The dbus-monitor child must never outlive the applet it was started for.

A leaked dbus-monitor per restart (AUDIT.md finding 8) is invisible in the UI
but shows up as a growing pile of processes.  These tests pin the two exit
paths that matter: the normal one (atexit-registered stopper) and
``restart_self()``, which leaves through ``os._exit()`` and therefore has to
stop the monitor itself.
"""

import subprocess
import sys

import pwr_tray.main as main_module
from pwr_tray.main import PwrTray


def _bare_tray(proc):
    """A PwrTray without __init__: only what the resume monitor touches."""
    tray = object.__new__(PwrTray)
    tray._resume_proc = proc
    tray._resume_buf = b''
    return tray


def _spy_process():
    """A real child process that will not exit on its own."""
    return subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.PIPE)


def test_stop_resume_monitor_terminates_and_reaps():
    proc = _spy_process()
    tray = _bare_tray(proc)

    tray._stop_resume_monitor()

    assert proc.poll() is not None, 'dbus-monitor child left running'
    assert tray._resume_proc is None


def test_stop_resume_monitor_is_idempotent():
    proc = _spy_process()
    tray = _bare_tray(proc)

    tray._stop_resume_monitor()
    tray._stop_resume_monitor()       # atexit may fire after an explicit stop

    assert proc.poll() is not None


def test_stop_resume_monitor_handles_absent_child():
    tray = _bare_tray(None)
    tray._stop_resume_monitor()       # must not raise


def test_check_resume_reaps_a_dead_monitor():
    """dbus-monitor dies (e.g. system bus restart): drop it, stop reading."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'],
                            stdout=subprocess.PIPE)
    proc.wait()
    tray = _bare_tray(proc)

    tray._check_resume()

    assert tray._resume_proc is None
    assert proc.stdout.closed


def test_restart_self_stops_monitor_before_spawning(monkeypatch):
    """os._exit() skips atexit, so restart_self must stop the child itself."""
    calls = []

    class FakeTrayIcon:
        @staticmethod
        def hide():
            calls.append('hide')

    class FakeThis:
        tray_icon = FakeTrayIcon()

        def _stop_resume_monitor(self):
            calls.append('stop-monitor')

    monkeypatch.setattr(main_module.PwrTray, 'singleton', FakeThis())
    monkeypatch.setattr(main_module.PwrTray, 'save_picks',
                        staticmethod(lambda: calls.append('save-picks')))
    monkeypatch.setattr(main_module.Utils.SingleInstance, 'release_current',
                        staticmethod(lambda: calls.append('release-lock')))
    monkeypatch.setattr(main_module.subprocess, 'Popen',
                        lambda *a, **kw: calls.append('spawn'))
    monkeypatch.setattr(main_module.os, '_exit',
                        lambda code: calls.append(f'exit-{code}'))

    main_module.PwrTray.restart_self(None)

    assert 'stop-monitor' in calls
    assert calls.index('stop-monitor') < calls.index('spawn')
    assert calls[-1] == 'exit-0'
