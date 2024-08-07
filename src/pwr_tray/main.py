#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This code is designed to control power matters from the tray.

This code is an derived (mostly) from a tutorial on Ubuntu Unity/Gnome AppIndicators:
- http://candidtim.github.io/appindicator/2014/09/13/ubuntu-appindicator-step-by-step.html

Prerequisites - Debian 12 / i3wm:
#   sudo apt install gir1.2-appindicator3-0.1
#   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 libappindicator3-1
    sudo apt install gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 ayatanalibappindicator3-1

Prerequisites - Arch / i3wm: # ??
    sudo pacman -S libappindicator-gtk3
    sudo pacman -S python-gobject
    sudo pacman -S python-cairo

Also depends on:
  - For KDE, cripple "powerdevil" so it does not try to do much.
  - systemd (i.e., systemctl, loginctl, xset, systemd-inhibit)
  - qdbus: in particular, this must work:
    - qdbus org.freedesktop.ScreenSaver /ScreenSaver GetSessionIdleTime

TODO:
 - and battery detection
 - add dimming controls (only if on battery)
 - add alternative lock/sleep (only if on battery)
 - add shutdown if low battery
 
qdbus org.kde.KWin /Scripting org.kde.kwin.Scripting.runScript "$(cat idle_time.js)"
var idleTime = workspace.readProperty("org.kde.kwin.idleTime", "idleTime");
print("Idle time: " + idleTime + " milliseconds");


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
import traceback
from types import SimpleNamespace
import psutil

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk as gtk
gi.require_version('Notify', '0.7')

try:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3 as appindicator
    ERRORMSG = None
except Exception:
    try:
        gi.require_version('AyatanaAppIndicator3', '0.1')
        from gi.repository import AyatanaAppIndicator3 as appindicator
        ERRORMSG = None
    except (ValueError, ImportError):
        ERRORMSG = 'Please install libappindicator3'

    # from gi.repository import AppIndicator3 as appindicator
from gi.repository import Notify as notify
from gi.repository import GLib as glib
import pwr_tray.Utils as Utils
from pwr_tray.Utils import prt, PyKill
from pwr_tray.SwayIdleMgr import SwayIdleManager
from pwr_tray.IniTool import IniTool


# from gi.repository import GObject as gobject

APPINDICATOR_ID = 'PowerInhibitIndicator'

class InhIndicator:
    """ pwr-tray main class.
    NOTES:
     - when icons are moved/edited, rename them or reboot to avoid cache confusion
    """
    svg_info = SimpleNamespace(version='03', subdir='resources/SetD'
                , bases= ['SettingSun',   # Normal (SleepAfterLock)
                          'FullSun',      # Presentation Mode
                          'Unlocked',     # LockOnly Mode
                          'GoingDown',    # LowBattery Mode
                          'PlayingNow',   # Inhibited
                          'RisingMoon',   # Normal and Locking Soon
                          'UnlockedMoon', # LockOnly and Locking Soon
                          ] )
    singleton = None
    @staticmethod
    def get_environment():
        desktop_session = os.environ.get('DESKTOP_SESSION', '').lower()
        xdg_session_desktop = os.environ.get('XDG_SESSION_DESKTOP', '').lower()
        xdg_current_desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        sway_socket = os.environ.get('SWAYSOCK')
        wayland_display = os.environ.get('WAYLAND_DISPLAY')
        display = os.environ.get('DISPLAY')

        if 'i3' in desktop_session and display: # Check for i3
            prt(f'ENV: i3 {desktop_session=} {display=}')
            return 'i3'
        if ('sway' in desktop_session or 'sway' in xdg_session_desktop
                or 'sway' in xdg_current_desktop) and sway_socket: # Check for Sway
            prt(f'ENV: sway {desktop_session=} {sway_socket=}')
            return 'sway'
        if 'plasma' in desktop_session or 'kde' in xdg_current_desktop:
            if wayland_display:
                env = 'kde-wayland'
                prt(f'ENV: {env} {desktop_session=} {wayland_display=}')
                assert False, f'unsupported env: {env}'
                return 'kde-wayland'
            if display:
                env = 'kde-x11'
                prt(f'ENV: {env} {desktop_session=} {display=}')
                return env
        if 'gnome' in desktop_session:
            if wayland_display:
                env='gnome-wayland'
                prt(f'ENV: {env} {desktop_session=} {wayland_display=}')
                assert False, f'unsupported env: {env}'
                return env
            if display:
                env='gnome-x11'
                prt(f'ENV: {env} {desktop_session=} {wayland_display=}')
                assert False, f'unsupported env: {env}'
                return 'gnome-x11'
        # Default case: no known environment detected
        assert False, 'cannot determine if i3/sway/kde-(x11|wayland)'

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
        'reset_idle': '',
        'reload_wm': '',
        'restart_wm': '',
