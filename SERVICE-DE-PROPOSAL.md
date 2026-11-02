# One constant unit, `[ServiceAllowed]` in config.ini, and a service that cannot block logout

Status: **implemented** (this file is the rationale; the behaviour is pinned by
the tests named in §10). Two details changed while implementing, and both are
noted where they matter:

* the "is this a service run?" test is an explicit `--svc-mode` on
  `ExecStart=`, not an `INVOCATION_ID` check — that variable is inherited by
  every child of a unit, so a terminal opened inside one made a hand-started
  pwr-tray look like the service (see §6.1);
* `config_path()` follows `IniTool` (`$HOME/.config/pwr-tray/config.ini`) and
  not `$XDG_CONFIG_HOME`, because the applet reads the section through IniTool
  and a verb must not write a file nobody reads (§4).

Accepted:

1. The set of desktops lives in `config.ini`, not in the unit (`ExecStart=`).
2. The ten shipped unit files collapse to one.
3. Exit code for "this session is not allowed" — details in §7.
4. No `-d all`.

Behaviour spec accepted from the review:

* (a) new `[ServiceAllowed]` section listing every supported compound desktop,
  `False` initially, e.g. `i3-x11 = True`;
* (b) the user may edit that section by hand;
* (c) `--svc-install` writes the unit if absent and sets the current desktop to
  `True`; (d) `--svc-uninstall` sets it to `False` and removes the unit only
  when no desktop is `True`;
* (e) start/stop keep today's meaning; (f) every `--svc-*` verb reports the
  allowed desktops; (g) "allow them all" = one `--svc-install`, then edit, or
  visit each DE once.

## 1. Why this model is better than the one I first proposed

Because the policy leaves the unit, the unit becomes a **constant**: no `--de`,
no `--desktops`, no session snapshot. `--svc-install` in niri then produces
byte-identical text to `--svc-install` in i3, so `--svc-status`'s "the file
differs from the shipped unit" keeps its meaning across sessions and machines,
and a stale snapshot can no longer decide where the applet runs. The gate moves
to the one place that can observe the live session: the applet.

## 2. The unit

`pwr_tray/resources/systemd/pwr-tray.service` (the ten per-desktop files go
away; `templates()`, `template_for()`, `render()` and `SESSION_KEYS` go with
them, because there is nothing left to substitute):

```ini
# pwr-tray as a systemd --user service.
#
#   pwr-tray --svc-install          # in each desktop that should run it
#   pwr-tray --svc-start
#
# Which desktops are allowed is NOT decided here: it is [ServiceAllowed] in
# ~/.config/pwr-tray/config.ini, because no unit file can know what happened in
# the session that is running now.  The applet asks the live session itself and
# exits 0 anywhere it is not wanted.
[Unit]
Description=pwr-tray (power/energy tray applet)
PartOf=graphical-session.target
After=graphical-session.target
BindsTo=graphical-session.target
StartLimitIntervalSec=60
StartLimitBurst=5
Requires=dbus.socket
After=dbus.socket

[Service]
Type=simple
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus
# A user unit can inherit a SESSION_MANAGER left behind by an earlier KDE
# login; joining it would make Qt open an ICE connection to a session that is
# not ours -- one more client ksmserver waits for at logout.
UnsetEnvironment=SESSION_MANAGER
# Deliberately no Environment=XDG_CURRENT_DESKTOP / XDG_SESSION_TYPE / display
# here.  Those lines were a snapshot of the login that installed the unit and
# they outrank the live session.  pwr_tray/Session.py derives them from the
# listening socket and loginctl before Qt starts.
ExecStart=%h/.local/bin/pwr-tray
# `on-failure`, never `always`: a logout signals the applet, and `always`
# answers by starting it again while the session is draining, so the session
# never empties and the logout hangs (measured in luks-tray).  `on-failure`
# still recovers a crash (SIGSEGV, SIGKILL, non-zero exit).
Restart=on-failure
RestartSec=5
# 2 = the applet could not be constructed, 3 = another instance holds the lock.
# systemd already treats SIGHUP/SIGINT/SIGTERM/SIGPIPE as a clean exit for
# `on-failure`; they are listed so an edit to `Restart=` cannot silently turn a
# logout back into a respawn.
RestartPreventExitStatus=2 3 SIGTERM SIGHUP SIGINT SIGPIPE
# Default is 90s: that is how long a wedged Qt event loop may hold a logout.
# 5s is ample to stop swayidle and the dbus-monitor children, and the default
# KillMode=control-group then SIGKILLs the cgroup, swayidle included.
TimeoutStopSec=5
SendSIGKILL=yes
StandardOutput=append:%h/.config/pwr-tray/debug.log
StandardError=inherit

[Install]
WantedBy=graphical-session.target
```

