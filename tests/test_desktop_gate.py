"""The ``[ServiceAllowed]`` gate: who may run the applet, and with what exit.

The decision belongs to the applet, not to ``ConditionEnvironment=``: the
condition is evaluated against the user manager's environment, which may name
no desktop at all, and a failed condition is a *silent* `skipped` -- no tray and
nothing in the log.  Here the applet knows the live session
(``pwr_tray.Session``) and makes the call itself.

The exit status is what keeps a logout safe: ``Restart=on-failure`` treats a
clean exit as final, so a disallowed desktop ends ``inactive (dead)``.  A
failure code would either restart five times and mark the unit `failed` at every
login, or need ``SuccessExitStatus=`` to do what exit 0 already does.

``pwr_tray.main.prt`` is replaced here: the real one re-points fds 1 and 2 at
``debug.log`` on its first call, which would tear the streams out from under
pytest's capture.  The messages are what the test wants anyway.
"""

import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from pwr_tray import service

DESKTOP = 'niri-wayland'


def write_config(home, rows):
    """Write ``[ServiceAllowed]`` into the isolated home's config.ini."""
    path = Path(home) / '.config' / 'pwr-tray' / 'config.ini'
    path.parent.mkdir(parents=True, exist_ok=True)
    body = '[ServiceAllowed]\n' + ''.join(f'{name} = {value}\n'
                                          for name, value in rows.items())
    path.write_text(body, encoding='utf-8')
    return path


class FakeTray:
    """Stand-in for ``PwrTray``: records construction, quits immediately."""

    constructed: ClassVar[list] = []
    executed: ClassVar[bool] = False

    def __init__(self, **kwargs):
        FakeTray.constructed.append(kwargs)
        self.app = SimpleNamespace(exec_=self._exec)

    def _exec(self):
        FakeTray.executed = True

    @staticmethod
    def goodbye(message=''):
        return None


@pytest.fixture(autouse=True)
def clean_fake_tray():
    FakeTray.constructed = []
    FakeTray.executed = False
    yield
    FakeTray.constructed = []
    FakeTray.executed = False


@pytest.fixture
def cli(monkeypatch, isolated_home):
    """The real CLI, pointed at a one-desktop YAML and a throw-away home."""
    import pwr_tray.main as cli_module

    messages = []

    def record(*args, **kwargs):
        # prt() takes `to_stdout=`; a plain list.append would not.
        messages.append(' '.join(str(arg) for arg in args))

    de_json = {'defaults': {}, 'session_types': {}, 'desktops': {DESKTOP: {}}}
    monkeypatch.setattr(cli_module, 'load_known_de_json', lambda: (de_json, 'test'))
    monkeypatch.setattr(cli_module, 'PwrTray', FakeTray)
    monkeypatch.setattr(cli_module, 'prt', record)
    # IniTool imported prt() by name; the real one re-points fds 1 and 2 at
    # debug.log, which would close pytest's capture streams out from under it.
    monkeypatch.setattr('pwr_tray.IniTool.prt', record)
    # ...and logind would answer about the session running the test suite.
    monkeypatch.setattr('pwr_tray.Session._loginctl', lambda *args: '')
    # main() points Utils at a log inside this test's temporary home.  Left
    # behind, the next test's first prt() would reopen fds 1 and 2 onto a file
    # that is about to be deleted -- closing pytest's capture streams with it.
    # _console_fd is the saved stderr that error_print() writes to, so it has to
    # belong to this test as well.
    monkeypatch.setattr('pwr_tray.Utils.prt_path', '')
    monkeypatch.setattr('pwr_tray.Utils.prt_to_init', True)
    monkeypatch.setattr('pwr_tray.Utils.prt_keep_stdout', False, raising=False)
    monkeypatch.setattr('pwr_tray.Utils._console_fd', None)
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'niri')
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    # An empty runtime directory and no display: the machine running the suite
    # must not lend this "session" its live Xwayland socket, or Session would
    # (correctly) repair the type to x11.
    runtime = Path(isolated_home) / 'run'
    runtime.mkdir()
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.setattr(sys, 'argv', ['pwr-tray'])
    return cli_module, isolated_home, messages


def as_service(monkeypatch):
    """Run the applet the way the shipped unit does."""
    monkeypatch.setattr(sys, 'argv', ['pwr-tray', '--svc-mode'])


@pytest.fixture
def restore_signals():
    """main() installs real SIGTERM/SIGHUP handlers; put ours back."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_a_disallowed_desktop_exits_cleanly_and_touches_nothing(cli, monkeypatch):
    """Exit 0: `Restart=on-failure` leaves it final, so the unit stays inactive."""
    module, home, messages = cli
    write_config(home, {DESKTOP: False, 'kde-x11': False})
    as_service(monkeypatch)

    assert module.main() is None

    assert FakeTray.constructed == []
    joined = '\n'.join(messages)
    assert 'not allowed in this session (niri-wayland)' in joined
    assert 'exiting 0' in joined
    assert not (Path(home) / '.config' / 'pwr-tray' / 'pwr-tray.lock').exists()


def test_an_allowed_desktop_starts_the_applet(cli, monkeypatch, restore_signals):
    module, home, _messages = cli
    write_config(home, {DESKTOP: True, 'kde-x11': False})
    as_service(monkeypatch)

    module.main()

    assert len(FakeTray.constructed) == 1
    assert FakeTray.executed is True


def test_a_manual_run_is_never_gated(cli):
    """[ServiceAllowed] is about the *service*; an i3 `exec` line still works."""
    module, home, _messages = cli
    write_config(home, {DESKTOP: False})

    module.main()

    assert len(FakeTray.constructed) == 1


def test_a_missing_section_runs_nothing(cli, monkeypatch):
    """Nothing is allowed until somebody says so."""
    module, _home, messages = cli
    as_service(monkeypatch)

    module.main()

    assert FakeTray.constructed == []
    assert 'enables: nothing' in '\n'.join(messages)


def test_an_unknown_desktop_is_a_loud_failure(cli, monkeypatch):
    """A permanent failure (exit 2), not a silent skip: the unit says `failed`."""
    module, _home, messages = cli
    write_config(_home, {DESKTOP: True})
    as_service(monkeypatch)
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'plan9')
    monkeypatch.setenv('XDG_SESSION_DESKTOP', 'plan9')
    monkeypatch.setenv('DESKTOP_SESSION', 'plan9')

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 2
    assert 'no desktop environment matched' in '\n'.join(messages)


def test_desktop_allowed_reads_the_isolated_home(isolated_home):
    write_config(isolated_home, {'i3-x11': True})
    assert service.desktop_allowed('i3-x11') is True
    assert service.desktop_allowed('niri-wayland') is False
