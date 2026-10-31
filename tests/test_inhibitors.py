"""logind's two answers, without a listing tool in between.

The verdict comes from ``BlockInhibited`` (a machine field) and the menu rows
from ``ListInhibitors`` (a bus call).  What these tests pin:

* the two shapes the verdict arrives in -- busctl's one-line ``s "..."`` and
  the property's raw value -- parse the same way, and an unreadable answer is
  ``None``, never ``set()`` (the dangerous direction is "not inhibited");
* a row produced from ``ListInhibitors`` reads exactly like the
  ``systemd-inhibit`` row it replaces, so the menu does not change shape;
* a missing QtDBus, a failed call or a malformed record costs rows, not the
  verdict.
"""
import sys
import types

import pwr_tray.Inhibitors as inhibitors

# -- BlockInhibited ---------------------------------------------------------

def test_block_inhibited_parses_the_busctl_answer():
    assert inhibitors.parse_block_inhibited('s "sleep:idle"\n') == 'sleep:idle'
    assert inhibitors.parse_block_inhibited('s ""\n') == ''


def test_an_unreadable_block_inhibited_answer_is_unknown_not_empty():
    for output in ('', 'garbage\n', 's "unterminated\n', 'b true\n'):
        assert inhibitors.parse_block_inhibited(output) is None


def test_blocked_operations_splits_the_colon_list():
    assert inhibitors.blocked_operations('sleep:idle') == {'sleep', 'idle'}
    assert inhibitors.blocked_operations('') == set()
    assert inhibitors.blocked_operations('sleep:') == {'sleep'}


# -- rows -------------------------------------------------------------------

class _Reply:
    def __init__(self, records):
        self._records = records

    def arguments(self):
        return [self._records]


class _Interface:
    """The slice of QDBusInterface that bus_rows() uses."""

    def __init__(self, records=None, fail=False):
        self.records = records or []
        self.fail = fail

    def call(self, name):
        assert name == 'ListInhibitors'
        if self.fail:
            raise RuntimeError('bus went away')
        return _Reply(self.records)


def _rows(monkeypatch, records, blocked, fail=False):
    monkeypatch.setattr(inhibitors, '_logind_interface',
                        lambda: _Interface(records, fail))
    monkeypatch.setattr(inhibitors, '_user_name', lambda uid: 'joe')
    monkeypatch.setattr(inhibitors, '_command_name', lambda pid: f'comm{pid}')
    return inhibitors.bus_rows(blocked)


FIREFOX = ['sleep', 'joe', 'video', 'block', 1000, 4242]
NIRI = ['handle-power-key', 'niri', 'Power key handling', 'block', 1000, 4951]


def test_a_row_reads_like_the_systemd_inhibit_listing(monkeypatch):
    rows = _rows(monkeypatch, [FIREFOX], {'sleep'})

    assert rows == ['blocked: sleep',
                    'joe 1000 joe 4242 comm4242 sleep video block']


def test_only_rows_blocking_the_verdict_are_kept(monkeypatch):
    """A compositor owning the power key is not "keep this session awake"."""
    rows = _rows(monkeypatch, [FIREFOX, NIRI], {'sleep'})

    assert any('4242' in row for row in rows)
    assert not any('4951' in row for row in rows)


def test_no_qtdbus_costs_rows_and_returns_none(monkeypatch):
    monkeypatch.setattr(inhibitors, '_logind_interface', lambda: None)

    assert inhibitors.bus_rows({'sleep'}) is None


def test_a_failing_list_inhibitors_call_returns_none(monkeypatch):
    assert _rows(monkeypatch, [], {'sleep'}, fail=True) is None


def test_a_malformed_record_is_skipped_not_guessed(monkeypatch):
    rows = _rows(monkeypatch, [['sleep'], FIREFOX], {'sleep'})

    assert len(rows) == 2
    assert rows[0] == 'blocked: sleep'


def test_a_pid_less_inhibitor_still_formats(monkeypatch):
    monkeypatch.setattr(inhibitors, '_user_name', lambda uid: 'joe')
    row = inhibitors.format_row(['idle', 'gdm', 'login manager', 'delay',
                                 0, 0])

    # /proc/0/comm does not exist, so COMM is the listing's own placeholder.
    assert row == 'gdm 0 joe 0 - idle login manager delay'


def test_an_unknown_uid_and_a_missing_process_do_not_raise():
    assert inhibitors._user_name(2 ** 31 - 1) == str(2 ** 31 - 1)
    assert inhibitors._command_name(0) == '-'


# -- the QtDBus seam -------------------------------------------------------

def _qtdbus(monkeypatch, *, connected=True, explode=False, calls=None):
    """Install a fake PyQt5.QtDBus module for the duration of one test."""
    module = types.ModuleType('PyQt5.QtDBus')

    class _Bus:
        @staticmethod
        def isConnected():
            return connected

    class _Connection:
        @staticmethod
        def systemBus():
            if explode:
                raise RuntimeError('no system bus')
            return _Bus()

    class _Interface:
        def __init__(self, service, path, interface, _bus):
            (calls if calls is not None else []).append(
                (service, path, interface))

        def setTimeout(self, milliseconds):
            (calls if calls is not None else []).append(milliseconds)

    module.QDBusConnection = _Connection
    module.QDBusInterface = _Interface
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', module)


def test_no_qtdbus_module_means_no_interface(monkeypatch):
    monkeypatch.setitem(sys.modules, 'PyQt5.QtDBus', None)

    assert inhibitors._logind_interface() is None


def test_a_disconnected_bus_means_no_interface(monkeypatch):
    _qtdbus(monkeypatch, connected=False)

    assert inhibitors._logind_interface() is None


def test_an_unreachable_bus_means_no_interface(monkeypatch):
    _qtdbus(monkeypatch, explode=True)

    assert inhibitors._logind_interface() is None


def test_the_manager_interface_gets_a_short_timeout(monkeypatch):
    """The default 25 s would freeze the tray if the bus wedged."""
    calls = []
    _qtdbus(monkeypatch, calls=calls)

    interface = inhibitors._logind_interface()

    assert interface is not None
    assert calls == [('org.freedesktop.login1', '/org/freedesktop/login1',
                      'org.freedesktop.login1.Manager'), 2000]
