# pwr-tray

`pwr-tray` is a PyQt5 tray applet for power/energy saving and system/DE controls. Tested DEs: i3wm, sway, Hyprland, niri and KDE (X11 and Wayland); built-in configs also cover XFCE, Cinnamon, MATE and LXQt. `systemd` is required.

Right-click to open the menu, left-click a line to act: switch to Presentation Mode, change screen-lock and sleep timeouts, log off, lock and blank monitors, and more. The icon shows the current state:

* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/FullSun-v03.svg?raw=true" alt="FullSun" width="24" height="24"> Presentation Mode (i.e., the full sun)
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/SettingSun-v03.svg?raw=true" alt="SettingSun" width="24" height="24"> SleepAfterLock Mode (i.e., the setting sun)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/RisingMoon-v03.svg?raw=true" alt="RisingMoon" width="24" height="24"> SleepAfterLock Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/Unlocked-v03.svg?raw=true" alt="Unlocked" width="24" height="24"> LockOnly Mode (i.e., the unlocked lock)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/UnlockedMoon-v03.svg?raw=true" alt="UnlockedMoon" width="24" height="24"> LockOnly Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/GoingDown-v03.svg?raw=true" alt="GoingDown" width="24" height="24"> LowBattery State (going down).
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/PlayingNow-v03.svg?raw=true" alt="PlayingNow" width="24" height="24"> Inhibited by playing media.
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/StopSign-v03.svg?raw=true" alt="StopSign" width="24" height="24"> Inhibited by systemd inhibitors.

