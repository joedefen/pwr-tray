#!/usr/bin/env python3
"""Install, inspect, refresh and remove the pwr-tray ``systemd --user`` unit.

The unit files in ``pwr_tray/resources/systemd/`` are the single source of
truth.  They ship inside the wheel (flit includes everything under the module
directory), they are validated by ``tests/test_service_examples.py``, and
``--svc-install`` copies the one matching the current desktop to
``~/.config/systemd/user/pwr-tray.service`` and is the only verb that writes
or enables it.  ``--svc-start`` and ``--svc-restart`` operate on a unit that
is already installed.  ``--show-svc-file`` prints the same rendered text
without touching the machine, so a customized unit can start from it.

The units never name an installation: they run ``%h/.local/bin/pwr-tray``, the
one path every install method on the machine agrees on, so installing from an
activated virtualenv -- or from any other shell -- produces the same file.  A
missing launcher is refused rather than installed or started, because such a
unit cannot start; ``stop``, ``uninstall`` and ``status`` keep working without
it, since that is when they are needed.  The only fact :func:`render` still
substitutes is the session's display variable, which systemd cannot inherit.

Naming the launcher is not the same question as which code the launcher
imports: a project virtualenv can hold a *frozen* copy of the package
(``pip install .``) that shadows the checkout it was built from, and
restarting then reloads the snapshot forever.  :func:`installation` reads the
``direct_url.json`` pip writes next to the installed metadata to tell an
editable install from a frozen one, so ``--svc-status`` and ``--svc-restart``
can say why a restart changes nothing.

The units append stdout/stderr to the applet's own log,
``~/.config/pwr-tray/debug.log`` (``StandardError=inherit`` shares the file),
so the tray menu and ``-f/--follow-log`` always show what the applet is
actually writing.  systemd will not create that directory: it fails every
process with ``EXIT_STDOUT`` (209) before ``ExecStart=`` is reached.
:func:`missing_dirs` reads the paths back out of the unit so
``--svc-install`` can create them rather than leaving the user with a unit
that cannot start.

Nothing here shells out to systemctl on its own: the verbs are built as
:class:`Step` lists by the ``plan_*()`` functions and performed by
:func:`execute`, with the subprocess call injected.  That keeps the whole
policy testable on a machine without a systemd session.
"""

import json
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

#: The one path the shipped units run.  ``pipx install`` and ``pipx install
#: --editable .`` both write it, so the unit file never has to know which of
#: them -- or which shell -- installed pwr-tray.
LAUNCHER = Path('.local') / 'bin' / EXECUTABLE

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
_EXEC_START_RE = re.compile(r'^ExecStart=(.*)$', re.MULTILINE)

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


def installed_executable(text, env=None):
    """Return the executable named by the first ``ExecStart=``, expanded.

    systemd expands ``%h`` when it starts the unit, so the comparison that
    matters -- does this unit run ``~/.local/bin/pwr-tray``? -- has to expand it
    the same way.  A unit installed by an older pwr-tray names an absolute path
    instead, usually a project virtualenv's, which is how a service keeps
    running a snapshot of a checkout.
    """
    match = _EXEC_START_RE.search(text)
    if not match:
        return None
    command = match.group(1).strip()
    if not command:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        return _expand(command[1:end], _env(env)) if end > 0 else None
    return _expand(command.split()[0], _env(env))


@dataclass(frozen=True)
class Installation:
    """Which code the ``pwr-tray`` executable at a path will really import.

    ``editable`` means the distribution is a ``.pth`` back to a source tree, so
    editing that tree is enough.  ``snapshot`` is the trap: a non-editable copy
    of a checkout that still exists on disk -- ``pip install .`` inside a
    project virtualenv -- which imports the copy, not the tree, no matter how
    often the unit is restarted.
    """

    executable: Path
    environment: Path
    editable: bool = False
    source: Path = None
    snapshot: bool = False


#: Where an interpreter inside a virtualenv or system prefix keeps its packages.
_SITE_GLOBS = ('lib/python*/site-packages', 'lib64/python*/site-packages',
               'Lib/site-packages')
_DIST_GLOB = 'pwr_tray-*.dist-info'


def _environment_of(executable):
    """Return the virtualenv or system prefix a console script belongs to."""
    path = Path(executable)
    if path.parent.name in ('bin', 'Scripts'):
        return path.parent.parent
    return path.parent


def _dist_info(executable):
    """Return the installed ``pwr_tray`` metadata directory for `executable`."""
    prefix = _environment_of(executable)
    for pattern in _SITE_GLOBS:
        for site in sorted(prefix.glob(pattern)):
            for dist in sorted(site.glob(_DIST_GLOB)):
                return dist
    return None


