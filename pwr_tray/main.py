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
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

import psutil
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCursor, QIcon
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon  #, QMessageBox
from ruamel.yaml import YAML

from pwr_tray import Utils, __version__, service
from pwr_tray.Errors import (
    DesktopDetectionError,
    MissingCommandsError,
    MissingResourcesError,
    PwrTrayError,
)
from pwr_tray.IniTool import IniTool
from pwr_tray.PowerEvents import PowerEvents
from pwr_tray.SwayIdleMgr import SwayIdleManager
from pwr_tray.Utils import PyKill, prt

# Every tray host -- plasmashell, xfce4-panel, waybar, swaybar+snixembed, ... --
# claims this name.  Its absence is not an error: a bare X11 bar has no
# StatusNotifierWatcher and the applet still runs, it just may not be visible.
STATUS_NOTIFIER_WATCHER = 'org.kde.StatusNotifierWatcher'
TRAY_WAIT_SECONDS = 20.0        # whole wait, reported while it happens
TRAY_POLL_SECONDS = 0.5
TRAY_REPORT_SECONDS = 5.0

# logind answers "is anything keeping this session awake?" with a property, not
# with a table: BlockInhibited is a colon-separated list of the operations that
# are blocked ("" when nothing is).  It is a machine field, so it does not care
# about column layout, locale or whether a listing tool exists.
LOGIND_SERVICE = 'org.freedesktop.login1'
LOGIND_PATH = '/org/freedesktop/login1'
LOGIND_MANAGER = 'org.freedesktop.login1.Manager'
BLOCK_INHIBITED = 'BlockInhibited'
# The operations this applet's own timeouts perform.  handle-power-key and
# handle-lid-switch say a compositor owns an input event, which is not a
# request to stay awake; asking which operations are blocked, rather than which
# rows a table happens to contain, is what removes the per-desktop exception
# list for xfce4-power-manager, org_kde_powerdevil and niri.
GUARDED_OPERATIONS = frozenset({'sleep', 'idle'})


def parse_block_inhibited(output):
    """Return the value of ``busctl get-property ... BlockInhibited``.

    busctl prints one line: ``s "sleep:idle"``, or ``s ""`` when nothing is
    blocked.  ``''`` and ``None`` are different answers -- the first means
    logind says nothing is blocked, the second means the answer could not be
    read -- and callers must never treat them alike.
    """
    lines = output.strip().splitlines()
    text = lines[0].strip() if lines else ''
    if not text.startswith('s "') or len(text) < 4 or not text.endswith('"'):
        return None
    return text[3:-1]


