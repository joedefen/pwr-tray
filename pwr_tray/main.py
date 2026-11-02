#!/usr/bin/env python3
"""
This code is designed to control power matters from the tray.
"""
# The applet is one cohesive module and its state machine is deliberately a
# single branch ladder; rule-specific suppressions sit next to their code.
# pylint: disable=missing-function-docstring,broad-except
# pylint: disable=too-many-instance-attributes,consider-using-with
# pylint: disable=too-many-lines,too-many-statements
# pylint: disable=too-many-branches,too-many-public-methods,too-many-locals

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from PyQt5.QtCore import QTimer, QtMsgType, qInstallMessageHandler
from PyQt5.QtGui import QCursor, QIcon
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon  #, QMessageBox
from ruamel.yaml import YAML

from pwr_tray import Desktop, Inhibitors, Session, Utils, __version__, service
from pwr_tray.Actions import Actions
from pwr_tray.Errors import MissingCommandsError, PwrTrayError
from pwr_tray.IdleStateMachine import IdleStateMachine
from pwr_tray.Inhibitors import (
    BLOCK_INHIBITED,
    GUARDED_OPERATIONS,
    LOGIND_MANAGER,
    LOGIND_PATH,
    LOGIND_SERVICE,
    blocked_operations,
    parse_block_inhibited,
)
from pwr_tray.IniTool import IniTool
from pwr_tray.PowerEvents import PowerEvents
from pwr_tray.PowerSupply import describe, read_power
from pwr_tray.SwayIdleMgr import SwayIdleManager
from pwr_tray.TrayWatch import STATUS_NOTIFIER_WATCHER, TrayWatch
from pwr_tray.Utils import PyKill, prt

# Every tray host -- plasmashell, xfce4-panel, waybar, swaybar+snixembed, ... --
# claims STATUS_NOTIFIER_WATCHER (see TrayWatch.py).  A host that is not on the
# bus yet is an event, not a wait: the item is registered again when one
# appears, so start-up never blocks on the bar.
TRAY_ATTACH_DELAY_MS = 500      # let a new host start serving calls

# Qt messages that are chatter rather than news.  The qt5ct/qt6ct platform
# themes print their whole palette table through qDebug() every time a widget
# asks for one -- some forty lines between the messages a reader actually
# wanted -- and Qt prints a "QThreadStorage ... destroyed" notice at exit that
# describes its own teardown.  The markers are matched against the text, not the
# level, because the palette dump arrives at warning level on some builds.
QT_NOISE = (
    'CTPlatformTheme::',    # qt5ct and qt6ct, whose class names differ by one digit
    'QWindowsWindow::',     # the Windows platform plugin's own chatter
    'QThreadStorage:',      # a thread outliving its thread-local storage (at exit)
)
_qt_seen = set()


def qt_message_handler(mode, context, message):
    """ Qt's own console output, filtered, deduplicated and stamped into the log.

    Called by Qt through `install_qt_messages()`.  Debug and information
    messages are dropped: they are the internals of Qt and of its plugins, and
    nothing in this applet acts on them.  What is kept is a warning or an error
    -- once per distinct text, with the `[file:line]` of the C++ side as the
    footer, so "who said this?" is answerable at a glance instead of looking
    like a pwr-tray bug.
    """
    if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
        return
    text = ' '.join(str(message).split())
    if not text or any(marker in text for marker in QT_NOISE):
        return
    if text in _qt_seen:
        return              # Qt retries the same warning on every repaint
    if len(_qt_seen) >= 64:
        _qt_seen.clear()    # bounded: the tray runs for weeks, Qt's texts do not grow
    _qt_seen.add(text)
    file_name = os.path.basename(str(getattr(context, 'file', '') or ''))
    line = getattr(context, 'line', 0) or 0
    Utils.prt_qt(text, f'[{file_name}:{line}]' if file_name else '[qt]')


def install_qt_messages():
    """ Send Qt's console output through prt(); call before QApplication exists. """
    qInstallMessageHandler(qt_message_handler)


# How many percentage points the fabricated no-battery test state loses per
# poll tick.  The point is to make the test look like a battery being drained:
# HiBattery stays above the low threshold and LoBattery counts down to zero.
TEST_DISCHARGE_STEP = 1

# The logind vocabulary -- BlockInhibited, the guarded operations, the busctl
# answer parser -- lives in pwr_tray.Inhibitors, where the event source can use
# it too.  It is re-exported here because the applet's long-standing entry
# points and tests refer to these names.  GUARDED_OPERATIONS holds the
# operations this applet's own timeouts perform: handle-power-key and
# handle-lid-switch say a compositor owns an input event, which is not a
# request to stay awake, and asking which operations are blocked rather than
# which rows a table happens to contain is what removes the per-desktop
# exception list for xfce4-power-manager, org_kde_powerdevil and niri.


