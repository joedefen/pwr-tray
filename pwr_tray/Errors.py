#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Errors that main() reports to the user instead of dumping a traceback.

These replace the `assert` guards that used to protect startup detection.  An
assertion is the wrong tool here twice over: it shows a stack trace to a user
who can only act on the message, and it disappears under `python -O`, which
let the application start with empty command strings and silently do nothing.

Every exception below carries enough context to build its own actionable text,
so main() only has to print it and exit.
"""


class PwrTrayError(Exception):
    """Base class for startup errors that are reported, not traced."""


class DesktopDetectionError(PwrTrayError):
    """No desktop environment matched the session, or --de named an unknown one.

    Carries the environment that was inspected and the desktop names the
    config knows about, so the message can point at `--de` and `--list-des`.
    """

    def __init__(self, summary, known_desktops=(), env=None):
        self.summary = summary
        self.known_desktops = sorted(known_desktops)
        self.env = dict(env or {})
        super().__init__(self.format_message())

    def format_message(self):
        """Build the full user-facing message."""
        lines = [f'pwr-tray: {self.summary}']
        session = ' '.join(f'{key}={value!r}'
                           for key, value in self.env.items() if value)
        if session:
            lines.append(f'  session: {session}')
        if self.known_desktops:
            lines.append('  known desktops:'
                         f' {", ".join(self.known_desktops)}')
        lines.append('  list them at any time with:  pwr-tray --list-des')
        lines.append('  or force the right one with: pwr-tray --de NAME')
        return '\n'.join(lines)


class MissingCommandsError(PwrTrayError):
    """Commands required by the detected desktop are not on $PATH."""

    def __init__(self, missing, desktop='', session_type=''):
        self.missing = list(missing)
        self.desktop = desktop
        self.session_type = session_type
        super().__init__(self.format_message())

    def format_message(self):
        """Build the full user-facing message."""
        where = ''
        if self.desktop:
            where = f' for {self.desktop}'
            if self.session_type:
                where += f' ({self.session_type})'
        lines = [
            f'pwr-tray: missing command(s){where}: {", ".join(self.missing)}',
            '  install them with your package manager, then start pwr-tray'
            ' again.',
            '  systemctl is always required; playerctl is optional'
            ' (media inhibit).',
            '  see what every desktop needs: pwr-tray --list-des',
        ]
        return '\n'.join(lines)
