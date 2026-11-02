"""A logout must be able to ask the applet to leave.

On X11 a logout is a conversation, not a kill: ksmserver asks every client
registered with it -- and Qt registers as soon as ``SESSION_MANAGER`` is set,
which KDE does for a terminal run and for a desktop autostart -- to save itself
and quit, then waits for that client to disappear.  Qt reports the request as
``commitDataRequest``.

Nothing was connected to it, so pwr-tray said nothing and waited to be killed.
With KDE's "logout immediately" option that is the hang the user sees; the
confirmation prompt gave the session manager a chance to time out or kill us
first, which is why that path looked like it worked.

These tests drive the two handlers directly with a fake QApplication, so they
need no X server and no session manager.
"""

import pytest

from pwr_tray import main as app_main

RESTART_NEVER = app_main.QSessionManager.RestartHint.RestartNever


class FakeSignal:
    """The one `QApplication` signal `install_session_shutdown_handler()` uses."""

    def __init__(self, refuse=None):
        self.slots = []
        self.refuse = refuse

    def connect(self, slot):
        if self.refuse is not None:
            raise self.refuse('QObject::connect: no such signal')
        self.slots.append(slot)


class FakeApp:
    """The `QApplication` surface the session-shutdown handlers touch."""

    def __init__(self, refuse_signal=None):
        self.commitDataRequest = FakeSignal(refuse=refuse_signal)
        self.quits = 0

    def quit(self):
        self.quits += 1


class FakeSessionManager:
    """Stands in for the `QSessionManager` Qt hands to `commitDataRequest`."""

    def __init__(self, refuse=False, no_hint=False):
        self.restart_hints = []
        self.refuse = refuse
        self.no_hint = no_hint

    def setRestartHint(self, hint):
        if self.no_hint:
            raise AttributeError('no session to hint at')
        if self.refuse:
            raise RuntimeError('no session to hint at')
        self.restart_hints.append(hint)


class SessionTray:
    """A tray with only what the session-shutdown handlers touch."""

    install_session_shutdown_handler = \
        app_main.PwrTray.install_session_shutdown_handler
    _on_commit_data_request = app_main.PwrTray._on_commit_data_request

    def __init__(self, app=None):
        self.app = FakeApp() if app is None else app


def test_a_session_manager_shutdown_quits_the_event_loop():
    tray = SessionTray()
    tray.install_session_shutdown_handler()
    assert len(tray.app.commitDataRequest.slots) == 1

    manager = FakeSessionManager()
    tray.app.commitDataRequest.slots[0](manager)

    # Quitting is what turns the logout into a clean exit.  RestartNever is the
    # protocol's own way of saying "do not restore this client".
    assert tray.app.quits == 1
    assert manager.restart_hints == [RESTART_NEVER]


@pytest.mark.parametrize('error', [TypeError, AttributeError])
def test_a_qt_without_session_management_still_starts(error):
    # No XSMP (Wayland, a bare X session, the shipped unit) means the signal
    # never fires -- but a Qt without it must not keep the tray from starting.
    tray = SessionTray(app=FakeApp(refuse_signal=error))
    tray.install_session_shutdown_handler()
    assert tray.app.commitDataRequest.slots == []


@pytest.mark.parametrize('manager', [
    FakeSessionManager(refuse=True),
    FakeSessionManager(no_hint=True),
])
def test_a_manager_that_refuses_the_restart_hint_still_quits(manager):
    # The hint is a courtesy; leaving the session is the promise.
    tray = SessionTray()
    tray.install_session_shutdown_handler()
    tray.app.commitDataRequest.slots[0](manager)
    assert tray.app.quits == 1
