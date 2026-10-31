#!/usr/bin/env python3
# pylint: disable=broad-exception-caught
"""Event sources that replace the applet's per-tick polling -- and the state
they carry.

The applet used to spawn ``systemd-inhibit`` and ``playerctl`` on every
two-second tick (roughly 43,000 processes per day), call
``psutil.sensors_battery()`` and stat ``config.ini`` on the same schedule.
logind, UPower and the MPRIS players already publish those changes as D-Bus
signals, and the signal carries the new value.  This module reads that stream
once and *keeps* the answers:

* :attr:`PowerEvents.blocked` -- logind's ``BlockInhibited``, ``None`` until
  the stream (or a caller's one-off query) has supplied it,
* :attr:`PowerEvents.media_playing` -- whether any MPRIS player reports
  ``Playing``, ``None`` until the stream knows,
* the ``config.ini`` watch, which only ever needs a notification.

The callbacks therefore only schedule a tick; the tick reads a dictionary
instead of running a program.  Two long-lived ``dbus-monitor`` children replace
the per-tick spawns: one on the system bus (logind inhibitors, UPower battery
and ``PrepareForSleep`` resume detection) and one on the session bus (MPRIS
playback and player lifetime).  Their pipes are read with ``QSocketNotifier``,
so there is no read polling either.  ``config.ini`` is watched with
``QFileSystemWatcher``, both as a file and through its directory (editors that
save by rename would otherwise remove the file watch).

If neither Qt nor ``dbus-monitor`` is available, ``start()`` reports that and
the applet falls back to the old polling -- querying logind and playerctl once
per tick -- so behaviour is never worse.
"""

import atexit
import contextlib
import os
import re
import subprocess

from pwr_tray.Inhibitors import blocked_operations

try:
    from PyQt5.QtCore import QFileSystemWatcher, QSocketNotifier
except ImportError:       # pragma: no cover - exercised only without PyQt5
    QFileSystemWatcher = None
    QSocketNotifier = None


def _is_class(obj):
    """True for real Qt classes, False for the permissive test stubs."""
    return isinstance(obj, type)


PROPERTIES_CHANGED = (
    "type='signal',interface='org.freedesktop.DBus.Properties',"
    "member='PropertiesChanged'")

# logind announces suspend (true) and resume (false) on this signal.  The
# applet only needs the resume half, to re-read state that went stale while
# the machine slept.
PREPARE_FOR_SLEEP = (
    "type='signal',interface='org.freedesktop.login1.Manager',"
    "member='PrepareForSleep'")

# A player that exits or is replaced must not leave a stale "Playing" behind:
# the unique name in the status map is what identifies the player.
NAME_OWNER_CHANGED = (
    "type='signal',interface='org.freedesktop.DBus',"
    "member='NameOwnerChanged',path='/org/freedesktop/DBus'")

MPRIS_SERVICE_PREFIX = 'org.mpris.MediaPlayer2'


