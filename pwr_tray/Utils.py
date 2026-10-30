#!/usr/bin/env python3
"""
Utility functions
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=consider-using-with,global-statement
# pylint: disable=too-few-public-methods

import contextlib
import importlib.resources as pkg_resources
import inspect
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO

# from pathlib import Path

try:
    import fcntl
except ImportError:     # non-POSIX: SingleInstance degrades to a no-op
    fcntl = None

prt_kb = 2048
prt_path = ''
prt_to_init = True

# One generation of history: debug.log -> debug.log.old, and the .old that was
# there before is discarded.  Two files are all we keep.
LOG_OLD_SUFFIX = '.old'


def rotate_log(path=None, max_bytes=None):
    """Move the live log to `<path>.old`, discarding any previous .old.

    Returns True when a rotation happened.  `os.replace()` gives the requested
    order -- old is discarded, current becomes old -- in a single atomic rename,
    so there is never a moment in which no log file exists, and the caller's
    reopen creates the fresh current file.

    The size of the log itself is measured, not of whatever stdout happens to
    be: a tty (or a `-o/--stdout` run) must not rotate a file we are not writing.
    """
    path = prt_path if path is None else path
    if not path:
        return False
    limit = (prt_kb * 1024) if max_bytes is None else max_bytes
    try:
        if os.path.getsize(path) <= limit:
            return False
        os.replace(path, f'{path}{LOG_OLD_SUFFIX}')
        return True
    except OSError:
        return False

def copy_to_folder(resource_name, dest):
    """ Get the path of a resource """
    with pkg_resources.path('pwr_tray.resources', resource_name) as file_path:
        shutil.copy(file_path, os.path.join(dest, resource_name))
    return file_path


def resource_text(resource_name):
    """ Return the text of a packaged resource (e.g. 'commands.yaml'). """
    with pkg_resources.path('pwr_tray.resources', resource_name) as file_path, \
            open(file_path, encoding='utf-8') as handle:
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
    # Last resort: never let reporting an error raise a second one.
    with contextlib.suppress(OSError, ValueError, AttributeError):
        stream = sys.__stderr__ or sys.stderr
        stream.write(text)
        stream.flush()


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
     - if we create a log file, its size is limited to `prt_kb` (2M) and then it
       is rotated: debug.log becomes debug.log.old and the previous .old is
       discarded, so there are never more than two files.
     """
    def check_stdout(use_stdout=None):
        def is_tty():
            try:
                os.ttyname(sys.stdout.fileno())
                return True
            except (OSError, ValueError, AttributeError):
                return False
        def is_reg():
            try:
                return stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
            except (OSError, ValueError, AttributeError):
                return False
        def reopen():
            global prt_to_init

            # Capture the old stdout/stderr to close them
            old_stdout = sys.stdout
            old_stderr = sys.stderr

            # Open the new one.  It must outlive this function: it becomes
            # sys.stdout/stderr for the life of the process.
            new_f = open(prt_path, "a+", encoding='utf-8')  # noqa: SIM115

            # Redirect
            sys.stdout = new_f
            sys.stderr = new_f
            os.dup2(new_f.fileno(), 1)
            os.dup2(new_f.fileno(), 2)

            # CLOSE the old ones if they weren't the original system TTYs
            # This prevents the leak
            with contextlib.suppress(OSError, ValueError):
                if old_stdout and not old_stdout.isatty():
                    old_stdout.close()
                if old_stderr and old_stderr is not old_stdout and not old_stderr.isatty():
                    old_stderr.close()

            prt_to_init = False

        if prt_kb > 0 and prt_path: # non-positive disables stdout "tuning"
            should_reopen = (
                use_stdout is False
                or (prt_to_init and sys.stdout.closed)
                or (prt_to_init and not is_tty() and not is_reg()))
            if should_reopen:
                reopen()
            elif not is_tty() and rotate_log():
                # What we were appending to just became debug.log.old;
                # reopen() creates the new, empty current file.
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


# Terminals that can be asked to run a command, with the flag each one uses.
# 'string' means the flag takes the whole command line as one shell-quoted
# argument (GLib terminals), 'argv' means it takes it as separate arguments.
_TERMINALS = (
    ('x-terminal-emulator', '-e', 'argv'),
    ('gnome-terminal', '--', 'argv'),
    ('konsole', '-e', 'argv'),
    ('kitty', '-e', 'argv'),
    ('alacritty', '-e', 'argv'),
    ('foot', '-e', 'argv'),
    ('xterm', '-e', 'argv'),
    ('xfce4-terminal', '-e', 'string'),
    ('mate-terminal', '-e', 'string'),
    ('lxterminal', '-e', 'string'),
)