class PwrTray:
    """ pwr-tray main class.
    NOTES:
     - when icons are moved/edited, rename them or reboot to avoid cache confusion
    """
    svg_info = SimpleNamespace(version='03'
                , bases= ['SettingSun',   # Normal (SleepAfterLock)
                          'FullSun',      # Presentation Mode
                          'Unlocked',     # LockOnly Mode
                          'GoingDown',    # LowBattery Mode
                          'PlayingNow',   # inhibited by a/v player
                          'RisingMoon',   # Normal and Locking Soon
                          'UnlockedMoon', # LockOnly and Locking Soon
                          'StopSign',     # systemd inhibited
                          ] )
    singleton = None

    # Desktop detection and resource installation are pure functions over
    # commands.yaml, $PATH and the config folder; their implementation lives in
    # Desktop.py.  These aliases keep the applet's long-standing static entry
    # points (used by --list-des, the --svc-* verbs and the tests) working.
    load_de_config = staticmethod(Desktop.load_de_config)
    must_haves_for = staticmethod(Desktop.must_haves_for)
    missing_commands = staticmethod(Desktop.missing_commands)
    build_variables = staticmethod(Desktop.build_variables)
    detect_de = staticmethod(Desktop.detect_de)
    ensure_resources = staticmethod(Desktop.ensure_resources)

    def __init__(self, ini_tool, quick=False, force_de=None):
        PwrTray.singleton = self
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
#       while not QSystemTrayIcon.isSystemTrayAvailable():
#           prt("System tray is not available. Retry in 1 second...")
#           time.sleep(1.0)
#       prt("System tray is available. Continuing...")

        self.ini_tool = ini_tool
        # The two extracted concerns.  They are handed the applet rather than
        # reaching for PwrTray.singleton, so they are usable -- and testable --
        # on their own.  The class keeps thin entry points for both.
        self.actions = Actions(self)
        self.idle_state = IdleStateMachine(self)
        # ``synthetic`` tracks whether the state on screen is a fabricated
        # HiBattery/LoBattery leg of the no-battery test cycler rather than a
        # reading.  It is what the menu's "(TESTING)" suffix keys off, so the
        # plugged-in state of a machine without a battery -- including the leg
        # that closes the cycle -- is not labelled as a test.
        self.battery = SimpleNamespace(present=None, synthetic=False,
                       plugged=True, percent=100, selector='Settings')
        self.reconfig()
        self.quick = quick

        self.loop = 0
        self.loop_sample = (1 if quick else 15)

        ## self.singleton.presentation_mode = False
        self.singleton.mode = 'SleepAfterLock' # or 'LockOnly' or 'Presentation'
        self.was_effective_mode = None
        self.was_inhibited = None
        self.was_play_state = ''
        self.was_selector = None
        self.was_output = ''
        self.svgs = [f'{base}-v{self.svg_info.version}.svg'
                     for base in self.svg_info.bases]
        # Resources live in the config folder; ensure_resources() needs that
        # folder explicitly.
        # One icon per entry in svg_info.bases; show_icon() indexes this list
        # by state, so a partial list would be an IndexError later.
        self.icons = [QIcon(path) for path in
                      self.ensure_resources(ini_tool.folder, self.svgs)]
        # Not a tray icon: the wallpaper the default locker command uses.
        self.ensure_resources(ini_tool.folder, ['lockpaper.png'])

        # The tray item is shown immediately; a host that is not on the bus
        # yet is picked up by the watch below instead of being waited for.
        # ``menu`` and ``current_icon_num`` exist first, so re-registering the
        # item later can restore both.
        self.menu = None
        self.menu_items = []
        self.current_icon_num = -1  # triggers immediate icon update

        # The watch is started before the item is shown: a watcher that
        # appears between the two still produces the event we need.
        tray_present = tray_watcher_present()
        self._tray_seen = tray_present is True
        self.tray_watch = TrayWatch(on_available=self._on_tray_host_appeared,
                                    on_lost=self._on_tray_host_lost)
        if not self.tray_watch.start() and tray_present is not None:
            prt('WARN: cannot watch for a tray host that starts later')
        self._install_tray_icon()
        if tray_present is True:
            prt('tray host detected.')
        elif tray_present is False:
            prt('no tray host yet; the icon registers when one appears.')
        else:
            prt('tray host status unknown (no QtDBus / no session bus).')

        # states are Awake, Locked, Blanked, Asleep
        # when is idle time
        self.state = SimpleNamespace(name='Awake', when=0)

        self.running_idle_s = 0.000
        self.poll_s = 2.000
        self.poll_100ms = False
        self.rebuild_menu = False
        self.picks_file = ini_tool.picks_path
        self.enable_playerctl = True

        # Event-driven refresh: a D-Bus signal or a config-file watch marks the
        # matching flag dirty and schedules one fast poll.  Inhibitors need no
        # flag: the event source keeps the answer, so every tick can read it
        # for the price of a dictionary lookup -- and a missed signal can no
        # longer leave a stale verdict behind.
        self._config_dirty = False
        self._battery_dirty = True
        self._inhibit_rows = []
        # Which blocked-operation set the cached rows describe.
        self._rows_blocked = None
        # The last inhibit-source failure that was logged, so a broken busctl or
        # a changed table is reported once instead of on every event.
        self._inhibit_warning = ''
        self._refresh_pending = False
        self._events_bus_ok = False
        self._events_config_ok = False
        self.events = None

        self.picks_restored = self.restore_picks()
        if not self.picks_restored:
            prt('NOTE: picks restore incomplete; configured values used where missing')
        if quick:
            for selector in self.ini_tool.get_selectors():
                params = self.ini_tool.params_by_selector[selector]
                params.lock_min_list = [1, 2, 4, 8, 32, 128]
                params.sleep_min_list = [1, 2, 4, 8, 32, 128]

        # self.down_state = self.opts.down_state

        # Load DE config from JSON and detect environment
        de_json = self.load_de_config(ini_tool.folder)
        self.de_config = self.detect_de(de_json, force_de=force_de)
        self.graphical = self.de_config['name']
        self.is_wayland = self.de_config['session_type'] == 'wayland'

        # Build variables: defaults merged with matched desktop commands
        self.variables = self.build_variables(de_json, self.de_config)

        # Validate must_haves (accumulated from defaults + session_type + desktop)
        dont_haves = self.missing_commands(self.de_config)
        if dont_haves:
            raise MissingCommandsError(
                dont_haves, desktop=self.de_config['name'],
                session_type=self.de_config['session_type'])

        # qdbus/qdbus6 auto-detection: replace 'qdbus ' in any command value
        has_qdbus = any(isinstance(v, str) and 'qdbus ' in v
                        for v in self.variables.values())
        if has_qdbus:
            qdbus_cmd = shutil.which('qdbus') or shutil.which('qdbus6')
            if not qdbus_cmd:
                raise MissingCommandsError(
                    ['qdbus or qdbus6'], desktop=self.de_config['name'],
                    session_type=self.de_config['session_type'])
            qdbus_name = os.path.basename(qdbus_cmd)
            if qdbus_name != 'qdbus':
                for key, val in list(self.variables.items()):
                    if isinstance(val, str) and 'qdbus ' in val:
                        self.variables[key] = val.replace('qdbus ', f'{qdbus_name} ')

        self.has_playerctl = bool(shutil.which('playerctl'))

        self.idle_manager = (SwayIdleManager(self)
            if self.de_config.get('idle_method') == 'swayidle' else None)

        self.build_menu()
        if self.idle_manager:
            self.idle_manager_start()

        self.timer = QTimer()
        self.timer.setInterval(100) # 100 is initial ... gets recomputed
        self.timer.timeout.connect(self.on_timeout)
        self.timer.start()

        self._start_event_sources(ini_tool.ini_path)

        # Safety net: signals keep us current, but a missed event (a service
        # that restarted, a player that vanished without a signal) must not
        # leave a stale icon forever.  This is 288 cheap checks per day rather
        # than a systemd-inhibit spawn every two seconds.
        self.sync_timer = QTimer()
        self.sync_timer.setInterval(300_000)
        self.sync_timer.timeout.connect(self._resync)
        self.sync_timer.start()

    def _start_event_sources(self, config_path):
        """Wire D-Bus signals and the config watch, or fall back to polling."""
        self.events = PowerEvents(
            on_inhibitors=self._on_inhibitors_event,
            on_battery=self._on_battery_event,
            on_playback=self._on_playback_event,
            on_config=self._on_config_event,
            on_resume=self._on_resume_event,
            on_source_lost=self._on_event_source_lost)
        self.events.start(config_path)
        self._events_bus_ok = self.events.bus_ok
        self._events_config_ok = self.events.config_ok
        if self._events_bus_ok or self._events_config_ok:
            prt(f'event sources: bus={self._events_bus_ok}'
                f' config={self._events_config_ok} (polling disabled)')
        else:
            prt('WARN: no event sources available; falling back to polling')

    def _on_event_source_lost(self):
        """A dbus-monitor child died: stop trusting it, resume polling."""
        self._events_bus_ok = False
        prt('WARN: D-Bus event monitor exited; falling back to polling')

    def _schedule_fast_poll(self):
        """Coalesce any number of events into a single 100 ms tick."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self.timer.start(100)

    def _on_inhibitors_event(self):
        # The new BlockInhibited value is already cached by the event source;
        # this only makes the tick that reads it happen now.
        self._schedule_fast_poll()

    def _on_battery_event(self):
        self._battery_dirty = True
        self._schedule_fast_poll()

    def _on_playback_event(self):
        # Playback state feeds check_inhibited(), so the icon and the swayidle
        # command have to be rebuilt when a player starts or stops.  The state
        # itself is already cached by the event source.
        self._schedule_fast_poll()

    def _on_resume_event(self):
        """logind reported the resume half of PrepareForSleep.

        State read before the machine slept (battery, inhibitors, idle time)
        is stale, so the cached answers are dropped -- the next tick re-reads
        them once -- and the slow branch of the tick is forced.
        """
        prt('resume detected')
        events = self._events()
        if events is not None:
            events.invalidate()
        self.poll_100ms = True
        self._schedule_fast_poll()

    def _on_config_event(self):
        self._config_dirty = True
        self._schedule_fast_poll()

    def _resync(self):
        """Periodic safety net for a D-Bus signal we might have missed.

        A cache is what makes a missed signal possible at all, so the cached
        answers are invalidated rather than trusted: the next tick re-reads
        logind and the players once.  That is 288 ``busctl``/``playerctl``
        calls per day -- the safety net, not the polling loop.
        """
        events = self._events()
        if events is not None:
            events.invalidate()
        self._battery_dirty = True
        self._schedule_fast_poll()
        self._ensure_tray_attached()

    def get_params(self, selector=None):
        selector = self.battery.selector if selector is None else selector
        return self.ini_tool.params_by_selector[selector]

    def get_lock_min_list(self, selector=None):
        """TBD"""
        selector = self.battery.selector if selector is None else selector
        return self.ini_tool.get_current_vals(selector, 'lock_min_list')

    def get_lock_rotated_list(self, selector=None, first=None):
        """TBD"""
        selector = self.battery.selector if selector is None else selector
        return self.ini_tool.get_rotated_vals(selector, 'lock_min_list', first)

    def get_sleep_min_list(self, selector=None):
        """TBD"""
        selector = self.battery.selector if selector is None else selector
        return self.ini_tool.get_current_vals(selector, 'sleep_min_list')

    def get_sleep_rotated_list(self, selector=None, first=None):
        """TBD"""
        selector = self.battery.selector if selector is None else selector
        return self.ini_tool.get_rotated_vals(selector, 'sleep_min_list', first)

    def get_effective_mode(self):
        """TBD"""
        return 'SleepAfterLock' if self.battery.selector == 'LoBattery' else self.mode

    def reconfig(self):
        """ update/fix config """
        if self.ini_tool.update_config():
            self.rebuild_menu = True

    @staticmethod
    def save_picks():
        this = PwrTray.singleton
        if this and not this.quick:
            picks = { 'mode': this.mode,
                      'enable_playerctl': this.enable_playerctl,
                      'lock_mins': {
                          'Settings': this.get_lock_min_list('Settings')[0],
                          'HiBattery': this.get_lock_min_list('HiBattery')[0],
                          'LoBattery': this.get_lock_min_list('LoBattery')[0],

                      }, 'sleep_mins': {
                          'Settings': this.get_sleep_min_list('Settings')[0],
                          'HiBattery': this.get_sleep_min_list('HiBattery')[0],
                          'LoBattery': this.get_sleep_min_list('LoBattery')[0],
                      },
                }
            try:
                picks_str = json.dumps(picks)
                with open(this.picks_file, 'w', encoding='utf-8') as f:
                    f.write(picks_str + '\n')
                print("Picks saved:", picks_str)
                # The mode/timeout change affects the icon as well as the
                # menu, so apply it on the next tick rather than in two.
                this.poll_100ms = True
            except (OSError, TypeError) as e:
                print(f"An error occurred while saving picks: {e}", file=sys.stderr)


    def restore_picks(self):
        """ Apply the state saved by save_picks().

        A missing, partial or hand-edited picks file is tolerated: every entry
        is restored independently, anything unusable is logged and skipped, and
        that selector keeps its configured values. Returns True only when the
        whole saved state was applied.
        """
        try:
            with open(self.picks_file, encoding='utf-8') as handle:
                picks = json.load(handle)
        except FileNotFoundError:
            prt('no picks file to restore; using configured values')
            return False
        except (OSError, ValueError) as e:
            prt(f'restored picks FAILED: {e}')
            selector = getattr(getattr(self, 'battery', None), 'selector', 'Settings')
            prt(f'mode={self.mode} lock_mins={self.get_lock_min_list(selector)}'
                f' sleep_mins={self.get_sleep_min_list(selector)}')
            return False

        if not isinstance(picks, dict):
            prt(f'restored picks FAILED: {type(picks).__name__} is not a JSON object')
            return False

        complete = True

        mode = picks.get('mode')
        if mode is None:
            pass
        elif isinstance(mode, str):
            self.mode = mode
        else:
            prt(f'WARN: picks.mode is {mode!r}; keeping {self.mode!r}')
            complete = False

        enable_playerctl = picks.get('enable_playerctl')
        if enable_playerctl is None:
            pass
        elif isinstance(enable_playerctl, bool):
            self.enable_playerctl = enable_playerctl
        else:
            prt(f'WARN: picks.enable_playerctl is {enable_playerctl!r};'
                f' keeping {self.enable_playerctl!r}')
            complete = False

        rotators = {'lock_mins': self.get_lock_rotated_list,
                    'sleep_mins': self.get_sleep_rotated_list}
        for attr, rotate in rotators.items():
            saved = picks.get(attr)
            if not isinstance(saved, dict):
                prt(f'WARN: picks has no usable {attr}; keeping configured values')
                complete = False
                continue
            for selector in ['LoBattery', 'HiBattery', 'Settings']:
                first = saved.get(selector)
                if not isinstance(first, int) or isinstance(first, bool):
                    prt(f'WARN: picks.{attr}.{selector} is {first!r};'
                        f' keeping configured value')
                    complete = False
                    continue
                rotate(selector, first=first)

        if complete:
            prt('restored picks OK:', picks)
        else:
            prt('restored picks PARTIAL:', picks)
        return complete



    def update_running_idle_s(self):
        """Update the running idle seconds (owned by the idle state machine)."""
        self.idle_state.update_running_idle_s()

    def DB(self):  # pylint: disable=invalid-name  # historic name
        """ is debug on? """
        rv = self.get_params().debug_mode
        return rv

    def show_icon(self, inhibited=''):
        """ Display Icon if updated """
        emode = self.get_effective_mode()
        num = (3 if self.battery.selector == 'LoBattery'
                else 1 if emode in ('Presentation', )
                else 7 if inhibited == 'systemd'
                else 4 if inhibited == 'player'
                else 0 if emode in ('SleepAfterLock',)
                else 2)
        lock_secs = self.get_lock_min_list()[0]*60
        # down_secs = self.get_sleep_min_list()[0]*60 + lock_secs
        moon_when = lock_secs - min(60 , lock_secs/8)
        if num == 0 and self.running_idle_s >= moon_when:
            num = 5
        elif num == 2 and self.running_idle_s >= moon_when:
            num = 6
        # prt(f'{num=} {self.running_idle_s=} {moon_when=}')

        if num != self.current_icon_num:
            self.tray_icon.setIcon(self.icons[num])
            self.current_icon_num = num
            return True # changed
        return False # unchanged

    def _warn_inhibit_source(self, message):
        """Report a failed inhibit query once, not once per event."""
        if message == self._inhibit_warning:
            return
        self._inhibit_warning = message
        prt(f'WARN: {message}')

    def _events(self):
        """The event source, if this applet has one (bare test instances do not)."""
        return getattr(self, 'events', None)

    def _bus_is_trusted(self):
        """True when the signal stream is expected to keep the cache current."""
        return (self._events() is not None
                and getattr(self, '_events_bus_ok', False))

    def _blocked_operations(self):
        """logind's blocked operations: the cache when it can answer.

        The event source keeps ``BlockInhibited`` up to date from the signal
        stream, so this is normally a dictionary lookup.  One query is made
        when the cache is empty (start-up, after a resync, or a property that
        arrived invalidated), and it is cached only when a live event source
        will keep it current -- otherwise the old per-tick polling stands.
        """
        events = self._events()
        if self._bus_is_trusted():
            cached = events.blocked
            if cached is not None:
                return set(cached)
            blocked = self._logind_blocked()
            if blocked is not None:
                events.note_blocked(blocked)
            return blocked
        return self._logind_blocked()

    def _cached_media(self):
        """True/False from the event source, or None when it cannot answer."""
        if not self._bus_is_trusted():
            return None
        return self._events().media_playing

    def _note_media(self, playing):
        """Cache a one-off ``playerctl status`` answer, when a bus will use it."""
        if self._bus_is_trusted():
            self._events().note_media(playing)

    def _playing_names(self):
        """The players the event source is currently showing as playing."""
        if not self._bus_is_trusted():
            return []
        return list(self._events().media_players)

    def _player_ctl_label(self):
        """The PlayerCtl entry: ``Disable``, ``Idle``, or ``Busy`` with who.

        The state word is short because the blocker rides on the same line,
        and the count comes *first* (``2+firefox``) so a long name can only
        lose its own tail, never the number of blockers.  The count appears
        only when more than one player is playing; a player the bus never
        named is still shown, as ``player``.
        """
        if not self.enable_playerctl:
            return '♫ PlayerCtl: Disable'
        if getattr(self, 'was_play_state', '') != 'playing':
            return '♫ PlayerCtl: Idle'
        names = self._playing_names()
        if not names:
            return '♫ PlayerCtl: Busy'
        who = f'{len(names)}+{names[0]}' if len(names) > 1 else names[0]
        return f'♫ PlayerCtl: Busy: {who}'

    def _play_state(self):
        """``playing``, another playerctl state, or ``''``.

        MPRIS is a bus protocol, so the signal stream already knows whether a
        player is playing: ``playerctl`` is consulted only when the cache has
        nothing to say (no event source, or a player that appeared without
        publishing a status).
        """
        if not self.enable_playerctl:
            return ''
        cached = self._cached_media()
        if cached is not None:
            return 'playing' if cached else ''
        if not self.has_playerctl:
            return ''
        child = subprocess.run(
            ['playerctl', 'status'],
            capture_output=True, text=True, check=False
        )
        play_state = child.stdout.strip().lower()
        # Exit 1 with an empty stdout is "no player", which is normal;
        # any other status is worth one line in the log.
        if child.returncode not in (0, 1):
            self._warn_inhibit_source(
                f'playerctl status failed (rc={child.returncode}):'
                f' {child.stderr.strip()}')
        self._note_media(play_state == 'playing')
        return play_state

    def _logind_blocked(self):
        """Ask logind which operations are blocked; None means "could not ask".

        busctl is used rather than a QtDBus call: it ships with systemd, which
        this applet already requires, and its exit status is meaningful.  It is
        only reached when the event source cannot answer, so the steady state
        spawns nothing.
        """
        argv = ['busctl', 'get-property', LOGIND_SERVICE, LOGIND_PATH,
                LOGIND_MANAGER, BLOCK_INHIBITED]
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    check=False)
        except OSError as exc:
            self._warn_inhibit_source(f'cannot run busctl: {exc}')
            return None
        if result.returncode != 0:
            self._warn_inhibit_source(
                f'busctl get-property {BLOCK_INHIBITED} failed'
                f' (rc={result.returncode}): {result.stderr.strip()}')
            return None
        value = parse_block_inhibited(result.stdout)
        if value is None:
            self._warn_inhibit_source(
                f'unexpected {BLOCK_INHIBITED} answer: {result.stdout.strip()!r}')
            return None
        return blocked_operations(value)

    def _inhibitor_rows(self, blocked, refresh=False):
        """Who is blocking what, for the menu.  Display only.

        ``--mode=block`` leaves delay inhibitors out on purpose: a session
        manager postponing suspend while it locks the screen is not preventing
        it.  Only rows blocking one of the operations behind the verdict are
        kept, so a compositor holding the power key does not read as "keep this
        session awake".  An unreadable source costs rows, never the verdict.

        The rows are cached per blocked set, because the same set is read on
        every tick while an inhibitor is held.  ``refresh`` forces a re-read;
        the menu does that when it opens, since *who* is blocking can change
        without the blocked set changing.
        """
        key = frozenset(blocked)
        if (not refresh and getattr(self, '_inhibit_rows', None)
                and key == getattr(self, '_rows_blocked', None)):
            return self._inhibit_rows
        rows = self._bus_rows(key)
        if rows is None:
            rows = self._table_rows(key)
        self._rows_blocked = key
        return rows

    def _bus_rows(self, blocked):
        """Rows from logind's ListInhibitors; None when it cannot be asked."""
        if not self._bus_is_trusted():
            return None
        return Inhibitors.bus_rows(blocked)

    def _table_rows(self, blocked):
        """Rows from the ``systemd-inhibit`` listing -- the no-QtDBus fallback."""
        summary = f'blocked: {" ".join(sorted(blocked))}'
        try:
            result = subprocess.run(
                ['systemd-inhibit', '--no-pager', '--no-legend',
                 '--mode=block'],
                capture_output=True, text=True, check=False)
        except OSError as exc:
            self._warn_inhibit_source(f'cannot run systemd-inhibit: {exc}')
            return [summary]
        if result.returncode != 0:
            self._warn_inhibit_source(
                f'systemd-inhibit failed (rc={result.returncode}):'
                f' {result.stderr.strip()}')
            return [summary]
        rows = [summary]
        for line in result.stdout.splitlines():
            # WHO UID USER PID COMM WHAT WHY MODE -- WHAT is at index 5 as long
            # as no name in the first five columns contains a space.  A row
            # this cannot read is skipped, never guessed at: the verdict does
            # not come from here.
            parts = line.split()
            if len(parts) < 6 or not blocked & set(parts[5].split(':')):
                continue
            rows.append(line.rstrip())
        return rows

    def check_inhibited(self):
        """Decide whether something is holding the session awake.

        The verdict comes from logind's BlockInhibited property, a machine
        field, not from the systemd-inhibit table: column layout, locale and
        whether the listing tool exists at all are properties of a rendering,
        and a parse that degrades reads as "not inhibited" -- the dangerous
        direction.

        The event source keeps that property (and the MPRIS play state) from
        the signal stream, so this method normally spawns nothing at all; a
        one-off query stands in only when the cache cannot answer.  The who/why
        rows are still read when something *is* blocked, because the menu shows
        them; see _inhibitor_rows().
        """
        blocked = self._blocked_operations()
        if blocked is None:
            # Unknown counts as "not inhibited": this applet exists to lock the
            # screen, so an unreadable inhibit state must never switch locking
            # off.  It is never silent -- _logind_blocked() logged it once.
            inhibited, rows = '', []
        else:
            inhibited = 'systemd' if blocked & GUARDED_OPERATIONS else ''
            rows = self._inhibitor_rows(blocked) if inhibited else []
            if self.DB():
                listed = ','.join(sorted(blocked)) or '(none)'
                prt('DB', f'logind {BLOCK_INHIBITED}={listed}')

        play_state = self._play_state()

        # Media playback inhibits locking/sleeping, but an existing systemd
        # inhibitor keeps priority (it is already visible as the StopSign).
        if play_state == 'playing' and not inhibited:
            inhibited = 'player'

        if self.was_play_state != play_state:
            prt(f'playerctl play_state={play_state!r}')
            self.was_play_state = play_state
            self.poll_100ms = True

        emode = self.get_effective_mode()

        # A change of the inhibit kind (not just the icon number) must also
        # restart the idle manager: on Wayland, swayidle is rebuilt w/o
        # timeouts while media plays, even if the icon did not change.
        # Assign first, because the idle manager reads self.was_inhibited.
        was_inhibited = self.was_inhibited
        self.was_inhibited = inhibited
        if self.show_icon(inhibited=inhibited) or was_inhibited != inhibited:
            self.poll_100ms = True
            self.idle_manager_start()

        self.was_effective_mode = emode
        self.was_selector = self.battery.selector
        # The menu rebuild key is a tuple, so it can never compare equal to the
        # 'Changed Mode' string the mode switches assign to was_output to force
        # a rebuild.
        was_output = self.was_output
        signature = (tuple(rows), frozenset(blocked or ()))
        self.was_output = signature
        return rows, bool(was_output != signature)

    def _battery_reading(self):
        """Where the battery stands, or None when the machine has none.

        The event source keeps ``Percentage``, ``State`` and ``IsPresent``
        from the UPower signal payload, so applying a battery event costs a
        dictionary lookup instead of a sysfs walk.  ``read_power()`` answers
        only when the bus has not spoken -- the first tick, a machine without
        UPower, and the five-minute resync -- and its answer is cached with
        the same lifetime as the other one-off queries, so a bus that stays
        silent still means one reading, not one per tick.

        ``read_power()`` rather than ``psutil`` because psutil derives
        ``power_plugged`` from ``AC/online`` alone, and that file is stale on
        some machines (Chromebooks on cros_ec report ``online=1`` while the
        battery says ``discharging``).  See PowerSupply for the cross-check.
        """
        events = self._events()
        if self._bus_is_trusted():
            if events.battery_present is False:
                return None
            plugged = events.battery_plugged
            percent = events.battery_percent
            if plugged is not None and percent is not None:
                return SimpleNamespace(present=True, plugged=plugged,
                                       percent=round(percent, 1))
        power = read_power()
        if power.present is not True:
            return None
        if self._bus_is_trusted() and power.percent is not None:
            events.note_battery(power.percent, power.plugged)
        return SimpleNamespace(present=True, plugged=power.plugged,
                               percent=power.percent)

    def update_battery_status(self):
        """Re-read the power state and pick the tier it belongs to.

        A machine with no battery keeps whatever the menu's test cycler put
        there, but the absence itself is re-tested on every tick: a battery
        the kernel reports late (or only after a resume) must not stay
        "absent" for the life of the process.
        """
        reading = self._battery_reading()
        was_present = self.battery.present
        if reading is None:
            self.battery.present = False
            if was_present is not False:
                self.battery.synthetic = False
                self.rebuild_menu = True
                prt('power: no battery; the power item is now a test cycler')
            return

        was_plugged = self.battery.plugged
        was_selector = self.battery.selector

        # A real reading replaces anything the test cycler fabricated.
        self.battery.present = True
        self.battery.synthetic = False
        self.battery.plugged = reading.plugged
        if reading.percent is not None:
            self.battery.percent = reading.percent
        if reading.plugged is True:
            self.battery.selector = 'Settings'
        elif reading.plugged is False:
            # Keep the tier a real reading would pick; percent is unknown only
            # on a driver that publishes neither capacity nor energy.
            percent = self.battery.percent
            if percent is None or percent > self.get_params().lo_battery_pct:
                self.battery.selector = 'HiBattery'
            else:
                self.battery.selector = 'LoBattery'
        if was_plugged != self.battery.plugged or was_selector != self.battery.selector:
            self.rebuild_menu = True
            prt(f'power: plugged={self.battery.plugged}'
                f' percent={self.battery.percent}'
                f' selector={self.battery.selector}')

    def battery_line(self):
        """The power menu item's label, built from what was detected.

        The label used to be inferred from the selector, and ``Settings`` is
        both the plugged-in bucket and the start-up default, so a machine
        whose battery was never read still showed "Plugged In".  The reading
        decides now; the selector is only a fallback for the unknown case.
        """
        battery = self.battery
        percent = battery.percent
        plugged = getattr(battery, 'plugged', None)
        on_mains = (plugged if plugged is not None
                    else battery.selector == 'Settings')
        if on_mains:
            label = '↯ Plugged In'
        else:
            icon = '█' if battery.selector == 'HiBattery' else '▃'
            label = f'{icon} {battery.selector}'
        # The percentage is the battery status: it is shown whatever the tier
        # is, including a full battery on mains, which used to show nothing.
        if percent is not None:
            label += f' {percent:g}%'
        if getattr(battery, 'synthetic', False):
            label += ' (TESTING)'
        return label

    def reset_xidle_ms(self):
        """ TBD"""
        self.run_command('reset_idle')

    def set_state(self, name):
        """Set the state of the applet (owned by the idle state machine)."""
        self.idle_state.set_state(name)

    def on_timeout(self):
        """Run the cheap state machine; expensive checks only on events."""
        self.loop += 1
        self._refresh_pending = False
        if self.DB():
            prt('DB', f'on_timeout() {self.loop=}/{self.loop_sample} ...')
