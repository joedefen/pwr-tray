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

"""
# pylint: disable=invalid-name,wrong-import-position,missing-function-docstring
# pylint: disable=broad-except,too-many-instance-attributes
# pylint: disable=global-statement,consider-using-with,too-many-lines
# pylint: disable=too-many-statements,too-few-public-methods
# pylint: disable=too-many-branches,too-many-public-methods

import os
import sys
needed = '/usr/lib/python3/dist-packages'
if needed not in sys.path:
    sys.path.append(needed) # pick up external dependencies
import signal
import time
import stat
import subprocess
# import threading
import json
import copy
import shutil
import inspect
import atexit
import traceback
from types import SimpleNamespace
from datetime import datetime
from io import StringIO
import configparser
import pkg_resources
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

# from gi.repository import GObject as gobject


def get_resource_path(resource_name):
    # Check if running from the source directory
    possible_path = os.path.join(os.path.dirname(__file__), 'resources', resource_name)
    if os.path.exists(possible_path):
        return possible_path
    # Fallback to package resources (useful when installed)
    return pkg_resources.resource_filename('my_package', f'resources/{resource_name}')



def where(above=0):
    """Get the file and line of the caller. Arguments:
     -  above -- how many frames to go up (or down) from the reference
        frame (which is 2 above). above=0 means the caller of the
        caller of the function (which is the frame ofkjk interest usually).
    Then keep going up until we get out of this file if possible
    Returns:
        [file:line]
    """
    stack, frameNo = inspect.stack(), 2 + above
    if frameNo < 0 or len(stack) < frameNo + 2:
        return '[n/a]'
    _, line_number = stack[frameNo][1:3]
        # lg.db(f'YES {os.path.basename(filename)}:{line_number}')
    # return f'[{filename.split("/")[-1]}:{line_number}]'
    return f'[:{line_number}]'


prt_kb = 512
prt_path = ''
prt_to_init = True

def prt(*args, **kwargs):
    """ Our custom print routine ...
     - use instead of print() to get time stamps.
     - unless stdout is a tty, say for debugging, ~/.config/pwr-tray/debug.log is used for stdout
     - if we create a log file, its size is limited to 512K and then it is truncated
     """
    def check_stdout(use_stdout=None):
        global prt_to_init
        def is_tty():
            try:
                os.ttyname(sys.stdout.fileno())
                return True
            except Exception:
                return False
        def is_reg():
            try:
                return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
            except Exception:
                return False
        def reopen():
            global prt_to_init
            sys.stdout = open(prt_path, "a+", encoding='utf-8')
            sys.stderr = sys.stdout
            os.dup2(sys.stdout.fileno(), 1)
            os.dup2(sys.stderr.fileno(), 2)
            prt_to_init = False

        if prt_kb > 0 and prt_path: # non-positive disables stdout "tuning"
            if use_stdout is False:
                reopen()
            elif prt_to_init and sys.stdout.closed: # Check if stdout is closed
                reopen()
            elif prt_to_init and not is_tty() and not is_reg():
                reopen()
            if os.fstat(sys.stdout.fileno()).st_size > prt_kb*1024:
                shutil.move(prt_path, f'{prt_path}1')
                reopen()

    to_stdout = None
    if 'to_stdout' in kwargs:
        to_stdout = bool(kwargs['to_stdout'])
        del kwargs['to_stdout']
    check_stdout(to_stdout)

    dt = datetime.now().strftime('%m-%d^%H:%M:%S')
    s = StringIO()
    print(dt, end=' ', file=s)
    kwargs['end'] = ' '
    kwargs['file'] = s
    print(*args, **kwargs)
    string = f'{s.getvalue()} {where()}'
    print(string, flush=True)

def restart_self():
    # Retrieve the current script name and arguments
    script_name = sys.argv[0]
    script_args = sys.argv[1:]

    # Use subprocess to run the script with the same arguments
    subprocess.run([sys.executable, script_name] + script_args, check=True)

class SwayIdleManager:
    """ TBD """
    def __init__(self, applet):
        self.process = None
        self.applet = applet
        self.current_cmd = ''
        self.clauses = SimpleNamespace(
            leader="""exec swayidle""",
            locker="""\\\n timeout [lock_s] 'exec [screenlock] [lockopts]'""",
            blanker="""\\\n timeout [blank_s] 'swaymsg "output * dpms off"'""",
            sleeper="""\\\n timeout [sleep_s] 'systemctl suspend'""",
            dimmer="""\\\n timeout [dim_s] 'brightnessctl set 50%'""", # perms?
            before_sleep="""\\\n before-sleep 'exec [screenlock] [lockopts]'""",
            after_resume="""\\\n after-resume"""
                        + """ 'pgrep -x copyq || copyq --start-server hide;"""
                        + """ pgrep -x nm-applet || nm-applet [undim][dpmsOn]'""",
            undim = """; brightnessctl set 100%""",
            screenlock = """swaylock --ignore-empty-password --show-failed-attempts""",
            unblank="""; swaymsg 'output * dpms on'""",
        )


    def build_cmd(self, mode=None):

        lock_s, lockopts, sleep_s, blank_s, dim_s = None, '', None, None, None
        mode = self.applet.mode if mode else mode
        til_sleep_s = None
        if mode in ('LockOnly', 'SleepAfterLock'):
            lock_s = self.applet.lock_mins * 60
            til_sleep_s = lock_s
            if self.applet.opts.turn_off_monitors:
                blank_s = 20 + lock_s
        if mode in ('SleepAfterLock', ):
            sleep_s = self.applet.sleep_mins * 60
            til_sleep_s += sleep_s

        til_sleep_s, sleeping, blanking, dimming = 0, False, False, False
        cmd = self.clauses.leader
        if isinstance(lock_s, (int,float)) and lock_s >= 0:
            til_sleep_s += lock_s
            cmd += self.clauses.locker.replace(
                "[lock_s]", str(lock_s)).replace(
                '[lockopts]', lockopts).replace(
                '[screenlock]', self.clauses.screenlock)
        if sleeping:
            sleeping = True
            til_sleep_s += sleep_s
            cmd += self.clauses.sleeper.replace("[sleep_s]", str(til_sleep_s))
        if blank_s is not None:
            blanking = True
            cmd += self.clauses.blanker.replace('[blank_s]', str(blank_s))
        if isinstance(dim_s, (int,float)) and dim_s >= 0:
            dimming = True
            cmd += self.clauses.dimmer.replace('[dim_s]', str(dim_s))
        cmd += self.clauses.before_sleep.replace(
             "[sleep_s]", str(til_sleep_s)).replace(
                 '[lockopts]', lockopts).replace(
                 '[screenlock]', self.clauses.screenlock)
        cmd += self.clauses.after_resume.replace(
             "[undim]", self.clauses.undim if dimming else '')
        cmd += self.clauses.after_resume.replace(
             "[unblank]", self.clauses.unblank if blanking else '')

        rv, self.current_cmd = bool(cmd != self.current_cmd), cmd
        return rv # whether updated

    def start(self):
        updated = self.build_cmd()
        if self.process and updated:
            self.stop()

        if not self.process and self.current_cmd:
            self.process = subprocess.Popen(self.current_cmd, shell=True)

        return self.process

    def stop(self):
        self.process.terminate()
        self.process.wait()
        self.process = None

    def checkup(self):
        if self.process.poll() is not None:
            self.start()

