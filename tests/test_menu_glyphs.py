"""The tray menu's "icons" are characters, so their font must be guaranteed.

Every symbol in ``build_menu()`` is part of the label string, not a QIcon, so
the toolkit renders it with whatever font fontconfig resolves for that
codepoint.  Emoji-block codepoints (U+1F000 and up, plus a few others such as
U+2B9C) are answered by ``Noto Color Emoji`` on a typical desktop: they come
out coloured, wide and visually unrelated to the rest of the menu -- and as
tofu boxes on a minimal install that has no symbol font at all.

A coloured symbol column was tried and rejected: an QAction icon is drawn in a
single column at the menu's left edge, which moves every symbol away from its
group name and indentation.  The grouping is worth more than the colour, so
the glyphs stay inline and inherit the menu's text colour.

The contract pinned here:

- ``pwr_tray/main.py`` uses only text-presentation glyphs that DejaVu Sans
  (the de-facto floor next to Liberation Sans) covers;
- no emoji-block codepoint appears in the shipped documentation either, so
  the README does not advertise a glyph the menu no longer shows;
- ``build_menu()`` puts each glyph where the grouping needs it: after a group
  name, or at the em-space indent of a sub-item -- never lifted into an icon.
"""

import os
import types
import unicodedata

import pwr_tray.main as main_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Codepoints the menu may use.  Each one is text-presentation (no emoji
# fallback) and present in DejaVu Sans, so it inherits the label's colour and
# weight instead of arriving from a colour font.
ALLOWED_GLYPHS = {
    0x2003,  # EM SPACE                    indentation of sub-items
    0x2190,  # ←  LEFTWARDS ARROW          "Presentation <- LockOnly Mode"
    0x21AF,  # ↯  DOWNWARDS ZIGZAG ARROW   Plugged In (charging)
    0x21BA,  # ↺  ANTICLOCKWISE ARROW      Restart pwr-tray vX.Y.Z
    0x2261,  # ≡  IDENTICAL TO             Open Applet Log (lines of text)
    0x2583,  # ▃  LOWER THREE EIGHTHS      LoBattery
    0x2588,  # █  FULL BLOCK               HiBattery
    0x25B7,  # ▷  WHITE RIGHT TRIANGLE     Lock/Blank/Reload/Restart/Log Off
    0x25BC,  # ▼  BLACK DOWN TRIANGLE      Suspend/Reboot/PowerOff
    0x25C9,  # ◉  FISHEYE                  Presentation mode
    0x25CB,  # ○  WHITE CIRCLE             SleepAfterLock mode
    0x25D0,  # ◐  CIRCLE LEFT HALF BLACK   LockOnly mode
    0x2613,  # ☓  SALTIRE                  Quit this Applet
    0x266B,  # ♫  BEAMED EIGHTH NOTES      PlayerCtl
    0x267A,  # ♺  RECYCLING SYMBOL         rotate the lock/sleep timeout
    0x270E,  # ✎  LOWER RIGHT PENCIL       Edit Applet Config
}


def _offenders(text):
    """Return {char: name} for characters outside the allowlist."""
    found = {}
    for char in text:
        if ord(char) > 0x7F and ord(char) not in ALLOWED_GLYPHS:
            name = unicodedata.name(char, 'UNNAMED')
            found.setdefault(char, name)
    return found


def _report(found, where):
    lines = []
    for char, name in found.items():
        lines.append(f'  {char!r} U+{ord(char):04X} {name}')
    return f'{where} uses glyphs outside the text-presentation allowlist:\n' + \
        '\n'.join(lines)


def test_source_uses_only_text_presentation_glyphs():
    with open(os.path.join(ROOT, 'pwr_tray', 'main.py'),
              encoding='utf-8') as handle:
        source = handle.read()

    found = _offenders(source)
    assert not found, _report(found, 'pwr_tray/main.py')


def test_readme_and_audit_do_not_advertise_emoji_glyphs():
    """The docs quote the menu; they must quote what the menu now shows."""
    for name in ('README.md', os.path.join('tests', 'AUDIT.md')):
        with open(os.path.join(ROOT, name), encoding='utf-8') as handle:
            text = handle.read()
        emoji = {char: unicodedata.name(char, 'UNNAMED')
                 for char in text if ord(char) >= 0x1F000}
        assert not emoji, _report(emoji, name)


# --------------------------------------------------------------------------
# the labels build_menu() actually hands to Qt
# --------------------------------------------------------------------------

class _FakeSignal:
    def connect(self, _callback):
        pass


class _FakeAction:
    def __init__(self, text):
        self.text = text
        self.triggered = _FakeSignal()
        self.enabled = True

    def setEnabled(self, enabled):
        self.enabled = enabled


class _FakeMenu:
    def __init__(self):
        self.actions = []

    def addAction(self, action):
        self.actions.append(action)

    def clear(self):
        self.actions = []


class _FakeTrayIcon:
    @staticmethod
    def isVisible():
        return True

    @staticmethod
    def setContextMenu(_menu):
        pass


def _fake_tray(**overrides):
    """A tray with exactly the attributes build_menu() reads."""

    def noop(*_args, **_kwargs):
        return None

    tray = types.SimpleNamespace(
        menu=_FakeMenu(),
        menu_items=[],
        mode='LockOnly',
        graphical='niri',
        variables={'monitors_off': 'swaymsg "output * dpms off"',
                   'reload_wm': 'niri msg action reload-config',
                   'restart_wm': ''},
        battery=types.SimpleNamespace(selector='HiBattery', percent=88),
        has_playerctl=True,
        enable_playerctl=True,
        tray_icon=_FakeTrayIcon(),
        get_params=lambda: types.SimpleNamespace(
            turn_off_monitors=True, gui_editor='geany', log_viewer='xterm -e'),
        _lock_rotate_str=lambda: '15m->30m',
        _sleep_rotate_str=lambda: '5m->30m',
    )
    tray.__dict__.update(overrides)
    for name in ('enable_presentation_mode', 'enable_nosleep_mode',
                 'enable_normal_mode', 'lock_screen', 'blank_quick',
                 'reload_wm', 'restart_wm', 'exit_wm', 'suspend', 'reboot',
                 'poweroff', '_toggle_battery', 'toggle_playerctl',
                 'edit_config', 'open_log', 'quit_self', 'restart_self',
                 'dummy'):
        setattr(tray, name, noop)
    return tray


def _menu_labels(monkeypatch, rows=None, **overrides):
    monkeypatch.setattr(main_module, 'QAction', _FakeAction)
    tray = _fake_tray(**overrides)
    main_module.PwrTray.build_menu(tray, rows=rows)
    return [action.text for action in tray.menu.actions]


def test_build_menu_labels_use_only_allowed_glyphs(monkeypatch):
    for label in _menu_labels(monkeypatch):
        found = _offenders(label)
        assert not found, _report(found, f'menu label {label!r}')


def test_build_menu_still_offers_the_expected_actions(monkeypatch):
    labels = _menu_labels(monkeypatch)

    assert '≡  Open Applet Log' in labels
    assert '✎  Edit Applet Config' in labels
    assert '♫ PlayerCtl: Enabled' in labels
    assert '◉ Presentation ← LockOnly Mode' in labels
    assert '○ SleepAfterLock ← LockOnly Mode' in labels
    assert '█ HiBattery 88%' in labels
    assert '☓ Quit this Applet' in labels
    assert f'↺ Restart pwr-tray v{main_module.__version__}' in labels


def test_restart_line_is_last_and_carries_the_version(monkeypatch):
    """The restart entry closes the menu and doubles as the identity line.

    The version must come from the package rather than a literal, so a bump
    shows up in the menu without a second edit -- and there is no separate,
    dead ``pwr-tray vX.Y.Z`` label below it any more.
    """
    monkeypatch.setattr(main_module, 'QAction', _FakeAction)
    tray = _fake_tray()
    main_module.PwrTray.build_menu(tray)

    last = tray.menu.actions[-1]
    assert last.text == f'↺ Restart pwr-tray v{main_module.__version__}'
    assert last.enabled is True
    assert all(action.enabled for action in tray.menu.actions)
    assert not any(action.text.startswith('pwr-tray v')
                   for action in tray.menu.actions)


def test_battery_plugged_in_uses_the_zigzag_arrow(monkeypatch):
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='Settings', percent=100))

    assert '↯ Plugged In' in labels


