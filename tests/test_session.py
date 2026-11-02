"""Which display server and desktop a login is really on.

The unit pins nothing, so these are the rules that decide what Qt connects to
and what ``[ServiceAllowed]`` is compared against.  A socket that exists is the
evidence: a snapshot naming a server that is not listening describes a session
that has ended, and acting on it is what made the applet start a tray nobody
could see (and would now route the gate from the wrong desktop).
"""

import socket

import pytest

from pwr_tray import Session


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A private ``$XDG_RUNTIME_DIR`` and a private ``/tmp/.X11-unix``."""
    runtime_dir = tmp_path / 'run'
    x11_dir = tmp_path / 'X11'
    runtime_dir.mkdir()
    x11_dir.mkdir()
    monkeypatch.setattr(Session, 'X11_SOCKET_DIR', str(x11_dir))
    return runtime_dir, x11_dir


@pytest.fixture
def sockets():
    """Create unix sockets and close them at the end of the test."""
    opened = []

    def make(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        opened.append(sock)
        return path

    yield make
    for sock in opened:
        sock.close()


def query(session='7', kind='', display='', desktop=''):
    """A stand-in for ``loginctl`` that answers about one session."""
    answers = {'Type': kind, 'Display': display, 'Desktop': desktop}

    def ask(*args):
        if args[0] == 'show-user':
            return session
        return answers[args[3]]

    return ask


def test_a_live_wayland_socket_beats_a_stale_x11_snapshot(runtime, sockets):
    run, _ = runtime
    sockets(run / 'wayland-1')
    env = {'XDG_RUNTIME_DIR': str(run), 'XDG_SESSION_TYPE': 'x11',
           'WAYLAND_DISPLAY': 'wayland-1', 'DISPLAY': ':0'}

    wanted = Session.session_values(env, query())

    assert wanted['XDG_SESSION_TYPE'] == 'wayland'
    assert wanted['WAYLAND_DISPLAY'] == 'wayland-1'
    assert wanted['DISPLAY'] is None        # nothing is listening on :0


def test_a_dead_wayland_snapshot_is_dropped_for_a_live_x11_display(runtime, sockets):
    run, x11 = runtime
    sockets(x11 / 'X1')
    env = {'XDG_RUNTIME_DIR': str(run), 'XDG_SESSION_TYPE': 'wayland',
           'WAYLAND_DISPLAY': 'wayland-9', 'DISPLAY': ':1'}

    wanted = Session.session_values(env)

    assert wanted['XDG_SESSION_TYPE'] == 'x11'
    assert wanted['DISPLAY'] == ':1'
    assert wanted['WAYLAND_DISPLAY'] is None


def test_xwayland_keeps_the_marker_and_the_live_second_display(runtime, sockets):
    """Both sockets exist on a Wayland login; only the marker can choose."""
    run, x11 = runtime
    sockets(run / 'wayland-1')
    sockets(x11 / 'X1')
    env = {'XDG_RUNTIME_DIR': str(run), 'XDG_SESSION_TYPE': 'x11',
           'WAYLAND_DISPLAY': 'wayland-1', 'DISPLAY': ':1'}

    wanted = Session.session_values(env)

    assert wanted == {'XDG_SESSION_TYPE': 'x11', 'WAYLAND_DISPLAY': 'wayland-1',
                      'DISPLAY': ':1'}


def test_nothing_live_means_ask_logind(runtime, sockets):
    run, _ = runtime
    sockets(run / 'wayland-1')
    env = {'XDG_RUNTIME_DIR': str(run), 'USER': 'joe'}
    ask = query(kind='wayland', desktop='niri')

    wanted = Session.session_values(env, ask)

    assert wanted == {'XDG_SESSION_TYPE': 'wayland',
                      'WAYLAND_DISPLAY': 'wayland-1', 'DISPLAY': None}
    assert Session.desktop_values(env, ask) == {
        'XDG_CURRENT_DESKTOP': 'niri', 'XDG_SESSION_DESKTOP': 'niri',
        'DESKTOP_SESSION': 'niri'}


def test_logind_supplies_the_missing_x_display(runtime):
    """logind leaves Display= empty once the session closes; :0 is the guess."""
    env = {'XDG_RUNTIME_DIR': str(runtime[0]), 'XDG_SESSION_ID': '7'}
    wanted = Session.session_values(env, query(kind='x11'))
    assert wanted == {'XDG_SESSION_TYPE': 'x11', 'WAYLAND_DISPLAY': None,
                      'DISPLAY': ':0'}


def test_the_session_desktop_wins_over_the_environment_snapshot(runtime, sockets):
    """The environment describes the login that started `systemd --user`."""
    run, _ = runtime
    sockets(run / 'wayland-1')
    env = {'XDG_RUNTIME_DIR': str(run), 'XDG_SESSION_TYPE': 'wayland',
           'WAYLAND_DISPLAY': 'wayland-1', 'XDG_CURRENT_DESKTOP': 'KDE',
           'USER': 'joe'}

    changes = Session.repair_environment(env, query(kind='wayland', desktop='niri'))

    assert ('XDG_CURRENT_DESKTOP', 'KDE', 'niri') in changes
    assert env['XDG_CURRENT_DESKTOP'] == 'niri'
    assert env['XDG_SESSION_DESKTOP'] == 'niri'
    assert env['DESKTOP_SESSION'] == 'niri'


def test_a_correct_environment_is_left_alone(runtime, sockets):
    run, _ = runtime
    sockets(run / 'wayland-1')
    env = {'XDG_RUNTIME_DIR': str(run), 'XDG_SESSION_TYPE': 'wayland',
           'WAYLAND_DISPLAY': 'wayland-1', 'XDG_CURRENT_DESKTOP': 'niri',
           'XDG_SESSION_DESKTOP': 'niri', 'DESKTOP_SESSION': 'niri'}

    assert Session.repair_environment(env, query(kind='wayland', desktop='niri')) == []


def test_the_environment_is_the_fallback_when_logind_names_nothing(runtime):
    env = {'XDG_RUNTIME_DIR': str(runtime[0]), 'XDG_CURRENT_DESKTOP': 'sway'}
    assert Session.desktop_name(env, query(session='')) == 'sway'


def test_nothing_can_be_asked_and_that_is_said_out_loud(runtime):
    notes = []
    env = {'XDG_RUNTIME_DIR': str(runtime[0])}

    assert Session.repair_environment(env, query(session=''), note=notes.append) == []
    assert notes and 'loginctl' in notes[0]
    assert 'XDG_CURRENT_DESKTOP' not in env          # nothing was invented


def test_x11_socket_path_only_understands_local_displays():
    assert Session.x11_socket_path(':0') == '/tmp/.X11-unix/X0'
    assert Session.x11_socket_path(':1.0') == '/tmp/.X11-unix/X1'
    assert Session.x11_socket_path('host:0') == ''
    assert Session.x11_socket_path('') == ''
