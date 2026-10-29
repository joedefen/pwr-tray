#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utility functions
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import os
import re
import sys
import stat
import time
import signal
import shutil
import subprocess
import inspect
from datetime import datetime
from io import StringIO

import importlib.resources as pkg_resources
# from pathlib import Path

try:
    import fcntl
except ImportError:     # non-POSIX: SingleInstance degrades to a no-op
    fcntl = None

prt_kb = 512
prt_path = ''
prt_to_init = True

def copy_to_folder(resource_name, dest):
    """ Get the path of a resource """
    with pkg_resources.path('pwr_tray.resources', resource_name) as file_path:
        shutil.copy(file_path, os.path.join(dest, resource_name))
    return file_path


def resource_text(resource_name):
    """ Return the text of a packaged resource (e.g. 'commands.yaml'). """
    with pkg_resources.path('pwr_tray.resources', resource_name) as file_path:
        with open(file_path, 'r', encoding='utf-8') as handle:
            return handle.read()


_console_fd = None

def capture_console():
    """Keep a duplicate of stderr, so startup errors cannot be swallowed.

    prt() redirects both stdout and stderr into debug.log whenever it is not
    attached to a tty (e.g. started from a systemd unit).  A message the user
    must act on -- unknown desktop, missing command -- has to bypass that
    redirect, so main() calls this before the first prt() call and reports
    through error_print().
    """
    global _console_fd
    if _console_fd is None:
        try:
            _console_fd = os.dup(2)
        except OSError:
            _console_fd = -1
    return _console_fd


def error_print(message):
    """Print `message` to the console the user started us from."""
    text = message if message.endswith('\n') else f'{message}\n'
    if _console_fd is not None and _console_fd >= 0:
        try:
            os.write(_console_fd, text.encode('utf-8', 'replace'))
            return
        except OSError:
            pass
    try:
        stream = sys.__stderr__ or sys.stderr
        stream.write(text)
        stream.flush()
    except Exception:
        pass


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
            
            # Capture the old stdout/stderr to close them
            old_stdout = sys.stdout
            old_stderr = sys.stderr

            # Open the new one
            new_f = open(prt_path, "a+", encoding='utf-8')
            
            # Redirect
            sys.stdout = new_f
            sys.stderr = new_f
            os.dup2(new_f.fileno(), 1)
            os.dup2(new_f.fileno(), 2)

            # CLOSE the old ones if they weren't the original system TTYs
            # This prevents the leak
            try:
                if old_stdout and not old_stdout.isatty():
                    old_stdout.close()
                if old_stderr and old_stderr is not old_stdout and not old_stderr.isatty():
                    old_stderr.close()
            except Exception:
                pass

            prt_to_init = False

        def old_reopen():
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


_PYTHON_RE = re.compile(r'^python[0-9.]*$')


def script_key(name):
    """Reduce a path/script/module name to a comparable key.

    'pwr_tray.main', 'main.py' and '/usr/bin/pwr-tray' become 'pwr-tray.main',
    'main' and 'pwr-tray'.  Underscores are folded into dashes because the
    executable (pwr-tray) and the package (pwr_tray) use different spellings.
    """
    base = os.path.basename(str(name)).lower()
    if base.endswith('.py'):
        base = base[:-3]
    return base.replace('_', '-')


def command_names(argv):
    """Return the names a process argv claims to be running.

    ``ps`` shows the *interpreter* command line, so a restarted instance is
    ``python -m pwr_tray.main`` (``restart_self()`` and ``./runner`` both use
    that form).  This returns the executable, the ``-m`` module (and its
    package prefixes), or the script path, plus the script's directory name
    (which is the project name when running ``python pwr_tray/main.py``).
    """
    if not argv:
        return []
    exe = os.path.basename(argv[0])
    if not _PYTHON_RE.match(exe):
        return [exe]     # a real executable, e.g. an installed pwr-tray
    names = []
    rest = argv[1:]
    for index, token in enumerate(rest):
        if token == '-m' and index + 1 < len(rest):
            module = rest[index + 1]
            parts = [part for part in module.split('.') if part]
            names.extend('.'.join(parts[:end]) for end in range(1, len(parts) + 1))
            break
        if token in ('-c',):
            break        # inline program: nothing to match on
        if token.startswith('-'):
            continue     # interpreter option (e.g. -u, -O, -X utf8=1)
        script = token
        names.append(os.path.basename(script))
        parent = os.path.basename(os.path.dirname(script))
        if parent:
            names.append(parent)
        break
    return names


class PyKill:
    """Class to kill python scripts and explain how; avoids suicide. """
    def __init__(self):
        self.last_sig = {} # keyed by target
        self.alive = {} # ps line of found targets

    def _kill_script(self, targets, sig):
        """ one iteration through every instance of the remaining processes """
        def kill_pid(pid, sig):
            # terminate the process
            alive = True
            try:
                os.kill(pid, sig)
            except OSError:
                alive = False
            return alive
        self.alive = {} # reset each time thru
        keys = {target: script_key(target) for target in targets}
        for line in os.popen("ps -eo pid,args"):
            words = line.split()
            if len(words) < 2:
                continue
            try:
                pid = int(words[0])
            except ValueError:
                continue     # header line
            if pid == os.getpid():
                continue  # no suicides
            names = {script_key(name) for name in command_names(words[1:])}
            for target, key in keys.items():
                if key not in names:
                    continue
                self.last_sig[target] = sig
                if kill_pid(pid, sig): # returns True if alive
                    self.alive[target] = line.strip()
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


class SingleInstance:
    """Exclusive PID/lock file, the backstop for the process-name killer.

    ``PyKill`` can only recognise the command lines it knows about, so a
    single ``flock`` on a lock file is what actually guarantees that two
    applets never coexist.  The kernel drops the lock when the holder exits --
    even on SIGKILL -- so a crash cannot leave a stale lock that blocks the
    next start.  The file's contents are informational (the holder's pid).
    """
    current = None   # the instance holding the lock in this process

    def __init__(self, path):
        self.path = path
        self.fd = None
        self.pid = None

    @staticmethod
    def read_pid(path):
        """Return the pid recorded in `path`, or None if unreadable."""
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return int(handle.read().strip()) or None
        except (OSError, ValueError):
            return None

    def acquire(self, retries=0, delay=0.2):
        """Take the lock.  Returns False if another live process holds it.

        `retries`/`delay` cover the few milliseconds between restart_self()
        spawning the replacement and the old process finally exiting.
        """
        if fcntl is None:      # not POSIX: no locking available
            return True
        if self.fd is not None:    # already held by this object
            return True
        for attempt in range(max(1, retries + 1)):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                if attempt < retries:
                    time.sleep(delay)
                    continue
                return False
            os.ftruncate(fd, 0)
            os.write(fd, f'{os.getpid()}\n'.encode('ascii'))
            os.fsync(fd)
            self.fd, self.pid = fd, os.getpid()
            SingleInstance.current = self
            return True
        return False

    def release(self):
        """Drop the lock (also used before spawning a restarted instance)."""
        if SingleInstance.current is self:
            SingleInstance.current = None
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None

    @classmethod
    def release_current(cls):
        """Release the lock held by this process, if any."""
        if cls.current is not None:
            cls.current.release()
