#!/usr/bin/env python3
"""The user-action layer: every menu entry that starts a process.

Each action is a short sequence of state changes plus, usually, one shell line
from ``commands.yaml``.  The shell is the point: a command value is a *line*
that relies on ``~`` expansion, quotes and ``;`` sequences, so it must reach
``/bin/sh`` untouched.  That knowledge belongs with the commands, not inside
the applet's event loop, so it lives here and is driven by the tests in
``tests/test_actions.py``.

``Actions`` takes the applet explicitly instead of reaching for
``PwrTray.singleton``; ``PwrTray`` keeps thin static wrappers so the menu (and
the existing call sites) still work.
"""

import os
import subprocess
import sys
import time

from pwr_tray import Utils, service
from pwr_tray.Utils import prt

#: How long a `logoff` command may take before the applet stops waiting for it.
#: These tools only hand the request to their session manager -- one D-Bus call
#: or one compositor message -- so a wait this long means the request was never
#: made.  The wait happens with the Qt event loop blocked, which is the reason
#: it is bounded at all: an applet that cannot answer anything is exactly what a
#: logout waits for.
LOGOFF_TIMEOUT = 15

#: A `systemctl --no-block` call only queues a job and answers; it should
#: return in milliseconds.  The bound keeps a wedged user manager from freezing
#: the tray menu, which is the same reason every other wait here is bounded.
SYSTEMCTL_TIMEOUT = 5


