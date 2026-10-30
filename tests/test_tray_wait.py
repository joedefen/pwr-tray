"""Tray detection must not cost more than it is worth (audit finding 11).

pwr_tray.main used to own a module-level pydbus ``SessionBus``, so merely
importing the module -- and therefore ``pwr-tray --help`` -- needed a session
bus, and ``wait_for_tray(timeout=300)`` blocked silently for up to five minutes
while its result was thrown away.

These tests pin the replacement: QtDBus is imported lazily (so no bus is opened
at import time and pydbus is not needed at all), the wait is bounded and
reports progress, and "no watcher" is a False the caller acts on.
"""

import inspect
import sys
import types

import pytest

import pwr_tray.main as main_module

# -- fakes for the QtDBus surface we use -------------------------------------

class FakeReply:
    """Stands in for QDBusReply<bool>."""

    def __init__(self, value=False, valid=True):
        self._value = value
        self._valid = valid

    def isValid(self):
        return self._valid

    def value(self):
        return self._value


class FakeInterface:
    """Stands in for QDBusConnectionInterface."""

    def __init__(self, reply):
        self.reply = reply
        self.timeout = None
        self.queried = []

    def setTimeout(self, milliseconds):
        self.timeout = milliseconds

    def isServiceRegistered(self, name):
        self.queried.append(name)
        return self.reply


class FakeBus:
    """Stands in for QDBusConnection."""

    def __init__(self, connected=True, reply=None):
        self._connected = connected
        self.interface_obj = FakeInterface(reply or FakeReply())

    def isConnected(self):
        return self._connected

    def interface(self):
        return self.interface_obj


def install_fake_qtdbus(monkeypatch, bus):
    module = types.ModuleType('PyQt5.QtDBus')
    module.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: bus)
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', module)


class FakeClock:
    """A monotonic clock whose sleep() advances time instead of blocking."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(main_module, 'time', fake)
    return fake


@pytest.fixture
def reported(monkeypatch):
    lines = []
    monkeypatch.setattr(main_module, 'prt',
                        lambda *args, **kwargs: lines.append(args[0] if args else ''))
    return lines


# -- no D-Bus at import time -------------------------------------------------

def test_import_does_not_open_a_session_bus():
    """The module must not create a bus while being imported."""
    source = inspect.getsource(main_module)
    assert 'import pydbus' not in source
    assert 'SessionBus' not in source
    assert not hasattr(main_module, 'SESSION_BUS')


# -- tray_watcher_present() --------------------------------------------------

def test_present_is_true_when_the_watcher_owns_the_name(monkeypatch):
    bus = FakeBus(reply=FakeReply(True))
    install_fake_qtdbus(monkeypatch, bus)

    assert main_module.tray_watcher_present() is True
    assert bus.interface_obj.queried == [main_module.STATUS_NOTIFIER_WATCHER]


def test_present_uses_a_short_bus_timeout(monkeypatch):
    """The 25s D-Bus default would defeat the caller's deadline."""
    bus = FakeBus(reply=FakeReply(True))
    install_fake_qtdbus(monkeypatch, bus)

    main_module.tray_watcher_present()

    assert bus.interface_obj.timeout is not None
    assert bus.interface_obj.timeout <= 5000


def test_present_is_false_when_nothing_owns_the_name(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(reply=FakeReply(False)))
    assert main_module.tray_watcher_present() is False


def test_present_is_false_for_an_invalid_reply(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(reply=FakeReply(True, valid=False)))
    assert main_module.tray_watcher_present() is False


def test_present_is_none_without_a_session_bus(monkeypatch, reported):
    install_fake_qtdbus(monkeypatch, FakeBus(connected=False))

    assert main_module.tray_watcher_present() is None
    assert any('session bus' in line for line in reported)


def test_present_is_none_without_qtdbus(monkeypatch, reported):
    """PyQt5 can be installed without QtDBus; the applet must still start."""
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', None)

    assert main_module.tray_watcher_present() is None
    assert any('QtDBus' in line for line in reported)


# -- wait_for_tray() ---------------------------------------------------------

def test_wait_returns_true_as_soon_as_the_watcher_appears(monkeypatch, clock):
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)

    assert main_module.wait_for_tray(timeout=30) is True
    assert clock.slept == []


def test_wait_is_bounded_when_there_is_no_watcher(monkeypatch, clock):
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: False)

    assert main_module.wait_for_tray(timeout=10) is False
    assert sum(clock.slept) == pytest.approx(10, abs=0.01)


def test_wait_reports_progress_instead_of_blocking_silently(monkeypatch, clock,
                                                            reported):
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: False)

    main_module.wait_for_tray(timeout=12)

    assert any(main_module.STATUS_NOTIFIER_WATCHER in line for line in reported)


def test_wait_gives_up_immediately_when_the_bus_is_unusable(monkeypatch, clock):
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: None)

    assert main_module.wait_for_tray(timeout=300) is False
    assert clock.slept == []