## 3. The section

```ini
[ServiceAllowed]
# Which desktops may run pwr-tray as a systemd service.  `pwr-tray --svc-install`
# sets the desktop you are logged into to True, and `--svc-uninstall` sets it to
# False; edit by hand to allow or forbid others.  False means the service
# starts and exits immediately (it does not fail, and it does not restart).
i3-x11 = False
sway-wayland = False
hyprland-wayland = False
niri-wayland = False
kde-x11 = False
kde-wayland = False
xfce-x11 = False
cinnamon-x11 = False
mate-x11 = False
lxqt-x11 = False
```

* The key list is **not** hard-coded: it is the `desktops:` keys of
  `commands.yaml`, the same ten names the unit files use today, and a test pins
  the two together so a new desktop cannot appear without a row.
* `IniTool._apply_section()` already has the `'true'`/`'false'` boolean parser
  (`pwr_tray/IniTool.py:276`), so this section can be read with the same
  vocabulary the rest of `config.ini` uses. `_parse_config()` iterates only
  `get_selectors()` (Settings/HiBattery/LoBattery), so an extra section is
  ignored by the existing parser — no change to the tier logic.
* `configparser` lowercases option names. Every desktop key is already
  lowercase; a test asserts that stays true (a future `foo:bar` key must not
  become silently unreadable).

## 4. Reading and writing the section without destroying the file

`IniTool` writes `config.ini` **only when the file does not exist**
(`ensure_ini_file()`, `pwr_tray/IniTool.py:135`); after that it only reads. That
is why hand editing works today, and it is the property (b) depends on. So the
service verbs must not use `configparser.write()`, which would reorder the file
and delete every comment the user wrote.

Add one pure function and reuse the existing write step:

```python
IniTool.set_flag_text(text, key, value) -> str
```

* the section exists and the key exists → replace only the value token, keeping
  indentation and any inline comment (`i3-x11 = False  # desktop at work`);
* the section exists, the key is new → append the line after the section's last
  key, so the file stays grouped;
* the section is missing → append the commented header and all ten keys;
* the file is missing → `ensure_ini_file()`'s defaults plus the section.

`service.py` then emits an ordinary `Step(write=(ini_path, new_text))`, so the
edit appears in the "about to:" list, `execute()` is untouched, and the whole
transform is testable as text in / text out. Write through a temporary file and
`os.replace()` so a full disk cannot truncate `config.ini`.

## 5. Verb semantics (final)

| Verb | `[ServiceAllowed]` | unit file | systemctl |
|---|---|---|---|
| `--svc-status` | read only (never writes) | read | `is-enabled`/`is-active` |
| `--svc-install` | seeds the section if missing; sets the **current desktop** to `True` | writes it if absent; `daemon-reload` only when it wrote | `enable` if disabled (does not start, as today) |
| `--svc-start` | read | no | `start`; refuses first if this desktop is `False` |
| `--svc-restart` | read | no | `restart`; refuses first if this desktop is `False` |
| `--svc-stop` | no | no | `stop` (stays enabled) |
| `--svc-uninstall` | sets the **current desktop** to `False` | removes only when no desktop is `True` | `stop` if running; `disable` + `daemon-reload` only when removing |
| `--show-svc-file` | no | prints the constant unit | — |

Every verb ends with the same footer (f):

```
  desktops allowed: i3-x11, niri-wayland
  this session: niri-wayland (allowed)
```

and, where it matters, the reason: `this session: kde-wayland (not allowed;
run pwr-tray --svc-install here to allow it)`.

Two consequences worth stating in the docs, because they are the only places
where the new model is less obvious than the old one:

1. **`--svc-uninstall` sometimes leaves the unit installed.** In i3, with
   `niri-wayland` still `True`, it stops the applet here, sets `i3-x11 = False`,
   and says `pwr-tray.service stays installed for: niri-wayland`. Deleting the
   unit is the last-desktop event, not the verb. To clear everything: set all
   rows to `False` (hand edit), then run `--svc-uninstall` once and it prunes.
