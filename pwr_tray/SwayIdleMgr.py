#!/usr/bin/env python3
"""
Wayland compositors do not provide a way to poll idle time, so the fundamental
design of pwr-tray using idle time polling does not work.  Instead, for Wayland,
we manage swayidle to run our config, and if there are special actions like
blank now, we kill the running swayidle and start one that does what we want.
"""
# pylint: disable=invalid-name,consider-using-with
# pylint: disable=broad-exception-caught,subprocess-run-check
import atexit
import os
import signal
import subprocess
import time
from types import SimpleNamespace

from pwr_tray.Utils import prt


class SwayIdleManager:
    """ Class to manage 'swayidle' for Wayland compositors """
    def __init__(self, applet):
        self.process = None
        self.applet = applet
        self.current_cmd = ''
        # Build clauses from merged variables (no DE-specific branching)
        v = applet.variables
        locker = v.get('locker', '')
        blanker = v.get('monitors_off', '')
        unblanker = v.get('monitors_on', '')
        self.clauses = SimpleNamespace(
            leader="""exec swayidle""",
            locker=f""" timeout [lock_s] '{locker}'""" if locker else "",
            blanker=f""" timeout [blank_s] '{blanker}'""" if blanker else "",
            sleeper=""" timeout [sleep_s] 'systemctl suspend'""",
            before_sleep=f""" before-sleep '{locker}'""" if locker else "",
            after_resume=f""" after-resume '{unblanker}'""" if unblanker else "",
        )
        self.kill_other_swayidle()
        # A stop, a logout and a quit all end the process; nothing else would
        # reap our swayidle, and on a Wayland session an orphan keeps counting
        # idle time against a session that is going away.
        atexit.register(self.stop)

    @staticmethod
    def kill_other_swayidle():
        """ Kills any stray swayidles"""
        try:
            pids = subprocess.check_output(['pgrep', 'swayidle']).decode().split()
            if pids:
                subprocess.run(['pkill', 'swayidle'])
            time.sleep(2) # Wait a moment to ensure processes are terminated

            pids = subprocess.check_output(['pgrep', 'swayidle']).decode().split()
            if pids:
                prt(f"Force killing remaining swayidle processes... {pids}")
                for pid in pids:
                    os.kill(int(pid), signal.SIGKILL)
            return
        except (OSError, subprocess.SubprocessError, ValueError):
            return # none left

    def build_cmd(self, mode=None):
        """ Build the swayidle command line from the current state. """

        lock_s, sleep_s, blank_s = None, None, None
        mode = mode if mode else self.applet.get_effective_mode()
        # While media plays (playerctl), no idle timeouts are passed to
        # swayidle, which is how the inhibit takes effect on Wayland.
        media_playing = getattr(self.applet, 'was_inhibited', '') == 'player'
        sleeping = False

        quick = self.applet.quick
        a_minute = 30 if quick else 60

        if mode in ('LockOnly', 'SleepAfterLock') and not media_playing:
            lock_s = self.applet.get_lock_min_list()[0] * a_minute
            if self.applet.get_params().turn_off_monitors:
                blank_s = 20 + lock_s
        if mode in ('SleepAfterLock', ) and not media_playing:
            sleep_s = self.applet.get_sleep_min_list()[0] * a_minute
            sleeping = True

        til_sleep_s, blanking = 0, False
        cmd = self.clauses.leader
        if isinstance(lock_s, (int,float)) and lock_s >= 0:
            til_sleep_s += lock_s
            cmd += self.clauses.locker.replace("[lock_s]", str(lock_s))
        if sleeping:
            til_sleep_s += sleep_s
            cmd += self.clauses.sleeper.replace("[sleep_s]", str(til_sleep_s))
        if blank_s is not None:
            blanking = True
            cmd += self.clauses.blanker.replace('[blank_s]', str(blank_s))
        cmd += self.clauses.before_sleep
        if blanking:
            cmd += self.clauses.after_resume

        rv, self.current_cmd = bool(cmd != self.current_cmd), cmd
        if rv:
            prt('NEW-SWAYIDLE:', self.current_cmd)

        return rv # whether updated

    def start(self):
        """ Build and start the command from the current state """
        updated = self.build_cmd()
        if self.process and updated:
            self.stop()
        if updated:
            prt(f'SWAYIDLE: {self.current_cmd}')

        if not self.process and self.current_cmd:
            self.process = subprocess.Popen(self.current_cmd, shell=True)

        return self.process

    def stop(self):
        """ Stop the current swayidle (normally to replace it, or at exit).

        Safe to call when nothing was ever started (the shutdown path does not
        know), and bounded: a swayidle that ignores SIGTERM is killed rather
        than holding the applet open past the unit's TimeoutStopSec.
        """
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def checkup(self):
        """ Check whether swayidle is running, normally to restart it
        with the current command. """
        if self.process.poll() is not None:
            self.start()
