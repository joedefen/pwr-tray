#!/usr/bin/env python3
"""Battery and AC (mains) state detection straight from sysfs.

``psutil.sensors_battery()`` derives ``power_plugged`` from
``/sys/class/power_supply/AC/online`` (or ``AC0/online``) alone -- see
``psutil._pslinux``.  On some machines that file goes stale: notably
Chromebooks on the ``cros_ec`` driver, which also expose extra
``CROS_USBPD_CHARGER*`` supplies, leave ``AC/online=1`` while the battery
reports ``status=discharging``.  A laptop running on its battery then still
looks "plugged in", which is exactly the wrong state for a power applet.

This module reads sysfs itself and cross-checks the mains ``online`` flag
against the battery's own ``status`` file, trusting the battery when the two
disagree (the battery says which way the power is actually flowing).  psutil
is only a fallback, for when sysfs cannot be read at all.

``read_power()`` is also what ``pwr-tray --battery`` prints, so a wrong state
can be attributed to the kernel rather than to the applet.
"""

import os
from types import SimpleNamespace

try:
    import psutil
except ImportError:      # pragma: no cover - psutil is a hard dependency
    psutil = None

SYSFS_ROOT = '/sys/class/power_supply'

# Battery statuses that mean mains power is being supplied.
PLUGGED_STATUSES = ('charging', 'full', 'not charging')


def _read(path, default=None):
    """Read one (small) sysfs file, returning ``default`` when absent."""
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read().strip()
    except OSError:
        return default


def _read_int(path, default=None):
    """Read one sysfs file as an int, returning ``default`` on any trouble."""
    text = _read(path)
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _read_supplies(root=SYSFS_ROOT):
    """Return one SimpleNamespace per power supply device under ``root``."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    supplies = []
    for name in names:
        base = os.path.join(root, name)
        stype = _read(os.path.join(base, 'type'))
        if not stype:
            continue        # not a power supply (or it vanished mid-scan)
        supplies.append(SimpleNamespace(
            name=name,
            type=stype.lower(),
            online=_read_int(os.path.join(base, 'online')),
            status=(_read(os.path.join(base, 'status'), '') or '').lower(),
            present=_read_int(os.path.join(base, 'present')),
            capacity=_read_int(os.path.join(base, 'capacity')),
            # charge_* on some drivers, energy_* on others
            energy_now=(_read_int(os.path.join(base, 'energy_now'))
                        or _read_int(os.path.join(base, 'charge_now'))),
            energy_full=(_read_int(os.path.join(base, 'energy_full'))
                         or _read_int(os.path.join(base, 'charge_full'))),
        ))
    return supplies


def _battery_percent(batteries):
    """Percent remaining, preferring the energy/charge ratio over capacity."""
    now = [bat.energy_now for bat in batteries]
    full = [bat.energy_full for bat in batteries]
    if (all(val is not None for val in now)
            and all(val is not None and val > 0 for val in full)):
        total_full = sum(full)
        if total_full:
            return round(100.0 * sum(now) / total_full, 1)
    caps = [bat.capacity for bat in batteries if bat.capacity is not None]
    if caps:
        return round(sum(caps) / len(caps), 1)
    return None


def _combined_status(batteries):
    """Collapse the per-battery ``status`` files into one word."""
    statuses = [bat.status for bat in batteries if bat.status]
    for status in ('discharging', 'charging', 'full', 'not charging'):
        if status in statuses:
            return status
    return statuses[0] if statuses else ''


def _summarize(supplies):
    """Short per-device strings, for the debug log and ``--battery``."""
    rv = []
    for sup in supplies:
        bits = [sup.type]
        if sup.online is not None:
            bits.append(f'online={sup.online}')
        if sup.status:
            bits.append(sup.status)
        if sup.capacity is not None:
            bits.append(f'{sup.capacity}%')
        rv.append(f'{sup.name}({",".join(bits)})')
    return rv


def read_power(root=SYSFS_ROOT):
    """Detect the power situation.

    Returns a SimpleNamespace with:

    ``present``   True (a battery was found), False (none: a desktop) or None
                  (could not tell).
    ``plugged``   True/False/None (None == unknown).
    ``percent``   0.0..100.0, or None when no battery reported a charge.
    ``status``    the battery's status word ('charging', 'discharging', ...).
    ``charging``  shortcut for ``status == 'charging'``.
    ``conflict``  True when the mains flag and the battery status disagreed.
    ``source``    'sysfs' or 'psutil'.
    ``supplies``  list of short strings naming every device that was read.
    """
    supplies = _read_supplies(root)
    if not supplies:
        return _from_psutil()

    batteries = [sup for sup in supplies
                 if sup.type == 'battery' and sup.present != 0]
    mains = [sup for sup in supplies if sup.type == 'mains']

    status = _combined_status(batteries)
    percent = _battery_percent(batteries) if batteries else None
    ac_online = any(sup.online == 1 for sup in mains) if mains else None
    conflict = False

    if not batteries:
        # A desktop or server: no battery, so the mains flag is the answer.
        plugged = True if ac_online is None else ac_online
    elif ac_online is not None:
        plugged = ac_online
        # The battery knows which way the power is actually flowing.
        if status == 'discharging' and plugged:
            plugged, conflict = False, True
        elif status == 'charging' and not plugged:
            plugged, conflict = True, True
    elif status in PLUGGED_STATUSES:
        plugged = True
    elif status == 'discharging':
        plugged = False
    else:
        plugged = None

    return SimpleNamespace(
        present=bool(batteries),
        plugged=plugged,
        percent=percent,
        status=status,
        charging=status == 'charging',
        conflict=conflict,
        source='sysfs',
        supplies=_summarize(supplies),
    )


def _from_psutil():
    """Fallback used when sysfs is unreadable (containers, odd kernels)."""
    try:
        battery = psutil.sensors_battery() if psutil else None
    except Exception:      # noqa: BLE001 - psutil raises platform-specific errors
        battery = None
    if battery is None:
        return SimpleNamespace(present=None, plugged=None, percent=None,
                               status='', charging=False, conflict=False,
                               source='psutil', supplies=[])
    return SimpleNamespace(
        present=True,
        plugged=battery.power_plugged,
        percent=round(battery.percent, 1),
        status='',
        charging=False,
        conflict=False,
        source='psutil',
        supplies=['psutil'],
    )


def describe(power):
    """One-line summary, used by the debug log and by ``--battery``."""
    def fmt(value):
        return '?' if value is None else str(value)

    bits = [f'present={fmt(power.present)}',
            f'plugged={fmt(power.plugged)}',
            f'percent={fmt(power.percent)}',
            f'status={power.status or "-"}',
            f'source={power.source}']
    if power.conflict:
        bits.append('CONFLICT(mains-vs-battery)')
    if power.supplies:
        bits.append('[' + ' '.join(power.supplies) + ']')
    return 'power: ' + ' '.join(bits)


if __name__ == '__main__':
    print(describe(read_power()))
