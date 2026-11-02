"""Qt's own console output: what reaches the log, and what is dropped.

Qt talks to its process through a message handler, not through `print()`.  On a
desktop that uses the qt5ct/qt6ct platform theme the plugin prints its whole
palette table on every widget it is asked about, Qt's layout code warns about a
null widget a *plugin* handed it, and Qt prints a `QThreadStorage ... destroyed`
notice while it tears itself down -- none of that has anything to do with
pwr-tray, and all of it used to land in `debug.log` between the lines a reader
actually wanted, in a place where it looked like our own bug.

`install_qt_messages()` puts a handler in front of that stream: debug and
information levels are dropped, the theme's palette chatter and the shutdown
notice are dropped, and everything else is kept -- once per distinct text --
stamped with the `[file:line]` of the C++ side Qt reports.  That footer is the
point: it says where to look.

These tests call the handler directly.  Installing it would capture the
warnings of the whole test session, and the handler is a plain function.
"""

from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QtMsgType

import pwr_tray.Utils as utils
from pwr_tray import main as app_main

PALETTE = ('virtual const QPalette* {}::palette('
           'QPlatformTheme::Palette) const')


@pytest.fixture(autouse=True)
def quiet_prt(monkeypatch):
    """prt() writes to stdout only, and each test starts with an empty set."""
    monkeypatch.setattr(utils, 'prt_path', '')
    monkeypatch.setattr(utils, 'prt_kb', 0)
    monkeypatch.setattr(app_main, '_qt_seen', set())


def context(file='/home/joe/src/qtbase/src/gui/painting/qpainter.cpp', line=42):
    """The `QMessageLogContext` Qt passes: the C++ file and line of the message."""
    return SimpleNamespace(file=file, line=line, function='drawText')


def test_debug_and_information_messages_are_dropped(capsys):
    app_main.qt_message_handler(QtMsgType.QtDebugMsg, context(), 'painter is sad')
    app_main.qt_message_handler(QtMsgType.QtInfoMsg, context(), 'device pixel ratio')

    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('theme', ['Qt5CTPlatformTheme', 'Qt6CTPlatformTheme'])
def test_the_theme_plugin_palette_table_is_dropped(capsys, theme):
    # This one arrives at warning level on some Qt builds, so it is matched by
    # text as well as by level.  Both spellings are covered on purpose: the
    # applet uses Qt5, the machine that produced the report uses Qt6.
    app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(),
                                PALETTE.format(theme))

    assert capsys.readouterr().out == ''


def test_the_shutdown_thread_storage_notice_is_dropped(capsys):
    # Printed once, at exit, with a fresh address each run: dropping it by
    # prefix is the only way a reader stops meeting it in the log.
    app_main.qt_message_handler(
        QtMsgType.QtWarningMsg, context(file=None, line=0),
        'QThreadStorage: Thread 0x55e6bec0b700 exited after QThreadStorage 8'
        ' destroyed')

    assert capsys.readouterr().out == ''


def test_a_warning_is_kept_with_the_origin_qt_reported(capsys):
    app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'bad paint engine')

    out = capsys.readouterr().out
    assert 'qt: bad paint engine' in out
    # The footer is Qt's file and line, not a line of this package: the reader
    # has to be able to see that the message is not ours.
    assert '[qpainter.cpp:42]' in out
    assert 'Utils.py' not in out


def test_a_warning_we_cannot_trace_stays_visible(capsys):
    # The null-widget warning looks alarming but belongs to the theme plugin.
    # Keeping it -- once, labelled, with whatever origin Qt gives -- is the
    # difference between filtering noise and hiding a real widget bug.
    app_main.qt_message_handler(
        QtMsgType.QtWarningMsg, context(file=None, line=0),
        'QLayout: Cannot add a null widget to QHBoxLayout')

    assert 'qt: QLayout: Cannot add a null widget' in capsys.readouterr().out


def test_a_warning_is_reported_once_not_on_every_repaint(capsys):
    for _ in range(3):
        app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'bad paint')
    out = capsys.readouterr().out

    assert out.count('bad paint') == 1


def test_a_different_warning_still_gets_through(capsys):
    app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(), 'first')
    app_main.qt_message_handler(QtMsgType.QtCriticalMsg, context(), 'second')

    out = capsys.readouterr().out
    assert 'qt: first' in out
    assert 'qt: second' in out


def test_the_dedupe_set_is_bounded(capsys):
    # The applet runs for weeks; remembering every warning text forever would
    # be a slow leak.  Filling the set must not lose the *next* message.
    app_main._qt_seen.update(str(number) for number in range(64))

    app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(),
                                'after the clear')

    assert 'qt: after the clear' in capsys.readouterr().out


def test_a_message_without_a_file_still_gets_a_footer(capsys):
    # Release builds of Qt are compiled without QT_MESSAGELOGCONTEXT, so the
    # context arrives empty; the line must still say it came from Qt.
    app_main.qt_message_handler(QtMsgType.QtWarningMsg,
                                context(file=None, line=0), 'anonymous complaint')

    out = capsys.readouterr().out
    assert 'qt: anonymous complaint' in out
    assert '[qt]' in out


def test_an_empty_message_is_ignored(capsys):
    app_main.qt_message_handler(QtMsgType.QtWarningMsg, context(), '   ')

    assert capsys.readouterr().out == ''


def test_install_registers_that_handler(monkeypatch):
    recorded = []
    monkeypatch.setattr(app_main, 'qInstallMessageHandler', recorded.append)

    app_main.install_qt_messages()

    assert recorded == [app_main.qt_message_handler]


def test_prt_qt_keeps_qt_s_own_file_and_line(capsys):
    # prt()'s usual footer is the Python frame that logged the line; for a
    # message Qt made, that frame is prt_qt() itself and says nothing useful.
    utils.prt_qt('bad paint engine', '[qpainter.cpp:42]')

    out = capsys.readouterr().out
    assert 'qt: bad paint engine' in out
    assert '[qpainter.cpp:42]' in out
    assert 'Utils.py' not in out
