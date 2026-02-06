#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sway does not provide a way to get the idle time, and so the fundamental design
of pwr-tray using idle time does nto work.  Instead, for sway, we must basically
set up swayidle to run our config, and if there are special actions like
blank now, we have to kill the running swayidle and start one that does what
we want.
"""
# pylint: disable=invalid-name,consider-using-with
import subprocess
import time
import os
import signal
from types import SimpleNamespace
from pwr_tray.Utils import prt

class SwayIdleManager:
    """ Class to manage 'swayidle' for Wayland compositors """
    def __init__(self, applet):
        self.process = None
        self.applet = applet
        self.current_cmd = ''
        # Build clauses from de_config (no DE-specific branching)
        de = applet.de_config
        locker = de.get('swayidle_locker') or applet.variables.get('locker', '')
        blanker = de.get('swayidle_blanker', '')
        unblanker = de.get('swayidle_unblanker', '')
        self.clauses = SimpleNamespace(
            leader="""exec swayidle""",
            locker=""" timeout [lock_s] '[screenlock] [lockopts]'""",
            blanker=f""" timeout [blank_s] '{blanker}'""" if blanker else "",
            sleeper=""" timeout [sleep_s] 'systemctl suspend'""",
            before_sleep=""" before-sleep '[screenlock] [lockopts]'""",
            after_resume=""" after-resume '[unblank]'""" if unblanker else "",
            screenlock=locker,
            unblank=f'; {unblanker}' if unblanker else '',
        )
        self.kill_other_swayidle()

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
        except Exception:
            return # none left

    def build_cmd(self, mode=None):
        """ Build the swayidle command line from the current statue. """

        # lock_s, lockopts, sleep_s, blank_s, dim_s = None, '', None, None, None
        lock_s, lockopts, sleep_s, blank_s = None, '', None, None
        mode = mode if mode else self.applet.get_effective_mode()
        til_sleep_s, sleeping = None, False

        ini_key = self.applet.de_config.get('lock_args_ini_key', '')
        lockopts = getattr(self.applet.get_params(), ini_key, '') if ini_key else ''
        quick = self.applet.quick
        a_minute = 30 if quick else 60

        if mode in ('LockOnly', 'SleepAfterLock'):
            lock_s = self.applet.get_lock_min_list()[0] * a_minute
            til_sleep_s = lock_s
            if self.applet.get_params().turn_off_monitors:
                blank_s = 20 + lock_s
        if mode in ('SleepAfterLock', ):
            sleep_s = self.applet.get_sleep_min_list()[0] * a_minute
            til_sleep_s = sleep_s
            sleeping = True

        til_sleep_s, blanking = 0, False
        cmd = self.clauses.leader
        if isinstance(lock_s, (int,float)) and lock_s >= 0:
            til_sleep_s += lock_s
            cmd += self.clauses.locker.replace(
                "[lock_s]", str(lock_s)).replace(
                '[lockopts]', lockopts).replace(
                '[screenlock]', self.clauses.screenlock)
        if sleeping:
            til_sleep_s += sleep_s
            cmd += self.clauses.sleeper.replace("[sleep_s]", str(til_sleep_s))
        if blank_s is not None:
            blanking = True
            cmd += self.clauses.blanker.replace('[blank_s]', str(blank_s))
#       if isinstance(dim_s, (int,float)) and dim_s >= 0:
#           dimming = True
#           cmd += self.clauses.dimmer.replace('[dim_s]', str(dim_s))
        cmd += self.clauses.before_sleep.replace(
             "[sleep_s]", str(til_sleep_s)).replace(
                 '[lockopts]', lockopts).replace(
                 '[screenlock]', self.clauses.screenlock)
#       cmd += self.clauses.after_resume.replace(
#            "[undim]", self.clauses.undim if dimming else '')
        if blanking:
            cmd += self.clauses.after_resume.replace(
                 "[unblank]", self.clauses.unblank if blanking else '')

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
        """ Stop the current swayidle (normally to replace it) """
        self.process.terminate()
        self.process.wait()
        self.process = None

    def checkup(self):
        """ Check whether swayidle is running, normally to restart it
        with the current command. """
        if self.process.poll() is not None:
            self.start()