2. **`--svc-install` does not start anything.** If the unit was already
   installed and idle (it exited 0 here earlier), flipping the flag to `True`
   leaves it idle until the next login, so the verb prints
   `run pwr-tray --svc-start to show the tray now`.

## 6. App side

**6.1 The gate** — in `main()`, after the config and log are settled, before
`QApplication`, before the lock, before the tray:

```python
if opts.svc_mode:                    # the shipped unit starts us with it
    session_desktop = Desktop.detect_de(...)['name']
    if not service.desktop_allowed(session_desktop):
        prt(...)
        return                       # exit 0; see §7
```

* `--svc-mode` is on the unit's `ExecStart=`.  A manual run, an `exec` line in
  i3 and a terminal run do not pass it, so they are never gated:
  `[ServiceAllowed]` governs **the service**, and a section with that name must
  not break i3's autostart;
* with the flag, `[ServiceAllowed]` is compared against the **detected** desktop
  — so detection runs before Qt, on the repaired environment (§6.2);
* an unreadable/absent section counts as "nothing allowed" and says so in the
  log; an unknown desktop is `detect_de()`'s existing failure, exit 2.

An environment check (`INVOCATION_ID`, `JOURNAL_STREAM`, `SYSTEMD_EXEC_PID`)
was tried first and rejected on evidence: those variables are **inherited by
every child of a unit**, so a terminal opened inside a service-run session makes
a hand-started pwr-tray look like the service.  The flag is explicit, testable,
and its failure mode is the safe one (a hand-edited unit that drops it simply
disables the gate).

**6.2 `pwr_tray/Session.py`** — a port of `luks_tray/Session.py`, called before
`QApplication` exists, correcting `os.environ` in place:

* `session_values()` — a display server is listening when its socket exists (a
  compositor's in `$XDG_RUNTIME_DIR`, an X server's in `/tmp/.X11-unix`), so a
  snapshot naming a dead server is dropped;
* `_named_kind()` — when both exist (Xwayland), the session marker decides and
  the live second display is kept;
* `_from_loginctl()` — `show-user … -p Display` gives the session id,
  `show-session … -p Type -p Desktop` the type and the desktop name (Wayland
  reports an empty `Display=`, verified on this machine, so the `wayland-N`
  socket is found in the runtime directory);
* unlike luks-tray, `XDG_CURRENT_DESKTOP`/`XDG_SESSION_DESKTOP`/
  `DESKTOP_SESSION` are corrected too — they now *route* the gate, so a stale
  one would make pwr-tray refuse to run in the desktop it is allowed in;
* a correct launch changes and logs nothing; a correction is logged with both
  values.

This is still required even though the unit no longer pins anything: the unit
inherits the user manager's environment, which is a snapshot of whichever login
started `systemd --user`, and on a multi-DE machine that is exactly the value
that goes stale.

**6.3 Clean stop** — the other half of "never blocks logout":

* `signal.signal(SIGTERM/SIGHUP, lambda *_: tray.app.quit())`, so `atexit` runs:
  the lock is released, `goodbye()` writes its `ENDED` line (today a
  `systemctl --user stop` looks like a kill in the log), and `swayidle` is
  stopped instead of waiting for `KillMode=control-group`;
* `SwayIdleManager.stop()` (`pwr_tray/SwayIdleMgr.py:113`) is not `None`-safe
  and is never called from the exit path — fix both.

With `TimeoutStopSec=5`, a stop is bounded at five seconds even if the event
loop is wedged.

**6.4 Switching between desktops — what starts the unit, and what does not**

| Situation | Starts? | Why |
|---|---|---|
| log out of one allowed desktop, log in to another | **yes** | the outgoing session manager stops `graphical-session.target`, the new one starts it, and `WantedBy=graphical-session.target` produces the start job |
| switch to a second session while the first is still logged in (`dm-tool switch-user`, `loginctl activate`, a second VT) | **no** | `graphical-session.target` belongs to the *user manager*, not to a session. It is already active, `start` on an active target is a no-op, so no start job exists for `WantedBy=` to act on |
| `--svc-stop`, then back to that desktop later in the same session | **no** | nothing pulls the unit until the target is started again |
| `pwr-tray --svc-start` | **yes** | an explicit job, and a no-op when the unit already runs |
| leaving a `False` desktop for a `True` one inside the same live session | **no** — the gate is not even consulted | the applet is not running; nothing asks systemd for a start |