APPINDICATOR_ID = 'PowerInhibitIndicator'

class InhIndicator:
    """ pwr-tray main class.
    NOTES:
     - when icons are moved/edited, rename them or reboot to avoid cache confusion
    """
    svg_info = SimpleNamespace(version='03', subdir='icons.d'
                , bases= ['NormMode', 'PresMode', 'LockOnlyMode'])
    singleton = None
    @staticmethod
    def get_environment():
        desktop_session = os.environ.get('DESKTOP_SESSION', '').lower()
        xdg_current_desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        sway_socket = os.environ.get('SWAYSOCK')
        wayland_display = os.environ.get('WAYLAND_DISPLAY')
        display = os.environ.get('DISPLAY')

        if xdg_current_desktop == 'i3': # Check for i3
            return 'i3'
        if sway_socket: # Check for Sway
            return 'sway'
        if 'kde' in desktop_session or 'plasma' in xdg_current_desktop:
            if wayland_display:
                return 'kde-wayland'
            if display:
                return 'kde-x11'
        if 'gnome' in desktop_session:
            if wayland_display:
                return 'gnome-wayland'
            if display:
                return 'gnome-x11'
        # Default case: no known environment detected
        assert False, 'cannot determine if i3/sway/kde-(x11|wayland)'

    default_variables = {
        'suspend': 'systemctl suspend',
        'poweroff': 'systemctl poweroff',
        'reboot': 'systemctl reboot',
        'dimmer': 'brightnessctl set {percent}%',
        'undim': 'brightnessctl set 100%',
        'logoff': '',
        'monitors_off': '',
        'locker': '',
        'get_idle_ms': '',
        'reset_idle': '',
        'reload_wm': '',
        'restart_wm': '',
        'must_haves': 'systemctl brightnessctl'.split(),

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
            # 'locker': 'i3lock -c 300000 --ignore-empty-password --show-failed-attempt',
            'must_haves': 'swaymsg i3lock'.split(),

        }, 'i3': {
            'reload_wm': 'i3-msg reload',
            'restart_wm': 'i3-msg restart',
            'logoff': 'i3-msg exit',
            'locker': 'i3lock -c 300000 --ignore-empty-password --show-failed-attempt',
            'must_haves': 'i3-msg i3lock'.split(),

        }, 'kde-xll': {
        }, 'kde-wayland': {
            # sudo apt-get install xdg-utils
            'reset_idle': 'qdbus org.freedesktop.ScreenSaver /ScreenSaver SimulateUserActivity',
            # gdbus introspect --session --dest org.gnome.SessionManager --object-path /org/gnome/SessionManager
            # gdbus call --session --dest org.gnome.SessionManager --object-path /org/gnome/SessionManager --method org.gnome.SessionManager.GetIdleTime
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
            #   screensaver = bus.get("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core")
            #   idle_time = screensaver.GetIdletime()

            #   print(f"Idle time in milliseconds: {idle_time}")
            'locker': 'loginctl lock-session',
            'must_haves': 'qdbus gnome-screensaver-command'.split(),

        }
    }

    def __init__(self, ini_tool, quick=False):
        InhIndicator.singleton = self
        self.ini_tool = ini_tool
        # self.params, self.lock_mins, self.sleep_mins = None, None, None
        self.params = None
        self.battery = SimpleNamespace(present=None, plugged=1, percent=100)
        self.reconfig()
        ## self.lock_mins = self.params.lock_min_list[0]
        ## self.sleep_mins = self.params.sleep_min_list[0]
        self.quick = quick

        self.loop = 0
        self.loop_sample = (4 if quick else 15)

        ## self.singleton.presentation_mode = False
        self.singleton.mode = 'SleepAfterLock' # or 'LockOnly' or 'Presentation'
        self.was_mode = None
        self.was_inhibited = None
        self.was_output = ''
        self.here_dir = os.path.dirname(os.path.abspath(__file__))
        self.svgs = []
        for base in self.svg_info.bases:
            self.svgs.append(get_resource_path(f'{base}-v{self.svg_info.version}.svg'))
        for svg in self.svgs:
            if not os.path.isfile(svg):
                prt(f'WARN: cannot find {repr(svg)}')

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

        self.restore_picks()
        if quick:
            self.params.lock_min_list = [1, 2, 4, 8, 32, 128]
            self.params.sleep_min_list = self.params.lock_min_list
            self.lock_mins = self.sleep_mins = 1

        # self.down_state = self.opts.down_state

        self.graphical = self.get_environment()
        self.variables = self.default_variables
        must_haves = self.default_variables['must_haves']
        if self.graphical in ('i3', 'kde-x11'):
            self.variables.update(self.overides['x11'])
            must_haves += self.default_variables['must_haves']

        self.variables.update(self.overides[self.graphical])
        must_haves += self.variables['must_haves']