def installation(executable):
    """Describe how the ``pwr_tray`` behind `executable` was installed.

    pip records the answer in ``direct_url.json`` next to the installed
    metadata: an editable install has ``dir_info.editable`` and a URL back to
    the tree, a wheel or ``pip install .`` has a plain ``file://`` URL, and an
    index install has only a registry URL.  A path without metadata at all --
    ``/usr/local/bin/pwr-tray`` that does not exist, a test double -- is
    reported as neither editable nor a snapshot rather than guessed at.
    """
    path = Path(executable)
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = path.absolute()    # a broken symlink still names its own prefix
    dist = _dist_info(path)
    editable, source = False, None
    if dist is not None:
        try:
            info = json.loads((dist / 'direct_url.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            info = {}
        editable = bool(info.get('dir_info', {}).get('editable'))
        url = str(info.get('url') or '')
        if url.startswith('file://'):
            source = Path(url[len('file://'):])
    snapshot = (not editable and source is not None
                and (source / 'pwr_tray' / 'main.py').is_file()
                and (source / 'pyproject.toml').is_file())
    return Installation(executable=path, environment=_environment_of(path),
                        editable=editable, source=source, snapshot=snapshot)


def _snapshot_note(info):
    """Explain why restarting a frozen install cannot pick up edits."""
    return (f'{info.executable} is a frozen copy of {info.source} (a non-editable'
            ' install), so restarting it reloads that snapshot, not the tree;'
            ' reinstall it editable (pipx install --editable . or python -m pip'
            ' install -e .) to make edits reach the service')


def launcher_path(env=None):
    """Return ``~/.local/bin/pwr-tray``: the path the shipped units run."""
    home = _env(env).get('HOME') or str(Path.home())
    return Path(home) / LAUNCHER


def launcher_problem(path):
    """Return why `path` cannot launch the applet, or ``None`` when it can."""
    path = Path(path)
    if path.is_symlink() and not path.exists():
        return f'{path} is a dangling symlink'
    if not path.exists():
        return f'{path} does not exist'
    if not path.is_file():
        return f'{path} is not a regular file'
    if not os.access(path, os.X_OK):
        return f'{path} is not executable'
    return None


def _launcher_hint():
    return ('install it with: pipx install --editable . (a checkout) or'
            ' pipx install pwr-tray / python -m pip install --user pwr-tray')


def require_launcher(env=None):
    """Return the launcher path, or refuse because no unit could start."""
    path = launcher_path(env)
    problem = launcher_problem(path)
    if problem:
        raise ServiceError(
            f'{problem}, so the unit could not start; {_launcher_hint()}')
    return path


def launcher_installation(env=None):
    """Return the :class:`Installation` behind ``~/.local/bin/pwr-tray``."""
    return installation(launcher_path(env))


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


def render(text, env=None):
    """Return `text` with this session's display variables substituted in.

    The executable is not touched: every shipped unit names
    ``%h/.local/bin/pwr-tray``, which is the same file for every installation
    on a machine, so installing from any shell produces identical text.  The
    single edit is a session variable the unit pins
    (``Environment=WAYLAND_DISPLAY=wayland-1``), overridden from the live
    environment by appending an ``Environment=`` line after ``ExecStart=``.
    The last assignment wins in systemd, so the shipped line stays readable and
    still documents the default.  Every other byte is preserved, which is what
    lets the tests keep asserting the shipped text.
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

    result = []
    placed = False
    for line in text.splitlines():
        result.append(line)
        if line.startswith('ExecStart=') and not placed:
            result.extend(overrides)
            placed = True
    if not placed:
        raise ServiceError('the unit has no ExecStart= line')
    return '\n'.join(result) + ('\n' if text.endswith('\n') else '')


def rendered_unit(desktop, env=None, source=None):
    """Return the unit text that would be installed for `desktop`."""
    return render(read_unit(template_for(desktop, source)), env)


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
    """Write the unit if needed, reload, and enable it (without starting).

    An executable that is a frozen copy of a checkout pip can still see is
    refused unless ``--force``: it is the one way to install a unit that looks
    current and can never run the working tree.
    """
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
    executable = installed_executable(rendered, env)
    if wrote and executable:
        # The unit always names the same launcher; say which file that is once
        # %h is expanded, and what it imports, because that decides what runs.
        plan.notes.append(f'the unit will run {executable}')
    info = installation(executable) if executable else None
    if info is not None and info.snapshot:
        plan.notes.append(_snapshot_note(info))
    elif info is not None and info.editable and info.source:
        plan.notes.append(f'{executable} is an editable install of {info.source}')
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


def _systemd_hint():
    return (f'inspect it with: systemctl --user status {UNIT_NAME}'
            f' or: journalctl --user -xeu {UNIT_NAME}')


def _running_outside_systemd(holder):
    return ServiceError(
        f'pwr-tray is already running outside systemd (pid {holder}); the unit'
        ' would exit 3 against the single-instance lock.  Remove the desktop'
        ' autostart that started it (or quit that instance) and run this again')


def _require_installed(status):
    """Refuse a start/restart that has no unit to operate on."""
    if not status.installed:
        raise ServiceError(
            f'{UNIT_NAME} is not installed; run pwr-tray --svc-install first')


def _refuse_moved_unit(status, env):
    """Refuse to start a unit that does not run the launcher.

    The shipped units are constants: they name ``~/.local/bin/pwr-tray`` and
    never an installation.  A unit naming anything else was either hand edited
    or installed by an older pwr-tray that baked in an absolute path -- usually
    a project virtualenv's, which is how a service silently keeps running a
    snapshot.  Rewriting the unit is that verb's decision, so it is refused
    here rather than fixed here.
    """
    try:
        installed = installed_executable(read_unit(status.path), env)
    except OSError:
        return
    wanted = str(launcher_path(env))
    if installed and installed != wanted:
        raise ServiceError(
            f'{UNIT_NAME} runs {installed}, but the shipped unit runs {wanted};'
            ' run pwr-tray --svc-install to update the unit, then try again')


def _note_stale_unit(plan, status, rendered):
    """Point at --svc-install when the file differs, without changing it."""
    if rendered is not None and status.installed and not status.up_to_date:
        plan.notes.append(
            f'{status.path} differs from the shipped unit;'
            ' run pwr-tray --svc-install to update it')


def plan_start(rendered, status, force=False, holder=None, env=None):
    """Enable if needed and start an installed unit; never writes the unit.

    Installing is a separate, explicit act (``--svc-install``).  ``start``
    only operates on the unit that is already installed, so it cannot
    silently commit the machine to systemd.  It does enable a disabled unit:
    "start" means "run now and come back at your next login".
    """
    _require_installed(status)
    _refuse_moved_unit(status, env)
    plan = Plan()
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if not status.enabled:
        plan.steps.append(Step(f'enable {UNIT_NAME}',
                               argv=systemctl('enable', UNIT_NAME)))
        plan.notes.append(
            f'{UNIT_NAME} was disabled; it is enabled so it returns at your'
            ' next login')
    if status.active:
        if status.up_to_date or rendered is None:
            plan.notes.append(f'{UNIT_NAME} is already running')
        else:
            # `start` on an active unit is a no-op; restarting is a separate
            # verb so that `start` never changes what is running.
            plan.notes.append(
                f'{UNIT_NAME} is already running; the installed unit differs'
                ' from the shipped one, so run pwr-tray --svc-install to update'
                ' it or pwr-tray --svc-restart to apply code changes')
        return plan
    if holder:
        raise _running_outside_systemd(holder)
    _note_stale_unit(plan, status, rendered)
    plan.steps.append(Step(
        f'start {UNIT_NAME}',
        argv=systemctl('start', UNIT_NAME),
        hint=_systemd_hint()))
    return plan


def plan_restart(rendered, status, force=False, holder=None, env=None):
    """Restart an installed unit so it runs the code its launcher names.

    An editable install changes the *code* without changing the unit file, so
    neither ``--svc-install`` nor ``--svc-start`` can notice it: only a
    restart makes systemd load the modules the launcher resolves to.
    A restart does not install, enable, or rewrite the unit; a unit that runs
    something other than ``~/.local/bin/pwr-tray`` is refused, because
    rewriting it is what ``--svc-install`` is for.  A launcher that is a
    *frozen* copy of a checkout -- ``pip install .`` inside a project
    virtualenv -- is restarted as asked, but with a note that no restart can
    make it run that checkout.
    """
    _require_installed(status)
    _refuse_moved_unit(status, env)
    plan = Plan()
    if status.failed:
        plan.steps.append(Step(f'clear the failed state of {UNIT_NAME}',
                               argv=systemctl('reset-failed', UNIT_NAME)))
    if holder and not status.active:
        raise _running_outside_systemd(holder)
    _note_stale_unit(plan, status, rendered)
    if not status.enabled:
        plan.notes.append(
            f'{UNIT_NAME} is not enabled; it runs now but will not return at'
            ' your next login (run pwr-tray --svc-install to enable it)')
    plan.steps.append(Step(
        f'restart {UNIT_NAME}',
        argv=systemctl('restart', UNIT_NAME),
        hint=_systemd_hint()))
    info = launcher_installation(env)
    if info.snapshot:
        plan.notes.append(_snapshot_note(info))
    else:
        plan.notes.append(
            f'{UNIT_NAME} restarts now, so it runs the code'
            f' {launcher_path(env)} resolves to')
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
             ask=None, run=run_command, holder=None, systemd=None):
    """Carry out one ``--svc-*`` verb and return a process exit status."""
    env = _env(env)
    ask = (ask or _no_prompt)
    if yes:
        ask = _auto_yes

    if verb == 'status':
        return _report(desktop, env, echo, run)

    # Dumping the unit needs neither a systemd user manager nor a launcher:
    # it is the starter for a hand-installed (or hand-edited) unit, so it has
    # to work exactly where an automatic install might not.
    if verb == 'show-file':
        return _show_file(desktop, env, echo)

    if not (systemd_available(env) if systemd is None else systemd):
        raise ServiceError(
            'no systemd user manager is reachable (is this a systemd system?);'
            ' use your desktop autostart instead')

    if verb in ('install', 'start', 'restart'):
        # The unit runs ~/.local/bin/pwr-tray and nothing else, so a missing
        # launcher is a unit that fails every restart.  Refuse before writing
        # or starting anything; `stop`, `uninstall` and `status` must still
        # work exactly when things are broken.
        require_launcher(env)
        rendered = rendered_unit(desktop, env)
    else:
        # stop/uninstall must work on an installed unit even when this
        # desktop's unit is unknown, so rendering is advisory.
        rendered = _rendered_if_possible(desktop, env)

    status = probe(rendered=rendered, env=env, run=run)
    if verb == 'install':
        plan = plan_install(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'start':
        plan = plan_start(rendered, status, force=force, holder=holder, env=env)
    elif verb == 'restart':
        plan = plan_restart(rendered, status, force=force, holder=holder, env=env)
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


def _rendered_if_possible(desktop, env):
    """Best-effort render, for verbs that must work on a broken install."""
    try:
        return rendered_unit(desktop, env)
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


def _show_file(desktop, env, echo):
    """Print the unit ``--svc-install`` would write, then return OK.

    This is a *starter*: edit it, then install it by hand (for example under
    ``~/.config/systemd/user/``).  It is rendered for this machine's desktop
    and display, so it is the same text an automatic install would produce.
    """
    for line in rendered_unit(desktop, env).splitlines():
        echo(line)
    return OK


def _report(desktop, env, echo, run):
    """Print the state of the unit and return the documented status code."""
    path = installed_unit(env)
    if not path.is_file():
        echo(f'{UNIT_NAME}: not installed ({desktop} unit available:'
             f' {template_for(desktop).name})')
        echo('  install it with: pwr-tray --svc-install')
        return STATUS_ABSENT

    rendered = _rendered_if_possible(desktop, env)
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
    launcher = launcher_path(env)
    installed_exe = installed_executable(read_unit(path), env)
    if installed_exe:
        echo(f'  runs: {installed_exe}')
        if Path(installed_exe) != launcher:
            echo(f'  note: the shipped unit runs {launcher};'
                 ' pwr-tray --svc-install would replace this unit')
    problem = launcher_problem(launcher)
    if problem:
        echo(f'  note: {problem}, so the unit cannot start ({_launcher_hint()})')
    else:
        target = Path(launcher).resolve()
        if target != launcher:
            echo(f'        -> {target}')
        info = installation(launcher)
        if info.snapshot:
            echo(f'  note: {_snapshot_note(info)}')
        elif info.editable and info.source:
            echo(f'        (editable install of {info.source})')
        found = shutil.which(EXECUTABLE, path=env.get('PATH'))
        if found and Path(found) != launcher:
            echo(f'  note: the pwr-tray on $PATH is {found}; a shell runs that,'
                 ' the unit runs the launcher above')
    if not status.installed:
        return STATUS_ABSENT
    if status.failed:
        echo(f'  note: the last start failed; see: journalctl --user -xeu {UNIT_NAME}')
        echo('  note: pwr-tray --svc-start clears the failed state and tries again')
    if rendered is not None and not status.up_to_date:
        echo('  note: the file differs from the shipped unit'
             ' (pwr-tray --svc-install would replace it)')
    return STATUS_ACTIVE if status.active else STATUS_INACTIVE