_less_follow_name = None


def terminal_argv(command, preferred=''):
    """Wrap `command` so it runs in a terminal window; [] if none can be found.

    The applet normally has no tty -- prt() has redirected fds 1 and 2 into
    debug.log -- so a full-screen program such as less cannot draw on what it
    inherits.  It has to be started inside a terminal emulator.

    `preferred` is the `log_viewer` setting: a command prefix such as
    'xterm -maximized -e' or 'gnome-terminal --'.  When it is empty the first
    installed terminal from _TERMINALS is used, with the flag it expects.
    """
    command = [str(part) for part in command]
    if preferred.strip():
        return shlex.split(preferred) + command
    for name, flag, style in _TERMINALS:
        exe = shutil.which(name)
        if not exe:
            continue
        if style == 'string':
            return [exe, flag, shlex.join(command)]
        return [exe, flag, *command]
    return []


def less_follow_name_supported():
    """True when the installed less knows --follow-name (less >= 451).

    With it, less reopens the log by name after a rotation instead of holding
    the renamed inode, which is what makes following debug.log survive our
    debug.log -> debug.log.old rename.
    """
    global _less_follow_name
    if _less_follow_name is None:
        _less_follow_name = False
        try:
            result = subprocess.run(['less', '--help'], capture_output=True,
                                    timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return _less_follow_name
        text = (result.stdout + result.stderr).decode('utf-8', 'replace')
        _less_follow_name = '--follow-name' in text
    return _less_follow_name


def log_viewer_argv(log_path, old_path=None, preferred=''):
    """argv that pages the applet log in a terminal window; [] if impossible.

    The current log is named first, so `less +F` starts following the live
    file; the rotated file (when it exists) follows and is reachable with `:n`.
    """
    command = ['less']
    if less_follow_name_supported():
        command.append('--follow-name')
    command.append('+F')
    command.append(log_path)
    if old_path and os.path.exists(old_path):
        command.append(old_path)
    return terminal_argv(command, preferred)


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
    """Find other instances of a script, and terminate one on request.

    There is deliberately no "kill every instance" entry point any more: a
    plain start must never shoot the incumbent (that is what let a systemd unit
    and a DE autostart restart-war forever).  Killing is the explicit
    ``--replace`` takeover path in ``main()``, and even then only a pid whose
    command line still names the target is signalled -- avoids suicide, and
    avoids hitting an unrelated process that recycled a stale pid.
    """

    @staticmethod
    def _iter_matches(targets):
        """Yield ``(pid, ps line, matched keys)`` for every live target.

        One ``ps`` call, shared by ``matching_pids()`` and ``kill_pid()`` so a
        pid can never be signalled on evidence the lookup would not accept.
        """
        wanted = {script_key(target) for target in targets}
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
            matched = wanted & names
            if matched:
                yield pid, line.strip(), matched

    @staticmethod
    def matching_pids(targets):
        """Return ``{pid: ps line}`` for live processes that look like targets."""
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        return {pid: line for pid, line, _ in PyKill._iter_matches(targets)}

    @staticmethod
    def kill_pid(pid, targets, grace=3.0, delay=0.1):
        """Terminate one process, but only if its argv still names a target.

        Returns True when the process is gone.  The name check is what makes
        ``--replace`` safe: the pid recorded in a lock file may since have
        been reused by an unrelated process, and a blind ``kill(2)`` would
        then hit the wrong program.  A pid whose command line does not match
        is never signalled.

        This replaces the old unconditional ``kill_loop()`` that ran before
        the lock was taken: killing an incumbent is a *takeover*, not the
        normal start-up path.
        """
        targets = targets if isinstance(targets, (list, tuple)) else [targets]
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        if pid not in PyKill.matching_pids(targets):
            return False
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
            try:
                os.kill(pid, sig)
            except OSError:
                return True      # already gone
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(delay)
                if pid not in PyKill.matching_pids(targets):
                    return True
        return pid not in PyKill.matching_pids(targets)


class SingleInstance:
    """Exclusive PID/lock file: the guarantee that two applets never coexist.

    The lock -- not the process-name scan -- is authoritative.  A start tries
    to take it *first*; if another live process holds it, the newcomer reports
    the holder and exits unless ``--replace`` was given.  The kernel drops the
    lock when the holder exits -- even on SIGKILL -- so a crash cannot leave a
    stale lock that blocks the next start.  The file's contents are
    informational (the holder's pid).
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
            with open(path, encoding='utf-8') as handle:
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
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = None

    @classmethod
    def release_current(cls):
        """Release the lock held by this process, if any."""
        if cls.current is not None:
            cls.current.release()