#       if self.graphical == 'i3' and self.params.i3lock_args:
#           self.variables['locker'] += f' {self.params.i3lock_args}'

        dont_haves = []
        for must_have in set(must_haves):
            if shutil.which(must_have) is None:
                dont_haves.append(must_have)
        assert not dont_haves, f'commands NOT on $PATH: {dont_haves}'


        self.idle_manager = SwayIdleManager(self) if self.graphical == 'sway' else None

        self.indicator = appindicator.Indicator.new(APPINDICATOR_ID,
                self.svgs[0], appindicator.IndicatorCategory.SYSTEM_SERVICES)

        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.build_menu()
        notify.init(APPINDICATOR_ID)
        if self.idle_manager:
            self.idle_manager_start()
        self.on_timeout()

    def get_lock_mins(self, plugged=None):
        """TBD"""
        plugged = self.battery.plugged if plugged is None else plugged
        return self.ini_tool.lock_mins[plugged]

    def get_sleep_mins(self, plugged=None):
        """TBD"""
        plugged = self.battery.plugged if plugged is None else plugged
        return self.ini_tool.sleep_mins[plugged]

    def reconfig(self):
        """ update/fix config """
        self.ini_tool.update_config(self.battery.plugged)
        self.params = self.ini_tool.params
#       if self.lock_mins not in self.params.lock_min_list:
#           self.lock_mins = self.params.lock_min_list[0]
#       if self.sleep_mins not in self.params.sleep_min_list:
#           self.sleep_mins = self.params.sleep_min_list[0]

    @staticmethod
    def save_picks():
        this = InhIndicator.singleton
        if this and not this.quick:
            picks = { 'mode': this.mode,
                      'Settings': {
                          'lock_mins': this.get_lock_mins(1),
                          'sleep_mins': this.get_sleep_mins(1),
                      },
                      'OnBattery': {
                          'lock_mins': this.get_lock_mins(0),
                          'sleep_mins': this.get_sleep_mins(0),
                      },
                }
            try:
                with open(this.picks_file, 'w', encoding='utf-8') as f:
                    json.dump(picks, f)
                print("Picks saved successfully.")
                this.poll_100ms = True
            except Exception as e:
                print(f"An error occurred while saving picks: {e}", file=sys.stderr)


    def restore_picks(self):
        try:
            with open(self.picks_file, 'r', encoding='utf-8') as handle:
                picks = json.load(handle)
            self.mode = picks['mode']
            for plugged, key in enumerate('OnBattery Settings'.split()):
                for attr in 'lock_mins sleep_mins'.split():
                    getattr(self.ini_tool, attr)[plugged] = picks[key][attr]

            self.ini_tool.set_effective_params(self.battery.plugged)
            prt('restored Picks OK:', picks)
            return True

        except Exception as e:
            prt(f'restored picks FAILED: {e}')
            prt(f'mode=self.mode lock_mins={self.get_lock_mins()}'
                f' sleep_mins={self.get_sleep_mins()}')
            return True



    def _get_down_state(self):
        return 'PowerDown' if self.params.power_down else 'Suspend'

    def update_running_idle_s(self):
        """ Update the running idle seconds (called after each regular timeout) """
        # pipe = subprocess.Popen(['qdbus', 'org.freedesktop.ScreenSaver', '/ScreenSaver',
            # 'GetSessionIdleTime'])
        cmd = self.variables['get_idle_ms']
        if cmd:
            xidle_ms = int(subprocess.check_output(cmd.split()).strip())
            self.running_idle_s = round(xidle_ms/1000, 3)

    def DB(self):
        """ is debug on? """
        rv = self.params.debug_mode
        return rv

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

        # inhibited = bool(inhibited or self.presentation_mode)
        inhibited = bool(inhibited or self.mode in ('Presentation',))
        was_inhibited = bool(self.state.name == 'Inhibited'
                             or self.mode in ('Presentation',))

        if inhibited != was_inhibited or self.was_mode != self.mode:
            svg = self.svgs[1 if inhibited else 0 if self.mode in ('SleepAfterLock',) else 2]
            self.indicator.set_icon_full(svg, 'PI')
            self.poll_100ms = True

        if (inhibited and self.mode not in ('Presentation', )
                and self.state.name in ('Awake', )):
            self.set_state('Inhibited')

        self.was_mode = self.mode
        was_output = self.was_output
        self.was_output = output
        return rows, bool(was_output != output)

    def update_battery_status(self):
        if not self.battery.present:
            return
        battery = psutil.sensors_battery()
        if battery is None:
            self.battery.present = False
            return
        was_plugged = self.battery.plugged

        self.battery.plugged = 1 if battery.power_plugged else 0
        self.battery.percent = battery.percent
        if was_plugged != self.battery.plugged:
            self.ini_tool.set_effective_params(self.battery.plugged)
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

        rows, updated = self.check_inhibited()
        if updated or self.rebuild_menu:
            self.build_menu(rows)
            self.rebuild_menu = False
            prt('re-built menu')

        if self.loop >= self.loop_sample:
            self.update_running_idle_s()
            self.update_battery_status()
            lock_secs = self.get_lock_mins()*60
            down_secs = self.get_sleep_mins()*60 + lock_secs
            blank_secs = 5 if self.quick else 20
            if 0 <= int(self.params.dim_pct_brightness) < 100:
                dim_secs = int(round(lock_secs * int(self.params.dim_pct_lock_min) / 100, 0))
            else:
                dim_secs = 2000 + lock_secs * 2  # make it never happen

            emit = f'idle_s={self.running_idle_s} state={self.state.name},{self.state.when}s'
            if self.mode in ('LockOnly', 'SleepAfterLock'):
                emit += f' @{self.get_lock_mins()}m'
            if self.mode in ('SleepAfterLock', ):
                emit += f'+{self.get_sleep_mins()}m'
            if not self.battery.plugged:
                emit += ' unplugged'
            prt(emit)

            if self.running_idle_s > min(50, lock_secs*0.40) and (
                    self.mode in ('Presentation',) or self.state.name in ('Inhibited')):
                self.reset_xidle_ms()

            if (self.running_idle_s >= down_secs and self.mode not in ('LockOnly',)
                    and self.state.name in ('Awake', 'Locked', 'Blanked')):
                if self._get_down_state() == 'PowerOff':
                    self.poweroff(None)
                else:
                    self.suspend(None)

            elif (self.running_idle_s >= lock_secs and self.mode not in ('Presentation',)
                    and self.state.name in ('Awake', 'Dim')):
                self.lock_screen(None)

            elif (self.running_idle_s >= dim_secs and self.mode not in ('Presentation',)
                    and self.state.name in ('Awake',)):
                self.dimmer(None)

            elif (self.running_idle_s >= self.state.when + blank_secs
                    and self.params.turn_off_monitors
                    and self.mode not in ('Presentation',)
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
        # TODO: this is temporary for testing
        self.battery.plugged = 0 if self.battery.plugged else 1
        self.ini_tool.set_effective_params(self.battery.plugged)
        self.rebuild_menu = True

    def _lock_rotate_next(self, advance=True):
        mins0 = self.get_lock_mins()
        if len(self.params.lock_min_list) < 1:
            return mins0
        idx0 = self.params.lock_min_list.index(mins0)
        idx1 = (idx0+1) % len(self.params.lock_min_list)
        next_mins = self.params.lock_min_list[idx1]
        if advance:
            self.ini_tool.lock_mins[self.battery.plugged] = next_mins
            prt(f'picked lock_mins={self.get_lock_mins()}')
            self.rebuild_menu = bool(idx0 != idx1)
            self.save_picks()
            self.idle_manager_start()
        return next_mins

    def _lock_rotate_str(self):
        mins0 = self.get_lock_mins()
        mins1 = self._lock_rotate_next(False)
        rv = f'{mins0}m' + ('' if mins0 == mins1 else f'->{mins1}m')
        return rv

    def _sleep_rotate_next(self, advance=True):
        mins0 = self.get_sleep_mins()
        if len(self.params.sleep_min_list) < 1:
            return mins0
        idx0 = self.params.sleep_min_list.index(mins0)
        idx1 = (idx0+1) % len(self.params.sleep_min_list)
        next_mins = self.params.sleep_min_list[idx1]
        if advance:
            self.ini_tool.sleep_mins[self.battery.plugged] = next_mins
            prt(f'picked sleep_mins={self.get_sleep_mins()}')
            self.rebuild_menu = bool(idx0 != idx1)
            self.save_picks()
            self.idle_manager_start()
        return next_mins

    def _sleep_rotate_str(self):
        mins0 = self.get_sleep_mins()
        mins1 = self._sleep_rotate_next(False)
        rv = f'{mins0}m' + ('' if mins0 == mins1 else f'->{mins1}m')
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
            item = gtk.MenuItem(label='☀ Presentation Mode')
            item.connect('activate', self.enable_presentation_mode)
            menu.append(item)

        if self.mode not in ('LockOnly',):
            item = gtk.MenuItem(label='☀ LockOnly Mode')
            item.connect('activate', self.enable_nosleep_mode)
            menu.append(item)

        if self.mode not in ('SleepAfterLock',):
            item = gtk.MenuItem(label='☀ SleepAfterLock Mode')
            item.connect('activate', self.enable_normal_mode)
            menu.append(item)

        # enable = 'Disable' if self.presentation_mode else 'Enable'
        # item = gtk.MenuItem(label=enable + ' Presentation Mode')
        # item.connect('activate', self.toggle_presentation_mode)
        # menu.append(item)

        item = gtk.MenuItem(label='▷ Lock Screen')
        item.connect('activate', self.lock_screen)
        menu.append(item)

        if self.params.turn_off_monitors:
            item = gtk.MenuItem(label='▷ Blank Monitors')
            item.connect('activate', self.blank_quick)
            menu.append(item)

        if has_cmd('reload_wm'):
            item = gtk.MenuItem(label=f'▷ Reload {self.graphical}')
            item.connect('activate', self.reload_wm)
            menu.append(item)

        if has_cmd('restart_wm'):
            item = gtk.MenuItem(label=f'▷ Restart {self.graphical}')
            item.connect('activate', self.restart_wm)
            menu.append(item)

        item = gtk.MenuItem(label='▷ Log Off')
        item.connect('activate', self.exit_wm)
        menu.append(item)

        item = gtk.MenuItem(label='▼ Suspend System')
        item.connect('activate', self.suspend)
        menu.append(item)

        item = gtk.MenuItem(label='▼ Reboot System')
        item.connect('activate', self.reboot)
        menu.append(item)

        item = gtk.MenuItem(label='▼ PowerOff System')
        item.connect('activate', self.poweroff)
        menu.append(item)

        item = gtk.MenuItem(label=
                '🔌 Plugged In' if self.battery.plugged else f'🔋 Battery {self.battery.percent}%')
        item.connect('activate', self._toggle_battery)
        menu.append(item)

        # if self.mode not in ('Presentation',) and len(self.opts.lock_min_list) > 1:
        if len(self.params.lock_min_list) > 1:
            item = gtk.MenuItem(label= f'  ♺ Lock: {self._lock_rotate_str()}')
            item.connect('activate', self._lock_rotate_next)
            menu.append(item)

        # if self.mode in ('SleepAfterLock',) and len(self.opts.sleep_min_list) > 1:
        if len(self.params.sleep_min_list) > 1:
            item = gtk.MenuItem(label=f'  ♺ Sleep (after Lock): {self._sleep_rotate_str()}')
            item.connect('activate', self._sleep_rotate_next)
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
            prt(f'{self.idle_manager.current_cmd}')

    @staticmethod
    def dummy(_):
        """TBD"""

    @staticmethod
    def run_command(key):
        this = InhIndicator.singleton
        command = this.variables.get(key, None)
        if key == 'locker' and this.graphical in ('i3', 'sway'):
            append = this.params.i3lock_args
            if not append:
                file = get_resource_path('lockpaper.png')
                if file:
                    append = f'-t -i {file}'
            command += ' ' + append
        elif key == 'dimmer':
            percent = int(round(int(this.params.dim_pct_brightness), 0))
            command = command.replace('{percent}', str(percent))

        if command:
            prt(f'+ {command}')
            subprocess.run(command.split(), check=False)

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
        if 0 <= int(this.params.dim_pct_brightness) < 100:
            this.undim(None)
        this.set_state('Locked')

    def blank_primitive(self, lock_screen=False):
        """TBD"""
        if self.params.turn_off_monitors:
            if lock_screen:
                self.lock_screen(None, before='sleep 1.5; ')
            cmd = self.variables['monitors_off']
            prt('+', cmd)
            subprocess.run(cmd, shell=True, check=False)
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

#######
####### ####### ####### ####### PyKill
#######

class PyKill:
    """Class to kill python scripts and explain how; avoids suicide. """
    def __init__(self):
        self.last_sig = {} # keyed by target
        self.alive = {} # ps line of found targets

    def _kill_script(self, targets, sig):
        """ one iteration through very instance of the remaining processes """
        def kill_pid(pid, sig):
            # terminate the process
            alive = True
            try:
                os.kill(pid, sig)
            except OSError:
                alive = False
            return alive
        self.alive = {} # reset each time thru
        for line in os.popen("ps -eo pid,args"):
            words = line.split()
            if len(words) < 3:
                continue
            pid, cmd = int(words[0]), os.path.basename(words[1])
            if pid == os.getpid():
                continue  # no suicides
            script = os.path.basename(words[2])
            for target in targets:
                if cmd in ('python', 'python3') and script in (target, f'{target}.py'):
                    self.last_sig[target] = sig
                    if kill_pid(pid, sig): # returns True if alive
                        self.alive[target] = line
        return self.alive

    def kill_loop(self, targets):
        """Loops thru the remaining process until all or gone or we
        become exhausted (e.g., unkillable processes)."""
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        for sig in [signal.SIGTERM, signal.SIGTERM, signal.SIGTERM,
                    signal.SIGKILL, signal.SIGKILL,]:
            if not self._kill_script(targets, sig):
                break
            time.sleep(1)
        for target, line in self.alive.items():
            prt(f'ALERT: running: {line} [sig={self.last_sig[target]}]')
        for target in targets:
            if target in self.alive:
                continue
            prt(f'INFO: gone: {target} [sig={self.last_sig.get(target, None)}]')
        return not bool(self.alive)

class IniTool:
    """ Configued Params for this class"""
    def __init__(self):
        self.defaults = {
            'Settings': {
                'i3lock_args': '',
            #   'i3lock_args': '-c 200020',
                'debug_mode': False,
                'power_down': False,
                'turn_off_monitors': False,
                'lock_min_list': '[15, 30, 2, 1]',
                'sleep_min_list': '[10, 1]',
                'dim_pct_brightness': 100,
                'dim_pct_lock_min': 100,
            },
            'OnBattery': {
                'down_percent': 10,
                'power_down': True,
                'lock_min_list': '[10, 20, 1]',
                'sleep_min_list': '[5, 1]',
                'dim_pct_brightness': 50,
                'dim_pct_lock_min': 70,
            }
        }
        self.lock_mins = [10, 15] # indexed by int(plugged)
        self.sleep_mins = [5, 10] # indexed by int(plugged)
        self.folder = os.path.expanduser("~/.config/pwr-tray")
        self.ini_path =  os.path.join(self.folder, "config.ini")
        self.log_path =  os.path.join(self.folder, "debug.log")
        self.picks_path =  os.path.join(self.folder, "picks.json")
        self.config = configparser.ConfigParser()
        self.last_mod_time = None
        self.section_params = {'Settings': {}, 'OnBattery': {}}
        self.params = SimpleNamespace()
        self.ensure_ini_file()

    def ensure_ini_file(self):
        """Check if the config file exists, create it if not."""
        if not os.path.exists(self.folder):
            os.makedirs(self.folder, exist_ok=True)
        if not os.path.exists(self.ini_path):
            self.config.read_dict(self.defaults)
            with open(self.ini_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)

    def update_config(self, plugged):
        """ Check if the file has been modified since the last read """
        def to_array(val_str):
            rv = []
            try:
                vals = json.loads(val_str)
            except Exception:
                vals = None
            if vals and not isinstance(vals, list):
                vals = [vals]
            if isinstance(vals, list):
                for val in vals:
                    if isinstance(val, int) and val > 0:
                        rv.append(val)
            if not rv:
                assert val_str != self.defaults[key]
                vals = to_array(self.defaults[key])
            return vals

        current_mod_time = os.path.getmtime(self.ini_path)
        if current_mod_time == self.last_mod_time or not self.params:
            return False
        # Re-read the configuration file if it has changed
        self.config.read(self.ini_path)
        self.last_mod_time = current_mod_time

        # Access the configuration values
        for section in 'Settings OnBattery'.split():
            section_params = SimpleNamespace(**self.section_params[section])
            for key in self.defaults[section]:
                try:
                    value = self.config.get(section, key)
                    # print(key, repr(value), type(value))
                    if isinstance(value, str):
                        if value.lower() == 'true':
                            value = True
                        elif value.lower() == 'false':
                            value = False
                    setattr(section_params, key, value)
                except Exception:
                    if not hasattr(self.params, key):
                        value = self.defaults[section].get(key, None)
                        if value is not None:
                            setattr(section_params, key, value)

            for key in ('lock_min_list', 'sleep_min_list'):
                array = to_array(getattr(section_params, key))
                setattr(section_params, key, array)
            self.section_params[section] = section_params

        self.set_effective_params(plugged)

    def set_effective_params(self, plugged):
        """ Set the effective parameters based on state of battery """
        params = copy.deepcopy(vars(self.section_params['Settings']))
        if not plugged:
            params.update(copy.deepcopy(vars(self.section_params['OnBattery'])))
        for plug, key in enumerate('OnBattery Settings'.split()):
            for pick, array in [
                    'lock_mins lock_min_list'.split(),
                    'sleep_mins sleep_min_list'.split()
                    ]:
                picks = getattr(self, pick)
                choices = getattr(self.section_params[key], array)
                if picks[plug] not in choices:
                    picks[plug] = choices[0]
        self.params = SimpleNamespace(**params)


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
            help='exec tail -f on log file')
    parser.add_argument('-e', '--edit-config', action='store_true',
            help='exec ${EDITOR:-vim} on config.ini file')
    parser.add_argument('-q', '--quick', action='store_true',
            help='quick mode (1m lock + 1m sleep')
    opts = parser.parse_args()

    ini_tool = IniTool()
    if opts.edit_config:
        editor = os.getenv('EDITOR', 'vim')
        args = [editor, ini_tool.ini_path]
        print(f'RUNNING: {args}')
        os.execvp(editor, args)
        sys.exit(1) # just in case ;-)

    if opts.follow_log:
        args = ['tail', '-f', ini_tool.log_path]
        print(f'RUNNING: {args}')
        os.execvp('tail', args)
        sys.exit(1) # just in case ;-)

    os.environ['DISPLAY'] = ':0'

    global prt_path
    prt_path = ini_tool.log_path
    prt('START-UP', to_stdout=opts.stdout)
    prt('Initial Params:', vars(ini_tool.params))
    PyKill().kill_loop('pwr-tray')
    atexit.register(InhIndicator.goodbye)

    # Start ACPI event listener in a separate thread: TODL make conditional
    # requires "acpid" which causes other issue
    # threading.Thread(target=InhIndicator.acpi_event_listener, daemon=True).start()
    # Start the applet
    if opts.debug:
        ini_tool.params.debug_mode = True # one-time override
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
