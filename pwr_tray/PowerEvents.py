#!/usr/bin/env python3
# pylint: disable=broad-exception-caught
"""Event sources that replace the applet's per-tick polling.

The applet used to spawn ``systemd-inhibit`` and ``playerctl`` on every
two-second tick (roughly 43,000 processes per day), call
``psutil.sensors_battery()`` and stat ``config.ini`` on the same schedule.
logind, UPower and the MPRIS players already publish those changes as D-Bus
signals, so this module watches that signal stream and calls back only when
something actually changed.  The caller then re-runs the (still
subprocess-based) query once.

Two long-lived ``dbus-monitor`` children replace the per-tick spawns: one on
the system bus (logind inhibitors, UPower battery and ``PrepareForSleep``
resume detection) and one on the session bus (MPRIS playback).  Their pipes
are read with ``QSocketNotifier``, so there is no read polling either.
``config.ini`` is watched with ``QFileSystemWatcher``, both as a file and
through its directory (editors that save by rename would otherwise remove the
file watch).

If neither Qt nor ``dbus-monitor`` is available, ``start()`` reports that and
the applet falls back to the old polling, so behaviour is never worse.
"""

import atexit
import contextlib
import os
import subprocess

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


class PowerEvents:
    """Watch the signals the applet used to poll for.

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
        self._context = {}
        self._stopping = False
        # A normal exit must not orphan the dbus-monitor children.  stop() is
        # idempotent, so an explicit call followed by atexit is harmless.
        atexit.register(self.stop)

    # -- lifecycle ---------------------------------------------------------

    def start(self, config_path=''):
        """Start every available source; return True if anything was wired."""
        system_ok = self._start_monitor('system', '--system')
        session_ok = self._start_monitor('session', '--session')
        # Both buses are needed to replace the old polling completely.
        self.bus_ok = system_ok and session_ok
        self._watch_config(config_path)
        return self.bus_ok or self.config_ok

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
        self._context[key] = ''
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
        self._context.pop(key, None)
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
            self._stop_monitor(key)
            return
        text = self._pending.get(key, b'') + chunk
        lines = text.split(b'\n')
        self._pending[key] = lines.pop()      # keep the partial last line
        for raw in lines:
            self._scan_line(key, raw.decode('utf-8', 'replace'))

    def _scan_line(self, key, line):
        """Classify one dbus-monitor output line.

        The header names the object path; property names appear on their own
        lines in the message body.  Firing on the property line means we do
        not have to wait for the next message to know a record ended.
        """
        if line.startswith('signal time='):
            self._context[key] = self._context_for(line)
            return
        context = self._context.get(key, '')
        if not context:
            return

        if context == 'login1_sleep':
            # ``boolean true`` means suspend is imminent; ``false`` is resume.
            value = line.strip()
            if value not in ('boolean true', 'boolean false'):
                return
            self._context[key] = ''
            if value == 'boolean false':
                self.on_resume()
            return

        if context == 'login1':
            matched = self._mentions(line, self.INHIBITOR_PROPERTIES)
            notify = self.on_inhibitors
        elif context == 'upower':
            matched = self._mentions(line, ('OnBattery', ))
            notify = self.on_battery
        elif context == 'upower_device':
            matched = self._mentions(line, self.BATTERY_PROPERTIES)
            notify = self.on_battery
        elif context == 'mpris':
            matched = self._mentions(line, ('PlaybackStatus', ))
            notify = self.on_playback
        else:
            return

        if matched:
            # One callback per message, not per changed property.
            self._context[key] = ''
            notify()

    @staticmethod
    def _mentions(line, names):
        return any(f'string "{name}"' in line for name in names)

    @classmethod
    def _context_for(cls, header):
        start = header.find('path=')
        if start < 0:
            return ''
        end = header.find(';', start)
        path = header[start + 5:end if end >= 0 else len(header)]
        if path.startswith(cls.LOGIN1_PATH):
            if 'member=PrepareForSleep' in header:
                return 'login1_sleep'
            return 'login1'
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
