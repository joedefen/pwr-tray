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


def test_readme_does_not_advertise_emoji_glyphs():
    """The README quotes the menu; it must quote what the menu now shows."""
    with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as handle:
        text = handle.read()
    emoji = {char: unicodedata.name(char, 'UNNAMED')
             for char in text if ord(char) >= 0x1F000}
    assert not emoji, _report(emoji, 'README.md')


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
                   'restart_wm': '',
                   'logoff': 'niri msg action quit --skip-confirmation'},
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
    # The PlayerCtl and battery labels are real logic, not constants: bind them
    # (and what they read) so these tests exercise the shipped state words.
    # Every row that carries a `key=` reads session_end_keys() and the arm
    # state; these tests do not ask for confirmation, so it only decides "not
    # armed" -- but build_menu() records that state, so it must be readable.
    for name in ('_events', '_bus_is_trusted', '_playing_names',
                 '_player_ctl_label', 'can_toggle_playerctl', 'battery_line',
                 'session_end_keys', '_pending_key', '_armed_key'):
        setattr(tray, name, getattr(main_module.PwrTray, name).__get__(tray))
    return tray


def _menu_labels(monkeypatch, rows=None, **overrides):
    monkeypatch.setattr(main_module, 'QAction', _FakeAction)
    tray = _fake_tray(**overrides)
    main_module.PwrTray.build_menu(tray, rows=rows)
    return [action.text for action in tray.menu.actions]


def _confirming_tray(**overrides):
    """`_fake_tray` with confirm_session_end on and the arming methods bound.

    The label is the whole confirmation UI, so these tests build the real menu
    from the real state: what a user sees after one click is a menu row.
    """
    tray = _fake_tray(**overrides)
    tray.get_params = lambda: types.SimpleNamespace(
        turn_off_monitors=True, gui_editor='geany', log_viewer='xterm -e',
        confirm_session_end=True)
    tray.ARM_SECONDS = main_module.PwrTray.ARM_SECONDS
    tray._menu_arm = None
    for name in ('session_end_keys', '_pending_key', '_armed_key',
                 '_confirm_suffix', '_arming', 'arm_or_run'):
        setattr(tray, name, getattr(main_module.PwrTray, name).__get__(tray))
    return tray


def _confirming_labels(monkeypatch, tray=None, rows=None):
    monkeypatch.setattr(main_module, 'QAction', _FakeAction)
    tray = tray or _confirming_tray()
    main_module.PwrTray.build_menu(tray, rows=rows)
    return tray, [action.text for action in tray.menu.actions]


def test_confirm_mode_marks_the_session_ending_entries(monkeypatch):
    _tray, labels = _confirming_labels(monkeypatch)

    assert '\u2003\u2003\u2003▷ Log Off (click twice)' in labels
    assert '\u2003\u2003\u2003\u2003▼ Reboot (click twice)' in labels
    assert '\u2003\u2003\u2003\u2003▼ PowerOff (click twice)' in labels
    # Suspend is reversible, so it is never marked.
    assert 'System: \u2003▼ Suspend' in labels
    # Nothing is armed yet.
    assert not any(label.endswith('(click NOW)') for label in labels)


def test_only_the_armed_entry_says_click_now(monkeypatch):
    tray = _confirming_tray()
    tray.arm_or_run('poweroff', 'Power off', lambda _checked=None: None)

    _tray, labels = _confirming_labels(monkeypatch, tray=tray)

    assert '\u2003\u2003\u2003\u2003▼ PowerOff (click NOW)' in labels
    # One "NOW" and only one: arming the same or another entry replaces it.
    assert sum(label.endswith('(click NOW)') for label in labels) == 1
    assert '\u2003\u2003\u2003▷ Log Off (click twice)' in labels


def test_a_desktop_without_a_logoff_command_gets_no_row(monkeypatch):
    """A `logoff: ""` hides the row, the rule Reload and Restart already follow.

    The row used to be added unconditionally, so an unbound action was a click
    that did nothing -- the opposite of what the neighbouring rows promise.
    """
    labels = _menu_labels(monkeypatch, variables={
        'monitors_off': '', 'reload_wm': '', 'restart_wm': '', 'logoff': ''})

    assert not any('Log Off' in label for label in labels)


def test_a_desktop_can_make_its_restart_session_ending(monkeypatch):
    """Requirement: a reload/restart that kills every app is gated like log off.

    `session_end` in commands.yaml decides, per desktop, so a desktop whose
    restart takes the clients with it lists `restart_wm` there -- as the
    shipped `wayland` session type does.  A reload is never listed: it keeps
    the apps.
    """
    tray = _confirming_tray(variables={
        'monitors_off': '',
        'reload_wm': 'swaymsg reload',
        'restart_wm': 'swaymsg exit',
        'session_end': ['logoff', 'poweroff', 'reboot', 'restart_wm'],
    })

    _tray, labels = _confirming_labels(monkeypatch, tray=tray)

    assert '\u2003\u2003\u2003▷ Restart (click twice)' in labels
    assert '\u2003\u2003\u2003▷ Reload' in labels
    assert '\u2003\u2003\u2003▷ Reload (click twice)' not in labels


def test_build_menu_labels_use_only_allowed_glyphs(monkeypatch):
    for label in _menu_labels(monkeypatch):
        found = _offenders(label)
        assert not found, _report(found, f'menu label {label!r}')


def test_the_player_ctl_entry_reads_as_a_state(monkeypatch):
    """Idle/Busy are states, so the off word must be one too, not "Disable"."""
    labels = _menu_labels(monkeypatch, enable_playerctl=False)

    assert '♫ PlayerCtl: Disabled' in labels
    assert not any(label.endswith('PlayerCtl: Disable') for label in labels)


