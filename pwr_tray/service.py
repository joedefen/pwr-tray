#!/usr/bin/env python3
"""Install, inspect and remove the pwr-tray ``systemd --user`` unit.

The unit files in ``pwr_tray/resources/systemd/`` are the single source of
truth.  They ship inside the wheel (flit includes everything under the module
directory), they are validated by ``tests/test_service_examples.py``, and
``--svc-install`` copies the one matching the current desktop to
``~/.config/systemd/user/pwr-tray.service``.

Two facts are only knowable on the machine that performs the install -- where
the ``pwr-tray`` executable actually lives, and which display the session is
on -- so :func:`render` rewrites just those.  Everything else is copied byte
for byte, which is what lets the tests keep asserting the shipped text.

The units capture stdout/stderr into ``%h/.local/log``, and systemd will not
create that directory: it fails every process with ``EXIT_STDOUT`` (209)
before ``ExecStart=`` is reached.  :func:`missing_dirs` reads the paths back
out of the unit so ``--svc-install`` can create them rather than leaving the
user with a unit that cannot start.

Nothing here shells out to systemctl on its own: the verbs are built as
:class:`Step` lists by the ``plan_*()`` functions and performed by
:func:`execute`, with the subprocess call injected.  That keeps the whole
policy testable on a machine without a systemd session.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pwr_tray.Errors import PwrTrayError
from pwr_tray.Utils import SingleInstance

UNIT_NAME = 'pwr-tray.service'
COMMAND = 'systemctl'
EXECUTABLE = 'pwr-tray'

#: Directory holding the units shipped in the wheel.  Tests repoint this.
SOURCE_DIR = Path(__file__).resolve().parent / 'resources' / 'systemd'

# `--svc-status` exit codes: the one verb meant to be used from `if`.
STATUS_ACTIVE = 0
STATUS_INACTIVE = 1
STATUS_ABSENT = 2

OK = 0
FAILED = 1

#: Session variables a unit pins to one value; install-time overrides them.
SESSION_KEYS = ('WAYLAND_DISPLAY', 'DISPLAY')

_DE_PATTERN = re.compile(r'--de[ =]([A-Za-z0-9-]+)')

#: ``StandardOutput=append:/path`` and friends: systemd opens these itself.
_OUTPUT_RE = re.compile(
    r'^Standard(?:Output|Error)=(?:append|file|truncate):(\S+)$', re.MULTILINE)


class ServiceError(PwrTrayError):
    """A ``--svc-*`` request that cannot be carried out as stated."""


@dataclass(frozen=True)
class Step:
    """One effect, in the order the verbs have to perform them.

    Exactly one of ``argv`` (run a command), ``write`` (create/replace a file),
    ``mkdir`` (create a directory) or ``remove`` (delete a file) is set;
    ``describe`` is what the user is shown before anything happens, and
    ``hint`` is appended to the error if the step turns out to fail.
    """

    describe: str
    argv: tuple = ()
    write: tuple = None
    remove: Path = None
    mkdir: Path = None
    hint: str = ''


@dataclass
class Plan:
    """What a verb intends to do, plus anything the user should know first."""

    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    details: list = field(default_factory=list)
    prompt: str = ''


@dataclass
class Status:
    """Current on-disk and systemd state of the installed unit."""

    path: Path
    installed: bool
    enabled: bool
    active: bool
    up_to_date: bool
    failed: bool


def _env(env=None):
    return dict(os.environ if env is None else env)


def run_command(argv):
    """Run one command and return ``(exit status, combined output)``."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, check=False)
    except OSError as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout or '') + (completed.stderr or '')


def systemctl(*args):
    """Build a ``systemctl --user`` argv tuple."""
    return (COMMAND, '--user', *args)


def systemd_available(env=None):
    """True if a user manager can be talked to on this machine."""
    return Path('/run/systemd/system').is_dir() and bool(_env(env).get('XDG_RUNTIME_DIR'))


def unit_dir(env=None):
    """Return ``$XDG_CONFIG_HOME/systemd/user`` (defaulting to ~/.config)."""
    base = _env(env).get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'systemd' / 'user'


def installed_unit(env=None):
    """Return the path the unit is installed to."""
    return unit_dir(env) / UNIT_NAME


def read_unit(path):
    """Read a unit file as text."""
    return Path(path).read_text(encoding='utf-8')


def desktop_of(text):
    """Return the compound desktop name a unit forces via ``--de``."""
    match = _DE_PATTERN.search(text)
    return match.group(1) if match else None


