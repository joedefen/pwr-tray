#!/usr/bin/env python3
"""The idle state machine: idle time in, one policy decision out.

``PwrTray.on_timeout()`` is a scheduler: it turns dirty flags into a handful of
queries and, every ``loop_sample`` ticks, asks this class what to do about the
idle clock.  The decision -- lock, blank, suspend/power off, reset the idle
clock, or go back to awake -- is the part with the branch ladder, and it is
kept here so it can be read and tested without a tray, a Qt timer or a running
process.

The class drives the applet through the same methods the menu does
(``lock_screen``, ``suspend``, ``set_state``, ...), so the seam is the public
surface of ``PwrTray`` rather than its internals.
"""

import subprocess

from pwr_tray.Utils import prt


class IdleStateMachine:
    """Evaluate idle time against the current mode and act on the transition."""

    def __init__(self, applet):
        self.applet = applet

    def set_state(self, name):
        """Set the state of the applet."""
        applet = self.applet
        prt(f'set_state: {name},{applet.running_idle_s}s',
            f'was: {applet.state.name},{applet.state.when}s')
        applet.state.name = name
        applet.state.when = applet.running_idle_s

    def update_running_idle_s(self):
        """Update the running idle seconds (called after each regular timeout)."""
        applet = self.applet
        cmd = applet.variables['get_idle_ms']
        scale = 1
        if not cmd:
            cmd = applet.variables.get('get_idle_s', '')
            scale = 1000
        if cmd:
            try:
                xidle = int(subprocess.check_output(cmd.split()).strip())
                xidle_ms = xidle * scale
                xidle_ms *= 2 if applet.quick else 1  # time warp
                applet.running_idle_s = round(xidle_ms/1000, 3)
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                prt(f'WARN: idle time command failed: {e}')

    def tick(self):
        """Run the once-per-sample branch ladder for the current idle time."""
        applet = self.applet
        applet.update_running_idle_s()
        # "Locking Soon" is an idle-time transition with no D-Bus signal.
        applet.show_icon(inhibited=applet.was_inhibited or '')
        lock_secs = applet.get_lock_min_list()[0]*60
        down_secs = applet.get_sleep_min_list()[0]*60 + lock_secs
        blank_secs = 5 if applet.quick else 20

        emit = (f'idle_s={applet.running_idle_s}'
                f' state={applet.state.name},{applet.state.when}s')
        emode = applet.get_effective_mode()
        if emode in ('LockOnly', 'SleepAfterLock'):
            emit += f' @{applet.get_lock_min_list()[0]}m'
        if emode in ('SleepAfterLock', ):
            emit += f'+{applet.get_sleep_min_list()[0]}m'
        if applet.battery.selector != 'Settings':
            emit += f' {applet.battery.selector}'
        prt(emit)

        if emode in ('Presentation',) or applet.was_inhibited:
            if applet.idle_manager:
                applet.reset_xidle_ms()  # idle_manager handles timeouts
            elif applet.running_idle_s > min(50, lock_secs*0.40):
                applet.reset_xidle_ms()  # we don't know when

        elif (applet.running_idle_s >= down_secs and emode not in ('LockOnly',)
                and applet.state.name in ('Awake', 'Locked', 'Blanked')):
            if applet.get_params().power_down:
                applet.poweroff(None)
            else:
                applet.suspend(None)

        elif (applet.running_idle_s >= lock_secs and emode not in ('Presentation',)
                and applet.state.name in ('Awake', 'Dim')):
            applet.lock_screen(None)

        elif (applet.running_idle_s >= applet.state.when + blank_secs
                and applet.get_params().turn_off_monitors
                and emode not in ('Presentation',)
                and applet.state.name in ('Locked',)):
            applet.blank_primitive()

        elif applet.running_idle_s < lock_secs and applet.state.name not in ('Awake', ):
            applet.set_state('Awake')
