"""Resume detection is part of the system-bus event source (AUDIT.md R5).

The applet used to run a third ``dbus-monitor`` child just for
``PrepareForSleep`` and read its pipe with a non-blocking ``os.read`` on every
tick.  That signal now rides on ``PowerEvents``' existing system-bus monitor,
so these tests pin two things:

- the system-monitor command actually asks dbus-monitor for the signal, and
- only ``PrepareForSleep(false)`` -- resume, not suspend -- is reported.

``restart_self()`` leaves through ``os._exit()`` and therefore still has to
stop the event source itself, which is pinned here too.
"""

import pwr_tray.Actions as actions_module
import pwr_tray.main as main_module
import pwr_tray.PowerEvents as events_module
from pwr_tray.PowerEvents import PowerEvents


def _header(member='PrepareForSleep'):
    return ('signal time=1 sender=:1.2 -> destination=(null destination) '
            'serial=2 path=/org/freedesktop/login1; '
            'interface=org.freedesktop.login1.Manager; '
            f'member={member}; signature=b')


def _watcher():
    calls = []
    watcher = PowerEvents(on_resume=lambda: calls.append('resume'))
    watcher._pending['system'] = b''
    watcher._context['system'] = ''
    return watcher, calls


def test_prepare_for_sleep_false_reports_a_resume():
    watcher, calls = _watcher()

    watcher._scan_line('system', _header())
    watcher._scan_line('system', '   boolean false')

    assert calls == ['resume']


def test_prepare_for_sleep_true_is_not_a_resume():
    """``true`` means suspend is starting; nothing has to be re-read yet."""
    watcher, calls = _watcher()

    watcher._scan_line('system', _header())
    watcher._scan_line('system', '   boolean true')

    assert calls == []


def test_inhibitor_and_resume_signals_share_the_system_monitor():
    """One system-bus child now carries both match rules."""
    spawned = []

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeNotifier:
        Read = 1

        def __init__(self, _fd, _mode):
            self.activated = FakeSignal()

        def setEnabled(self, _enabled):
            pass

    class FakeStdout:
        @staticmethod
        def fileno():
            return 0

        @staticmethod
        def close():
            pass

    class FakeProc:
        stdout = FakeStdout()

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            pass

        @staticmethod
        def kill():
            pass

    def fake_popen(args, **_kwargs):
        spawned.append(args)
        return FakeProc()

    original_notifier = events_module.QSocketNotifier
    original_set_blocking = events_module.os.set_blocking
    original_popen = events_module.subprocess.Popen
    events_module.QSocketNotifier = FakeNotifier
    events_module.os.set_blocking = lambda _fd, _blocking: None
    events_module.subprocess.Popen = fake_popen
    try:
        watcher = PowerEvents()
        assert watcher._start_monitor('system', '--system')
        watcher.stop()
    finally:
        events_module.QSocketNotifier = original_notifier
        events_module.os.set_blocking = original_set_blocking
        events_module.subprocess.Popen = original_popen

    assert events_module.PREPARE_FOR_SLEEP in spawned[0]
    assert events_module.PROPERTIES_CHANGED in spawned[0]


def test_restart_self_stops_event_source_before_spawning(monkeypatch):
    """os._exit() skips atexit, so restart_self must stop the children itself."""
    calls = []

    class FakeTrayIcon:
        @staticmethod
        def hide():
            calls.append('hide')

    class FakeEvents:
        @staticmethod
        def stop():
            calls.append('stop-events')

    class FakeThis:
        tray_icon = FakeTrayIcon()
        events = FakeEvents()

        @staticmethod
        def save_picks():
            calls.append('save-picks')

    monkeypatch.setattr(main_module.PwrTray, 'singleton', FakeThis())
    monkeypatch.setattr(main_module.Utils.SingleInstance, 'release_current',
                        staticmethod(lambda: calls.append('release-lock')))
    # restart_self() lives in the action layer now, so the process it would
    # spawn is faked there -- never in main.py.
    monkeypatch.setattr(actions_module.subprocess, 'Popen',
                        lambda *a, **kw: calls.append('spawn'))
    monkeypatch.setattr(actions_module.os, '_exit',
                        lambda code: calls.append(f'exit-{code}'))

    main_module.PwrTray.restart_self(None)

    assert 'stop-events' in calls
    assert calls.index('stop-events') < calls.index('spawn')
    assert calls[-1] == 'exit-0'