def templates(source=None):
    """Map every shipped compound desktop name to its unit file."""
    folder = SOURCE_DIR if source is None else Path(source)
    found = {}
    for path in sorted(folder.glob('pwr-tray-*.service')):
        desktop = desktop_of(read_unit(path))
        if desktop:
            found[desktop] = path
    return found


def template_for(desktop, source=None):
    """Return the shipped unit file for `desktop`, or explain what exists."""
    available = templates(source)
    try:
        return available[desktop]
    except KeyError:
        shipped = ', '.join(sorted(available)) or 'none'
        raise ServiceError(
            f'no shipped systemd unit for desktop {desktop!r};'
            f' shipped units cover: {shipped}') from None


def executable_path(env=None):
    """Return the absolute path of the pwr-tray executable to put in a unit."""
    found = shutil.which(EXECUTABLE, path=_env(env).get('PATH'))
    if not found:
        raise ServiceError(
            f'{EXECUTABLE} is not on $PATH, so a unit cannot name it;'
            ' install pwr-tray first (or fix $PATH)')
    return str(Path(found).resolve())


def render(text, desktop, executable, env=None):
    """Return `text` with the facts of this machine substituted in.

    Two edits only:

    * the executable named by ``ExecStart=`` becomes its real absolute path;
    * a session variable the unit pins (``Environment=WAYLAND_DISPLAY=wayland-1``)
      is overridden from the live environment by appending an ``Environment=``
      line.  The last assignment wins in systemd, so the shipped line stays
      readable and still documents the default.

    Every other byte is preserved, so the tests can keep asserting the shipped
    file and the installed file with the same parser.
    """
    env = _env(env)
    overrides = []
    for key in SESSION_KEYS:
        value = env.get(key)
        pinned = re.search(rf'^Environment={key}=(.*)$', text, re.MULTILINE)
        if not value or pinned is None:
            continue    # nothing pinned to correct, so nothing to correct
        if pinned.group(1).strip() == value:
            continue    # the shipped default is already right
        overrides.append(f'Environment={key}={value}')

    lines = text.splitlines()
    result = []
    replaced = False
    for line in lines:
        if line.startswith('ExecStart=') and not replaced:
            result.extend(overrides)
            result.append(f'ExecStart={_quote(executable)}{_arguments(line)}')
            replaced = True
            continue
        result.append(line)
    if not replaced:
        raise ServiceError(f'the {desktop!r} unit has no ExecStart= line')
    return '\n'.join(result) + ('\n' if text.endswith('\n') else '')


def _quote(path):
    return f'"{path}"' if ' ' in path else path


def _arguments(line):
    """Return the arguments of an ``ExecStart=`` line, with a leading space."""
    rest = line[len('ExecStart='):].split()
    return f" {' '.join(rest[1:])}" if len(rest) > 1 else ''


def rendered_unit(desktop, env=None, script=None, source=None):
    """Return the unit text that would be installed for `desktop`."""
    template = template_for(desktop, source)
    executable = script or executable_path(env)
    return render(read_unit(template), desktop, executable, env)


def probe(rendered=None, env=None, run=run_command):
    """Return the current state of the installed unit."""
    path = installed_unit(env)
    installed = path.is_file()
    enabled = active = failed = False
    if installed:
        enabled = run(systemctl('is-enabled', '--quiet', UNIT_NAME))[0] == 0
        active = run(systemctl('is-active', '--quiet', UNIT_NAME))[0] == 0
        # A unit the start rate limiter gave up on stays failed until it is
        # reset; `start` reports "attempted too often" until then.
        failed = run(systemctl('is-failed', '--quiet', UNIT_NAME))[0] == 0
    up_to_date = False
    if installed and rendered is not None:
        try:
            up_to_date = path.read_text(encoding='utf-8') == rendered
        except OSError:
            up_to_date = False
    return Status(path=path, installed=installed, enabled=enabled,
                  active=active, up_to_date=up_to_date, failed=failed)


def changed_lines(current, wanted):
    """Return ``-``/``+`` lines describing how `current` differs from `wanted`."""
    old = current.splitlines()
    new = wanted.splitlines()
    spare = sum(1 for line in old if line not in new) + sum(1 for line in new if line not in old)
    if spare > 12:
        return [f'  ({spare} lines differ)']
    lines = [f'- {line}' for line in old if line not in new]
    lines += [f'+ {line}' for line in new if line not in old]
    return lines