Installation, configuration, the CLI and per-DE notes are in [INSTALLATION AND CLI](#installation-and-cli).

---

### Why pwr-tray?

Power management is usually configured once, per desktop, and then lived with. The alternatives each cover part of the problem:

| Option | Covers | Gap it leaves |
|--------|--------|---------------|
| `swayidle` / `hypridle` config | Lock + sleep timers on one compositor | No on-screen state, no inhibitor visibility, no per-battery-tier policy, no manual "keep awake" toggle |
| DE power manager (Powerdevil, XFCE, Cinnamon, MATE, LXQt) | Native, integrated, vendor-maintained | Confined to its own DE; conflicts with `swayidle`-style tools; no uniform behaviour when you switch WMs |
| `systemd-inhibit` wrappers / caffeine-style tools | One-shot "don't suspend now" | No scheduling, no sequencing of lock→sleep, no feedback about *who* is inhibiting |
| Hand-rolled shell scripts | Exactly your policy | You own the state machine, the inhibit parsing, the tray, and the edge cases across DEs |

`pwr-tray` occupies the niche that table cannot fill: **one policy engine and one tray UI across ten desktops**, with lock→sleep sequencing, per-battery-tier timers, visible inhibition, and a manual override -- the same behaviour on i3, sway, niri, Hyprland, KDE, XFCE, Cinnamon, MATE, and LXQt. Set the policy once and it follows you from one WM to the next.

---

### HowTo Use pwr-tray

Three *major power modes* control what the timeouts do:
- **◉ Presentation ←** - keeps the screen unlocked/on and the system up.
- **◐ LockOnly ←** - keeps the system up; the screen may lock.
- **○ SleepAfterLock ←** - allows screen locking and the system to go down (the normal mode).

Locking, blanking and DE operations:
- **▷ Lock Screen** - locks the screen immediately.
- **▷ Blank Monitors** - locks, then blanks the screen.
- **▷ Reload** - DE-dependent reloads (WM config, etc.).
- **▷ Log Off** - terminates the session.

System state:
- **▼ Suspend System** - suspends immediately.
- **▼ Reboot System** - reboots immediately.
- **▼ Poweroff System** - powers down immediately.

Status lines:
- **↯ Plugged In 100%** (or **█ HiBattery 87%**, **▃ LoBattery 8%**) - power state and charge. A machine with no battery shows **↯ Plugged In** alone; clicking it cycles fabricated battery states for testing (see Post-Install Quick Tests).
- **♺ Chg Screen Idle: 15m->30m** - click for the next screen-idle timeout.
- **♺ Chg System Idle: 5m->30m** - click for the next system-idle timeout.
- **♫ PlayerCtl** - shows the media-inhibition state; when installed, a click toggles it.

The applet itself:
- **✎ Edit Applet Config** - edits `config.ini`.
- **≡ Open Applet Log** - follows `debug.log` in a terminal window; offered when a terminal is found (see `log_viewer`).
- **☓ Quit this Applet** - exits the applet. Under the systemd unit it also stops the unit for this session; it stays enabled and returns at your next login.
- **↺ Restart pwr-tray vX.Y.Z** - restarts the applet, showing the version it runs. Under the systemd unit this asks systemd to restart the unit, so the new instance is the one the unit's `ExecStart=` launches.

---

## INSTALLATION AND CLI

### Requirements
* **Python** 3.11+
* **systemd** (`systemctl`, `loginctl`, `busctl`, `systemd-inhibit`; the `--svc-*` verbs also need a reachable `systemd --user` manager)
* **UPower** (optional, but present on most desktops; its D-Bus signals carry battery state and AC changes, so the applet never polls)
* **pipx** (recommended for installation)

Per-DE requirements:

| DE | Required Packages | Notes |
|----|-------------------|-------|
| **i3wm** | `i3lock`, `xprintidle`, `xset` | X11 only; `xss-lock` recommended (see below) |
| **sway** | `swaylock`, `swayidle` (1.8+) | `pwr-tray` manages `swayidle` |
| **Hyprland** | `swaylock`, `swayidle` (1.8+) | `pwr-tray` manages `swayidle` |
| **niri** | `niri`, `swaylock`, `swayidle` (1.8+) | Wayland only; `pwr-tray` manages `swayidle` |
| **KDE Wayland** | `swayidle` (1.8+) | Requires **Plasma 6**; `pwr-tray` manages `swayidle` |
| **KDE X11** | `xprintidle`, `xset` | Plasma 6 (Plasma 5 untested); `qdbus`/`qdbus6` auto-detected |
| **XFCE, Cinnamon, MATE, LXQt** | `xprintidle`, `xset` | X11 only; built-in configs |

**Distro notes:**
* Developed and tested on **Debian 13 (trixie)**; should work on any distro with sufficiently recent packages.
* KDE Wayland requires **Plasma 6** and **swayidle 1.8+**. Debian 12 and Ubuntu 24.04 ship Plasma 5 and swayidle 1.7, so KDE Wayland will **not** work there; i3 and sway are fine.
* Arch Linux and Fedora 40+ ship current versions and should work for all DEs.

### HowTo Install and Start pwr-tray
* `pipx install pwr-tray` (to run a source checkout instead, see [`README-DEV.md`](README-DEV.md))
* Manually run as `pwr-tray -o`, which:
    * creates `~/.config/pwr-tray/config.ini` and copies the default DE commands to `~/.config/pwr-tray/commands.yaml`;
    * detects your desktop -- if none is recognised it prints the session variables it saw and exits (use `--de NAME` to force one; `pwr-tray --list-des` lists every known desktop and its commands, marking any missing from `$PATH`);
    * checks the DE-dependent commands and exits with an install hint if any are missing (`systemctl` is always required).
* Playing media inhibits locking and sleeping over MPRIS on the session bus, so no extra package is needed. Install `playerctl` only as a fallback.
* **PyQt5 is installed for you** as a declared dependency. A `ModuleNotFoundError: No module named 'PyQt5'` means a different interpreter ran pwr-tray; add PyQt5 to that one (`pipx inject pwr-tray PyQt5`), or to the system Python if that is what runs it (`sudo apt install python3-pyqt5` on Debian/Ubuntu, `sudo pacman -S python-pyqt5` on Arch, `sudo dnf install python3-qt5` on Fedora).
* Autostart `pwr-tray` however your DE/WM manages autostarts, or start it in the background with `setsid pwr-tray`. Only one instance runs: a plain start **refuses and exits** if one is running; `setsid pwr-tray --replace` deliberately replaces it.
* Start order against your status bar does not matter: the applet registers its icon with the bar as soon as the bar's tray appears, so no `sleep` is needed.

#### Updating pwr-tray
`pipx upgrade pwr-tray` changes the installed code; `pwr-tray --svc-restart` makes the running service use it. The unit always starts `~/.local/bin/pwr-tray`, so it never needs rewriting.

### Post-Install Quick Tests of pwr-tray
Hardware varies, so before investing too much in a full setup you may want to kick the tires. `pwr-tray` has built-in manual test aids:

* `pwr-tray --quick` sets the lock and sleep timeouts to 1 minute and runs double-time (they expire in 30s). The default mode, `SleepAfterLock`, exercises the most code paths.
* Check that closing the lid, pressing the power button, etc. have the desired effects.
* **systemd inhibits:** `systemd-inhibit --why="Prevent sleep for demonstration" sleep infinity` shows the StopSign icon. Only an inhibitor that blocks `sleep` or `idle` counts; one holding `handle-power-key`/`handle-lid-switch` does not.
* **Media inhibit:** play media in any MPRIS-capable player. The `PlayingNow` icon appears, `PlayerCtl` changes from `Idle` to `Busy: <player>` (or `Busy: 2+<player>`), and the timeouts are suspended until playback stops. No `playerctl` needed.
* **Hi/Lo Battery states (machines without a battery):** clicking the battery line steps `Plugged In` → `HiBattery` → `LoBattery` → `Plugged In`. `HiBattery` starts full and counts down to just above `lo_battery_pct`; `LoBattery` starts at `lo_battery_pct` and counts down to 0%, where that tier's `power_down` policy can act. Fabricated states carry a `(TESTING)` suffix; the real plugged-in state does not.

#### The battery line looks wrong

`pwr-tray --battery` prints the reading and the devices it came from, then exits:

```
power: present=True plugged=False percent=98.3 status=discharging source=sysfs
[AC(mains,online=0) BAT0(battery,discharging,98%) CROS_USBPD_CHARGER0(usb,online=0,not charging)]
```

- `plugged` is what the menu line follows. It comes from UPower when its D-Bus events are available, else from `/sys/class/power_supply` -- never from the mains `online` flag alone, because on some machines (Chromebooks on `cros_ec`) that flag stays `1` while the battery says `discharging`.
- `CONFLICT(mains-vs-battery)` means exactly that disagreement; the battery wins, and the debug log records the line when the state changes.
- If `status` says `charging` with the charger out, the kernel/EC reports it, not the applet. Compare with `upower -i /org/freedesktop/UPower/devices/battery_BAT0` and `cat /sys/class/power_supply/BAT0/status`.

### Command Line Options
| Option | Description |
|--------|-------------|
| `-o`, `--stdout` | Log to stdout instead of `debug.log` (ignored inside a systemd unit). |
| `-D`, `--debug` | More frequent/elaborate logging; overrides `debug_mode`. |
| `-q`, `--quick` | Set lock and sleep timeouts to 1 minute and run double-time (for testing). |
| `-e`, `--edit-config` | Open `config.ini` in `$EDITOR` (default `vim`). |
| `-f`, `--follow-log` | Tail `~/.config/pwr-tray/debug.log`. |
| `--de NAME` | Force desktop detection (e.g. `--de i3-x11`, `--de niri-wayland`). |
| `--replace` | Replace the running instance. |
| `--list-des` | List known desktops and their commands, marking missing ones, then exit. |
| `-b`, `--battery` | Print the detected power state and its devices, then exit. |
| `--svc`, `--svc-status` | Report the `systemd --user` unit and allowed desktops. Exits `0` running, `1` installed but stopped or not allowed here, `2` not installed. |
| `--svc-install` | Record this desktop as allowed and write the unit if missing, then reload and enable it (does not start it). Refuses if `~/.local/bin/pwr-tray` is missing. |
| `--svc-start` | Start the unit now; enables it if it was disabled. Leaves a running unit alone. |
| `--svc-restart` | Restart the unit so the running applet is the code `~/.local/bin/pwr-tray` resolves to. Never installs or rewrites the unit. |
| `--svc-stop` | Stop the unit. It stays enabled, so it returns at your next login. |
| `--svc-uninstall` | Record this desktop as not allowed and stop the applet; disable and delete the unit only when no desktop is allowed. `config.ini`, picks and logs are kept. |
| `--show-svc-file` | Print the systemd unit, then exit. |
| `-y`, `--yes` | Never prompt for the `--svc-*` verbs. |
| `--force` | Replace or delete a unit pwr-tray did not write; implies `--yes`. |
| `-V`, `--version` | Print the package version and exit. |

For initial setup and troubleshooting, `pwr-tray -D -o` is a good starting point.

### HowTo Configure pwr-tray

#### config.ini
- Created with defaults when the program is started without one. It has three sections:
    * **Settings**: settings for when plugged in.
    * **HiBattery**: on battery, not low.
    * **LoBattery**: on battery and low.
- Missing or invalid keys are inherited from `Settings`; a key absent from both uses the tier default.

'Settings' defaults:
```
    [Settings]
    debug_mode = False                  # more frequent and elaborate logging
    power_down = False                  # power down (rather than suspend)
    turn_off_monitors = False           # turn off monitors after locking screen
    confirm_session_end = False         # ask before Log Off / Reboot / PowerOff
    lock_min_list = [15, 30]            # lock minutes choices
    sleep_min_list = [5, 30]            # sleep minutes choices (after lock)
    lo_battery_pct = 10                 # define "low battery" state
    gui_editor = geany                  # gui editor for .ini file
    log_viewer =                        # terminal prefix for "Open Applet Log" (empty = auto-detect)
```
Tier defaults for keys absent from both sections: HiBattery `power_down = False`, `lock_min_list = [10, 20]`, `sleep_min_list = [1, 10]`; LoBattery `power_down = True`, `lock_min_list = [1]`, `sleep_min_list = [1]`.

**NOTES**:
* `confirm_session_end = True` makes session-ending rows a two-click arm: the row reads `Reboot (click twice)`, and after one click `Reboot (click NOW)` -- reopen the menu and click again promptly. Only one entry is armed at a time. Idle-timeout and low-battery actions run straight through, and Suspend is never armed. The guarded actions are the `session_end` list in `commands.yaml`: log off, power off, reboot, plus `restart_wm` on Wayland.
* At startup, rows this version expects but the file lacks are added, and nothing is ever removed; unknown keys are reported in `debug.log` and left where they are.
* If monitors fail to sleep, or the system cannot wake with the monitors off, disable `turn_off_monitors`.
* `gui_editor` may be a command such as `konsole -e vim`.
* `log_viewer` is the terminal prefix for the log viewer, e.g. `xterm -e`; when empty, the first installed terminal it knows is used.
* `debug.log` is capped at 2 MiB and rotated to `debug.log.old`.
* Edits to `config.ini` are picked up while pwr-tray runs; no restart needed.
* Picks of mode, timeouts, etc. are saved when changed and restored on the next start.
* In LoBattery, SleepAfterLock becomes the effective mode.

#### commands.yaml (DE commands)
On each startup, `pwr-tray` copies its built-in `commands.yaml` to `~/.config/pwr-tray/commands.yaml` so the current defaults are always visible. To customize DE commands (fix a logoff command, change the locker, add a DE), copy it to `my-commands.yaml` in the same folder, edit, and restart. `my-commands.yaml` is loaded if present, otherwise the built-in file is used, and it is never overwritten.

The config merges three layers: **defaults** → **session_type** (x11 or wayland) → **desktop**. Desktop entries are keyed by compound names like `i3-x11` or `kde-wayland`: the part before the last `-` is matched against `$XDG_CURRENT_DESKTOP`, `$XDG_SESSION_DESKTOP` and `$DESKTOP_SESSION`, and the suffix must match `$XDG_SESSION_TYPE`.

`session_end` lists the action keys that end the session, which is what `confirm_session_end` guards. Log off, power off and reboot are always there; `session_types.wayland` adds `restart_wm`. If your desktop's `reload_wm` or `restart_wm` kills every app, name that key in the desktop's own `session_end` list.

### Running pwr-tray as a systemd Service

The `--svc-*` verbs drive **one** `systemd --user` unit that runs `~/.local/bin/pwr-tray` (created by `pipx install`). They need a reachable `systemd --user` manager; without one they refuse, and starting pwr-tray is your DE/WM's job.

The unit is a constant: it names no desktop and pins no display variable, so it is the same file on every machine. **Which desktops may run it is `[ServiceAllowed]` in `config.ini`.** Quick start, once in each desktop or WM that should have a tray:

```
pwr-tray --svc-install    # allow this desktop, write/enable the unit if missing
pwr-tray --svc-start      # show the tray now
```

That is how one machine running i3 and niri gets a tray in both and leaves KDE alone. Every name is `False` until you allow it.

```ini
[ServiceAllowed]
# pwr-tray --svc-install sets the desktop you are logged into to True
i3-x11 = True
niri-wayland = True
kde-wayland = False
# ...every supported desktop is listed; False until you opt in
```

`--svc-install` edits that section **in place**, so your comments and your own sections survive; edit it by hand whenever that is easier. Startup only *adds* a row for a supported desktop the section lacks (as `False`). A row naming an unsupported desktop is reported in `debug.log` and left alone.

**What a disallowed desktop looks like.** The unit still starts at login; the applet sees `False` for its desktop, writes one line to `debug.log` and exits **0**. A clean exit is final for `Restart=on-failure`, so the unit ends `inactive` -- not `failed`.

**Quit and Restart in the menu are unit operations.** Under the unit, both rows ask `systemctl --user` to act on `pwr-tray.service` instead of exiting into a replacement process. A replacement spawned by the applet would share the unit's cgroup, which systemd kills when the main process exits, so it died with the old instance -- which is why `↺ Restart pwr-tray` looked like a stop for the session. The request is queued with `--no-block` before the applet leaves, so systemd carries it out even though the same stop also signals the applet; the unit's own `ExecStart=` starts the new instance. `☓ Quit this Applet` stops the unit the same way (a clean exit alone would be undone by a unit configured with `Restart=always`); it stays enabled and returns at your next login, exactly like `--svc-stop`.

**Logout is never blocked.** The applet does not outlive a logout it asked for: `▷ Log Off` runs the desktop's `logoff` command with a bounded wait, and a zero status quits the applet (exit 0, so nothing restarts it, and the lock release and `ENDED` line still happen). A desktop that defines no `logoff` gets no row at all, and one whose tool fails keeps the applet running. The unit bounds its own stop as well: `TimeoutStopSec=5`, the stop exit codes listed in `RestartPreventExitStatus`, and SIGTERM turned into a normal quit.

**Switching desktops.** Logging out and back in always works. Switching to a *second* session that is still logged in does not -- for pwr-tray or any other `graphical-session.target` unit -- because the target is already active, so no start job exists. Add one line to that desktop's startup file and the tray always appears:

```
# i3, sway
exec systemctl --user start pwr-tray.service
# niri
spawn-at-startup "systemctl" "--user" "start" "pwr-tray.service"
# Hyprland
exec-once = systemctl --user start pwr-tray.service
```

`systemctl --user start` on a running unit does nothing, so the line is safe in every case.

#### Rolling your own unit

`pwr-tray --show-svc-file > pwr-tray.service` dumps the shipped unit, the same text `--svc-install` writes. Keep `--svc-mode` on `ExecStart=`: that flag is what makes a unit run obey `[ServiceAllowed]` (a desktop autostart or a terminal run is never gated). Then install it as you normally would, e.g. into `~/.config/systemd/user/` followed by `systemctl --user daemon-reload && systemctl --user enable --now pwr-tray.service`, and create the directory its `StandardOutput` names (`~/.config/pwr-tray` for the shipped unit) -- systemd opens that file but never creates its directory.

Keep one supervisor per session: do not enable the unit *and* a DE/WM autostart for the same login.

### Per-DE Specific Notes

#### i3wm Specific Notes
* Disable all competing energy saving programs (e.g. `xscreensaver`, `xfce4-power-manager`) and defeat the X11 defaults in `~/.config/i3/config`:
```
        exec --no-startup-id xset s off ; xset s noblank ; xset -dpms
```
* **Recommended**: install `xss-lock` for lid close and power button events:
```
set $screenlock i3lock -t -i ~/.config/pwr-tray/lockpaper.png --ignore-empty-password --show-failed-attempts
exec --no-startup-id xss-lock --transfer-sleep-lock -- $screenlock --nofork
bindsym XF86PowerOff exec --no-startup-id $screenlock && systemctl suspend
bindsym $mod+Escape exec --no-startup-id $screenlock  # create shortcut to lock screen only
```
* In `/etc/systemd/logind.conf`, uncomment `HandlePowerKey=` and `HandleLidSwitch=`, set each to `suspend`, then reboot or restart `systemd-logind`.
* Start `pwr-tray` as a systemd service (above), or from i3's config:
```
        bar {
            status_command i3status
            tray_output primary
        }
        exec_always --no-startup-id ~/.local/bin/pwr-tray
```
* With `polybar`, run `pwr-tray` from its launch script, e.g. `setsid ~/.local/bin/pwr-tray &`. The icon registers when polybar's tray appears, so no delay is needed.

#### sway Specific Notes
* Disable any other `swayidle` instances or competing energy savers; `pwr-tray` manages its own `swayidle` and kills stray instances on startup. On sway the idle time is not readable, so those timeouts come from that process.
* In `/etc/systemd/logind.conf`, uncomment `HandlePowerKey=` and `HandleLidSwitch=`, set each to `suspend`, then reboot or restart `systemd-logind`.
* Start `pwr-tray` as a systemd service (above) or with `exec_always --no-startup-id ~/.local/bin/pwr-tray`.

#### Hyprland Specific Notes
* Disable any other idle managers; `pwr-tray` manages its own `swayidle`.
* Start `pwr-tray` as a systemd service (above) or with `exec-once = ~/.local/bin/pwr-tray`.
* Screen locking uses `swaylock`; monitor control uses `hyprctl dispatch dpms`.

#### niri Specific Notes
* The bundled `niri-wayland` profile is selected from `$XDG_CURRENT_DESKTOP`/`$XDG_SESSION_DESKTOP`, or force it with `--de niri-wayland`.
* Install `niri`, `swaylock` and `swayidle` (1.8+), and disable any other idle manager.
* Monitor blanking uses `niri msg action power-off-monitors`; `monitors_on` is intentionally empty, since input wakes the display.
* Log off and config reload use `niri msg action quit --skip-confirmation` and `niri msg action reload-config`; plain `quit` would wait on stdin, which the applet does not have.
* niri handles the power key itself, so its `handle-power-key` inhibitor does not show the StopSign icon.
* Start `pwr-tray` as a systemd service (above) or with:
```
spawn-at-startup "sh" "-c" "~/.local/bin/pwr-tray"
```

#### KDE Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving" and "Suspend session"; keep "Button events handling" as you wish.
* Start it as a systemd service (above) or add `~/.local/bin/pwr-tray` in Settings/AutoStart.
* `qdbus` (or `qdbus6` on Plasma 6) is required for X11 and auto-detected; idle time comes from `xprintidle`.
* On **KDE Wayland**, `swayidle` is required and managed by `pwr-tray`. Locking uses `loginctl lock-session` on both.

#### Other DEs (XFCE, Cinnamon, MATE, LXQt)
* The built-in configs should work out of the box on X11.
* Disable any competing energy saving features in the DE's own power settings.
* Screen locking uses `loginctl lock-session` (the DE provides the lock handler).
* If the built-in commands don't work for your setup, create a `my-commands.yaml` override (see above).