def test_glyphs_stay_inline_where_the_grouping_needs_them(monkeypatch):
    """A symbol must never be lifted out of the label into an icon column.

    The icon column would drag every ▷ to the left edge, away from its group
    name ("niri:"/"System:") and away from the em-space indent of a sub-item.
    """
    tray_labels = _menu_labels(monkeypatch)

    # group headers keep the glyph after the name
    assert 'niri:\u2003 ▷ Lock Screen' in tray_labels
    assert 'System: \u2003▼ Suspend' in tray_labels
    # sub-items keep both the glyph and their indentation
    assert '\u2003\u2003\u2003▷ Blank Monitors' in tray_labels
    assert '\u2003\u2003\u2003▷ Reload' in tray_labels
    assert '\u2003\u2003\u2003▷ Log Off' in tray_labels
    assert '\u2003\u2003\u2003\u2003▼ Reboot' in tray_labels
    assert '\u2003\u2003♺ Lock: 15m->30m' in tray_labels


def test_rows_stay_untouched(monkeypatch):
    """Header/detail rows come from the inhibitor scan and stay plain."""
    rows = ['systemd-inhibit: sleep', '   (blocking: handle-power-key)']
    labels = _menu_labels(monkeypatch, rows=rows)

    assert labels[:len(rows)] == rows