def _expand(path, env):
    """Expand the ``%h``/``%U`` specifiers systemd would expand in a path.

    ``%%`` is left alone: no shipped unit uses it, and guessing wrong is worse
    than reporting a directory that does not exist.
    """
    home = env.get('HOME') or str(Path.home())
    return path.replace('%h', home).replace('%U', str(os.getuid()))


def output_dirs(text, env=None):
    """Return the directories a unit makes systemd open a log file inside.

    Only directives that name a file are considered.  ``journal``, ``null``,
    ``inherit`` and friends name no path, so they need no directory and are
    skipped rather than guessed at.
    """
    env = _env(env)
    folders = []
    for match in _OUTPUT_RE.finditer(text):
        path = Path(_expand(match.group(1), env))
        if path.is_absolute() and path.parent not in folders:
            folders.append(path.parent)
    return folders


def missing_dirs(text, env=None):
    """Return the directories `text` needs that do not exist yet."""
    return [folder for folder in output_dirs(text, env) if not folder.is_dir()]


def plan_install(rendered, status, force=False, holder=None, env=None):
    """Write the unit if needed, reload, and enable it (without starting)."""
    plan = Plan()
    target = status.path
    wrote = False
    if status.installed and status.up_to_date:
        plan.notes.append(f'{target} is already up to date')
    elif status.installed:
        try:
            plan.details = changed_lines(read_unit(target), rendered)
        except OSError:
            plan.details = []
        plan.steps.append(Step(f'write {target}', write=(target, rendered)))
        wrote = True
        plan.prompt = f'{target} exists and differs from the shipped unit; replace it?'
    else:
        plan.steps.append(Step(f'write {target}', write=(target, rendered)))
        wrote = True
    # systemd does not create the directories it is told to log into, it fails
    # the unit with EXIT_STDOUT (209), so install time is when they must exist.
    for folder in missing_dirs(rendered, env):
        plan.steps.append(Step(f'create {folder}', mkdir=folder))
    if wrote:
        plan.steps.append(Step('reload the unit files', argv=systemctl('daemon-reload')))
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if not status.enabled:
        plan.steps.append(Step(f'enable {UNIT_NAME}', argv=systemctl('enable', UNIT_NAME)))
    if holder and not status.active:
        plan.notes.append(
            f'pwr-tray is already running outside systemd (pid {holder}); it keeps'
            ' running, and the unit takes over at your next login')
    return plan


def plan_start(rendered, status, force=False, holder=None, env=None):
    """Install if needed, enable, and start now."""
    plan = plan_install(rendered, status, force=force, holder=holder, env=env)
    if status.active:
        plan.notes.append(f'{UNIT_NAME} is already running')
        return plan
    if holder:
        raise ServiceError(
            f'pwr-tray is already running outside systemd (pid {holder}); the unit'
            ' would exit 3 against the single-instance lock.  Remove the desktop'
            ' autostart that started it (or quit that instance) and run this again')
    plan.steps.append(Step(
        f'start {UNIT_NAME}',
        argv=systemctl('start', UNIT_NAME),
        hint=(f'inspect it with: systemctl --user status {UNIT_NAME}'
              f' or: journalctl --user -xeu {UNIT_NAME}')))
    return plan


def plan_stop(status):
    """Stop the unit; it stays enabled, so it returns at the next login."""
    plan = Plan()
    if not status.installed:
        raise ServiceError(f'{UNIT_NAME} is not installed, so there is nothing to stop')
    if not status.active:
        plan.notes.append(f'{UNIT_NAME} is not running')
        return plan
    plan.steps.append(Step(f'stop {UNIT_NAME}', argv=systemctl('stop', UNIT_NAME)))
    plan.notes.append(f'{UNIT_NAME} stays enabled; it starts again at your next login')
    return plan


def plan_uninstall(status, force=False):
    """Stop, disable and delete the unit, leaving config and logs alone."""
    plan = Plan()
    if not status.installed:
        plan.notes.append(f'{UNIT_NAME} is not installed; nothing to do')
        return plan
    if not (status.up_to_date or force):
        raise ServiceError(
            f'{status.path} was edited after it was installed, so it is not'
            ' pwr-tray\'s to delete; remove it yourself, or pass --force')
    if status.active:
        plan.steps.append(Step(f'stop {UNIT_NAME}', argv=systemctl('stop', UNIT_NAME)))
    if status.enabled:
        plan.steps.append(Step(f'disable {UNIT_NAME}', argv=systemctl('disable', UNIT_NAME)))
    plan.steps.append(Step(f'remove {status.path}', remove=status.path))
    plan.steps.append(Step('reload the unit files', argv=systemctl('daemon-reload')))
    plan.notes.append('config.ini, picks and logs are left in place')
    return plan


