#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This code is designed to control power matters from the tray.
"""
# pylint: disable=invalid-name,wrong-import-position,missing-function-docstring
# pylint: disable=broad-except,too-many-instance-attributes
# pylint: disable=global-statement,consider-using-with,too-many-lines
# pylint: disable=too-many-statements,too-few-public-methods
# pylint: disable=too-many-branches,too-many-public-methods
# pylint: disable=consider-using-from-import,too-many-locals

import os
import sys
needed = '/usr/lib/python3/dist-packages'
if needed not in sys.path:
    sys.path.append(needed) # pick up external dependencies
import signal
import re
import subprocess
import json
import shutil
import atexit
import time
import traceback
from types import SimpleNamespace
import psutil
# import pydbus
from pydbus import SessionBus
from ruamel.yaml import YAML
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction #, QMessageBox
from PyQt5.QtGui import QIcon, QCursor
from PyQt5.QtCore import QTimer

import pwr_tray.Utils as Utils
from pwr_tray.Utils import prt, PyKill
from pwr_tray.Errors import PwrTrayError, DesktopDetectionError
from pwr_tray.Errors import MissingCommandsError
from pwr_tray.SwayIdleMgr import SwayIdleManager
from pwr_tray.IniTool import IniTool

SESSION_BUS = SessionBus()


class PwrTray:
    """ pwr-tray main class.
    NOTES:
     - when icons are moved/edited, rename them or reboot to avoid cache confusion
    """
    svg_info = SimpleNamespace(version='03', subdir='resources/SetD'
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
        with open(user_yaml, 'r', encoding='utf-8') as f:
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
    def detect_de(de_json, force_de=None):
        """Detect the current DE from environment using JSON config rules.
        Merges three layers: defaults -> session_type (x11/wayland) -> desktop.
        Returns merged config dict.  Use force_de to skip detection.

        Raises DesktopDetectionError when nothing matches: an assert would be
        stripped by python -O, leaving the app running with empty commands.
        """
        env = {key: os.environ.get(key, '')
               for key in ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                           'DESKTOP_SESSION', 'XDG_SESSION_TYPE')}
        lowered = {key: value.lower() for key, value in env.items()}
        session_type = lowered['XDG_SESSION_TYPE']
        desktop_str = ' '.join(lowered[key] for key in
                               ('XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP',
                                'DESKTOP_SESSION'))

        prt(f'ENV: XDG_CURRENT_DESKTOP={env["XDG_CURRENT_DESKTOP"]!r}'
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
            prt(f'ENV: forced {force_de!r}')
            return _merge(force_de, desktops[force_de])

        # Desktop keys are compound: "kde-wayland", "i3-x11", etc.
        for compound_name, entry in desktops.items():
            detect, _, stype = compound_name.rpartition('-')
            if detect in desktop_str and stype == session_type:
                prt(f'ENV: matched {compound_name!r}')
                return _merge(compound_name, entry)

        raise DesktopDetectionError(
            'no desktop environment matched the current session',
            known_desktops=known, env=env)

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
        self.here_dir = os.path.dirname(os.path.abspath(__file__))
        self.svgs = []
        self.icons = []
        for base in self.svg_info.bases:
            self.svgs.append(f'{base}-v{self.svg_info.version}.svg')
        for resource in self.svgs + ['lockpaper.png']:
            if not os.path.isfile(resource):
                Utils.copy_to_folder(resource, ini_tool.folder)
            if not os.path.isfile(resource):
                prt(f'WARN: cannot find {repr(resource)}')
                continue
            self.icons.append(QIcon(os.path.join(self.ini_tool.folder, resource)))

            # states are Awake, Locked, Blanked, Asleep
            # when is idle time
        self.tray_icon = QSystemTrayIcon(self.icons[0], self.app)
        self.tray_icon.setToolTip("pwr-tray")
        self.tray_icon.setVisible(True)
        self.state = SimpleNamespace(name='Awake', when=0)

        self.running_idle_s = 0.000
        self.poll_s = 2.000
        self.poll_100ms = False
        self.lock_began_secs = None   # TBD: remove
        self.inh_lock_began_secs = False # TBD: refactor?
        self.rebuild_menu = False
        self.picks_file = ini_tool.picks_path
        self.current_icon_num = -1  # triggers immediate icon update
        self.enable_playerctl = True

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
        self._resume_proc = self._start_resume_monitor()
        self._resume_buf = b''

        self.timer = QTimer()
        self.timer.setInterval(100) # 100 is initial ... gets recomputed
        self.timer.timeout.connect(self.on_timeout)
        self.timer.start()

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
                this.poll_100ms = True
            except Exception as e:
                print(f"An error occurred while saving picks: {e}", file=sys.stderr)


    def restore_picks(self):
        """ Apply the state saved by save_picks().

        A missing, partial or hand-edited picks file is tolerated: every entry
        is restored independently, anything unusable is logged and skipped, and
        that selector keeps its configured values. Returns True only when the
        whole saved state was applied.
        """
        try:
            with open(self.picks_file, 'r', encoding='utf-8') as handle:
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
            for selector in 'LoBattery HiBattery Settings'.split():
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
            except Exception as e:
                prt(f'WARN: idle time command failed: {e}')

    def DB(self):
        """ is debug on? """
        rv = self.get_params().debug_mode
        return rv

    def effective_mode(self):
        """ TBD """
        return 'SleepAfterLock' if self.battery.selector == 'LoBattery' else self.mode

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

    def check_inhibited(self):
        # Using run() is cleaner and safer than Popen/communicate manually
        result = subprocess.run(
            ['systemd-inhibit', '--no-pager', '--mode=block'],
            capture_output=True, text=True, check=False
        )
        output = result.stdout
        lines = output.splitlines()

        if self.DB() and 'No inhibitors' not in output:
            prt('DB', 'systemd-inhibit:', output.strip())
        rows = []
        inhibited = ''
        for count, line in enumerate(lines):
            if count == 0:
                rows.append(line.strip())
                continue
            if 'block' not in line:
                continue
            if count == 1:
                if ('xfce4-power-man' not in line
                        and 'org_kde_powerde' not in line
                        and 'handle-power-key' not in line): # Ignore Niri's button hijacking
                    inhibited = 'systemd'
                    rows.append(line)
            else:
                inhibited = 'systemd'
                rows.append(line)
        if len(rows) == 1:
            rows = []
        play_state = ''
        if self.has_playerctl and self.enable_playerctl:
            # subprocess.run is already safe, but ensure we aren't leaking
            # elsewhere if this is called 10 times per second.
            child = subprocess.run(
                ['playerctl', 'status'],
                capture_output=True, text=True, check=False
            )
            play_state = child.stdout.strip().lower()

        # Media playback inhibits locking/sleeping, but an existing systemd
        # inhibitor keeps priority (it is already visible as the StopSign).
        if play_state == 'playing' and not inhibited:
            inhibited = 'player'

        if self.was_play_state != play_state:
            prt(f'playerctl play_state={play_state!r}')
            self.was_play_state = play_state
            self.poll_100ms = True

        emode = self.effective_mode()

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
        was_output = self.was_output
        self.was_output = output
        return rows, bool(was_output != output)

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
        """TBD"""
        self.loop += 1
        if self.DB():
            prt('DB', f'on_timeout() {self.loop=}/{self.loop_sample} ...')
#       if not QSystemTrayIcon.isSystemTrayAvailable():
#           prt('SystemTray is gone ... restarting')
#           self.restart_self(None)

        self._check_resume()
        self.reconfig()
        if self.idle_manager:
            self.idle_manager.checkup()
        self.update_battery_status()

        rows, updated = self.check_inhibited()
        if updated or self.rebuild_menu:
            self.build_menu(rows)
            self.rebuild_menu = False
            prt('re-built menu')

        if self.loop >= self.loop_sample:
            self.update_running_idle_s()
            lock_secs = self.get_lock_min_list()[0]*60
            down_secs = self.get_sleep_min_list()[0]*60 + lock_secs
            blank_secs = 5 if self.quick else 20
#           if 0 <= int(self.get_params().dim_pct_brightness) < 100:
#               dim_secs = int(round(lock_secs * int(self.get_params().dim_pct_lock_min) / 100, 0))
#           else:
#               dim_secs = 2000 + lock_secs * 2  # make it never happen

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

#           elif (self.running_idle_s >= dim_secs and emode not in ('Presentation',)
#                   and self.state.name in ('Awake',)):
#               self.dimmer(None)

            elif (self.running_idle_s >= self.state.when + blank_secs
                    and self.get_params().turn_off_monitors
                    and emode not in ('Presentation',)
                    and self.state.name in ('Locked',)):
                self.blank_primitive()

            elif self.inh_lock_began_secs:
                self.inh_lock_began_secs = False

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

        def add_item(text, callback):
            nonlocal self
            item = QAction(text)
            item.triggered.connect(callback)
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

        if self.mode not in ('Presentation',):
            add_item(f'🅟 Presentation ⮜ {self.mode} Mode', self.enable_presentation_mode)

        if self.mode not in ('LockOnly',):
            add_item(f'🅛 LockOnly ⮜ {self.mode} Mode', self.enable_nosleep_mode)

        if self.mode not in ('SleepAfterLock',):
            add_item(f'🅢 SleepAfterLock ⮜ {self.mode} Mode', self.enable_normal_mode)

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
        add_item('🗲 Plugged In' if selector == 'Settings'
                     else (('█' if selector == 'HiBattery' else '▃') + f' {selector}')
                + (f' {percent}%' if percent < 100 or selector != 'Settings' else '')
                , self._toggle_battery)

        # if self.mode not in ('Presentation',) and len(self.opts.lock_min_list) > 1:
        add_item(f'  ♺ Lock: {self._lock_rotate_str()}',
                 lambda: self._lock_rotate_next())

        # if self.mode in ('SleepAfterLock',) and len(self.opts.sleep_min_list) > 1:
        add_item(f'  ♺ Sleep (after Lock): {self._sleep_rotate_str()}',
                 lambda: self._sleep_rotate_next())

        label = '🎝 PlayerCtl: '
        label += ('not installed' if not self.has_playerctl
                   else 'Enabled' if self.enable_playerctl
                   else 'Disabled')
        add_item(label, self.toggle_playerctl)
        if self.get_params().gui_editor:
            add_item('🖹  Edit Applet Config', self.edit_config)

        add_item('☓ Quit this Applet', self.quit_self)

        add_item('↺ Restart this Applet', self.restart_self)

        if first_menu:
            self.tray_icon.setContextMenu(self.menu)

        if not self.tray_icon.isVisible():
            prt('self.tray_icon.isVisible() is False ... restarting app')
            time.sleep(0.5)
            self.restart_self(None)

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Context:  # Right click
            self.tray_icon.contextMenu().exec_(QCursor.pos())  # Show the context menu
        elif (reason == QSystemTrayIcon.Trigger  # Left click
                 and not self.is_wayland):  # wayland behaves badly
            self.tray_icon.contextMenu().exec_(QCursor.pos())  # Show the context menu


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
        """
        prt('+', 'restarting applet...')
        this = PwrTray.singleton
        if this:
            this.tray_icon.hide()
        PwrTray.save_picks()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args = [sys.executable, '-m', 'pwr_tray.main'] + sys.argv[1:]
        Utils.SingleInstance.release_current()
        subprocess.Popen(args, cwd=project_root)
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
            except Exception as e:
                prt(f"Edit Config ERR: {e}")

    @staticmethod
    def old_suspend(_):
        """TBD"""
        this = PwrTray.singleton
        this.set_state('Asleep')
        this.reset_xidle_ms()
        PwrTray.run_command('locker')
        PwrTray.run_command('suspend')
        # systemctl suspend blocks until resume
        prt('suspend: resumed')

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
    def dimmer(_):
        """TBD"""
        PwrTray.run_command('dimmer')

    @staticmethod
    def undim(_):
        """TBD"""
        PwrTray.run_command('undim')

    @staticmethod
    def lock_screen(_):
        this = PwrTray.singleton
        PwrTray.run_command('locker')
        this.update_running_idle_s()
