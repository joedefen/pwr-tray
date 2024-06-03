#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utility functions
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import os
import sys
import stat
import time
import signal
import shutil
import subprocess
import inspect
from datetime import datetime
from io import StringIO

import pkg_resources

prt_kb = 512
prt_path = ''
prt_to_init = True

def get_resource_path(resource_name):
    """ Get the path of a resource """
    # Check if running from the source directory
    possible_path = os.path.join(os.path.dirname(__file__), 'resources', resource_name)
    if os.path.exists(possible_path):
        return possible_path
    # Fallback to package resources (useful when installed)
    return pkg_resources.resource_filename('pwr_tray', f'resources/{resource_name}')

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
    filename, line_number = stack[frameNo][1:3]
        # lg.db(f'YES {os.path.basename(filename)}:{line_number}')
    return f'[{filename.split("/")[-1]}:{line_number}]'
    # return f'[:{line_number}]'


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

def x_restart_self():
    """ TBD """
    # Retrieve the current script name and arguments
    script_name = sys.argv[0]
    script_args = sys.argv[1:]

    # Use subprocess to run the script with the same arguments
    subprocess.run([sys.executable, script_name] + script_args, check=True)

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