#       'must_haves': 'systemctl brightnessctl'.split(),
        'must_haves': 'systemctl playerctl'.split(),

    }
    overides = {
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
            # 'logoff': 'loginctl terminate-session {XDG_SESSION_ID}',
            'logoff': 'qdbus org.kde.ksmserver /KSMServer org.kde.KSMServerInterface.logout 0 0 0',
            'restart_wm': 'killall plasmashell && kstart5 plasmashell && sleep 3 && pwr-tray',
            'must_haves': 'loginctl qdbus'.split(),
        }, 'kde-wayland': {
            # sudo apt-get install xdg-utils
            'reset_idle': 'qdbus org.freedesktop.ScreenSaver /ScreenSaver SimulateUserActivity',
            # gdbus introspect --session --dest org.gnome.SessionManager
            #       --object-path /org/gnome/SessionManager
            # gdbus call --session --dest org.gnome.SessionManager
            #       --object-path /org/gnome/SessionManager
            #       --method org.gnome.SessionManager.GetIdleTime
            #   from pydbus import SessionBus
            #   import time
            #   def get_idle_time():
            #       bus = SessionBus()
            #       screensaver = bus.get("org.freedesktop.ScreenSaver")
            #       # The GetSessionIdleTime method returns the idle time in seconds.
            #       idle_time = screensaver.GetSessionIdleTime()
            #       return idle_time
            #   if __name__ == "__main__":
            #       while True:
            #           idle_time = get_idle_time()
            #           print(f"Idle time in seconds: {idle_time}")
            #           time.sleep(5)
            'locker': 'loginctl lock-session',
            'must_haves': 'loginctl qdbus'.split(),

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
        InhIndicator.singleton = self
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
        for base in self.svg_info.bases:
            self.svgs.append(f'{base}-v{self.svg_info.version}.svg')
        for resource in self.svgs + ['lockpaper.png']:
            if not os.path.isfile(resource):
                Utils.copy_to_folder(resource, ini_tool.folder)
            if not os.path.isfile(resource):
                prt(f'WARN: cannot find {repr(resource)}')

            # states are Awake, Locked, Blanked, Asleep
            # when is idle time
        self.state = SimpleNamespace(name='Awake', when=0)

        self.running_idle_s = 0.000
        self.poll_s = 2.000
        self.poll_100ms = False
        self.lock_began_secs = None   # TBD: remove
        self.inh_lock_began_secs = False # TBD: refactor?
        self.rebuild_menu = False
        self.picks_file = ini_tool.picks_path
        self.current_icon_num = -1  # triggers immediate icon update

        self.restore_picks()
        if quick:
            for selector in self.ini_tool.get_selectors():
                params = self.ini_tool.params_by_selector[selector]
                params.lock_min_list = [1, 2, 4, 8, 32, 128]
                params.sleep_min_list = [1, 2, 4, 8, 32, 128]

        # self.down_state = self.opts.down_state

        self.graphical = self.get_environment()
        self.variables = self.default_variables
        must_haves = self.default_variables['must_haves']
        if self.graphical in ('i3', 'kde-x11'):
            self.variables.update(self.overides['x11'])
            must_haves += self.default_variables['must_haves']

        self.variables.update(self.overides[self.graphical])
        must_haves += self.variables['must_haves']

        dont_haves = []
        for must_have in set(must_haves):
            if shutil.which(must_have) is None:
                dont_haves.append(must_have)
        assert not dont_haves, f'commands NOT on $PATH: {dont_haves}'


        self.idle_manager = SwayIdleManager(self) if self.graphical == 'sway' else None

        self.indicator = appindicator.Indicator.new(APPINDICATOR_ID,
                os.path.join(self.ini_tool.folder, self.svgs[0]),
                appindicator.IndicatorCategory.SYSTEM_SERVICES)

        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.build_menu()
        notify.init(APPINDICATOR_ID)
        self.show_icon()
        if self.idle_manager:
            self.idle_manager_start()
        self.on_timeout()

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
        this = InhIndicator.singleton
        if this and not this.quick:
            picks = { 'mode': this.mode,
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
            self.mode = picks['mode']
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
        # pipe = subprocess.Popen(['qdbus', 'org.freedesktop.ScreenSaver', '/ScreenSaver',
            # 'GetSessionIdleTime'])
        cmd = self.variables['get_idle_ms']
        if cmd:
            xidle_ms = int(subprocess.check_output(cmd.split()).strip())
            xidle_ms *= 2 if self.quick else 1  # time warp
            self.running_idle_s = round(xidle_ms/1000, 3)

    def DB(self):
        """ is debug on? """
        rv = self.get_params().debug_mode
        return rv

    def effective_mode(self):
        """ TBD """
        return 'SleepAfterLock' if self.battery.selector == 'LoBattery' else self.mode

    def show_icon(self, inhibited=False):
        """ Display Icon if updated """
        emode = self.get_effective_mode()
        num = (3 if self.battery.selector == 'LoBattery'
                else 4 if inhibited
                else 1 if emode in ('Presentation', )
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
            svg = self.svgs[num]
            self.indicator.set_icon_full(
                os.path.join(self.ini_tool.folder, svg), 'PI')
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
        inhibited = False
        for count, line in enumerate(lines):
            if count == 0:
                rows.append(line.strip())
                continue
            if 'block' not in line:
                continue
            if count == 1:
                if ('xfce4-power-man' not in line
                        and 'org_kde_powerde' not in line):
                    inhibited = True
                    rows.append(line)
            else:
                inhibited = True
                rows.append(line)
        if len(rows) == 1:
            rows = []
        child = subprocess.run('playerctl status'.split(), check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        play_state = child.stdout.decode('utf-8').strip().lower()
        if play_state == 'playing':
            inhibited = True
        if self.was_play_state != play_state:
            prt(f'{play_state=}')
            self.was_play_state = play_state

        # inhibited = bool(inhibited or self.presentation_mode)
        emode = self.effective_mode()
        # inhibited = bool(inhibited or emode in ('Presentation',))

        if self.show_icon(inhibited=inhibited):
            self.poll_100ms = True
            self.idle_manager_start()

#       if (inhibited and emode not in ('Presentation', )
#               and self.battery.selector != 'LoBattery'
#               and self.state.name in ('Awake', )):
#           self.set_state('Inhibited')

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
                if 'sway' in self.graphical:
                    self.reset_xidle_ms() # we don't know when
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
        glib.timeout_add(poll_ms, self.on_timeout)

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

    def _lock_rotate_next(self, advance=True):
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

    def _sleep_rotate_next(self, advance=True):
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
        def has_cmd(label):
            return bool(self.variables.get(label, None))

        menu = gtk.Menu()
        if rows:
            for row in rows:
                item = gtk.MenuItem(label=row)
                item.connect('activate', self.dummy)
                menu.append(item)

        if self.mode not in ('Presentation',):
            item = gtk.MenuItem(label=f'🅟 Presentation ⮜ {self.mode} Mode')
            item.connect('activate', self.enable_presentation_mode)
            menu.append(item)

        if self.mode not in ('LockOnly',):
            item = gtk.MenuItem(label=f'🅛 LockOnly ⮜ {self.mode} Mode')
            item.connect('activate', self.enable_nosleep_mode)
            menu.append(item)

        if self.mode not in ('SleepAfterLock',):
            item = gtk.MenuItem(label=f'🅢 SleepAfterLock ⮜ {self.mode} Mode')
            item.connect('activate', self.enable_normal_mode)
            menu.append(item)


        # enable = 'Disable' if self.presentation_mode else 'Enable'
        # item = gtk.MenuItem(label=enable + ' Presentation Mode')
        # item.connect('activate', self.toggle_presentation_mode)
        # menu.append(item)

        item = gtk.MenuItem(label=f'{self.graphical}:  ▷ Lock Screen')
        item.connect('activate', self.lock_screen)
        menu.append(item)

        if (self.get_params().turn_off_monitors and
                self.variables['monitors_off']):
            item = gtk.MenuItem(label='   ▷ Blank Monitors')
            item.connect('activate', self.blank_quick)
            menu.append(item)

        if has_cmd('reload_wm'):
            item = gtk.MenuItem(label='   ▷ Reload')
            item.connect('activate', self.reload_wm)
            menu.append(item)

        if has_cmd('restart_wm'):
            item = gtk.MenuItem(label='   ▷ Restart')
            item.connect('activate', self.restart_wm)
            menu.append(item)

        item = gtk.MenuItem(label='   ▷ Log Off')
        item.connect('activate', self.exit_wm)
        menu.append(item)

        item = gtk.MenuItem(label='System:  ▼ Suspend')
        item.connect('activate', self.suspend)
        menu.append(item)

        item = gtk.MenuItem(label='    ▼ Reboot')
        item.connect('activate', self.reboot)
        menu.append(item)

        item = gtk.MenuItem(label='    ▼ PowerOff')
        item.connect('activate', self.poweroff)
        menu.append(item)

        selector, percent = self.battery.selector, self.battery.percent
        item = gtk.MenuItem(label=
                ('🗲 Plugged In' if selector == 'Settings'
                     else (('█' if selector == 'HiBattery' else '▃') + f' {selector}')
                 + (f' {percent}%' if percent < 100 or selector != 'Settings' else '')) )
        item.connect('activate', self._toggle_battery)
        menu.append(item)

        # if self.mode not in ('Presentation',) and len(self.opts.lock_min_list) > 1:
        item = gtk.MenuItem(label= f'  ♺ Lock: {self._lock_rotate_str()}')
        item.connect('activate', self._lock_rotate_next)
        menu.append(item)

        # if self.mode in ('SleepAfterLock',) and len(self.opts.sleep_min_list) > 1:
        item = gtk.MenuItem(label=f'  ♺ Sleep (after Lock): {self._sleep_rotate_str()}')
        item.connect('activate', self._sleep_rotate_next)
        menu.append(item)

        if self.get_params().gui_editor:
            item = gtk.MenuItem(label='🖹  Edit Applet Config')
            item.connect('activate', self.edit_config)
            menu.append(item)

        item = gtk.MenuItem(label='☓ Quit this Applet')
        item.connect('activate', self.quit_self)
        menu.append(item)

        item = gtk.MenuItem(label='↺ Restart this Applet')
        item.connect('activate', self.restart_self)
        menu.append(item)

#       item = gtk.MenuItem(label='Wake-SCSI')
#       item.connect('activate', self.wake_scsi)
#       menu.append(item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def idle_manager_start(self):
        """ For any mode, get the idle manager started"""
        if self.idle_manager:
            self.idle_manager.start()

    @staticmethod
    def dummy(_):
        """TBD"""

    @staticmethod
    def run_command(key):
        this = InhIndicator.singleton
        command = this.variables.get(key, None)
        if command and key == 'locker' and this.graphical in ('i3', 'sway'):
            if this.graphical == 'i3':
                append = this.get_params().i3lock_args
            elif this.graphical == 'sway':
                append = this.get_params().swaylock_args
            if not append:
                append = '-t -i ./lockpaper.png'
            command += ' ' + append
        # if '{XDG_SESSION_ID}' in command:
        #    command = command.replace('{XDG_SESSION_ID}',
        #             os.environ.get('XDG_SESSION_ID', '-1'))

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
        notify.uninit()
        gtk.main_quit()

    @staticmethod
    def restart_self(_):
        """TBD"""
        prt('+', 'restarting applet...')
        notify.uninit()
        InhIndicator.save_picks()
        os.execv(sys.executable, [sys.executable] + sys.argv[:])

    @staticmethod
    def edit_config(_):
        this = InhIndicator.singleton
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
        this = InhIndicator.singleton
        this.set_state('Asleep')
        this.reset_xidle_ms()
        InhIndicator.run_command('suspend')

    @staticmethod
    def poweroff(_):
        """TBD"""
        InhIndicator.run_command('poweroff')

    @staticmethod
    def reboot(_):
        """TBD"""
        InhIndicator.run_command('reboot')

    @staticmethod
    def dimmer(_):
        """TBD"""
        InhIndicator.run_command('dimmer')

    @staticmethod
    def undim(_):
        """TBD"""
        InhIndicator.run_command('undim')

    @staticmethod
    def lock_screen(_, before=''):
        this = InhIndicator.singleton
        # NOTE: loginctl works for most systems, but at least on KDE Neon, the Desktop Session
        # is not managed by systemd-login.service.
        # cmd = 'loginctl lock-session'
        # cmd = f'{before}qdbus org.kde.ksmserver /ScreenSaver SetActive true'
        # cmd = 'exec i3lock -c 200020 --ignore-empty-password --show-failed-attempt'
        InhIndicator.run_command('locker')
        this.update_running_idle_s()
#       if 0 <= int(thisget_params()params.dim_pct_brightness) < 100:
#           this.undim(None)
        this.set_state('Locked')

    def blank_primitive(self, lock_screen=False):
        """TBD"""
        cmd = self.variables['monitors_off']
        if cmd and self.get_params().turn_off_monitors:
            if lock_screen:
                self.lock_screen(None, before='sleep 1.5; ')
            prt('+', cmd)
            result = subprocess.run(cmd, shell=True, check=False)
            if result.returncode != 0:
                prt(f'   NOTE: returncode={result.returncode}')
            self.set_state('Blanked')

        else:
            prt('NOTE: blanking screen unsupported')

    @staticmethod
    def blank_quick(_):
        this = InhIndicator.singleton
        this.blank_primitive(lock_screen=True)

    @staticmethod
    def reload_wm(_):
        InhIndicator.run_command('reload_wm')

    @staticmethod
    def restart_wm(_):
        InhIndicator.run_command('restart_wm')

    @staticmethod
    def exit_wm(_):
        InhIndicator.run_command('logoff')

    @staticmethod
    def enable_presentation_mode(_):
        """TBD"""
        this = InhIndicator.singleton
        this.mode = 'Presentation'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

    @staticmethod
    def enable_nosleep_mode(_):
        """TBD"""
        this = InhIndicator.singleton
        this.mode = 'LockOnly'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

    @staticmethod
    def enable_normal_mode(_):
        """TBD"""
        this = InhIndicator.singleton
        this.mode = 'SleepAfterLock'
        this.was_output = 'Changed Mode'
        prt('+', f'{this.mode=}')
        this.save_picks()
        this.idle_manager_start()

    @staticmethod
    def wake_scsi(_):
        this = InhIndicator.singleton
        cmd = os.path.join(this.here_dir, "wake-scsi")
        prt('+', cmd)
        os.system(f'exec {cmd}')

    # Function to check for ACPI events
    @staticmethod
    def acpi_event_listener():
        acpi_pipe = subprocess.Popen(["acpi_listen"], stdout=subprocess.PIPE)
        for line in acpi_pipe.stdout:
            this = InhIndicator.singleton
            if this and b"resume" in line:
                prt('acpi event:', line)
                # Reset idle timer
                this.reset_xidle_ms()
            elif this:
                prt('UNSELECTED acpi event:', line)
                # Reset idle timer
                this.reset_xidle_ms()
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
    atexit.register(InhIndicator.goodbye)


    if opts.debug:
        for selector in ini_tool.get_selectors():
            ini_tool.params_by_selector[selector].debug_mode = True # one-time override


    _ = InhIndicator(ini_tool=ini_tool, quick=opts.quick)
    gtk.main()

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