No exit code or directive changes the two "no" rows:

* `WantedBy=graphical-session.target` reacts only to the target being
  **started**; an active target absorbs a second `start` and creates no job;
* `BindsTo=`/`PartOf=` propagate **stops** only;
* `Restart=on-failure` governs only the unit that just exited, and exit `0` (or
  `4` + `SuccessExitStatus=4`) explicitly tells systemd *not* to bring it back.

This is not a pwr-tray defect: `waybar.service` and `nm-applet.service` behave
identically in a second concurrent session, for the same reason. The fix is the
session's own startup hook, which every WM already has and which the README
already tells users to edit:

```
# i3
exec systemctl --user start pwr-tray.service
# niri
spawn-at-startup "systemctl" "--user" "start" "pwr-tray.service"
```

`start` on a running unit is a no-op, so the line is safe in every case, and it
is the only trigger that does not depend on the target having been stopped. The
README's per-DE snippets should carry it **instead of** a bare `pwr-tray`
autostart: a bare autostart also works, but then the WM owns the process and
systemd does not — the double-supervisor situation the single-instance lock
exists to prevent.

Tempting and rejected: exit non-zero and leave the code out of
`RestartPreventExitStatus`, so `Restart=on-failure` polls for the switch.
`StartLimitBurst=5` at `RestartSec=5` covers 25 s inside the 60 s window, then
systemd marks the unit `failed`: it would only catch a switch inside that
minute, and it paints a red unit for a normal condition.

## 7. Decision 3, in detail: exit 0 or a distinct code

The applet, when systemd started it and this desktop is `False`, has to exit
*somehow*. What systemd then does with each choice:

| app exit | unit directives | what systemd does | `systemctl --user status` |
|---|---|---|---|
| `0` | `Restart=on-failure` | deactivates successfully, no restart | `inactive (dead)` — clean, no red |
| `0` | `Restart=always` | restarts 5s later, forever | `active`, and the logout bug is back |
| `4` | `Restart=on-failure` | counts as failure → restarts, trips `StartLimitBurst=5` in 60s, gives up | `failed` (red) at **every** login |
| `4` | `Restart=on-failure` + `RestartPreventExitStatus=4` | no restart, still a failure | `failed` (red) — the flag only stops the loop |
| `4` | `Restart=on-failure` + `SuccessExitStatus=4` | treated as success, no restart | `inactive (dead)` — clean, and `$?` is 4 |

The only two clean options are the first row and the last row; the middle rows
are what a "distinct failure code" gets you if it is not paired with
`SuccessExitStatus=`, and they are bad: a deliberately idle unit showing red in
every KDE login trains people to ignore `systemctl --user status`.

So the real question is whether the exit status must be *readable by a human or
a script*. It is not:

* systemd is the only reader (the gate never applies to a manual launch, §6.1),
  and both options look identical to it;
* `--svc-start` and `--svc-restart` refuse **before** handing anything to
  systemctl, so they never depend on the code;
* `--svc-status` reports the desktop list and the reason in prose, not via a
  code.

**Recommendation: exit `0`.** One directive fewer (`SuccessExitStatus=4`) and
one fewer number that has to stay in sync with the code; the explanation lives
in the log and in `--svc-status`:

```
not allowed in this session (kde-wayland); [ServiceAllowed] enables
i3-x11, niri-wayland; exiting 0
```

A test pins the exit status with that reasoning in its docstring, so nobody
"improves" it into a failure code and re-creates the red-unit noise. If you
would rather have `pwr-tray` itself return a distinguishable status (so `if
pwr-tray` in a script can tell "not hired here" from "ran"), the same clean
state is available as exit `4` plus `SuccessExitStatus=4` in the unit — the
gate would then have to apply to manual launches too, which conflicts with (b)
being about the *service*.

## 8. Does the resulting model hold together?

Yes, with these six points stated in the docs/tests:

1. **The unit no longer varies per desktop**, so install from any session, on
   any machine, is the same bytes; `--svc-status` "differs from shipped" is
   once again a real signal.
2. **Nothing is allowed until a desktop is visited** (or the file is edited).
   That is (a) as specified; it also means the section must be seeded by the
   first `--svc-install`, and `--svc-status` must name the exact next step when
   the current desktop is `False`.