#       if 0 <= int(thisget_params()params.dim_pct_brightness) < 100:
#           this.undim(None)
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

    def _start_resume_monitor(self):
        """Start dbus-monitor subprocess for resume detection (non-blocking)."""
        try:
            proc = subprocess.Popen(
                ['dbus-monitor', '--system',
                 "type='signal',interface='org.freedesktop.login1.Manager',"
                 "member='PrepareForSleep'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(proc.stdout.fileno(), False)
            prt('resume monitor: started')
            return proc
        except FileNotFoundError:
            prt('WARN: dbus-monitor not found; resume detection disabled')
            return None

    def _check_resume(self):
        """Poll dbus-monitor for resume signal (called from on_timeout)."""
        if not self._resume_proc:
            return
        try:
            chunk = os.read(self._resume_proc.stdout.fileno(), 4096)
            if chunk:
                self._resume_buf += chunk
                if b'boolean false' in self._resume_buf:
                    prt('resume detected')
                    self._resume_buf = b''
                    self.poll_100ms = True  # trigger immediate re-poll
                # Prevent unbounded growth; keep tail for partial matches
                elif len(self._resume_buf) > 1024:
                    self._resume_buf = self._resume_buf[-512:]
        except BlockingIOError:
            pass
        except OSError:
            pass
    @staticmethod
    def goodbye(message=''):
        prt(f'ENDED {message}')

def wait_for_tray(timeout=300):
    """Wait for the StatusNotifierWatcher (the tray) to appear on DBus."""
    for i in range(timeout):
        try:
            # Check if the name exists specifically rather than listing all
            if SESSION_BUS.dbus.NameHasOwner("org.kde.StatusNotifierWatcher"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def load_known_de_json():
    """Return (de_json, source) for --list-des.

    A user override in my-commands.yaml wins over the packaged defaults, so the
    listing matches what the running application would actually use.
    """
    ini_tool = IniTool(paths_only=True)
    user_yaml = os.path.join(ini_tool.folder, 'my-commands.yaml')
    if os.path.exists(user_yaml):
        with open(user_yaml, 'r', encoding='utf-8') as handle:
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


def main():
    # pylint: disable=import-outside-toplevel
    import argparse
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # os.chdir(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser()
    parser.add_argument('-D', '--debug', action='store_true',
            help='override debug_mode from .ini initially')
    parser.add_argument('-o', '--stdout', action='store_true',
            help='log to stdout (if a tty)')
    parser.add_argument('-f', '--follow-log', action='store_true',
            help='exec tail -n50 -F on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('-q', '--quick', action='store_true',
            help='quick mode (1m lock + 1m sleep')
    parser.add_argument('--de', metavar='NAME',
            help='force desktop (e.g. i3-x11, sway-wayland, kde-wayland)')
    parser.add_argument('--list-des', action='store_true',
            help='list known desktops and the commands each requires, then exit')
    opts = parser.parse_args()

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
    prt('START-UP', to_stdout=opts.stdout)
    PyKill().kill_loop('pwr-tray')

    # The killer only recognises command lines it knows about; the lock is the
    # real guarantee that two applets (and two swayidle managers) never coexist.
    lock = Utils.SingleInstance(os.path.join(ini_tool.folder, 'pwr-tray.lock'))
    if not lock.acquire(retries=5, delay=0.2):
        holder = Utils.SingleInstance.read_pid(lock.path)
        message = 'another pwr-tray is already running'
        if holder:
            message += f' (pid {holder})'
        prt(f'ERROR: {message}')
        Utils.error_print(f'ERROR: {message}')
        sys.exit(3)
    atexit.register(lock.release)
    atexit.register(PwrTray.goodbye)


    ini_tool.update_config()
    if opts.debug:
        for selector in ini_tool.get_selectors():
            ini_tool.params_by_selector[selector].debug_mode = True # one-time override


    # Wait for the tray to be ready so we don't need 'sleep' in the config
    prt("Waiting for tray...")
    wait_for_tray()
    prt("                ... tray detected.")
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

    except Exception as exc:
        prt("Caught exception running main(), so exiting ...\n",
            traceback.format_exc(limit=24))
        sys.exit(9)
