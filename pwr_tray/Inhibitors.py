#!/usr/bin/env python3
"""What logind says is holding the session awake, asked as D-Bus questions.

logind answers this twice, and the two answers have different jobs:

* ``BlockInhibited`` -- a colon-separated list of the operations that are
  blocked (``""`` when nothing is).  This is the *verdict*: it is a machine
  field, so column layout, locale and the presence of a listing tool cannot
  change it.  :class:`pwr_tray.PowerEvents.PowerEvents` keeps it up to date
  from the signal stream; :func:`blocked_operations` and
  :func:`parse_block_inhibited` are the two shapes that value arrives in.
* ``ListInhibitors`` -- who, why, how long, for the menu.  Display only.  A
  ``QDBusInterface`` call answers it in-process, so the who/why rows no longer
  cost a ``systemd-inhibit`` fork; the two columns logind does not answer
  (USER, COMM) are looked up in ``pwd`` and ``/proc``, so a row reads exactly
  like the one systemd-inhibit prints.

Every bus-using function here degrades to ``None`` -- the caller keeps its
subprocess fallback -- because a missing binding or a wedged bus must cost
rows, never the verdict.  PyQt5 is already a hard dependency and supplies
QtDBus, so none of this adds a package.
"""

import pwd

GUARDED_OPERATIONS = frozenset({'sleep', 'idle'})

LOGIND_SERVICE = 'org.freedesktop.login1'
LOGIND_PATH = '/org/freedesktop/login1'
LOGIND_MANAGER = 'org.freedesktop.login1.Manager'
BLOCK_INHIBITED = 'BlockInhibited'


def parse_block_inhibited(output):
    """Return the value of ``busctl get-property ... BlockInhibited``.

    busctl prints one line: ``s "sleep:idle"``, or ``s ""`` when nothing is
    blocked.  ``''`` and ``None`` are different answers -- the first means
    logind says nothing is blocked, the second means the answer could not be
    read -- and callers must never treat them alike.
    """
    lines = output.strip().splitlines()
    text = lines[0].strip() if lines else ''
    if not text.startswith('s "') or len(text) < 4 or not text.endswith('"'):
        return None
    return text[3:-1]


def blocked_operations(value):
    """Split a BlockInhibited value into operation names."""
    return {token for token in value.split(':') if token}


def _user_name(uid):
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except (KeyError, OSError, ValueError, TypeError):
        return str(uid)


def _command_name(pid):
    """The process name, which logind does not report but the table shows."""
    try:
        with open(f'/proc/{int(pid)}/comm', encoding='utf-8') as handle:
            return handle.read().strip() or '-'
    except (OSError, ValueError, TypeError):
        return '-'


def format_row(record):
    """One row in the shape ``systemd-inhibit --no-legend`` prints.

    ``WHO UID USER PID COMM WHAT WHY MODE``.  A row that cannot be read is
    skipped by the caller, never guessed at: the verdict does not come from
    here.
    """
    what, who, why, mode, uid, pid = record
    return (f'{who} {uid} {_user_name(uid)} {pid} {_command_name(pid)}'
            f' {what} {why} {mode}')


def _logind_interface():
    """logind's Manager interface, or None when QtDBus cannot supply it."""
    try:
        # pylint: disable=import-outside-toplevel
        from PyQt5.QtDBus import QDBusConnection, QDBusInterface
    except ImportError:
        return None
    try:
        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            return None
        interface = QDBusInterface(LOGIND_SERVICE, LOGIND_PATH,
                                   LOGIND_MANAGER, bus)
        # The default is 25 s, which would freeze the tray if the bus wedged.
        interface.setTimeout(2000)
        return interface
    # QDBusError has no import-safe base class, so this one has to be broad.
    except Exception:                       # noqa: BLE001
        return None


def bus_rows(blocked):
    """Inhibitor rows from logind's ListInhibitors; None when unavailable.

    ``blocked`` is the operation set behind the verdict: only rows blocking
    one of those operations are kept, so a compositor holding the power key
    does not read as "keep this session awake" -- the same rule the
    ``--mode=block`` listing applied.
    """
    interface = _logind_interface()
    if interface is None:
        return None
    try:
        reply = interface.call('ListInhibitors')
        records = reply.arguments()[0] if reply.arguments() else []
    except Exception:                       # noqa: BLE001
        return None

    rows = [f'blocked: {" ".join(sorted(blocked))}']
    for record in records:
        try:
            # A row this cannot read is skipped, never guessed at: the verdict
            # does not come from here.
            if len(record) != 6:
                continue
            what = str(record[0])
        except (IndexError, TypeError):
            continue
        if blocked & set(what.split(':')):
            rows.append(format_row(record))
    return rows