#       if not QSystemTrayIcon.isSystemTrayAvailable():
#           prt('SystemTray is gone ... restarting')
#           self.restart_self(None)

        # Without an event source for a given input there is nothing to tell
        # us it changed, so keep the old once-per-tick polling for that input.
        # (The inhibit verdict needs no flag here: _blocked_operations() goes
        # back to querying logind itself when no bus is trusted.)
        if not self._events_bus_ok:
            self._battery_dirty = True
        if not self._events_config_ok:
            self._config_dirty = True

        if self._config_dirty:
            self._config_dirty = False
            self.reconfig()
        if self.idle_manager:
            self.idle_manager.checkup()
        if self._battery_dirty:
            self._battery_dirty = False
            self.update_battery_status()
            # A new battery tier changes the icon even when no inhibitor did.
            self.show_icon(inhibited=self.was_inhibited or '')
        # The fabricated battery drains even when no real power event arrives.
        self._advance_test_battery()
        # Reading the cached verdict costs nothing, so it happens every tick:
        # there is no dirty flag a missed signal could leave stale.
        rows, updated = self.check_inhibited()
        self._inhibit_rows = rows
        if updated:
            self.rebuild_menu = True
        if self.rebuild_menu:
            self.build_menu(self._inhibit_rows)
            self.rebuild_menu = False
            prt('re-built menu')

        if self.loop >= self.loop_sample:
            # The branch ladder (lock / blank / sleep / reset / awake) is the
            # idle state machine; this method only schedules it.
            self.idle_state.tick()
            self.loop = 0

        if self.poll_100ms:
            poll_ms = 100
            self.poll_100ms = False
            self.loop = self.loop_sample
            prt(f'{poll_ms=}')
        else:
            poll_ms = int(self.poll_s * 1000)
        self.timer.setInterval(poll_ms)

    def _toggle_battery(self, _=None):
        if self.battery.present is False:
            # lets you either use the lo/hi battery setting for another
            # purpose or test out your battery settings
            selector = self.battery.selector
            selector = ('LoBattery' if selector == 'HiBattery'
                        else 'Settings' if selector == 'LoBattery' else 'HiBattery')
            self.battery.selector = selector
            # Only the battery legs of the cycle are manufactured; the
            # Plugged In leg is the machine's real state, so it drops the
            # menu's "(TESTING)" suffix again.
            self.battery.synthetic = selector != 'Settings'
            if selector == 'Settings':
                self.battery.plugged = True
                self.battery.percent = 100
            else:
                self.battery.plugged = False
                # Start each tier where a real reading would put it: a full
                # charge for HiBattery, and the threshold itself -- the
                # highest percentage that still counts as LoBattery.
                self.battery.percent = (100 if selector == 'HiBattery'
                                        else self.get_params().lo_battery_pct)
            # self.ini_tool.set_effective_params(selector)
            # The selector also picks the icon, and check_inhibited() re-shows
            # it on the next tick.
            self.rebuild_menu = True

    def _advance_test_battery(self):
        """Discharge the fabricated battery so the test state counts down.

        Which tier is under test is the user's choice, so the selector is not
        changed here.  HiBattery stops one point above the low threshold and
        stays a high-battery state; LoBattery runs down to zero, where that
        tier's own power_down policy can act.
        """
        if not getattr(self.battery, 'synthetic', False):
            return
        if self.battery.selector == 'Settings':
            return
        floor = (self.get_params().lo_battery_pct + 1
                 if self.battery.selector == 'HiBattery' else 0)
        percent = max(floor, self.battery.percent - TEST_DISCHARGE_STEP)
        if percent != self.battery.percent:
            self.battery.percent = percent
            self.rebuild_menu = True

    def _lock_rotate_next(self, advance=None):
        advance = True if advance is None else advance
        mins = self.get_lock_min_list()
        if len(mins) < 1 or (len(mins) == 2 and mins[0] == mins[1]):
            return mins[0]
        next_mins = mins[1]
        if advance:
            mins0 = mins[0]
            mins = self.get_lock_rotated_list(first=next_mins)
            self.rebuild_menu = bool(mins0 != mins[0])
            self.save_picks()
            self.idle_manager_start()
        return next_mins

    def _lock_rotate_str(self):
        mins = self.get_lock_min_list()
        rv = f'{mins[0]}m' + ('' if mins[0] == mins[1] else f'->{mins[1]}m')
        return rv

    def toggle_playerctl(self):
        # MPRIS is a bus protocol: with a live event source the media inhibit
        # works without playerctl, which is only the fallback query.
        if self.has_playerctl or self._bus_is_trusted():
            self.enable_playerctl = not bool(self.enable_playerctl)
            self.save_picks()
            self.rebuild_menu = True
            self.poll_100ms = True # apply the new setting immediately

    def _sleep_rotate_next(self, advance=None):
        advance = True if advance is None else advance
        mins = self.get_sleep_min_list()
        if len(mins) < 1 or (len(mins) == 2 and mins[0] == mins[1]):
            return mins[0]
        next_mins = mins[1]
        if advance:
            mins0 = mins[0]
            mins = self.get_sleep_rotated_list(first=next_mins)
            self.rebuild_menu = bool(mins0 != mins[0])
            self.save_picks()
            self.idle_manager_start()
        return next_mins

    def _sleep_rotate_str(self):
        mins = self.get_sleep_min_list()
        rv = f'{mins[0]}m' + ('' if mins[0] == mins[1] else f'->{mins[1]}m')
        return rv

    def build_menu(self, rows=None):
        """TBD"""
        # pylint: disable=unnecessary-lambda
        def has_cmd(label):
            return bool(self.variables.get(label, None))

        def add_item(text, callback, enabled=True):
            nonlocal self
            item = QAction(text)
            item.triggered.connect(callback)
            item.setEnabled(enabled)
            self.menu.addAction(item)
            self.menu_items.append(item)

        first_menu = self.menu is None
        if first_menu:
            self.menu = QMenu()
        else:
            self.menu.clear()
        self.menu_items = []

        if rows:
            for row in rows:
                add_item(row, self.dummy)

        # Menu "icons" are glyphs, not images, so they must come from the same
        # font as the labels: only characters covered by DejaVu Sans (the
        # de-facto floor on Linux desktops, alongside Liberation Sans) are
        # used here.  Emoji-block codepoints (U+1F000 and up) fall back to
        # Noto Color Emoji, which renders them coloured, wide and out of
        # style -- and as tofu boxes where that font is absent.
        # They stay *inside* the label on purpose.  A QAction icon is painted
        # in one column at the menu's left edge, which moves every symbol away
        # from its group name ("niri:  ▷ Lock Screen") and from the em-space
        # indent of a sub-item -- and that grouping matters more than drawing
        # the symbols in their own colour.  Inline glyphs inherit the menu's
        # text colour, so per-symbol colouring is not available here.
        if self.mode not in ('Presentation',):
            add_item(f'◉ Presentation ← {self.mode} Mode', self.enable_presentation_mode)

        if self.mode not in ('LockOnly',):
            add_item(f'◐ LockOnly ← {self.mode} Mode', self.enable_nosleep_mode)

        if self.mode not in ('SleepAfterLock',):
            add_item(f'○ SleepAfterLock ← {self.mode} Mode', self.enable_normal_mode)

        add_item(f'{self.graphical}:  ▷ Lock Screen', self.lock_screen)

        if (self.get_params().turn_off_monitors and self.variables['monitors_off']):
            add_item('   ▷ Blank Monitors', self.blank_quick)

        if has_cmd('reload_wm'):
            add_item('   ▷ Reload', self.reload_wm)

        if has_cmd('restart_wm'):
            add_item('   ▷ Restart', self.restart_wm)

        add_item('   ▷ Log Off', self.exit_wm)
        add_item('System:  ▼ Suspend', self.suspend)
        add_item('    ▼ Reboot', self.reboot)
        add_item('    ▼ PowerOff', self.poweroff)

        # Only a fabricated HiBattery/LoBattery leg is synthetic; the
        # plugged-in state of a machine without a battery, whether reached at
        # start-up or by closing the cycle, is its real state and must not be
        # labelled as a test.
        add_item(self.battery_line(), self._toggle_battery)

        # if self.mode not in ('Presentation',) and len(self.opts.lock_min_list) > 1:
        add_item(f'  ♺ Lock: {self._lock_rotate_str()}',
                 lambda: self._lock_rotate_next())

        # if self.mode in ('SleepAfterLock',) and len(self.opts.sleep_min_list) > 1:
        add_item(f'  ♺ Sleep (after Lock): {self._sleep_rotate_str()}',
                 lambda: self._sleep_rotate_next())

        label = self._player_ctl_label()
        add_item(label, self.toggle_playerctl)
        self._player_ctl_cache = label
        if self.get_params().gui_editor:
            add_item('✎  Edit Applet Config', self.edit_config)
        # Only offer the log viewer when something can actually show it: the
        # applet has no tty of its own, so less needs a terminal emulator.
        if Utils.terminal_argv([], getattr(self.get_params(), 'log_viewer', '')):
            add_item('≡  Open Applet Log', self.open_log)

        add_item('☓ Quit this Applet', self.quit_self)

        # The version rides on the restart entry instead of a separate dead
        # label: the log says the same thing at START-UP, but "which build am I
        # running?" is a question asked from the menu, usually about a DE that
        # behaves differently from what the reader expects.
        add_item(f'↺ Restart pwr-tray v{__version__}', self.restart_self)

        if first_menu:
            self.tray_icon.setContextMenu(self.menu)
            # The rows name *who* is blocking, and that can change while the
            # blocked-operation set does not, so refresh them when the menu is
            # opened -- a ListInhibitors call, only when someone is looking.
            self.menu.aboutToShow.connect(self._on_menu_about_show)

        if not self.tray_icon.isVisible():
            prt('self.tray_icon.isVisible() is False ... restarting app')
            time.sleep(0.5)
            self.restart_self(None)

    def _on_menu_about_show(self):
        """Refresh the rows and the PlayerCtl label when the menu opens.

        The verdict is event-driven, but the rows and the blocker's name are
        renderings of it: the processes holding sleep, and which player is the
        one playing, change while the blocked-operation set stays the same --
        and logind announces neither.  Asking once per menu open keeps both
        honest without a fork.  When no bus is trusted the rows come from the
        polling fallback anyway and no player name is available, so nothing is
        done here.
        """
        if not self._bus_is_trusted():
            return
        blocked = self._blocked_operations()
        rows = []
        if blocked and blocked & GUARDED_OPERATIONS:
            rows = self._inhibitor_rows(blocked, refresh=True)
        label = self._player_ctl_label()
        if (rows != self._inhibit_rows
                or label != getattr(self, '_player_ctl_cache', None)):
            self._inhibit_rows = rows
            self.build_menu(rows)

    def on_tray_icon_activated(self, reason):
        left_click = (reason == QSystemTrayIcon.Trigger and not self.is_wayland)
        if reason == QSystemTrayIcon.Context or left_click:
            self.tray_icon.contextMenu().exec_(QCursor.pos())

    # -- tray registration -------------------------------------------------
    def _install_tray_icon(self):
        """Create the tray item; showing it is what registers it with a host.

        Qt talks to the StatusNotifierWatcher once, when the item is shown, and
        never retries, so re-creating the item is the only way to register with
        a host that was absent the first time -- or with a host that has been
        restarted since.  The previous item is hidden before the new one is
        shown, so a host never lists two pwr-tray entries.
        """
        previous = getattr(self, 'tray_icon', None)
        if previous is not None:
            previous.hide()
            previous.deleteLater()
        icon_num = self.current_icon_num if self.current_icon_num >= 0 else 0
        self.tray_icon = QSystemTrayIcon(self.icons[icon_num], self.app)
        self.tray_icon.setToolTip('pwr-tray')
        if self.menu is not None:
            self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.setVisible(True)

    def _on_tray_host_appeared(self):
        """A host appeared (or restarted): register the item with it again."""
        self._tray_seen = True
        # A watcher claims its bus name just before it starts serving calls,
        # so give it a moment instead of racing its own start-up.
        QTimer.singleShot(TRAY_ATTACH_DELAY_MS, self._reattach_tray)

    def _on_tray_host_lost(self):
        """The host went away; the registration went with it."""
        self._tray_seen = False

    def _reattach_tray(self):
        """Re-create the item at last, if a host is really there now."""
        if tray_watcher_present() is not True:
            # The host left again between its announcement and this call; the
            # next appearance schedules another attempt.
            self._tray_seen = False
            return
        self._install_tray_icon()
        prt('tray icon registered with the host that is now running')

    def _ensure_tray_attached(self):
        """Retry a missed host announcement from the periodic resync."""
        watch = getattr(self, 'tray_watch', None)
        if getattr(self, '_tray_seen', True) or watch is None or not watch.watching:
            return
        if tray_watcher_present():
            self._on_tray_host_appeared()

    def idle_manager_start(self):
        """ For any mode, get the idle manager started"""
        if self.idle_manager:
            self.idle_manager.start()

    @staticmethod
    def dummy(_):
        """TBD"""

    # -- user actions ------------------------------------------------------
    # The command sequences and process spawning live in Actions.py.  These
    # thin entry points keep the menu wiring and the historical static call
    # surface (PwrTray.suspend(None), ...) intact; the class no longer owns
    # the action layer.

    @staticmethod
    def run_command(key):
        Actions(PwrTray.singleton).run_command(key)

    @staticmethod
    def quit_self(_):
        Actions(PwrTray.singleton).quit_self()

    @staticmethod
    def restart_self(_):
        Actions(PwrTray.singleton).restart_self()

    @staticmethod
    def edit_config(_):
        Actions(PwrTray.singleton).edit_config()

    @staticmethod
    def open_log(_):
        Actions(PwrTray.singleton).open_log()

    @staticmethod
    def suspend(_):
        Actions(PwrTray.singleton).suspend()

    @staticmethod
    def poweroff(_):
        Actions(PwrTray.singleton).poweroff()

    @staticmethod
    def reboot(_):
        Actions(PwrTray.singleton).reboot()

    @staticmethod
    def lock_screen(_):
        Actions(PwrTray.singleton).lock_screen()

    def blank_primitive(self, lock_screen=False):
        Actions(self).blank_primitive(lock_screen=lock_screen)

    @staticmethod
    def blank_quick(_):
        Actions(PwrTray.singleton).blank_quick()

    @staticmethod
    def reload_wm(_):
        Actions(PwrTray.singleton).reload_wm()

    @staticmethod
    def restart_wm(_):
        Actions(PwrTray.singleton).restart_wm()

    @staticmethod
    def exit_wm(_):
        Actions(PwrTray.singleton).exit_wm()

    @staticmethod
    def enable_presentation_mode(_):
        Actions(PwrTray.singleton).enable_presentation_mode()

    @staticmethod
    def enable_nosleep_mode(_):
        Actions(PwrTray.singleton).enable_nosleep_mode()

    @staticmethod
    def enable_normal_mode(_):
        Actions(PwrTray.singleton).enable_normal_mode()

    @staticmethod
    def goodbye(message=''):
        """Close the run that main() opened with its START-UP line.

        Registered with atexit, so it runs for sys.exit(), for a normal return
        from the event loop, and for an exception that reaches the
        interpreter.  It does *not* run after SIGKILL or after
        restart_self()'s os._exit(), which is why the replacement process
        prints its own START-UP: an ENDED line with no later START-UP is the
        end of a run, and a START-UP with no ENDED is a run that was killed.
        """
        detail = f' {message}' if message else ''
        prt(f'ENDED pwr-tray v{__version__} pid {os.getpid()}{detail}')