def execute(plan, run=run_command):
    """Perform every step of `plan` in order; the plan was already shown."""
    for step in plan.steps:
        if step.write:
            path, content = step.write[0], step.write[1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        elif step.mkdir:
            step.mkdir.mkdir(parents=True, exist_ok=True)
        elif step.remove:
            step.remove.unlink(missing_ok=True)
        elif step.argv:
            code, output = run(step.argv)
            if code != 0:
                message = f'{step.describe} failed (exit {code}): {output.strip()}'
                raise ServiceError(f'{message}\n  {step.hint}' if step.hint else message)


def applet_holder(lock_dir):
    """Return the pid of a live pwr-tray holding the single-instance lock.

    The lock is authoritative here, not a ``ps`` scan: a command line that
    merely *mentions* pwr-tray -- the shell running ``pwr-tray --svc-start``, an
    editor, ``tail`` on the log -- must not look like a running applet, and a
    stale pid file must not either.  Holding the lock is the only thing that
    makes the next start exit 3, which is the case worth warning about.
    """
    path = Path(lock_dir) / 'pwr-tray.lock'
    if not path.exists():
        return None
    lock = SingleInstance(str(path))
    if lock.acquire():
        lock.release()
        return None
    return SingleInstance.read_pid(lock.path)


def _no_prompt(_question):
    return False


def _auto_yes(_question):
    return True


def dispatch(verb, desktop, env=None, force=False, yes=False, echo=print,
             ask=None, run=run_command, holder=None, script=None,
             systemd=None):
    """Carry out one ``--svc-*`` verb and return a process exit status."""
    env = _env(env)
    ask = (ask or _no_prompt)
    if yes:
        ask = _auto_yes

    if verb == 'status':
        return _report(desktop, env, echo, run, script)

    if not (systemd_available(env) if systemd is None else systemd):
        raise ServiceError(
            'no systemd user manager is reachable (is this a systemd system?);'
            ' use your desktop autostart instead')

    rendered = None
    if verb != 'uninstall':
        rendered = rendered_unit(desktop, env, script=script)
    else:
        rendered = _rendered_if_possible(desktop, env, script)

    status = probe(rendered=rendered, env=env, run=run)
    if verb == 'install':
        plan = plan_install(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'start':
        plan = plan_start(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'stop':
        plan = plan_stop(status)
    elif verb == 'uninstall':
        plan = plan_uninstall(status, force=force)
    else:
        raise ServiceError(f'unknown service verb {verb!r}')

    _show(plan, echo)
    accepted = True
    if plan.prompt and plan.steps and not force:
        accepted = ask(plan.prompt)
    if not accepted:
        echo('  left unchanged')
        return FAILED
    if not plan.steps:
        return OK
    execute(plan, run=run)
    return OK


def _rendered_if_possible(desktop, env, script):
    """Best-effort render, for verbs that must work without a reachable install."""
    try:
        return rendered_unit(desktop, env, script=script)
    except ServiceError:
        return None


def _show(plan, echo):
    for note in plan.notes:
        echo(note)
    for line in plan.details:
        echo(line)
    if plan.steps:
        echo('about to:')
        for step in plan.steps:
            echo(f'  {step.describe}')


def _report(desktop, env, echo, run, script):
    """Print the state of the unit and return the documented status code."""
    path = installed_unit(env)
    if not path.is_file():
        echo(f'{UNIT_NAME}: not installed ({desktop} unit available:'
             f' {template_for(desktop).name})')
        echo('  install it with: pwr-tray --svc-install')
        return STATUS_ABSENT

    rendered = _rendered_if_possible(desktop, env, script)
    status = probe(rendered=rendered, env=env, run=run)
    if status.active:
        state = 'active'
    elif status.failed:
        state = 'failed'
    else:
        state = 'installed, not running'
    flags = 'enabled' if status.enabled else 'disabled'
    echo(f'{UNIT_NAME}: {state} ({flags})')
    echo(f'  unit: {path}')
    if not status.installed:
        return STATUS_ABSENT
    if status.failed:
        echo(f'  note: the last start failed; see: journalctl --user -xeu {UNIT_NAME}')
        echo('  note: pwr-tray --svc-start clears the failed state and tries again')
    if rendered is not None and not status.up_to_date:
        echo('  note: the file differs from the shipped unit'
             ' (pwr-tray --svc-install would replace it)')
    return STATUS_ACTIVE if status.active else STATUS_INACTIVE