class PowerEvents:
    """Watch the signals the applet used to poll for, and keep their values.

    Each callback may be invoked more than once for the same D-Bus message
    (one per changed property); callers are expected to coalesce them.  The
    applet marks a dirty flag and schedules a single fast poll.
    """

    # Header/path prefixes from the dbus-monitor stream.
    LOGIN1_PATH = '/org/freedesktop/login1'
    UPOWER_PATH = '/org/freedesktop/UPower'
    UPOWER_DEVICE_PREFIX = '/org/freedesktop/UPower/devices/'
    MPRIS_PATH = '/org/mpris/MediaPlayer2'

    # Properties whose change means the cached reading is stale.
    BATTERY_PROPERTIES = ('Percentage', 'State', 'IsPresent',
                          'TimeToEmpty', 'TimeToFull')
    INHIBITOR_PROPERTIES = ('BlockInhibited', 'DelayInhibited')
    PLAYBACK_PROPERTIES = ('PlaybackStatus', )

    #: One D-Bus string literal.  dbus-monitor quotes and escapes it the same
    #: way in every body it prints, so one pattern serves names and values.
    STRING_LITERAL = re.compile(r'string "((?:[^"\\]|\\.)*)"')

    def __init__(self, on_inhibitors=None, on_battery=None,
                 on_playback=None, on_config=None, on_resume=None,
                 on_source_lost=None):
        self.on_inhibitors = on_inhibitors or (lambda: None)
        self.on_battery = on_battery or (lambda: None)
        self.on_playback = on_playback or (lambda: None)
        self.on_config = on_config or (lambda: None)
        self.on_resume = on_resume or (lambda: None)
        self.on_source_lost = on_source_lost or (lambda: None)

        self.watcher = None
        self.config_path = ''
        self.config_ok = False
        self.bus_ok = False

        self._procs = {}
        self._notifiers = {}
        self._pending = {}
        self._messages = {}
        self._stopping = False

        # The answers the signal stream has supplied so far.
        self._blocked = None
        self._players = {}
        self._names = {}
        self._media_known = False
        self._media_fallback = None

        # A normal exit must not orphan the dbus-monitor children.  stop() is
        # idempotent, so an explicit call followed by atexit is harmless.
        atexit.register(self.stop)

    # -- cached answers ----------------------------------------------------

    @property
    def blocked(self):
        """logind's blocked operations, or None while the value is unknown."""
        return self._blocked

    @property
    def media_playing(self):
        """True/False once the stream knows; None before it does.

        A player that is present and reported ``Paused`` is a real ``False``;
        "nothing has been heard yet" is ``None`` so the caller can ask once
        (``playerctl``) instead of assuming silence.
        """
        if self._players:
            return any(status == 'Playing'
                       for status in self._players.values())
        if not self._media_known:
            return None
        return bool(self._media_fallback)

    def note_blocked(self, blocked):
        """Cache a one-off query's answer (the first tick, or a fallback)."""
        self._blocked = None if blocked is None else frozenset(blocked)

    def note_media(self, playing):
        """Cache a one-off ``playerctl status`` answer."""
        self._media_fallback = bool(playing)
        self._media_known = True

    @property
    def media_players(self):
        """Display names of the players currently reporting ``Playing``.

        A unique bus name (``:1.42``) is not something to put in front of a
        user, so the well-known name learned from ``NameOwnerChanged`` is
        trimmed to its root: ``org.mpris.MediaPlayer2.firefox.instance3217``
        becomes ``firefox``.  A player whose name was never learned still
        counts -- it is shown as ``player`` rather than dropped.
        """
        return [self._player_name(sender, path)
                for (sender, path), status in self._players.items()
                if status == 'Playing']

    def _player_name(self, sender, _path):
        name = self._names.get(sender, '')
        prefix = f'{MPRIS_SERVICE_PREFIX}.'
        if name.startswith(prefix):
            name = name[len(prefix):]
        root, marker, suffix = name.rpartition('.instance')
        if marker and suffix.isdigit():
            name = root
        return name or 'player'

    def invalidate(self):
        """Forget both answers so the next tick re-reads them once.

        Used by the applet's periodic resync: a missed signal is exactly the
        failure a cache introduces, and one query every five minutes is the
        price of not polling every two seconds.  Player names are not an
        answer about now -- they are re-learned only when an owner changes --
        so they are kept.
        """
        self._blocked = None
        self._players.clear()
        self._media_known = False
        self._media_fallback = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, config_path=''):
        """Start every available source; return True if anything was wired."""
        system_ok = self._start_monitor('system', '--system')
        session_ok = self._start_monitor('session', '--session')
        # Both buses are needed to replace the old polling completely.
        self.bus_ok = system_ok and session_ok
        if session_ok:
            self._seed_players()
        self._watch_config(config_path)
        return self.bus_ok or self.config_ok

    def _seed_players(self):
        """Learn the unique name behind the players already on the bus.

        A player that started before the applet never produces a
        ``NameOwnerChanged`` this process sees, and a unique name is not
        something to put in a menu.  One ListNames plus a GetNameOwner per
        player, in-process through QtDBus, is enough: no fork, and the only
        query this module makes.
        """
        try:
            # pylint: disable=import-outside-toplevel
            from PyQt5.QtDBus import QDBusConnection
        except ImportError:
            return
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return
            interface = bus.interface()
            for name in interface.registeredServiceNames().value():
                if not str(name).startswith(MPRIS_SERVICE_PREFIX):
                    continue
                owner = interface.serviceOwner(name).value()
                if owner:
                    self._names[str(owner)] = str(name)
        # QDBusError has no import-safe base class, so this one has to be broad.
        except Exception:                   # noqa: BLE001
            return

    def stop(self):
        """Terminate the monitors, drop the file watches.  Idempotent."""
        if self._stopping:
            return
        self._stopping = True
        try:
            for key in list(self._procs):
                self._stop_monitor(key)
            if self.watcher is not None:
                with contextlib.suppress(AttributeError, RuntimeError):
                    self.watcher.deleteLater()
            self.watcher = None
        finally:
            self._stopping = False
            self.bus_ok = False
            self.config_ok = False

    # -- dbus-monitor children --------------------------------------------

    def _start_monitor(self, key, bus_flag):
        if not _is_class(QSocketNotifier):
            return False
        # dbus-monitor ORs multiple match rules given as separate arguments.
        # The system bus therefore carries both the property watches and the
        # resume signal, which used to need a third child of its own.
        rules = [PROPERTIES_CHANGED]
        if key == 'system':
            rules.append(PREPARE_FOR_SLEEP)
        else:
            # Players come and go; without this a closed player would keep
            # reporting the status it had when it exited.
            rules.append(NAME_OWNER_CHANGED)
        args = ['dbus-monitor', bus_flag, *rules]
        proc = None
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            notifier = QSocketNotifier(fd, QSocketNotifier.Read)
            notifier.activated.connect(
                lambda _fd=None, k=key: self._drain(k))
            notifier.setEnabled(True)
        except (OSError, AttributeError, TypeError, RuntimeError):
            if proc is not None:
                with contextlib.suppress(OSError):
                    proc.kill()
            return False
        self._procs[key] = proc
        self._notifiers[key] = notifier
        self._pending[key] = b''
        self._messages[key] = None
        return True

    def _stop_monitor(self, key):
        notifier = self._notifiers.pop(key, None)
        if notifier is not None:
            with contextlib.suppress(AttributeError, RuntimeError):
                notifier.setEnabled(False)
            with contextlib.suppress(AttributeError, RuntimeError):
                notifier.deleteLater()
        proc = self._procs.pop(key, None)
        if proc is not None:
            with contextlib.suppress(OSError):
                if proc.poll() is None:
                    proc.terminate()
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()
        self._pending.pop(key, None)
        self._messages.pop(key, None)
        if proc is not None and not self._stopping:
            # The monitor died on its own: the caller should stop trusting
            # these signals and go back to polling.
            self.bus_ok = False
            self.on_source_lost()

    def _drain(self, key):
        """Read whatever dbus-monitor wrote, without blocking."""
        proc = self._procs.get(key)
        if proc is None or proc.stdout is None:
            return
        try:
            chunk = os.read(proc.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            self._stop_monitor(key)
            return
        if not chunk:                  # EOF: dbus-monitor exited
            self._close_message(key)
            self._stop_monitor(key)
            return
        text = self._pending.get(key, b'') + chunk
        lines = text.split(b'\n')
        self._pending[key] = lines.pop()      # keep the partial last line
        for raw in lines:
            self._scan_line(key, raw.decode('utf-8', 'replace'))

    def _scan_line(self, key, line):
        """Classify one dbus-monitor output line, keeping the values.

        The header names the object path; property names appear on their own
        lines in the message body, and the value the property changed *to*
        follows as a ``variant`` line.  Reading both turns the stream from an
        invalidation source into a state source: the applet no longer has to
        ask a program what the new value is.
        """
        if line.startswith('signal time='):
            self._close_message(key)
            self._messages[key] = self._new_message(line)
            return
        message = self._messages.get(key)
        if not message:
            return
        kind = message['kind']

        if kind == 'login1_sleep':
            # ``boolean true`` means suspend is imminent; ``false`` is resume.
            value = line.strip()
            if value in ('boolean true', 'boolean false'):
                self._messages[key] = None
                if value == 'boolean false':
                    self.on_resume()
            return

        if kind == 'name_owner':
            name = self._quoted(line)
            if name is None:
                return
            message['strings'].append(name)
            if len(message['strings']) == 3:
                self._messages[key] = None
                self._owner_changed(*message['strings'])
            return

        if not kind:
            return

        if message['prop'] is None and self._mentions(line,
                                                      self._interesting(kind)):
            message['prop'] = self._quoted(line)
            message['valued'] = False
            if not message['notified']:
                # One callback per message, not per changed property.
                message['notified'] = True
                self._notify(kind)
            return

        value = self._variant_string(line)
        if value is not None and message['prop'] is not None:
            message['valued'] = True
            self._apply_value(kind, message['sender'], message['path'],
                              message['prop'], value)
            message['prop'] = None

    def _new_message(self, header):
        """Describe one message: what it is about, and who sent it."""
        path = self._header_field(header, 'path')
        member = self._header_field(header, 'member')
        return {'kind': self._kind_for(path, member),
                'sender': self._header_field(header, 'sender'),
                'path': path,
                'prop': None,
                'valued': True,
                'notified': False,
                'strings': []}

    def _close_message(self, key):
        """A message ended: an interesting property that never showed a value
        arrived *invalidated*, so the cached answer is no longer known."""
        message = self._messages.get(key)
        self._messages[key] = None
        if not message or message['prop'] is None or message['valued']:
            return
        self._mark_unknown(message['kind'], message['prop'])

    @staticmethod
    def _interesting(kind):
        """The property names whose change the applet acts on, per message."""
        if kind == 'login1':
            return PowerEvents.INHIBITOR_PROPERTIES
        if kind == 'upower':
            return ('OnBattery', )
        if kind == 'upower_device':
            return PowerEvents.BATTERY_PROPERTIES
        if kind == 'mpris':
            return PowerEvents.PLAYBACK_PROPERTIES
        return ()

    def _notify(self, kind):
        if kind == 'login1':
            self.on_inhibitors()
        elif kind in ('upower', 'upower_device'):
            self.on_battery()
        else:
            self.on_playback()

    def _apply_value(self, kind, sender, path, prop, value):
        """Store the value a property changed to."""
        if kind == 'login1' and prop == 'BlockInhibited':
            # '' is a real answer ("nothing is blocked"); None stays unknown.
            self._blocked = frozenset(blocked_operations(value))
        elif kind == 'mpris' and prop == 'PlaybackStatus':
            self._players[(sender, path)] = value
            self._media_known = True
            self._media_fallback = None

    def _mark_unknown(self, kind, prop):
        """The property arrived invalidated; its value was not in the message."""
        if kind == 'login1' and prop == 'BlockInhibited':
            self._blocked = None
        elif kind == 'mpris' and prop == 'PlaybackStatus':
            self._players.clear()
            self._media_known = False
            self._media_fallback = None

    def _owner_changed(self, name, old_owner, new_owner):
        """Track player lifetime by the unique name behind each status."""
        if not name.startswith(MPRIS_SERVICE_PREFIX):
            return
        if new_owner:
            # A player just appeared: remember the name behind its unique bus
            # name, then ask once -- a player that never publishes a status
            # would otherwise go unnoticed until resync.
            self._names[str(new_owner)] = name
            self._media_known = False
            self.on_playback()
            return
        self._names.pop(old_owner, None)
        stale = [key for key in self._players if key[0] == old_owner]
        if not stale:
            return
        for key in stale:
            del self._players[key]
        if not self._players:
            self._media_known = True
            self._media_fallback = False
        self.on_playback()

    @classmethod
    def _mentions(cls, line, names):
        return any(f'string "{name}"' in line for name in names)

    @classmethod
    def _quoted(cls, line):
        """The first D-Bus string literal on the line, unescaped."""
        match = cls.STRING_LITERAL.search(line)
        if match is None:
            return None
        return match.group(1).replace('\\"', '"').replace('\\\\', '\\')

    @classmethod
    def _variant_string(cls, line):
        """The value of a ``variant string "..."`` line, if this is one.

        dbus-monitor prints the payload on one line (``variant   string
        "Playing"``), which is what makes keeping the value possible.
        """
        text = line.strip()
        if not text.startswith('variant'):
            return None
        return cls._quoted(text[len('variant'):])

    @staticmethod
    def _header_field(header, name):
        """One ``name=value`` field of a dbus-monitor header."""
        marker = f'{name}='
        start = header.find(marker)
        if start < 0:
            return ''
        start += len(marker)
        end = len(header)
        for stop in (' ', ';', ')'):
            found = header.find(stop, start)
            if 0 <= found < end:
                end = found
        return header[start:end]

    @classmethod
    def _kind_for(cls, path, member):
        if member == 'NameOwnerChanged' and path == '/org/freedesktop/DBus':
            return 'name_owner'
        if path == cls.LOGIN1_PATH:
            if member == 'PrepareForSleep':
                return 'login1_sleep'
            return 'login1'
        if path.startswith(cls.LOGIN1_PATH):
            # A session object (/org/freedesktop/login1/session/_32) has its
            # own BlockInhibited, scoped to that session.  The verdict has
            # always come from the Manager object, so a session's value is not
            # allowed to overwrite it -- different scope, different answer.
            return ''
        if path.startswith(cls.UPOWER_DEVICE_PREFIX):
            return 'upower_device'
        if path.startswith(cls.UPOWER_PATH):
            return 'upower'
        if path.startswith(cls.MPRIS_PATH):
            return 'mpris'
        return ''

    # -- config file -------------------------------------------------------

    def _watch_config(self, config_path):
        if not config_path or not _is_class(QFileSystemWatcher):
            return
        try:
            watcher = QFileSystemWatcher()
            folder = os.path.dirname(os.path.abspath(config_path))
            self.config_path = config_path
            if os.path.exists(config_path):
                watcher.addPath(config_path)
            # Directory watch survives an atomic "write temp + rename" save.
            if folder and not watcher.addPath(folder):
                return
            watcher.fileChanged.connect(self._on_config_changed)
            watcher.directoryChanged.connect(self._on_config_changed)
        except (OSError, AttributeError, TypeError, RuntimeError):
            return
        self.watcher = watcher
        self.config_ok = True

    def _on_config_changed(self, _path):
        """Re-arm the file watch after a rename, then report the change."""
        if (self.watcher is not None and self.config_path
                and os.path.exists(self.config_path)
                and self.config_path not in self.watcher.files()):
            self.watcher.addPath(self.config_path)
        self.on_config()