def test_the_player_ctl_entry_is_hidden_when_it_cannot_work(monkeypatch):
    """No playerctl and no bus: the click would be swallowed, so no row.

    Same rule as Reload/Restart, which only appear when the desktop defines a
    command for them.
    """
    labels = _menu_labels(monkeypatch, has_playerctl=False)

    assert not any('PlayerCtl' in label for label in labels)


def test_build_menu_still_offers_the_expected_actions(monkeypatch):
    labels = _menu_labels(monkeypatch)

    assert '≡  Open Applet Log' in labels
    assert '✎  Edit Applet Config' in labels
    assert '♫ PlayerCtl: Idle' in labels
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

    assert '↯ Plugged In 100%' in labels


def test_a_full_battery_still_shows_its_percentage(monkeypatch):
    """The charge level is the battery status, so it is never hidden.

    It used to be dropped whenever the machine was on mains at 100%, which is
    exactly the reading a user on battery reported as "not showing the battery
    status".
    """
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='Settings', percent=100,
                                      plugged=True, present=True))

    assert '↯ Plugged In 100%' in labels


def test_the_reading_decides_plugged_in_not_the_selector(monkeypatch):
    """``Settings`` is both the plugged-in bucket and the start-up default.

    A machine whose battery was read as discharging is on battery whatever the
    selector still says, so the label must not claim "Plugged In" for it.
    """
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='HiBattery', percent=42,
                                      plugged=False, present=True))

    assert '█ HiBattery 42%' in labels
    assert not any('Plugged In' in label for label in labels)


def test_a_fractional_reading_is_not_padded(monkeypatch):
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='HiBattery', percent=98.3,
                                      plugged=False, present=True))

    assert '█ HiBattery 98.3%' in labels


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

    assert '↯ Plugged In 100%' in labels
    assert not any('TESTING' in label for label in labels)


def test_real_battery_state_is_not_marked_as_testing(monkeypatch):
    labels = _menu_labels(
        monkeypatch,
        battery=types.SimpleNamespace(selector='Settings', percent=100,
                                      present=True, synthetic=False))

    assert '↯ Plugged In 100%' in labels
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
                                         selector='Settings', percent=100,
                                         plugged=True)
    tray.rebuild_menu = False
    tray._inhibit_dirty = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)

    seen = []
    for _ in range(3):
        tray._toggle_battery()
        seen.append((tray.battery.selector, tray.battery.synthetic,
                     tray.battery.percent))

    assert seen == [('HiBattery', True, 100), ('LoBattery', True, 20),
                    ('Settings', False, 100)]
    assert tray.rebuild_menu is True


def test_cycling_back_to_plugged_in_drops_the_testing_suffix(monkeypatch):
    """A full Settings -> HiBattery -> LoBattery -> Settings run ends clean."""
    cycler = object.__new__(main_module.PwrTray)
    cycler.battery = types.SimpleNamespace(present=False, synthetic=False,
                                           selector='Settings', percent=100,
                                           plugged=True)
    cycler.rebuild_menu = False
    cycler._inhibit_dirty = False
    cycler.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)
    for _ in range(3):
        cycler._toggle_battery()

    labels = _menu_labels(monkeypatch, battery=cycler.battery)

    assert '↯ Plugged In 100%' in labels
    assert not any('TESTING' in label for label in labels)


def test_the_test_tiers_start_high_and_at_the_low_threshold():
    """HiBattery starts full; LoBattery starts at the top of its range.

    The threshold is the highest percentage that still selects LoBattery, so
    the second click lands exactly there before the countdown begins.
    """
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=False, synthetic=False,
                                         selector='Settings', percent=100,
                                         plugged=True)
    tray.rebuild_menu = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)

    tray._toggle_battery()
    assert (tray.battery.selector, tray.battery.percent,
            tray.battery.plugged) == ('HiBattery', 100, False)

    tray._toggle_battery()
    assert (tray.battery.selector, tray.battery.percent,
            tray.battery.plugged) == ('LoBattery', 20, False)


def test_hibattery_counts_down_but_stays_above_the_threshold():
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=False, synthetic=False,
                                         selector='Settings', percent=100,
                                         plugged=True)
    tray.rebuild_menu = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)

    tray._toggle_battery()
    tray.rebuild_menu = False
    for _ in range(200):
        tray._advance_test_battery()

    assert (tray.battery.selector, tray.battery.percent) == ('HiBattery', 21)
    assert tray.rebuild_menu is True


def test_lobattery_counts_down_to_zero():
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=False, synthetic=False,
                                         selector='Settings', percent=100,
                                         plugged=True)
    tray.rebuild_menu = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)

    tray._toggle_battery()
    tray._toggle_battery()
    for _ in range(50):
        tray._advance_test_battery()

    assert (tray.battery.selector, tray.battery.percent) == ('LoBattery', 0)


def test_plugged_in_and_real_batteries_do_not_count_down():
    tray = object.__new__(main_module.PwrTray)
    tray.battery = types.SimpleNamespace(present=False, synthetic=False,
                                         selector='Settings', percent=100,
                                         plugged=True)
    tray.rebuild_menu = False
    tray.get_params = lambda: types.SimpleNamespace(lo_battery_pct=20)

    for _ in range(3):
        tray._toggle_battery()
    tray._advance_test_battery()
    assert tray.battery.percent == 100

    tray.battery.synthetic = False
    tray.battery.selector = 'HiBattery'
    tray.battery.percent = 77
    tray._advance_test_battery()
    assert tray.battery.percent == 77
