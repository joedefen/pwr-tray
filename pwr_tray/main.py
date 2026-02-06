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
# pylint: disable=consider-using-from-import

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
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction #, QMessageBox
from PyQt5.QtGui import QIcon, QCursor
from PyQt5.QtCore import QTimer

import pwr_tray.Utils as Utils
from pwr_tray.Utils import prt, PyKill
from pwr_tray.SwayIdleMgr import SwayIdleManager
from pwr_tray.IniTool import IniTool

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
    def get_environment():
        desktop_session = os.environ.get('DESKTOP_SESSION', '').lower()
        xdg_session_desktop = os.environ.get('XDG_SESSION_DESKTOP', '').lower()
        xdg_current_desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        sway_socket = os.environ.get('SWAYSOCK')
        wayland_display = os.environ.get('WAYLAND_DISPLAY')
        is_wayland = bool(wayland_display)
        display = os.environ.get('DISPLAY')

        if 'i3' in desktop_session and display: # Check for i3
            prt(f'ENV: i3 {desktop_session=} {display=}')
            return 'i3', is_wayland
        if ('sway' in desktop_session or 'sway' in xdg_session_desktop
                or 'sway' in xdg_current_desktop) and sway_socket: # Check for Sway
            prt(f'ENV: sway {desktop_session=} {sway_socket=}')
            return 'sway', is_wayland
        if 'plasma' in desktop_session or 'kde' in xdg_current_desktop:
            if is_wayland:
                env = 'kde-wayland'
                prt(f'ENV: {env} {desktop_session=} {wayland_display=}')
                return 'kde-wayland', is_wayland
            if display:
                env = 'kde-x11'
                prt(f'ENV: {env} {desktop_session=} {display=}')
                return env, is_wayland
        if 'gnome' in desktop_session:
            if is_wayland:
                env='gnome-wayland'
                prt(f'ENV: {env} {desktop_session=} {wayland_display=}')
                assert False, f'unsupported env: {env}'
                return env, is_wayland
            if display:
                env='gnome-x11'
                prt(f'ENV: {env} {desktop_session=} {display=}')
                assert False, f'unsupported env: {env}'
                return 'gnome-x11', is_wayland
        # Default case: no known environment detected
        assert False, 'cannot determine if i3/sway/(kde|gnome)-(x11|wayland)'

    default_variables = {
        'suspend': 'systemctl suspend',
        'poweroff': 'systemctl poweroff',
        'reboot': 'systemctl reboot',
#       'dimmer': 'brightnessctl set {percent}%',
#       'undim': 'brightnessctl set 100%',
        'logoff': '',
        'monitors_off': '',
        'locker': '',
        'get_idle_ms': '',
        'get_idle_s': '',
        'reset_idle': '',
        'reload_wm': '',
        'restart_wm': '',
#       'must_haves': 'systemctl brightnessctl'.split(),
        'must_haves': 'systemctl'.split(),

    }
    overrides = {
        'x11': {
            'reset_idle': 'xset s reset',
            'get_idle_ms': 'xprintidle',
            'monitors_off': 'sleep 1.0; exec xset dpms force off',
            'must_haves': 'xset xprintidle'.split(),
        }, 'sway': {
            # swayidle timeout 300 'swaylock' resume 'swaymsg "exec kill -USR1 $(pgrep swayidle)"' &
            # kill -USR1 $(pgrep swayidle)
            'reload_wm': 'swaymsg reload',
            'logoff': 'swaymsg exit',
            'locker': 'swaylock --ignore-empty-password --show-failed-attempt',
            # 'monitors_off': """sleep 1.0; swaymsg 'output * dpms off'""",
            'must_haves': 'swaymsg i3lock'.split(),

        }, 'i3': {
            'reload_wm': 'i3-msg reload',
            'restart_wm': 'i3-msg restart',
            'logoff': 'i3-msg exit',
            'locker': 'pkill i3lock; sleep 0.5; i3lock --ignore-empty-password --show-failed-attempt',
            'must_haves': 'i3-msg i3lock'.split(),

        }, 'kde-x11': {
            'locker': 'loginctl lock-session',
            'logoff': 'qdbus org.kde.Shutdown /Shutdown org.kde.Shutdown.logout',
            'restart_wm': 'killall plasmashell && kstart5 plasmashell && sleep 3 && pwr-tray',
            'must_haves': 'loginctl qdbus'.split(),
        }, 'kde-wayland': {
            'locker': 'loginctl lock-session',
            'logoff': 'qdbus org.kde.Shutdown /Shutdown org.kde.Shutdown.logout',
            'must_haves': 'loginctl swayidle'.split(),
        }, 'gnome-x11': {

        }, 'gnome-wayland': {
            # sudo apt-get install xdg-utils
            'reset_idle': ('gdbus call --session --dest org.gnome.ScreenSaver --object-path'
                ' /org/gnome/ScreenSaver --method org.gnome.ScreenSaver.SimulateUserActivity'),
            # - idle time:
            # pip install pydbus
            # from pydbus import SessionBus
            #   bus = SessionBus()
            #   screensaver = bus.get("org.gnome.Mutter.IdleMonitor",
            #        "/org/gnome/Mutter/IdleMonitor/Core")
            #   idle_time = screensaver.GetIdletime()

            #   print(f"Idle time in milliseconds: {idle_time}")
            'locker': 'loginctl lock-session',
            'must_haves': 'qdbus gnome-screensaver-command'.split(),

        }
    }

    def __init__(self, ini_tool, quick=False):
        PwrTray.singleton = self
        self.app = QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        while not QSystemTrayIcon.isSystemTrayAvailable():
            prt("System tray is not available. Retry in 1 second...")
            time.sleep(1.0)
        prt("System tray is available. Continuing...")

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

        self.restore_picks()
        if quick:
            for selector in self.ini_tool.get_selectors():
                params = self.ini_tool.params_by_selector[selector]
                params.lock_min_list = [1, 2, 4, 8, 32, 128]
                params.sleep_min_list = [1, 2, 4, 8, 32, 128]

        # self.down_state = self.opts.down_state

        self.graphical, self.is_wayland = self.get_environment()
        self.variables = self.default_variables
        must_haves = self.default_variables['must_haves']
        if self.graphical in ('i3', 'kde-x11'):
            self.variables.update(self.overrides['x11'])
            must_haves += self.default_variables['must_haves']

        self.variables.update(self.overrides[self.graphical])
        must_haves += self.variables['must_haves']
        
        if self.graphical == 'kde-wayland':
            if self.get_params().kde_wayland_hard_logout:
            #   self.variables['logoff'] = ("loginctl terminate-session "
            #       + os.environ.get('XDG_SESSION_ID'))
                self.variables['logoff'] = 'pkill kwin'
            if self.DB():
                prt(str(self.variables))


        dont_haves = []
        for must_have in set(must_haves):
            if shutil.which(must_have) is None:
                dont_haves.append(must_have)
        assert not dont_haves, f'commands NOT on $PATH: {dont_haves}'

        if self.graphical in ('kde-x11', 'kde-wayland'):
            qdbus_cmd = shutil.which('qdbus') or shutil.which('qdbus6')
            assert qdbus_cmd, 'neither qdbus nor qdbus6 found on $PATH'
            qdbus_name = os.path.basename(qdbus_cmd)
            if qdbus_name != 'qdbus':
                for key, val in list(self.variables.items()):
                    if isinstance(val, str) and 'qdbus ' in val:
                        self.variables[key] = val.replace('qdbus ', f'{qdbus_name} ')

        self.has_playerctl = bool(shutil.which('playerctl'))


        self.idle_manager = (SwayIdleManager(self)
            if self.graphical in ('sway', 'kde-wayland') else None)

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
        try:
            with open(self.picks_file, 'r', encoding='utf-8') as handle:
                picks = json.load(handle)
            self.mode = picks.get('mode', 'SleepAfterLock')
            self.enable_playerctl = picks.get('enable_playerctl', True)
            for attr in 'lock_mins sleep_mins'.split():
                for selector in 'LoBattery HiBattery Settings'.split():
                    self.get_lock_rotated_list(selector, first=picks[attr][selector])
            prt('restored Picks OK:', picks)
            return True

        except Exception as e:
            prt(f'restored picks FAILED: {e}')
            prt(f'mode=self.mode lock_mins={self.get_lock_min_list()}'
                f' sleep_mins={self.get_sleep_min_list()}')
            return True



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
        pipe = subprocess.Popen(
                # ['systemd-inhibit', '--no-legend', '--no-pager', '--mode=block'],
                ['systemd-inhibit', '--no-pager', '--mode=block'],
                stdout=subprocess.PIPE)
        output, _ = pipe.communicate()
        output = output.decode('utf-8')
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
                        and 'org_kde_powerde' not in line):
                    inhibited = 'systemd'
                    rows.append(line)
            else:
                inhibited = 'systemd'
                rows.append(line)
        if len(rows) == 1:
            rows = []
        if self.has_playerctl and self.enable_playerctl:
            child = subprocess.run('playerctl status'.split(), check=False,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            play_state = child.stdout.decode('utf-8').strip().lower()
            if play_state == 'playing':
                inhibited = 'player'
            if self.was_play_state != play_state:
                prt(f'{play_state=}')
                self.was_play_state = play_state

        emode = self.effective_mode()

        if self.show_icon(inhibited=inhibited):
            self.poll_100ms = True
            self.idle_manager_start()

        self.was_effective_mode = emode
        self.was_selector = self.battery.selector
        was_output = self.was_output
        self.was_output = output
        self.was_inhibited = inhibited
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
        if not QSystemTrayIcon.isSystemTrayAvailable():
            prt('SystemTray is gone ... restarting')
            self.restart_self(None)

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
                if self._get_down_state() == 'PowerOff':
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
        if command and key == 'locker' and this.graphical in ('i3', 'sway'):
            if this.graphical == 'i3':
                append = this.get_params().i3lock_args
            elif this.graphical == 'sway':
                append = this.get_params().swaylock_args
            if not append:
                append = '-t -i ./lockpaper.png'
            command += ' ' + append

#       elif key == 'dimmer':
#           percent = int(round(int(thisget_params()params.dim_pct_brightness), 0))
#           command = command.replace('{percent}', str(percent))

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
        """TBD"""
        prt('+', 'restarting applet...')
        this = PwrTray.singleton
        if this:
            this.tray_icon.hide()
        PwrTray.save_picks()
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args = [sys.executable, '-m', 'pwr_tray.main'] + sys.argv[1:]
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
    def suspend(_):
        """TBD"""
        this = PwrTray.singleton
        this.set_state('Asleep')
        this.reset_xidle_ms()
        if this.graphical in ('i3', ):
            PwrTray.run_command('locker')
        PwrTray.run_command('suspend')
        # systemctl suspend blocks until resume; restart to restore tray icon
        prt('suspend: resumed, restarting applet...')
        PwrTray.restart_self(None)

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
                    prt('resume detected, restarting...')
                    self.restart_self(None)
                # Prevent unbounded growth; keep tail for partial matches
                if len(self._resume_buf) > 1024:
                    self._resume_buf = self._resume_buf[-512:]
        except BlockingIOError:
            pass
        except OSError:
            pass
    @staticmethod
    def goodbye(message=''):
        prt(f'ENDED {message}')


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
    opts = parser.parse_args()

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
    prt('START-UP', to_stdout=opts.stdout)
    PyKill().kill_loop('pwr-tray')
    atexit.register(PwrTray.goodbye)


    ini_tool.update_config()
    if opts.debug:
        for selector in ini_tool.get_selectors():
            ini_tool.params_by_selector[selector].debug_mode = True # one-time override


    tray = PwrTray(ini_tool=ini_tool, quick=opts.quick)
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
