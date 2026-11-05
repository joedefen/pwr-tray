"""Tray (re-)registration must survive a bar that starts late.

``pwr_tray.main`` used to own a module-level pydbus ``SessionBus``, so merely
importing the module -- and therefore ``pwr-tray --help`` -- needed a session
bus, and ``wait_for_tray(timeout=300)`` blocked silently for up to five minutes
while its result was thrown away.  The bounded wait was the wrong shape as
well: a bar that started after the wait, or restarted, left the applet running
with an item no host knew about, because Qt registers the icon once and never
retries -- exactly the pwr-tray-invisible-next-to-nm-applet case.

These tests pin the replacement: QtDBus is imported lazily (so no bus is opened
at import time and pydbus is not needed at all), the watcher's own bus name is
the event that re-registers the item, and "cannot ask the bus" is a ``None`` the
caller reports instead of a wait.

They also pin what that event can and cannot repair, which is narrower than it
looks.  Measured on Qt 5.15's Wayland platform plugin: an item shown while no
host exists is inert, and so is every item shown later in that same process --
the plugin decides once, at the first show.  A unit is started before the bar,
so the applet must not show an item until a host is there (``defer_tray_item``),
and re-registration has to be *verified* against the host's own item list
instead of being assumed (``tray_item_registered``).
"""

import inspect
import os
import sys
import types
from typing import ClassVar

import pytest

import pwr_tray.main as main_module
from pwr_tray.TrayWatch import (
    STATUS_NOTIFIER_ITEMS,
    STATUS_NOTIFIER_WATCHER,
    STATUS_NOTIFIER_WATCHER_PATH,
    TrayWatch,
)

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