def blocked_operations(value):
    """Split a BlockInhibited value into operation names."""
    return {token for token in value.split(':') if token}


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

    @staticmethod
    def load_de_config(config_dir):
        """Load DE commands YAML config.
        - Always copies default commands.yaml to config dir (so user sees latest)
        - If my-commands.yaml exists in config dir, loads that instead
        """
        yaml = YAML()
        Utils.copy_to_folder('commands.yaml', config_dir)
        user_yaml = os.path.join(config_dir, 'my-commands.yaml')
        if not os.path.exists(user_yaml):
            user_yaml = os.path.join(config_dir, 'commands.yaml')
        prt(f'loading DE config: {user_yaml}')
        with open(user_yaml, encoding='utf-8') as f:
            return dict(yaml.load(f))

    @staticmethod
    def must_haves_for(de_json, compound_name):
        """Return the sorted commands a compound desktop name needs.

        The three config layers accumulate: defaults, session type, desktop.
        """
        _, _, stype = compound_name.rpartition('-')
        session_types = de_json.get('session_types', {})
        desktops = de_json.get('desktops', {})
        must = (de_json.get('defaults', {}).get('must_haves', [])
                + session_types.get(stype, {}).get('must_haves', [])
                + desktops.get(compound_name, {}).get('must_haves', []))
        return sorted(set(must))

    @staticmethod
    def missing_commands(de_config):
        """Return the must_haves of `de_config` that are not on $PATH."""
        return sorted(cmd for cmd in set(de_config.get('must_haves', []))
                      if shutil.which(cmd) is None)

    @staticmethod
    def detect_de(de_json, force_de=None, log=prt):
        """Detect the current DE from environment using JSON config rules.
        Merges three layers: defaults -> session_type (x11/wayland) -> desktop.
        Returns merged config dict.  Use force_de to skip detection.

        Raises DesktopDetectionError when nothing matches: an assert would be
        stripped by python -O, leaving the app running with empty commands.

        `log` is the narration sink: the applet passes prt(), while the
        ``--svc-*`` verbs pass a no-op so their stdout stays machine-readable.
        """
        env = {key: os.environ.get(key, '')
               for key in ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                           'DESKTOP_SESSION', 'XDG_SESSION_TYPE')}
        lowered = {key: value.lower() for key, value in env.items()}
        session_type = lowered['XDG_SESSION_TYPE']
        desktop_str = ' '.join(lowered[key] for key in
                               ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                                'DESKTOP_SESSION'))

        log(f'ENV: XDG_CURRENT_DESKTOP={env["XDG_CURRENT_DESKTOP"]!r}'
            f' XDG_SESSION_DESKTOP={env["XDG_SESSION_DESKTOP"]!r}'
            f' DESKTOP_SESSION={env["DESKTOP_SESSION"]!r}'
            f' XDG_SESSION_TYPE={env["XDG_SESSION_TYPE"]!r}')

        session_types = de_json.get('session_types', {})
        desktops = de_json.get('desktops', {})
        known = list(desktops)

        def _merge(compound_name, entry):
            _, _, stype = compound_name.rpartition('-')
            scfg = session_types.get(stype, {})
            merged = dict(scfg)
            merged.update(entry)
            merged['name'] = compound_name
            merged['session_type'] = stype
            merged['must_haves'] = PwrTray.must_haves_for(de_json,
                                                          compound_name)
            return merged

        # --de override: skip detection
        if force_de:
            if force_de not in desktops:
                raise DesktopDetectionError(
                    f'--de {force_de!r} is not a known desktop',
                    known_desktops=known, env=env)
            log(f'ENV: forced {force_de!r}')
            return _merge(force_de, desktops[force_de])

        # Desktop keys are compound: "kde-wayland", "i3-x11", etc.
        for compound_name, entry in desktops.items():
            detect, _, stype = compound_name.rpartition('-')
            if detect in desktop_str and stype == session_type:
                log(f'ENV: matched {compound_name!r}')
                return _merge(compound_name, entry)

        raise DesktopDetectionError(
            'no desktop environment matched the current session',
            known_desktops=known, env=env)

    @staticmethod
    def ensure_resources(folder, resources):
        """Return absolute paths for `resources`, installing them if needed.

        Paths are always built from `folder`; a bare resource name would
        otherwise resolve against whatever directory the applet was started
        from.  A resource that is still absent after the copy is a startup
        error -- a short icon list used to surface much later as an
        IndexError from show_icon().
        """
        paths = []
        missing = []
        for resource in resources:
            path = os.path.join(folder, resource)
            if not os.path.isfile(path):
                try:
                    Utils.copy_to_folder(resource, folder)
                except OSError as exc:
                    prt(f'WARN: cannot copy {resource!r} to {folder!r}: {exc}')
            if os.path.isfile(path):
                paths.append(path)
            else:
                prt(f'WARN: cannot find {path!r}')
                missing.append(resource)
        if missing:
            raise MissingResourcesError(missing, folder)
        return paths

    def __init__(self, ini_tool, quick=False, force_de=None):
        PwrTray.singleton = self
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
#       while not QSystemTrayIcon.isSystemTrayAvailable():
#           prt("System tray is not available. Retry in 1 second...")
#           time.sleep(1.0)
#       prt("System tray is available. Continuing...")

        self.ini_tool = ini_tool
        self.battery = SimpleNamespace(present=None,
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

        # states are Awake, Locked, Blanked, Asleep
        # when is idle time
        self.tray_icon = QSystemTrayIcon(self.icons[0], self.app)
        self.tray_icon.setToolTip("pwr-tray")
        self.tray_icon.setVisible(True)
        self.state = SimpleNamespace(name='Awake', when=0)

        self.running_idle_s = 0.000
        self.poll_s = 2.000
        self.poll_100ms = False
        self.rebuild_menu = False
        self.picks_file = ini_tool.picks_path
        self.current_icon_num = -1  # triggers immediate icon update
        self.enable_playerctl = True

        # Event-driven refresh: a D-Bus signal or a config-file watch marks the
        # matching flag dirty and schedules one fast poll.  The timer itself
        # only runs the cheap state machine; nothing is spawned per tick.
        self._config_dirty = False
        self._battery_dirty = True
        self._inhibit_dirty = True
        self._inhibit_rows = []
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
        self.variables = dict(de_json['defaults'])
        cmd_keys = set(self.variables.keys()) - {'must_haves'}
        for key in cmd_keys:
            if key in self.de_config:
                self.variables[key] = self.de_config[key]

        # Validate must_haves (accumulated from defaults + session_type + desktop)
        dont_haves = self.missing_commands(self.de_config)
        if dont_haves:
            raise MissingCommandsError(
                dont_haves, desktop=self.de_config['name'],
                session_type=self.de_config['session_type'])

        # Expand {{variable}} references in command values
        for key, val in list(self.variables.items()):
            if isinstance(val, str) and '{{' in val:
                self.variables[key] = re.sub(
                    r'\{\{(\w+)\}\}',
                    lambda m: self.variables.get(m.group(1), m.group(0)),
                    val)

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

        self.menu_items = []
        self.menu = None
        self.build_menu()
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
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
        self._inhibit_dirty = True
        self._schedule_fast_poll()

    def _on_battery_event(self):
        self._battery_dirty = True
        self._schedule_fast_poll()

    def _on_playback_event(self):
        # Playback state feeds check_inhibited(), so the icon and the swayidle
        # command have to be rebuilt when a player starts or stops.
        self._inhibit_dirty = True
        self._schedule_fast_poll()

    def _on_resume_event(self):
        """logind reported the resume half of PrepareForSleep.

        State read before the machine slept (battery, inhibitors, idle time)
        is stale, so force the slow branch of the next tick.
        """
        prt('resume detected')
        self.poll_100ms = True
        self._schedule_fast_poll()

    def _on_config_event(self):
        self._config_dirty = True
        self._schedule_fast_poll()

    def _resync(self):
        """Periodic safety net for a D-Bus signal we might have missed."""
        self._inhibit_dirty = True
        self._battery_dirty = True
        self._schedule_fast_poll()

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
                # menu, so make the next tick re-read the inhibit state.
                this._inhibit_dirty = True
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



    def _get_down_state(self):
        return 'PowerDown' if self.get_params().power_down else 'Suspend'

    def update_running_idle_s(self):
        """ Update the running idle seconds (called after each regular timeout) """
        cmd = self.variables['get_idle_ms']
        scale = 1
        if not cmd:
            cmd = self.variables.get('get_idle_s', '')
            scale = 1000
        if cmd:
            try:
                xidle = int(subprocess.check_output(cmd.split()).strip())
                xidle_ms = xidle * scale
                xidle_ms *= 2 if self.quick else 1  # time warp
                self.running_idle_s = round(xidle_ms/1000, 3)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                prt(f'WARN: idle time command failed: {e}')

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

    def _logind_blocked(self):
        """Ask logind which operations are blocked; None means "could not ask".

        busctl is used rather than a D-Bus binding: it ships with systemd,
        which this applet already requires, and its exit status is meaningful.
        The cost is one short-lived process per inhibitor change -- the caller
        is event-driven, not polled.
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

    def _inhibitor_rows(self, blocked):
        """Who is blocking what, for the menu.  Display only.

        ``--mode=block`` leaves delay inhibitors out on purpose: a session
        manager postponing suspend while it locks the screen is not preventing
        it.  Only rows blocking one of the operations behind the verdict are
        kept, so a compositor holding the power key does not read as "keep this
        session awake".  An unreadable table costs rows, never the verdict.
        """
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
        direction.  The table is still read when something *is* blocked,
        because the menu shows who and why; see _inhibitor_rows().
        """
        blocked = self._logind_blocked()
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

        play_state = ''
        if self.has_playerctl and self.enable_playerctl:
            # Event-driven: this query runs when logind or an MPRIS player
            # reported a change, not on every poll tick.
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

    def update_battery_status(self):
        if self.battery.present is False:
            return
        battery = psutil.sensors_battery()
        if battery is None:
            self.battery.present = False
            return
        was_plugged = self.battery.plugged
        was_selector = self.battery.selector

        self.battery.plugged = battery.power_plugged
        self.battery.percent = round(battery.percent, 1)
        if self.battery.plugged:
            self.battery.selector = 'Settings'
        elif self.battery.percent > self.get_params().lo_battery_pct:
            self.battery.selector = 'HiBattery'
        else:
            self.battery.selector = 'LoBattery'
        if was_plugged != self.battery.plugged or was_selector != self.battery.selector:
            self.rebuild_menu = True
#   IF WANTING TIME LEFT
#       secsleft = battery.secsleft
#       if secsleft == psutil.POWER_TIME_UNLIMITED:
#           time_left = "Calculating..."
#       elif secsleft == psutil.POWER_TIME_UNKNOWN:
#           time_left = "Unknown"
#       else:
#           hours, remainder = divmod(secsleft, 3600)
#           minutes, seconds = divmod(remainder, 60)
#           time_left = f"{hours:02}:{minutes:02}"

    def reset_xidle_ms(self):
        """ TBD"""
        self.run_command('reset_idle')

    def set_state(self, name):
        """ Set the state of the applet """
        prt(f'set_state: {name},{self.running_idle_s}s',
                f'was: {self.state.name},{self.state.when}s')
        self.state.name =  name
        self.state.when = self.running_idle_s

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
        if not self._events_bus_ok:
            self._inhibit_dirty = True
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
        if self._inhibit_dirty:
            self._inhibit_dirty = False
            rows, updated = self.check_inhibited()
            self._inhibit_rows = rows
            if updated:
                self.rebuild_menu = True
        if self.rebuild_menu:
            self.build_menu(self._inhibit_rows)
            self.rebuild_menu = False
            prt('re-built menu')

        if self.loop >= self.loop_sample:
            self.update_running_idle_s()
            # "Locking Soon" is an idle-time transition with no D-Bus signal.
            self.show_icon(inhibited=self.was_inhibited or '')
            lock_secs = self.get_lock_min_list()[0]*60
            down_secs = self.get_sleep_min_list()[0]*60 + lock_secs
            blank_secs = 5 if self.quick else 20

            emit = f'idle_s={self.running_idle_s} state={self.state.name},{self.state.when}s'
            emode = self.get_effective_mode()
            if emode in ('LockOnly', 'SleepAfterLock'):
                emit += f' @{self.get_lock_min_list()[0]}m'
            if emode in ('SleepAfterLock', ):
                emit += f'+{self.get_sleep_min_list()[0]}m'
            if self.battery.selector != 'Settings':
                emit += f' {self.battery.selector}'
            prt(emit)

            if emode in ('Presentation',) or self.was_inhibited:
                if self.idle_manager:
                    self.reset_xidle_ms() # idle_manager handles timeouts
                elif self.running_idle_s > min(50, lock_secs*0.40):
                    self.reset_xidle_ms() # we don't know when

            elif (self.running_idle_s >= down_secs and emode not in ('LockOnly',)
                    and self.state.name in ('Awake', 'Locked', 'Blanked')):
                if self._get_down_state() == 'PowerDown':
                    self.poweroff(None)
                else:
                    self.suspend(None)

            elif (self.running_idle_s >= lock_secs and emode not in ('Presentation',)
                    and self.state.name in ('Awake', 'Dim')):
                self.lock_screen(None)

            elif (self.running_idle_s >= self.state.when + blank_secs
                    and self.get_params().turn_off_monitors
                    and emode not in ('Presentation',)
                    and self.state.name in ('Locked',)):
                self.blank_primitive()

            elif self.running_idle_s < lock_secs and self.state.name not in ('Awake', ):
                self.set_state('Awake')

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
            # self.ini_tool.set_effective_params(selector)
            self.rebuild_menu = True
            self._inhibit_dirty = True   # the selector changes the icon

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
        if self.has_playerctl:
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

        selector, percent = self.battery.selector, self.battery.percent
        # With no battery present the whole power state is synthetic (this item
        # is then a test cycler), so say so rather than letting it look real.
        testing = getattr(self.battery, 'present', None) is False
        if selector == 'Settings':
            label = '↯ Plugged In'
        else:
            label = ('█' if selector == 'HiBattery' else '▃') + f' {selector}'
        if selector != 'Settings' or percent < 100:
            label += f' {percent}%'
        if testing:
            label += ' (TESTING)'
        add_item(label, self._toggle_battery)

        # if self.mode not in ('Presentation',) and len(self.opts.lock_min_list) > 1:
        add_item(f'  ♺ Lock: {self._lock_rotate_str()}',
                 lambda: self._lock_rotate_next())

        # if self.mode in ('SleepAfterLock',) and len(self.opts.sleep_min_list) > 1:
        add_item(f'  ♺ Sleep (after Lock): {self._sleep_rotate_str()}',
                 lambda: self._sleep_rotate_next())

        label = '♫ PlayerCtl: '
        label += ('not installed' if not self.has_playerctl
                   else 'Enabled' if self.enable_playerctl
                   else 'Disabled')
        add_item(label, self.toggle_playerctl)
        if self.get_params().gui_editor:
            add_item('✎  Edit Applet Config', self.edit_config)
        # Only offer the log viewer when something can actually show it: the
        # applet has no tty of its own, so less needs a terminal emulator.
        if Utils.terminal_argv([], getattr(self.get_params(), 'log_viewer', '')):
            add_item('≡  Open Applet Log', self.open_log)

        add_item('☓ Quit this Applet', self.quit_self)

        add_item('↺ Restart this Applet', self.restart_self)
        # Identity line, deliberately last and deliberately dead.  The log says
        # the same thing at START-UP, but "which build am I running?" is a
        # question asked from the menu, usually about a DE that behaves
        # differently from what the reader expects.  A disabled QAction cannot
        # be clicked, so it reads as a label rather than as an action that
        # silently does nothing.
        add_item(f'pwr-tray v{__version__}', self.dummy, enabled=False)

        if first_menu:
            self.tray_icon.setContextMenu(self.menu)

        if not self.tray_icon.isVisible():
            prt('self.tray_icon.isVisible() is False ... restarting app')
            time.sleep(0.5)
            self.restart_self(None)

    def on_tray_icon_activated(self, reason):
        left_click = (reason == QSystemTrayIcon.Trigger and not self.is_wayland)
        if reason == QSystemTrayIcon.Context or left_click:
            self.tray_icon.contextMenu().exec_(QCursor.pos())


    def idle_manager_start(self):
        """ For any mode, get the idle manager started"""
        if self.idle_manager:
            self.idle_manager.start()

    @staticmethod
    def dummy(_):
        """TBD"""

    @staticmethod
    def run_command(key):
        this = PwrTray.singleton
        command = this.variables.get(key, None)

        if command:
            prt(f'+ {command}')
            if  re.match(r'^(\s\w\-)*$', command):
                result = subprocess.run(command.split(), check=False)
            else:
                result = subprocess.run(command, check=False, shell=True)
            if result.returncode != 0:
                prt(f'   NOTE: returncode={result.returncode}')

    @staticmethod
    def quit_self(_):
        """TBD"""
        prt('+', 'quitting applet...')
        this = PwrTray.singleton
        if this:
            this.tray_icon.hide()
        sys.exit()

    @staticmethod
    def restart_self(_):
        """Replace this process with a fresh instance of the applet.

        The replacement is launched as ``python -m pwr_tray.main`` so that
        PyKill recognises it via command_names().  The single-instance lock is
        released first: os._exit() skips atexit handlers, and the replacement
        must be able to take the lock as soon as it starts.

        The package root goes on PYTHONPATH rather than into ``cwd=``: the
        replacement inherits whatever directory the user is in and must still
        be able to import the module when running from a source checkout.
        """
        prt('+', 'restarting applet...')
        this = PwrTray.singleton
        if this:
            this.tray_icon.hide()
            # os._exit() below skips atexit, which is where the monitors are
            # usually reaped; without this each restart orphans dbus-monitor.
            events = getattr(this, 'events', None)
            if events is not None:
                events.stop()
        PwrTray.save_picks()
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            [package_root] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
        # --replace is never inherited: a restart releases the lock first, so
        # the replacement has nothing to take over, and it must not keep a
        # destructive flag should the lock change hands in between.
        inherited = [arg for arg in sys.argv[1:] if arg != '--replace']
        args = [sys.executable, '-m', 'pwr_tray.main', *inherited]
        Utils.SingleInstance.release_current()
        subprocess.Popen(args, env=env)
        os._exit(0)

    @staticmethod
    def edit_config(_):
        this = PwrTray.singleton
        if this.get_params().gui_editor:
            try:
                ini_path = this.ini_tool.ini_path
                arguments = this.get_params().gui_editor.split()
                arguments.append(ini_path)
                prt('+', 'running:', arguments)
                subprocess.run(arguments, check=True)
            except (OSError, subprocess.SubprocessError) as e:
                prt(f"Edit Config ERR: {e}")

    @staticmethod
    def open_log(_):
        """Page debug.log (plus the rotated copy) in a terminal window.

        Launched with Popen rather than run(): the viewer stays open for as
        long as the user keeps it open, and blocking the Qt event loop that
        long would freeze the tray, the idle polling and every other action.
        spawn_detached() puts the viewer in its own session -- and, under
        systemd, its own transient scope -- so a stop or restart of the applet
        does not take the log window down with it.
        """
        this = PwrTray.singleton
        try:
            log_path = this.ini_tool.log_path
            old_path = getattr(this.ini_tool, 'log_old_path', None)
            preferred = getattr(this.get_params(), 'log_viewer', '')
        except AttributeError as e:
            prt(f'Open Log ERR: {e}')
            return

        arguments = Utils.log_viewer_argv(log_path, old_path, preferred)
        if not arguments:
            prt('Open Log ERR: no terminal emulator found'
                ' (set log_viewer in config.ini)')
            return

        prt('+', 'running:', arguments)
        try:
            Utils.spawn_detached(arguments)
        except (OSError, subprocess.SubprocessError) as e:
            prt(f'Open Log ERR: {e}')

    @staticmethod
    def suspend(_):
        this = PwrTray.singleton
        this.set_state('Asleep')
        this.reset_xidle_ms()

        locker_cmd = this.variables.get('locker')

        # Logic: Only manually lock if we aren't in a 'Full' DE that handles it.
        # Most tiling WMs (niri, sway, i3) need the manual kick.
        is_full_de = any(name in this.de_config['name'] for name in ['kde', 'xfce', 'cinnamon'])

        if locker_cmd and not is_full_de:
            prt(f'+ {locker_cmd} (async spawn)')
            subprocess.Popen(locker_cmd, shell=True)
            # Give the locker a tiny head-start to grab the screen
            time.sleep(0.2)

        # This is the "Point of No Return"
        PwrTray.run_command('suspend')
        prt('suspend: resumed')

    @staticmethod
    def poweroff(_):
        """TBD"""
        PwrTray.run_command('poweroff')

    @staticmethod
    def reboot(_):
        """TBD"""
        PwrTray.run_command('reboot')

    @staticmethod
    def lock_screen(_):
        this = PwrTray.singleton
        PwrTray.run_command('locker')
        this.update_running_idle_s()
        this.set_state('Locked')

    def blank_primitive(self, lock_screen=False):
        """TBD"""
        cmd = self.variables['monitors_off']
        if cmd and self.get_params().turn_off_monitors:
            if lock_screen:
                # self.lock_screen(None, before='sleep 1.5; ')
                self.lock_screen(None)
            prt('+', cmd)
            result = subprocess.run(cmd, shell=True, check=False)
            if result.returncode != 0:
                prt(f'   NOTE: returncode={result.returncode}')
            self.set_state('Blanked')

        else:
            prt('NOTE: blanking screen unsupported')

    @staticmethod
    def blank_quick(_):
        this = PwrTray.singleton
        this.blank_primitive(lock_screen=True)

    @staticmethod
    def reload_wm(_):
        PwrTray.run_command('reload_wm')

    @staticmethod
    def restart_wm(_):
        PwrTray.run_command('restart_wm')

    @staticmethod
    def exit_wm(_):
        PwrTray.run_command('logoff')

    @staticmethod
    def enable_presentation_mode(_):
        """TBD"""
        this = PwrTray.singleton
        this.mode = 'Presentation'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

    @staticmethod
    def enable_nosleep_mode(_):
        """TBD"""
        this = PwrTray.singleton
        this.mode = 'LockOnly'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

    @staticmethod
    def enable_normal_mode(_):
        """TBD"""
        this = PwrTray.singleton
        this.mode = 'SleepAfterLock'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

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
        # The default D-Bus timeout is 25s, which would defeat the caller's
        # deadline if the bus is wedged.
        interface.setTimeout(2000)
        reply = interface.isServiceRegistered(STATUS_NOTIFIER_WATCHER)
        return bool(reply.isValid() and reply.value())
    # QDBusError has no import-safe base class, so this one has to be broad.
    except Exception as exc:                    # noqa: BLE001
        prt(f'WARN: tray detection failed: {exc}')
        return None


def wait_for_tray(timeout=TRAY_WAIT_SECONDS):
    """Wait up to `timeout` seconds for the tray to register itself.

    Returns True when the watcher appears, False when it does not (or when the
    bus cannot be queried at all).  The wait is bounded and logs progress, so a
    session without a watcher -- a bare X11 bar, for instance -- costs at most
    `timeout` seconds and is visible in the log; the caller decides whether to
    carry on, and normally it does.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    next_report = time.monotonic() + TRAY_REPORT_SECONDS
    while True:
        present = tray_watcher_present()
        if present is None:            # D-Bus unusable: retrying cannot help
            return False
        if present:
            return True
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            return False
        if now >= next_report:
            prt(f'  ... still no {STATUS_NOTIFIER_WATCHER}'
                f' ({remaining:.0f}s left)')
            next_report = now + TRAY_REPORT_SECONDS
        time.sleep(min(TRAY_POLL_SECONDS, remaining))


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
    parser.add_argument('--replace', action='store_true',
            help='if another pwr-tray holds the single-instance lock, terminate'
                 ' it and take over (default: refuse to start)')
    parser.add_argument('-y', '--yes', action='store_true',
            help='never prompt; assume yes for --svc-* questions')
    parser.add_argument('--force', action='store_true',
            help='replace or delete a unit file pwr-tray did not write'
                 ' (implies --yes)')
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
    opts = parser.parse_args()

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
    # Paired with PwrTray.goodbye(): one line at each end of a run, both
    # carrying the version and the pid, so a debug.log copied out of the wild
    # answers "which build, which process?" without guesswork.
    prt(f'START-UP pwr-tray v{__version__} pid {os.getpid()}',
        to_stdout=opts.stdout)

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


    # Wait for the tray to be ready so we don't need 'sleep' in the config.
    # The result is used, not discarded: no watcher is a warning, not an error,
    # because an X11 bar without one still runs the applet.
    prt("Waiting for tray...")
    if wait_for_tray():
        prt("                ... tray detected.")
    else:
        prt("                ... no tray watcher found; starting anyway.")
    try:
        tray = PwrTray(ini_tool=ini_tool, quick=opts.quick, force_de=opts.de)
    except PwrTrayError as exc:
        prt(f'ERROR: {exc}')      # keep a copy in debug.log
        Utils.error_print(str(exc))  # ...and put it where the user can see it
        sys.exit(2)
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