class Actions:
    """Run the applet's user-facing actions against one applet instance."""

    def __init__(self, applet):
        self.applet = applet

    def run_command(self, key, timeout=None):
        """Run this desktop's command for `key` through the shell.

        The value in commands.yaml is a command *line*, not an argv: it relies
        on `~` expansion, on quotes and on `;` sequences --
        `sleep 1.0; exec xset dpms force off`, `swaymsg "output * dpms off"`.
        A shell is therefore the only correct reading.

        Returns the exit status, or None when the desktop leaves the key unbound
        or the command outlived `timeout`; `exit_wm()` reads it to decide whether
        the session really is on its way out.
        """
        command = self.applet.variables.get(key, None)
        if not command:
            return None
        prt(f'+ {command}')
        try:
            result = subprocess.run(command, check=False, shell=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            prt(f'   NOTE: no answer after {timeout:g}s')
            return None
        if result.returncode != 0:
            prt(f'   NOTE: returncode={result.returncode}')
        return result.returncode

    def quit_self(self):
        """Hide the tray icon and exit the process."""
        prt('+', 'quitting applet...')
        applet = self.applet
        if applet:
            applet.tray_icon.hide()
        # Under the unit, exiting is not enough: a unit configured with
        # Restart=always would bring the applet straight back.  Ask systemd
        # to stop the unit, from outside its cgroup, and let the event loop
        # end normally so atexit releases the lock and writes ENDED.
        if getattr(applet, 'svc_mode', False) and self._ask_systemd('stop'):
            app = getattr(applet, 'app', None)
            if app is not None:
                app.quit()
                return
        sys.exit()

    def restart_self(self):
        """Replace this process with a fresh instance of the applet.

        The replacement is launched as ``python -m pwr_tray.main`` so that
        PyKill recognises it via command_names().  The single-instance lock is
        released first: os._exit() skips atexit handlers, and the replacement
        must be able to take the lock as soon as it starts.

        The package root goes on PYTHONPATH rather than into ``cwd=``: the
        replacement inherits whatever directory the user is in and must still
        be able to import the module when running from a source checkout.

        Under the systemd unit this is a different operation.  The applet and
        everything it spawns share the unit's cgroup, which systemd kills when
        the main process exits, so a replacement started here would die with
        the old instance: that is why the menu used to act like "stop for the
        session".  Instead, ask systemd to restart the unit; the control
        command runs in its own transient scope (see Utils.spawn_detached), so
        it survives the stop it is requesting.  The applet then exits cleanly,
        and the unit's own ExecStart brings the fresh instance up.
        """
        prt('+', 'restarting applet...')
        applet = self.applet
        if applet:
            applet.tray_icon.hide()
            # os._exit() below skips atexit, which is where the monitors are
            # usually reaped; without this each restart orphans dbus-monitor.
            events = getattr(applet, 'events', None)
            if events is not None:
                events.stop()
        applet.save_picks()
        if getattr(applet, 'svc_mode', False):
            if self._ask_systemd('restart'):
                app = getattr(applet, 'app', None)
                if app is not None:
                    # Unlike the non-service path, this one goes through
                    # atexit: the lock is released and ENDED is written before
                    # systemd's start job launches the replacement.
                    app.quit()
                    return
            # No control command could be started.  A non-zero exit is the one
            # thing the unit's Restart=on-failure turns into a fresh instance
            # on its own, so use that instead of spawning into a cgroup
            # systemd is about to kill.
            sys.exit(1)
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
    def _ask_systemd(verb):
        """Ask the user manager to `verb` the unit this applet runs under.

        Returns True when the request was handed to systemd.  `--no-block`
        is what makes this safe from inside the unit: the job is queued before
        systemctl waits for anything, so the request has been accepted even
        though stopping the unit also signals the client.  The caller quits
        immediately after, and systemd carries the job out -- for `restart`
        that means the unit's own ExecStart starts the fresh instance.

        Failure is logged and reported, and the caller falls back to its own
        exit path so a missing systemctl cannot leave the tray running with a
        dead menu row.
        """
        argv = service.systemctl('--no-block', verb, service.UNIT_NAME)
        prt('+', ' '.join(argv))
        try:
            subprocess.run(argv, check=False, timeout=SYSTEMCTL_TIMEOUT,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            prt(f'systemd {verb} ERR: {exc}')
            return False
        return True

    def edit_config(self):
        applet = self.applet
        params = applet.get_params()
        if params.gui_editor:
            try:
                ini_path = applet.ini_tool.ini_path
                arguments = params.gui_editor.split()
                arguments.append(ini_path)
                prt('+', 'running:', arguments)
                # gui_editor is often a terminal emulator ('konsole -e vim'),
                # so this child is a toolkit application as well: left alone
                # it inherits our fds 1/2 -- debug.log -- and appends its own
                # raw Qt output where our message handler cannot filter it.
                subprocess.run(arguments, check=True,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                prt(f"Edit Config ERR: {e}")

    def open_log(self):
        """Page debug.log (plus the rotated copy) in a terminal window.

        Launched with Popen rather than run(): the viewer stays open for as
        long as the user keeps it open, and blocking the Qt event loop that
        long would freeze the tray, the idle polling and every other action.
        spawn_detached() puts the viewer in its own session -- and, under
        systemd, its own transient scope -- so a stop or restart of the applet
        does not take the log window down with it.

        The viewer is spawned quiet: prt() points our fds 1 and 2 at
        debug.log, so without redirection the terminal emulator inherits that
        file as its stdout and appends its own toolkit chatter -- konsole, a
        Qt6 application, dumps its palette table and layout warnings there in
        raw Qt format.  Those lines never pass through our Qt message handler,
        which only sees this process, so the redirection is the only fix.
        """
        applet = self.applet
        try:
            log_path = applet.ini_tool.log_path
            old_path = getattr(applet.ini_tool, 'log_old_path', None)
            preferred = getattr(applet.get_params(), 'log_viewer', '')
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
            Utils.spawn_detached(arguments, quiet=True)
        except (OSError, subprocess.SubprocessError) as e:
            prt(f'Open Log ERR: {e}')

    def suspend(self):
        applet = self.applet
        applet.set_state('Asleep')
        applet.reset_xidle_ms()

        locker_cmd = applet.variables.get('locker')

        # Logic: Only manually lock if we aren't in a 'Full' DE that handles it.
        # Most tiling WMs (niri, sway, i3) need the manual kick.
        is_full_de = any(name in applet.de_config['name']
                         for name in ['kde', 'xfce', 'cinnamon'])

        if locker_cmd and not is_full_de:
            prt(f'+ {locker_cmd} (async spawn)')
            subprocess.Popen(locker_cmd, shell=True)
            # Give the locker a tiny head-start to grab the screen
            time.sleep(0.2)

        # This is the "Point of No Return"
        self.run_command('suspend')
        prt('suspend: resumed')

    def poweroff(self):
        self.run_command('poweroff')

    def reboot(self):
        self.run_command('reboot')

    def lock_screen(self):
        self.run_command('locker')
        self.applet.update_running_idle_s()
        self.applet.set_state('Locked')

    def blank_primitive(self, lock_screen=False):
        applet = self.applet
        cmd = applet.variables['monitors_off']
        if cmd and applet.get_params().turn_off_monitors:
            if lock_screen:
                self.lock_screen()
            prt('+', cmd)
            result = subprocess.run(cmd, shell=True, check=False)
            if result.returncode != 0:
                prt(f'   NOTE: returncode={result.returncode}')
            applet.set_state('Blanked')
        else:
            prt('NOTE: blanking screen unsupported')

    def blank_quick(self):
        self.blank_primitive(lock_screen=True)

    def reload_wm(self):
        self.run_command('reload_wm')

    def restart_wm(self):
        self.run_command('restart_wm')

    def exit_wm(self):
        """Log out, then leave instead of being left behind by it.

        A logout asks the desktop to end the session; it does not kill this
        process, which keeps running until the session's own teardown reaches it
        -- and on a full desktop that teardown waits for the processes in the
        session.  Measured on kde-x11: `qdbus6 org.kde.Shutdown … logout` took
        the session's services down in the same second, and the logout only
        finished two and a half minutes later, when this applet was killed by
        hand.  Leaving here is what turns that wait into a clean end.

        Only on success: a desktop whose tool is missing, or a logout the user
        cancels in the desktop's own prompt, returns a non-zero status, and the
        applet must not disappear for a logout that never happened.  The wait is
        bounded (LOGOFF_TIMEOUT) for the same reason.

        `app.quit()` rather than `sys.exit()`: releasing the single-instance
        lock, stopping the monitors and writing the ENDED line all live in
        atexit.  Exit 0 is also final for `Restart=on-failure`, so nothing
        respawns into a session that is draining.
        """
        if self.run_command('logoff', timeout=LOGOFF_TIMEOUT) != 0:
            return
        app = getattr(self.applet, 'app', None)
        if app is not None:
            prt('+ log out accepted; leaving the session')
            app.quit()

    def enable_presentation_mode(self):
        self._set_mode('Presentation')

    def enable_nosleep_mode(self):
        self._set_mode('LockOnly')

    def enable_normal_mode(self):
        self._set_mode('SleepAfterLock')

    def _set_mode(self, mode):
        applet = self.applet
        applet.mode = mode
        applet.was_output = 'Changed Mode'
        prt('+', f'this.mode={applet.mode!r}')
        applet.save_picks()
        applet.idle_manager_start()