def test_synthetic_power_state_is_marked_as_testing(monkeypatch):
    """A state produced by the no-battery cycler says so."""
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='HiBattery', percent=88,
                                      present=False, synthetic=True))

    assert '█ HiBattery 88% (TESTING)' in labels


def test_untouched_no_battery_state_is_not_marked_as_testing(monkeypatch):
    """The plugged-in state of a battery-less machine is real.

    That covers both start-up and the leg that closes the cycle: it is what
    the machine is doing (on AC), so it must not wear the "(TESTING)" suffix
    -- only the fabricated HiBattery/LoBattery legs should.
    """
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='Settings', percent=100,
                                      present=False, synthetic=False))

    assert '↯ Plugged In' in labels
    assert not any('TESTING' in label for label in labels)


def test_real_battery_state_is_not_marked_as_testing(monkeypatch):
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='Settings', percent=100,
                                      present=True, synthetic=False))

    assert '↯ Plugged In' in labels
    assert not any('TESTING' in label for label in labels)


def test_the_cycler_marks_only_the_battery_legs_as_synthetic():
    """The flag the suffix keys off is maintained by the cycler itself.

    Without this, the menu and the thing that manufactures the state could
    drift apart: a future edit could cycle the selector and leave the label
    claiming the result is real -- or, as reported, keep "(TESTING)" showing
    after the cycle returned to Plugged In.
    """
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=False, synthetic=False,
                                         selector='Settings')
    tray.rebuild_menu = False
    tray._inhibit_dirty = False

    seen = []
    for _ in range(3):
        tray._toggle_battery()
        seen.append((tray.battery.selector, tray.battery.synthetic))

    assert seen == [('HiBattery', True), ('LoBattery', True),
                    ('Settings', False)]
    assert tray.rebuild_menu is True


def test_cycling_back_to_plugged_in_drops_the_testing_suffix(monkeypatch):
    """A full Settings -> HiBattery -> LoBattery -> Settings run ends clean."""
    cycler = object.__new__(main_module.PwrTray)
    cycler.battery = types.SimpleNamespace(present=False, synthetic=False,
                                           selector='Settings', percent=100,
                                           plugged=True)
    cycler.rebuild_menu = False
    cycler._inhibit_dirty = False
    for _ in range(3):
        cycler._toggle_battery()

    labels = _menu_labels(monkeypatch, battery=cycler.battery)

    assert '↯ Plugged In' in labels
    assert not any('TESTING' in label for label in labels)