3. **`--svc-uninstall` is "opt out here"**, not "remove pwr-tray" — see §5.1.
4. **`--svc-install` is "opt in here"**, not "start now" — see §5.2.
5. **The gate is service-only** (`--svc-mode`), so i3/Hyprland `exec` lines and
   terminal runs are unaffected by the section.
6. **Logout safety does not depend on the section at all**: `Restart=on-failure`,
   the signal list, `TimeoutStopSec=5`, `UnsetEnvironment=SESSION_MANAGER` and
   the SIGTERM handler are what make any logout, allowed or not, finish.
7. **A desktop switch is automatic only across a logout** (§6.4). Moving to a
   second session that is already logged in needs the WM startup hook
   (`systemctl --user start pwr-tray.service`) or `--svc-start`; no exit code and
   no restart policy can substitute for a start job.

## 9. Migration

* An existing install has `--de <one desktop>` and `Restart=always`;
  `--svc-status` already says "the file differs from the shipped unit", so one
  `pwr-tray --svc-install` (in each desktop that should run it) replaces it.
* The section is created on the first `--svc-install`; every desktop is
  `False` except the one you ran it in. `--svc-status` prints the list, so the
  "why is there no tray here" question is answered by one command.
* The ten starter files are gone; `--show-svc-file` prints the one unit to copy.

## 10. Tests

* `tests/test_service_examples.py` — one unit; the logout bug pinned:
  `Restart=on-failure` (docstring naming the measured luks-tray cause),
  `RestartPreventExitStatus ⊇ {2, 3, SIGTERM, SIGHUP, SIGINT, SIGPIPE}`,
  `TimeoutStopSec` present and ≤ 10, `UnsetEnvironment=SESSION_MANAGER`,
  `ExecStart` names the bare launcher (no `--de`, no `--desktops`), no
  `Environment=XDG_CURRENT_DESKTOP=` / `XDG_SESSION_TYPE=` / display pin (the
  snapshot class of bug must not return).
* `tests/test_config_service_allowed.py` — `set_flag_text()` truth table
  (existing key with inline comment, new key, missing section, missing file,
  CRLF), comment preservation, atomic replace, and "the seeded key list equals
  `commands.yaml`'s desktops".
* `tests/test_service_commands.py` — install flips the current desktop to
  `True` and writes the unit only when absent; uninstall flips it to `False`,
  stops, prunes only when nothing is allowed; start/restart refuse outside the
  set; the footer lists the allowed desktops; `--svc-status` exits `1` when
  installed-but-not-allowed-here.
* `tests/test_desktop_gate.py` — exit `0` under `--svc-mode` with one log line
  and no lock file; runs normally without the flag; exit `2` for an unknown
  desktop; an unreadable section allows nothing and says so.
* `tests/test_session.py` — ported from luks-tray (both directions, dead
  socket, Xwayland, loginctl paths including `Desktop=`).

## 11. Verification on the machine

```
pwr-tray --svc-install            # in niri
pwr-tray --svc-status             # desktops allowed: niri-wayland; this session: niri-wayland
systemctl --user show -p Restart -p TimeoutStopUSec -p KillMode pwr-tray
pwr-tray --svc-start              # in KDE: refuses and names --svc-install
systemctl --user is-active graphical-session.target   # one target for the user, not per session
systemctl --user list-units 'pwr-tray*'   # after a KDE logout: nothing respawned
journalctl --user -u pwr-tray -b --no-pager | tail -20
```
The real test is the one you already know: log out of KDE X11 with
`kde-x11 = False` (expect instant logout, one `exiting 0` line) and log out of
niri (expect instant logout, `ENDED`, no respawn).

## 12. The three small choices, as settled

1. `--svc-uninstall` with other desktops still allowed keeps the unit installed
   and enabled, stops the applet here, and says so:
   `pwr-tray.service stays installed and enabled for: niri-wayland`.
2. `--svc-install` never starts anything. When it only flipped a flag on an
   already-installed, already-enabled unit it prints
   `run pwr-tray --svc-start to show the tray now`.
3. The disallowed path exits `0` (§7), pinned by
   `tests/test_desktop_gate.py::test_a_disallowed_desktop_exits_cleanly_and_touches_nothing`.
