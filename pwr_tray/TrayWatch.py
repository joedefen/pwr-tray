"""Watch for a tray host that appears -- or restarts -- after the applet.

Qt registers a ``QSystemTrayIcon`` with ``org.kde.StatusNotifierWatcher`` once,
when the item is first shown, and never retries.  A session where the bar comes
up later than the applet -- waybar still starting, the applet launched by a
systemd unit before the compositor's bar, a bar that crashed and was started
again -- therefore leaves the applet running with an item no host knows about:
it is alive, it keeps working, and it is invisible until someone restarts it.
Other applets in the same session (nm-applet, copyq) put their items on the bus
again on their own, which is why they show up where pwr-tray does not.

Waiting at start-up cannot fix that.  A bar that starts five minutes later is
outside any bounded wait, and a bar that *restarts* is outside it entirely, so
the old ``wait_for_tray()`` only moved the failure window.  The watcher's own
bus name is the event instead: every time the name is registered, the applet is
asked to register its item again.

What that event can and cannot repair is narrower than it looks, and it is
worth writing down because the difference is invisible in a log.  Measured on
Qt 5.15's Wayland platform plugin (the PyQt5 build this applet ships against):

* an item shown while a host exists registers immediately, and re-creating the
  item after the bar *restarts* registers again -- the watch's job;
* an item shown while no host exists is inert, and so is every item shown later
  in that same process, even with a host present by then.  The plugin decides
  once, at the first show, and never revisits it, so re-creating the item after
  a host appears reports success while nothing is on the bus.

That is why ``main.py`` does not show an item at all until a host is there
(``defer_tray_item()``) and why re-registration is verified against the host's
``RegisteredStatusNotifierItems`` instead of being assumed (``tray_item_registered()``).

The trigger is ``QDBusServiceWatcher``, not a hand-made connection to
``NameOwnerChanged``: it is the supported API for exactly this question, and it
reports a name registered by *another* process.  (The visible variants do not
all work -- ``QDBusConnectionInterface.serviceOwnerChanged`` still relays the
bus signal but is deprecated and prints a notice into debug.log, while
``serviceRegistered`` there only covers services this same connection
registered.)

QtDBus is imported lazily, so importing this module never opens a bus.  A
failed watch is not fatal: an X11 bar without a StatusNotifierWatcher works
through XEmbed and needs none of this.
"""

import contextlib

STATUS_NOTIFIER_WATCHER = 'org.kde.StatusNotifierWatcher'

#: The watcher's object path and the property that lists the items it knows.
#: Read back after registering: Qt reports no error for an item it never put on
#: the bus, so the host's own list is the only evidence that one exists.
STATUS_NOTIFIER_WATCHER_PATH = '/StatusNotifierWatcher'
STATUS_NOTIFIER_ITEMS = 'RegisteredStatusNotifierItems'


class TrayWatch:
    """Turn "the watcher's bus name was registered" into a callback.

    ``on_available`` runs when the watcher's name appears or is re-registered
    (a bar starting, or restarting); ``on_lost`` when the name goes away (the
    bar exited).  Both are plain callables, so this class subclasses no QObject
    and can be driven from a fake bus in tests.
    """

    def __init__(self, on_available, on_lost=None):
        self.on_available = on_available
        self.on_lost = on_lost
        self.watching = False
        self._watcher = None

    def start(self):
        """Watch the session bus; False when this session cannot be watched."""
        if self.watching:
            return True
        try:
            # pylint: disable=import-outside-toplevel
            from PyQt5.QtDBus import QDBusConnection, QDBusServiceWatcher
        except ImportError:
            return False
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            watcher = QDBusServiceWatcher(
                STATUS_NOTIFIER_WATCHER, bus,
                QDBusServiceWatcher.WatchForRegistration
                | QDBusServiceWatcher.WatchForUnregistration)
            watcher.serviceRegistered.connect(self._on_registered)
            watcher.serviceUnregistered.connect(self._on_unregistered)
        # QDBusError has no import-safe base class, so this one has to be broad.
        except Exception:                       # noqa: BLE001
            return False
        self._watcher = watcher
        self.watching = True
        return True

    def stop(self):
        """Stop watching; safe to call when start() never succeeded."""
        watcher, self._watcher = self._watcher, None
        self.watching = False
        if watcher is None:
            return
        for signal, slot in (
                (watcher.serviceRegistered, self._on_registered),
                (watcher.serviceUnregistered, self._on_unregistered)):
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)

    def _on_registered(self, name):
        """The watcher only reports its own service, but stay defensive."""
        if name == STATUS_NOTIFIER_WATCHER:
            self.on_available()

    def _on_unregistered(self, name):
        if name == STATUS_NOTIFIER_WATCHER and self.on_lost is not None:
            self.on_lost()