def tray_watcher_present():
    """Ask the session bus whether a StatusNotifierWatcher is registered.

    Returns True/False, or None when the question cannot be asked at all
    (no QtDBus module, no session bus, D-Bus error).  QtDBus is used instead of
    pydbus: PyQt5 is already a hard dependency, so the check adds no package
    and does not depend on an unmaintained binding.  The import is local, so
    importing this module -- and therefore `pwr-tray --help` -- never touches
    D-Bus.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from PyQt5.QtDBus import QDBusConnection
    except ImportError:
        prt('WARN: QtDBus unavailable; cannot detect a tray')
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            prt('WARN: no D-Bus session bus; cannot detect a tray')
            return None
        interface = bus.interface()
        # The default D-Bus timeout is 25s, which would stall start-up and the
        # periodic resync if the bus is wedged.
        interface.setTimeout(2000)
        reply = interface.isServiceRegistered(STATUS_NOTIFIER_WATCHER)
        return bool(reply.isValid() and reply.value())
    # QDBusError has no import-safe base class, so this one has to be broad.
    except Exception as exc:                    # noqa: BLE001
        prt(f'WARN: tray detection failed: {exc}')
        return None


def load_known_de_json():
    """Return (de_json, source) for --list-des.

    A user override in my-commands.yaml wins over the packaged defaults, so the
    listing matches what the running application would actually use.
    """
    ini_tool = IniTool(paths_only=True)
    user_yaml = os.path.join(ini_tool.folder, 'my-commands.yaml')
    if os.path.exists(user_yaml):
        with open(user_yaml, encoding='utf-8') as handle:
            return dict(YAML().load(handle)), user_yaml
    return (dict(YAML().load(Utils.resource_text('commands.yaml'))),
            'built-in commands.yaml')


def format_desktops(de_json, source=''):
    """Return the --list-des report as a list of lines.

    Each known desktop is shown with every command it needs (defaults,
    session type and desktop layers merged).  Commands missing from $PATH are
    marked, which is the "must be installed if missing" report.
    """
    desktops = de_json.get('desktops', {})
    lines = [f'desktops known to pwr-tray (from {source or "commands.yaml"}):']
    width = max((len(name) for name in desktops), default=0)
    any_missing = False
    for name in sorted(desktops):
        shown = []
        for cmd in PwrTray.must_haves_for(de_json, name):
            if shutil.which(cmd) is None:
                shown.append(f'{cmd} [!]')
                any_missing = True
            else:
                shown.append(cmd)
        lines.append(f'  {name:<{width}}  requires:'
                     f' {" ".join(shown) if shown else "nothing"}')
    if any_missing:
        lines.append('  [!] = not found on $PATH'
                     ' (install it before using that desktop)')
    lines.append('force a desktop with: pwr-tray --de NAME')
    return lines


def acquire_lock_or_take_over(lock, replace=False):
    """Take the single-instance lock and report whether this process owns it.

    The lock comes *first* now.  Killing the incumbent used to run before the
    lock, so a second launch never detected the first one -- it terminated it
    and then took over.  With ``Restart=always`` in the shipped units, a
    systemd unit plus a DE autostart could then ping-pong forever: each new
    instance killed the other, and each supervisor restarted its own.

    Killing is now the *takeover* path, and it is opt-in (``--replace``).  Even
    then the recorded pid is only signalled after its command line is confirmed
    to still be pwr-tray, so a recycled pid or a foreign lock holder is left
    alone.
    """
    if lock.acquire(retries=5, delay=0.2):
        return True
    holder = Utils.SingleInstance.read_pid(lock.path)
    if not replace or not holder:
        return False
    prt(f'--replace: terminating lock holder pid {holder}')
    if not PyKill().kill_pid(holder, ['pwr-tray']):
        prt(f'WARNING: pid {holder} does not look like pwr-tray; not killing it')
        return False
    # The old process may need a moment to exit and let go of the flock.
    return lock.acquire(retries=25, delay=0.2)


def confirm(question):
    """Ask the user on the terminal; anything but an explicit yes is no."""
    if not sys.stdin.isatty():
        print('  (standard input is not a terminal; pass --yes to confirm)')
        return False
    try:
        answer = input(f'{question} [y/N] ')
    except EOFError:
        return False
    return answer.strip().lower() in ('y', 'yes')


def run_service_command(opts):
    """Run one --svc-* verb and return the process exit status.

    None of these verbs construct the applet: they render the shipped unit for
    the detected desktop, install it, and hand control to systemctl.  Desktop
    detection is shared with the applet, so --de means the same thing here.
    """
    de_json, _ = load_known_de_json()
    try:
        desktop = PwrTray.detect_de(
            de_json, opts.de, log=lambda *args, **kwargs: None)['name']
    except PwrTrayError as exc:
        Utils.error_print(str(exc))
        return 2
    try:
        holder = service.applet_holder(IniTool(paths_only=True).folder)
    except OSError:
        holder = None    # unreadable lock: not a reason to refuse the request
    try:
        return service.dispatch(opts.svc, desktop,
                                force=opts.force,
                                yes=opts.yes or opts.force,
                                ask=confirm,
                                holder=holder)
    except service.ServiceError as exc:
        Utils.error_print(f'pwr-tray: {exc}')
        return 1


def main():
    # pylint: disable=import-outside-toplevel
    import argparse
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = argparse.ArgumentParser(prog='pwr-tray')
    parser.add_argument('-V', '--version', action='version',
            version=f'pwr-tray {__version__}',
            help='show version and exit')
    parser.add_argument('-D', '--debug', action='store_true',
            help='override debug_mode from .ini initially')
    parser.add_argument('-o', '--stdout', action='store_true',
            help='keep logging on stdout instead of debug.log (ignored inside'
                 ' a systemd unit)')
    parser.add_argument('-f', '--follow-log', action='store_true',
            help='exec tail -n50 -F on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('-q', '--quick', action='store_true',
            help='quick mode (1m lock + 1m sleep)')
    parser.add_argument('--de', metavar='NAME',
            help='force desktop (e.g. i3-x11, sway-wayland, niri-wayland,'
                 ' kde-wayland)')
    parser.add_argument('--list-des', action='store_true',
            help='list known desktops and the commands each requires, then exit')
    parser.add_argument('-b', '--battery', action='store_true',
            help='print the detected battery/AC state, plus the raw'
                 ' /sys/class/power_supply values behind it, then exit')
    parser.add_argument('--replace', action='store_true',
            help='if another pwr-tray holds the single-instance lock, terminate'
                 ' it and take over (default: refuse to start)')
    parser.add_argument('-y', '--yes', action='store_true',
            help='never prompt; assume yes for --svc-* questions')
    parser.add_argument('--force', action='store_true',
            help='replace or delete a unit file pwr-tray did not write'
                 ' (implies --yes)')
    # Internal: the shipped unit starts pwr-tray with it.  It marks the one kind
    # of run that obeys [ServiceAllowed].  A flag rather than an environment
    # check, because INVOCATION_ID is inherited by every child of a unit -- a
    # terminal opened inside one would make a hand-started pwr-tray look like
    # the service.
    parser.add_argument('--svc-mode', action='store_true', dest='svc_mode',
            help=argparse.SUPPRESS)
    svc = parser.add_mutually_exclusive_group()
    svc.add_argument('--svc', '--svc-status', dest='svc', action='store_const',
            const='status',
            help='report the systemd --user unit state, then exit')
    svc.add_argument('--svc-install', dest='svc', action='store_const',
            const='install',
            help='write the unit for this desktop and enable it (not started);'
                 ' only this verb writes the unit')
    svc.add_argument('--svc-start', dest='svc', action='store_const',
            const='start',
            help='start the installed unit now; enables it if disabled, but'
                 ' never writes the unit')
    svc.add_argument('--svc-restart', dest='svc', action='store_const',
            const='restart',
            help='restart the installed unit so it runs the current code'
                 ' (use after an editable install or a pull); never writes it')
    svc.add_argument('--svc-stop', dest='svc', action='store_const',
            const='stop',
            help='stop it now; it stays enabled for your next login')
    svc.add_argument('--svc-uninstall', dest='svc', action='store_const',
            const='uninstall',
            help='stop, disable and delete the unit (config and logs are kept)')
    svc.add_argument('--show-svc-file', dest='svc', action='store_const',
            const='show-file',
            help='print the systemd unit for this desktop, then exit'
                 ' (a starter for a hand-installed or customized unit)')
    opts = parser.parse_args()

    if opts.battery:
        print(describe(read_power()))
        return

    if opts.svc:
        sys.exit(run_service_command(opts))

    if opts.list_des:
        for line in format_desktops(*load_known_de_json()):
            print(line)
        return

    if opts.edit_config:
        ini_tool = IniTool(paths_only=True)
        editor = os.getenv('EDITOR', 'vim')
        args = [editor, ini_tool.ini_path]
        print(f'RUNNING: {args}')
        os.execvp(editor, args)
        sys.exit(1) # just in case ;-)

    if opts.follow_log:
        ini_tool = IniTool(paths_only=True)
        args = ['tail', '-n50', '-F', ini_tool.log_path]
        print(f'RUNNING: {args}')
        os.execvp('tail', args)
        sys.exit(1) # just in case ;-)

    # os.environ['DISPLAY'] = ':0'

    ini_tool = IniTool(paths_only=False)
    Utils.prt_path = ini_tool.log_path
    # Must happen before the first prt(): prt() may redirect fd 2 into
    # debug.log, which would hide the startup errors reported below.
    Utils.capture_console()
    # Qt talks to its process through a message handler, not through print().
    # Installing ours here -- before PwrTray() builds the QApplication, and with
    # the log path already settled -- turns the platform theme's palette dump
    # and Qt's own warnings into labelled, deduplicated log lines instead of a
    # wall of text with no author.
    install_qt_messages()
    # Paired with PwrTray.goodbye(): one line at each end of a run, both
    # carrying the version and the pid, so a debug.log copied out of the wild
    # answers "which build, which process?" without guesswork.
    prt(f'START-UP pwr-tray v{__version__} pid {os.getpid()}',
        to_stdout=opts.stdout)

    # A unit cannot see the session it was started in: the variables it inherits
    # describe whichever login started `systemd --user`, and Qt picks its
    # platform plugin from them.  Point them at the server that is actually
    # listening -- before Qt exists, and before detect_de() reads the desktop
    # name that [ServiceAllowed] is keyed by.
    for name, old, new in Session.repair_environment(
            note=lambda message: prt(f'SESSION: {message}')):
        prt(f'SESSION: {name}: {old!r} -> {new!r}')

    # Started by the unit, [ServiceAllowed] decides whether this desktop runs
    # the applet at all.  A manual run and a window manager's autostart are never
    # gated: that section is about the service.  Exit 0 is the answer for
    # "nothing to do here", which `Restart=on-failure` leaves final -- the unit
    # ends `inactive` in that session, never `failed`, and never respawns into a
    # session that is logging out.
    if opts.svc_mode:
        try:
            session_desktop = Desktop.detect_de(
                load_known_de_json()[0], opts.de,
                log=lambda *args, **kwargs: None)['name']
        except PwrTrayError as exc:
            prt(f'ERROR: {exc}')
            Utils.error_print(str(exc))
            sys.exit(2)
        allowed = service.allowed_desktops()
        if not allowed.get(session_desktop, False):
            names = ', '.join(name for name, on in allowed.items() if on) or 'nothing'
            prt(f'not allowed in this session ({session_desktop});'
                f' [ServiceAllowed] in {service.config_path()} enables: {names};'
                ' exiting 0')
            return

    # The lock is the guarantee that two applets (and two swayidle managers)
    # never coexist.  It is taken before anything destructive happens: a plain
    # launch refuses to start when an instance already owns it, and only
    # `--replace` inspects the holder, terminates it and takes over.
    lock = Utils.SingleInstance(os.path.join(ini_tool.folder, 'pwr-tray.lock'))
    if not acquire_lock_or_take_over(lock, replace=opts.replace):
        holder = Utils.SingleInstance.read_pid(lock.path)
        message = 'another pwr-tray is already running'
        if holder:
            message += f' (pid {holder})'
        if not opts.replace:
            message += '; use --replace to terminate it and take over'
        prt(f'ERROR: {message}')
        Utils.error_print(f'ERROR: {message}')
        sys.exit(3)
    atexit.register(lock.release)
    atexit.register(PwrTray.goodbye)


    ini_tool.update_config()
    if opts.debug:
        for selector in ini_tool.get_selectors():
            ini_tool.params_by_selector[selector].debug_mode = True # one-time override


    # Nothing waits for the tray here: PwrTray asks once where this session
    # stands, and TrayWatch registers the item whenever a host appears, so a
    # bar that starts late (or restarts) no longer needs a 'sleep' before
    # pwr-tray, nor a restart after it.
    try:
        tray = PwrTray(ini_tool=ini_tool, quick=opts.quick, force_de=opts.de)
    except PwrTrayError as exc:
        prt(f'ERROR: {exc}')      # keep a copy in debug.log
        Utils.error_print(str(exc))  # ...and put it where the user can see it
        sys.exit(2)
    # A logout does not stop this unit with a clean exit: systemd stops
    # graphical-session.target, which signals us.  Turning that into quit() lets
    # atexit release the single-instance lock, write the ENDED line and stop
    # swayidle, instead of the interpreter dying where it stands (and leaving a
    # swayidle behind until the compositor exits).
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_args: tray.app.quit())
    tray.app.exec_()

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)

    ###### AppIndicator3 is catching most of these so may not get many exceptions
    except KeyboardInterrupt:
        prt("Shutdown requested, so exiting ...")
        sys.exit(1)

    # Last-resort handler: report every unexpected error and exit cleanly.
    except Exception:  # noqa: BLE001
        prt("Caught exception running main()\n",
            traceback.format_exc(limit=24))
        sys.exit(9)