class FakeSignal:
    """Stands in for a Qt signal on a bus interface."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


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


class FakeServiceWatcher:
    """Stands in for QDBusServiceWatcher; instances are recorded for the tests.

    A real watcher reports a name registered by another process; the fake just
    hands the signals to whoever asked for them.
    """

    WatchForRegistration = 1
    WatchForUnregistration = 2
    instances: ClassVar[list] = []

    def __init__(self, service, connection, mode):
        self.service = service
        self.connection = connection
        self.mode = mode
        self.serviceRegistered = FakeSignal()
        self.serviceUnregistered = FakeSignal()
        FakeServiceWatcher.instances.append(self)


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
    """Install a fake QtDBus module whose watchers are recorded."""
    FakeServiceWatcher.instances = []
    module = types.ModuleType('PyQt5.QtDBus')
    module.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: bus)
    module.QDBusServiceWatcher = FakeServiceWatcher
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', module)


def last_watcher():
    """The QDBusServiceWatcher the code under test just created."""
    return FakeServiceWatcher.instances[-1]


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


def test_start_up_no_longer_blocks_on_the_tray():
    """A bar that is late is an event; there is no fixed wait any more."""
    assert not hasattr(main_module, 'wait_for_tray')
    assert not hasattr(main_module, 'TRAY_WAIT_SECONDS')


# -- tray_watcher_present() --------------------------------------------------

def test_present_is_true_when_the_watcher_owns_the_name(monkeypatch):
    bus = FakeBus(reply=FakeReply(True))
    install_fake_qtdbus(monkeypatch, bus)

    assert main_module.tray_watcher_present() is True
    assert bus.interface_obj.queried == [main_module.STATUS_NOTIFIER_WATCHER]


def test_present_uses_a_short_bus_timeout(monkeypatch):
    """The 25s D-Bus default would stall start-up if the bus is wedged."""
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


# -- TrayWatch ---------------------------------------------------------------

def make_watch(monkeypatch, bus):
    """A TrayWatch on a fake bus, with its two callbacks recorded."""
    install_fake_qtdbus(monkeypatch, bus)
    events = []
    watch = TrayWatch(on_available=lambda: events.append('available'),
                      on_lost=lambda: events.append('lost'))
    assert watch.start() is True
    assert watch.watching is True
    return watch, events


def test_the_watch_is_bound_to_the_tray_host_name(monkeypatch):
    make_watch(monkeypatch, FakeBus())

    watcher = last_watcher()
    assert watcher.service == STATUS_NOTIFIER_WATCHER
    assert watcher.mode == (FakeServiceWatcher.WatchForRegistration
                            | FakeServiceWatcher.WatchForUnregistration)


def test_the_watcher_appearing_asks_for_a_registration(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['available']


def test_a_watcher_restart_is_an_appearance_too(monkeypatch):
    """waybar being restarted re-registers its name, i.e. starts over."""
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)
    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['lost', 'available']


def test_the_watcher_leaving_is_reported_as_a_loss(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == ['lost']


def test_another_name_is_ignored(monkeypatch):
    _watch, events = make_watch(monkeypatch, FakeBus())

    last_watcher().serviceRegistered.emit('org.freedesktop.Notifications')
    last_watcher().serviceUnregistered.emit('org.freedesktop.Notifications')

    assert events == []


def test_a_missing_on_lost_callback_is_not_an_error(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus())
    watch = TrayWatch(on_available=lambda: None)
    assert watch.start() is True

    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)


def test_stop_disconnects_the_watch(monkeypatch):
    watch, events = make_watch(monkeypatch, FakeBus())

    watch.stop()
    last_watcher().serviceRegistered.emit(STATUS_NOTIFIER_WATCHER)
    last_watcher().serviceUnregistered.emit(STATUS_NOTIFIER_WATCHER)

    assert events == []
    assert watch.watching is False
    assert last_watcher().serviceRegistered.slots == []
    assert last_watcher().serviceUnregistered.slots == []


def test_stop_is_safe_before_start(monkeypatch):
    TrayWatch(on_available=lambda: None).stop()


def test_start_is_false_without_qtdbus(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', None)

    watch = TrayWatch(on_available=lambda: None)

    assert watch.start() is False
    assert watch.watching is False


def test_start_is_false_without_a_session_bus(monkeypatch):
    install_fake_qtdbus(monkeypatch, FakeBus(connected=False))

    watch = TrayWatch(on_available=lambda: None)

    assert watch.start() is False
    assert watch.watching is False


# -- PwrTray: re-registering the item ----------------------------------------

class FakeTrayIcon:
    """Records what QSystemTrayIcon is asked to do."""

    def __init__(self, icon, parent=None):
        self.icon = icon
        self.parent = parent
        self.menu = None
        self.tooltip = ''
        self.visible = False
        self.hidden = 0
        self.deleted = 0
        self.activated = FakeSignal()

    def setToolTip(self, text):
        self.tooltip = text

    def setContextMenu(self, menu):
        self.menu = menu

    def setVisible(self, value):
        self.visible = value

    def hide(self):
        self.hidden += 1
        self.visible = False

    def deleteLater(self):
        self.deleted += 1


def bare_tray(monkeypatch, current_icon_num=-1, menu='MENU'):
    """A PwrTray stripped down to the tray-registration surface."""
    monkeypatch.setattr(main_module, 'QSystemTrayIcon', FakeTrayIcon)
    # Never ask the real session bus from a unit test: on a developer's machine
    # a bar may be running, and the answer would then depend on it.  The tests
    # that care about the answer set it themselves.
    monkeypatch.setattr(main_module, 'tray_item_registered', lambda: None)
    tray = object.__new__(main_module.PwrTray)
    tray.app = object()
    tray.icons = ['icon-0', 'icon-1', 'icon-2']
    tray.current_icon_num = current_icon_num
    tray.menu = menu
    tray.is_wayland = False
    return tray


def test_installing_the_icon_shows_it_with_the_current_icon(monkeypatch):
    tray = bare_tray(monkeypatch, current_icon_num=2)

    tray._install_tray_icon()

    assert tray.tray_icon.icon == 'icon-2'
    assert tray.tray_icon.visible is True
    assert tray.tray_icon.menu == 'MENU'
    assert tray.tray_icon.tooltip == 'pwr-tray'


def test_the_first_icon_is_used_before_any_state_was_shown(monkeypatch):
    tray = bare_tray(monkeypatch, current_icon_num=-1)

    tray._install_tray_icon()

    assert tray.tray_icon.icon == 'icon-0'


def test_reinstalling_hides_the_old_item_instead_of_listing_two(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._install_tray_icon()
    previous = tray.tray_icon

    tray._install_tray_icon()

    assert previous.hidden == 1
    assert previous.deleted == 1
    assert tray.tray_icon is not previous
    assert tray.tray_icon.menu == 'MENU'
    assert tray.tray_icon.visible is True


def test_an_appearing_host_schedules_a_re_registration(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = False
    scheduled = []
    monkeypatch.setattr(main_module, 'QTimer', types.SimpleNamespace(
        singleShot=lambda delay, slot: scheduled.append((delay, slot))))

    tray._on_tray_host_appeared()

    assert tray._tray_seen is True
    assert scheduled == [(main_module.TRAY_ATTACH_DELAY_MS,
                          tray._reattach_tray)]


def test_a_host_that_leaves_again_gets_no_item(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = True
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: False)

    tray._reattach_tray()

    assert tray._tray_seen is False
    assert not hasattr(tray, 'tray_icon')


def test_a_host_that_is_really_there_gets_the_item_again(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._install_tray_icon()
    previous = tray.tray_icon
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)

    tray._reattach_tray()

    assert previous.hidden == 1
    assert tray.tray_icon is not previous


def test_losing_the_host_marks_the_item_as_unregistered(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray._tray_seen = True

    tray._on_tray_host_lost()

    assert tray._tray_seen is False


def test_the_resync_retries_a_missed_announcement(monkeypatch):
    """A watcher whose name-owner signal never arrived is still picked up."""
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=True)
    tray._tray_seen = False
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    retried = []
    monkeypatch.setattr(tray, '_on_tray_host_appeared',
                        lambda: retried.append(True))

    tray._ensure_tray_attached()

    assert retried == [True]


def test_the_resync_does_nothing_while_attached(monkeypatch):
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=True)
    tray._tray_seen = True
    monkeypatch.setattr(main_module, 'tray_watcher_present',
                        lambda: pytest.fail('the bus must not be asked'))

    tray._ensure_tray_attached()


def test_the_resync_does_nothing_when_watching_is_impossible(monkeypatch):
    """Without a watch there is no missed signal to compensate for."""
    tray = bare_tray(monkeypatch)
    tray.tray_watch = types.SimpleNamespace(watching=False)
    tray._tray_seen = False
    monkeypatch.setattr(main_module, 'tray_watcher_present',
                        lambda: pytest.fail('the bus must not be asked'))

    tray._ensure_tray_attached()


def test_the_watcher_name_is_the_one_qt_hosts_claim():
    assert main_module.STATUS_NOTIFIER_WATCHER == STATUS_NOTIFIER_WATCHER
    assert STATUS_NOTIFIER_WATCHER == 'org.kde.StatusNotifierWatcher'


# -- tray_item_registered(): is the item really on the host's list? -----------

class FakeMessage:
    """Stands in for QDBusMessage, recording the Get it was built for."""

    ReplyMessage = 1

    def __init__(self, service, path, interface, method):
        self.service = service
        self.path = path
        self.interface = interface
        self.method = method
        self.arguments = []

    def __lshift__(self, value):
        self.arguments.append(value)
        return self


class FakeCallReply:
    """Stands in for the reply to Properties.Get."""

    def __init__(self, listed, kind=1):
        self.listed = listed
        self.kind = kind

    def type(self):
        return self.kind

    def arguments(self):
        return [] if self.listed is None else [self.listed]

    def errorMessage(self):
        return 'no reply'


class CallingBus(FakeBus):
    """A FakeBus whose blocking call answers like the watcher would."""

    def __init__(self, listed, base=':1.42', connected=True):
        super().__init__(connected=connected, reply=FakeReply(True))
        self.listed = listed
        self.base = base
        self.calls = []

    def baseService(self):
        return self.base

    def call(self, message, mode=None, timeout=None):
        self.calls.append((message, mode, timeout))
        if isinstance(self.listed, Exception):
            raise self.listed
        return FakeCallReply(self.listed)


def install_calling_bus(monkeypatch, bus):
    """install_fake_qtdbus(), plus the QDBusMessage/QDBus surface Get needs."""
    FakeServiceWatcher.instances = []
    module = types.ModuleType('PyQt5.QtDBus')
    module.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: bus)
    module.QDBusServiceWatcher = FakeServiceWatcher
    module.QDBus = types.SimpleNamespace(Block=0, NoBlock=1)
    module.QDBusMessage = types.SimpleNamespace(
        createMethodCall=FakeMessage, ReplyMessage=FakeMessage.ReplyMessage)
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', module)


def test_registered_is_true_for_an_item_naming_this_process(monkeypatch):
    """Qt registers as org.kde.StatusNotifierItem-<pid>-<n>."""
    installed = f'org.kde.StatusNotifierItem-{os.getpid()}-1/StatusNotifierItem'
    install_calling_bus(monkeypatch, CallingBus([installed]))

    assert main_module.tray_item_registered() is True


def test_registered_is_true_for_this_connections_unique_name(monkeypatch):
    """A host that registered a bare bus name is still recognised."""
    install_calling_bus(monkeypatch, CallingBus([':1.42/StatusNotifierItem'],
                                                base=':1.42'))

    assert main_module.tray_item_registered() is True


def test_registered_is_false_for_someone_elses_item(monkeypatch):
    install_calling_bus(monkeypatch, CallingBus(
        [':1.99/StatusNotifierItem',
         'org.kde.StatusNotifierItem-999999-1/StatusNotifierItem'], base=':1.42'))

    assert main_module.tray_item_registered() is False


def test_registered_asks_the_host_for_its_item_list(monkeypatch):
    bus = CallingBus([])
    install_calling_bus(monkeypatch, bus)

    main_module.tray_item_registered()

    message, _mode, timeout = bus.calls[0]
    assert message.service == STATUS_NOTIFIER_WATCHER
    assert message.path == STATUS_NOTIFIER_WATCHER_PATH
    assert message.interface == 'org.freedesktop.DBus.Properties'
    assert message.method == 'Get'
    assert message.arguments == [STATUS_NOTIFIER_WATCHER,
                                 STATUS_NOTIFIER_ITEMS]
    assert timeout is not None and timeout <= 5000


def test_registered_is_none_when_the_host_cannot_be_asked(monkeypatch, reported):
    """An unanswerable question is not evidence that nothing registered."""
    install_calling_bus(monkeypatch, CallingBus(RuntimeError('no such method')))

    assert main_module.tray_item_registered() is None
    assert any('tray list' in line for line in reported)


def test_registered_is_none_without_qtdbus(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', None)
    assert main_module.tray_item_registered() is None


def test_registered_is_none_without_a_session_bus(monkeypatch):
    install_calling_bus(monkeypatch, CallingBus([], connected=False))
    assert main_module.tray_item_registered() is None


# -- defer_tray_item(): Qt's one-shot answer must be the right one ------------

def test_a_wayland_item_waits_for_a_host():
    assert main_module.defer_tray_item(False, wayland=True) is True


def test_an_item_is_shown_at_once_when_a_host_is_there():
    assert main_module.defer_tray_item(True, wayland=True) is False


def test_an_x11_item_is_shown_at_once_although_no_watcher_exists():
    """XEmbed needs no watcher, so X11 keeps the old behaviour."""
    assert main_module.defer_tray_item(False, wayland=False) is False


def test_an_unaskable_bus_does_not_defer():
    assert main_module.defer_tray_item(None, wayland=True) is False


def test_the_session_type_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv('XDG_SESSION_TYPE', 'x11')
    assert main_module.wayland_session() is False
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    assert main_module.wayland_session() is True


def test_a_late_host_still_gets_a_verified_registration(monkeypatch, reported):
    tray = bare_tray(monkeypatch)
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    monkeypatch.setattr(main_module, 'tray_item_registered', lambda: True)

    tray._reattach_tray()

    assert tray.tray_icon.visible is True
    assert any('registered with the host' in line for line in reported)


def test_an_unverified_registration_is_reported_as_such(monkeypatch, reported):
    tray = bare_tray(monkeypatch)
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    monkeypatch.setattr(main_module, 'tray_item_registered', lambda: None)
    restarts = []
    monkeypatch.setattr(tray, 'restart_self', lambda _arg: restarts.append(True))

    tray._reattach_tray()

    assert restarts == []
    assert any('not verified' in line for line in reported)


def test_an_item_the_host_never_got_replaces_the_process(monkeypatch, reported):
    """Qt cannot revise its answer, so the process has to ask again."""
    tray = bare_tray(monkeypatch)
    tray._tray_restart_done = False
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    monkeypatch.setattr(main_module, 'tray_item_registered', lambda: False)
    restarts = []
    monkeypatch.setattr(tray, 'restart_self', lambda _arg: restarts.append(True))

    tray._reattach_tray()

    assert restarts == [True]
    assert any('restarting' in line for line in reported)


def test_the_process_is_replaced_at_most_once(monkeypatch, reported):
    """A host that can never be satisfied must not become a restart loop."""
    tray = bare_tray(monkeypatch)
    tray._tray_restart_done = False
    monkeypatch.setattr(main_module, 'tray_watcher_present', lambda: True)
    monkeypatch.setattr(main_module, 'tray_item_registered', lambda: False)
    restarts = []
    monkeypatch.setattr(tray, 'restart_self', lambda _arg: restarts.append(True))

    tray._reattach_tray()
    tray._reattach_tray()

    assert restarts == [True]
    assert any('stays invisible' in line for line in reported)
