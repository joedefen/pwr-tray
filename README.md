# pwr-tray

`pwr-tray` is a PyQt5 Tray Applet for Power/Energy Saving and System/DE Controls. Tested DEs include i3wm, swaywm, Hyprland, and KDE (X11 and Wayland). niri is supported by the bundled `niri-wayland` commands. Additional DEs with built-in configs: XFCE, Cinnamon, MATE, and LXQt. `systemd` is required. The `pwr-tray` menu will look similar to:

<p align="center">
  <img src="https://github.com/joedefen/pwr-tray/blob/main/images/pwr-tray-screenshot.png?raw=true" alt="screenshot">
</p>


With just a right-click and a left-click, you can do most operations such as change to Presentation Mode, change screen lock and sleep timeouts, log off, lock and blank your monitors, and more. The `pwr-tray` icon changes based on state:

* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/FullSun-v03.svg?raw=true" alt="FullSun" width="24" height="24"> Presentation Mode (i.e., the full sun)
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/SettingSun-v03.svg?raw=true" alt="SettingSun" width="24" height="24"> SleepAfterLock Mode (i.e., the setting sun)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/RisingMoon-v03.svg?raw=true" alt="RisingMoon" width="24" height="24"> SleepAfterLock Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/Unlocked-v03.svg?raw=true" alt="Unlocked" width="24" height="24"> LockOnly Mode  (i.e., the unlocked lock)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/UnlockedMoon-v03.svg?raw=true" alt="UnlockedMoon" width="24" height="24"> LockOnly Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/GoingDown-v03.svg?raw=true" alt="GoingDown" width="24" height="24"> LowBattery State (going down).
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/PlayingNow-v03.svg?raw=true" alt="PlayingNow" width="24" height="24"> Inhibited by playing media.
* <img src="https://github.com/joedefen/pwr-tray/blob/main/pwr_tray/resources/StopSign-v03.svg?raw=true" alt="StopSign" width="24" height="24"> Inhibited by systemd inhibitors.

---

### Requirements
* **Python** 3.11+ with **PyQt5** (5.15+) and **ruamel.yaml** (0.17+)
* **systemd** (for `systemctl`, `loginctl`, `busctl` and `systemd-inhibit`; the `--svc-*` verbs also need a reachable `systemd --user` manager)
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
| **XFCE** | `xprintidle`, `xset` | X11 only |
| **Cinnamon** | `xprintidle`, `xset` | X11 only |
| **MATE** | `xprintidle`, `xset` | X11 only |
| **LXQt** | `xprintidle`, `xset` | X11 only |

**Distro notes:**
* Developed and tested on **Debian 13 (trixie)**. Should work on any distro with sufficiently recent packages.
* KDE Wayland support requires **Plasma 6** (uses the `org.kde.Shutdown` D-Bus API) and **swayidle 1.8+** (for `ext-idle-notify-v1` protocol support).
* Debian 12 (bookworm) and Ubuntu 24.04 ship Plasma 5 and swayidle 1.7 -- KDE Wayland will **not** work on those. i3 and sway should be fine.
* Arch Linux and Fedora 40+ ship current versions and should work for all DEs.

---

### HowTo Install and Start pwr-tray
* Basically: `pipx install pwr-tray` (exactly how depends on your installation and its state)
* From a checkout, `pipx install --editable .` makes `pwr-tray` run that tree; after editing, `pwr-tray --svc-restart` reloads the applet systemd is running.
* Manually run as `pwr-tray -o`:
    * Creates config (in `~/.config/pwr-tray/config.ini`).
    * Copies default DE commands to `~/.config/pwr-tray/commands.yaml`.
    * Detects your desktop environment. If none is recognised, it prints the
      session variables it saw, the list of known desktops, and exits -- use
      `--de NAME` to force the right one. `pwr-tray --list-des` prints every
      known desktop together with the commands it needs, marking any that are
      not currently on `$PATH`.
    * Checks the DE-dependent commands it needs and exits with an install hint
      if any are missing. Note:
      * `systemctl` is always required.
      * Optionally, install `playerctl` if you wish playing media to inhibit screen saving and sleeping.
    * **PyQt5 is installed for you.** It is a declared dependency, and the
      published wheels bundle the Qt libraries they need, so `pipx install
      pwr-tray` (or `pip install`) is normally all there is to it. A missing
      `ModuleNotFoundError: No module named 'PyQt5'` therefore means something
      else ran pwr-tray -- usually a Python that was never set up for it. Add
      PyQt5 to *that* interpreter:
        * `pipx inject pwr-tray PyQt5` -- the pipx venv that `pwr-tray` runs from
        * `pip install PyQt5` -- inside an activated venv, or `pipx inject` as above
        * `sudo apt install python3-pyqt5` (Debian/Ubuntu), `sudo pacman -S python-pyqt5` (Arch), `sudo dnf install python3-qt5` (Fedora) -- only when you run pwr-tray with the **system** Python; a venv does not see distro packages.
      Only if pip cannot use a prebuilt wheel (an unusual CPU architecture, or a musl system such as Alpine) does it fall back to compiling PyQt5, which needs Qt development headers (e.g. `qtbase5-dev` plus `python3-dev`).

* Then, follow the "Per-DE Specific Notes" below to ensure proper operation. To just kick the tires, you can defer this until ready to go forward.
* Read the other sections for customization and everyday use.
* From the CLI, you can start pwr-tray in the background with `setsid pwr-tray`; typically, you will "autostart" `pwr-tray` when you log in however your DE/WM manages autostarts. Only one instance ever runs: if one is already running, a plain start **refuses and exits** rather than disturbing it; use `setsid pwr-tray --replace` to deliberately replace it.

### Command Line Options
| Option | Description |
|--------|-------------|
| `-o`, `--stdout` | Keep logging on stdout instead of `debug.log` (ignored inside a systemd unit, where stdout is the unit's own redirect). |
| `-D`, `--debug` | Enable debug mode (more frequent/elaborate logging); overrides the `debug_mode` config setting. |
| `-q`, `--quick` | Quick mode: sets lock and sleep timeouts to 1 minute and runs double-time (timers expire in 30s wall clock). Useful for testing. |
| `-e`, `--edit-config` | Open `~/.config/pwr-tray/config.ini` in `$EDITOR` (default: `vim`). |
| `-f`, `--follow-log` | Tail the log file (`~/.config/pwr-tray/debug.log`) from a terminal. |
| `--de NAME` | Force desktop detection (e.g., `--de i3-x11`, `--de sway-wayland`, `--de kde-wayland`). Useful when env vars are unreliable. |
| `--replace` | Replace the `pwr-tray` that is already running: terminate it and start in its place. Without this flag a second start refuses and exits. |
| `--list-des` | Print the known desktops and the commands each one requires, mark commands missing from `$PATH`, then exit. |
| `--svc` / `--svc-status` | Report the `systemd --user` unit: installed, enabled, running. Exits `0` running, `1` installed-but-stopped, `2` not installed. |
| `--svc-install` | Write the unit matching this desktop to `~/.config/systemd/user/`, reload, and enable it (does not start it now). This is the only verb that writes or enables the unit. The unit runs `~/.local/bin/pwr-tray`, so the verb refuses if that launcher is missing. |
| `--svc-start` | Start the installed unit now. Enables it if it was disabled, so it returns at your next login; never writes the unit. An already-running unit is left alone; use `--svc-restart` to load new code. Refuses if `~/.local/bin/pwr-tray` is missing. |
| `--svc-restart` | `systemctl --user restart` the installed unit so the running applet is the one `~/.local/bin/pwr-tray` resolves to. Never installs, enables, or rewrites the unit; if the unit runs anything else, run `--svc-install` first. This is what applies `pipx upgrade`, an editable install or a `git pull`; it says so when the launcher is a frozen copy, which no restart can update. |
| `--svc-stop` | Stop the unit. It stays enabled, so it returns at your next login. |
| `--svc-uninstall` | Stop, disable and delete the unit. `config.ini`, picks and logs are kept. |
| `-y`, `--yes` | Never prompt for the `--svc-*` verbs (needed when stdin is not a terminal). |
| `--force` | Replace or delete a unit file pwr-tray did not write; implies `--yes`. |
| `-V`, `--version` | Print the package version and exit. |

For initial setup and troubleshooting, `pwr-tray -D -o` is a good starting point.

---

### HowTo Use pwr-tray
Open the `pwr-tray` menu with a right-click. Then left-click a line to have an effect ...

Choose from three *major power modes* (to control the effects of timeouts):
- **◉ Presentation ←** -  Keeps the screen unlocked/on and system up.
- **◐ LockOnly ←** - Keeps the system up, but the screen may lock.
- **○ SleepAfterLock ←** - Allows screen locking and system to go down (the "normal" mode).

Or choose from various locking/blanking/DE operations:
- **▷ Lock Screen** - locks the screen immediately.
- **▷ Blank Monitors** - blanks the screen after locking the screen.
- **▷ Reload** - various DE-dependent actions (reload WM config, etc.).
- **▷ Log Off** - terminate your user session.

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Next, you may see:
- **↯ Plugged In** (or HiBattery or LoBattery). Shows the state of the battery.
- **♺ Chg Screen Idle: 15m->30m** - change the time to start the screen saver; each time clicked, it changes to the next choice.
- **♺ Chg System Idle: 5m->30m** - change the time to take the system down; clicking selects the next choice.
- **♫ PlayerCtl** - shows the state (not installed, enabled, disabled); if installed, a click toggles whether playing media inhibits screen locking and sleeping.

The symbols are ordinary text glyphs taken from fonts every desktop has
(DejaVu Sans, Liberation Sans), never emoji: they follow the menu's own text
colour and sit where the grouping needs them -- after a group name, or indented
with the sub-items.

Or act on the applet itself:
- **✎  Edit Applet Config** - edit the applet's .ini file.
- **≡ Open Applet Log** - show `debug.log` (and the rotated `debug.log.old`, when present) in a terminal window, following the current log with `less +F`. It is offered only when a terminal emulator can be found; see `log_viewer` below. The viewer is started in its own session -- and, when the applet runs as a systemd service, in its own transient scope -- so it keeps following the log when the applet stops or restarts.
- **☓ Quit this Applet** -  exit applet.
- **↺ Restart pwr-tray vX.Y.Z** - restart applet. It is the last entry and it carries the running version, so "which build am I running?" is answered without reading the log (which says the same thing at `START-UP` and `ENDED`).


---

### Testing pwr-tray
- Running `pwr-tray --quick` reduces the lock and sleep timeout to 1 minute (although you can 'click' the current value to try others), and `--quick` runs double-time (so 1 minute timers expire in 30s per the wall clock).
- You can run in various modes, but the default, `SleepAfterLock`, exercises the most code paths.
- Then, ensure closing the lid, hitting the power button, etc., have the desired effects.
- To test systemd inhibits: create a test inhibit with `systemd-inhibit --why="Prevent sleep for demonstration" sleep infinity`. Only an inhibitor that blocks `sleep` or `idle` shows the StopSign icon; one that only holds the power or lid key (`handle-power-key`, `handle-lid-switch`) does not, because a compositor owning an input event is not asking the session to stay awake.
- To test media inhibit: with `playerctl` installed, play media in any MPRIS-capable player; the `PlayingNow` icon appears and the lock/sleep timeouts are suspended until playback stops.
- To test Hi/Lo Battery states (only on a system w/o a battery), click the battery state which artificially changes to HiBattery or LoBattery states for testing behaviors in those states. The menu entry is suffixed with `(TESTING)` only while it shows one of those fabricated battery states, so it cannot be mistaken for a real power state; the plugged-in state -- at start-up, or when the cycle comes back around to it -- is the machine's real state and carries no suffix.

#### Updating pwr-tray

`pipx upgrade pwr-tray`, or `git pull` followed by `pipx install --editable .`
for a checkout, changes the installed code. `pwr-tray --svc-restart` is what
makes the running service use it. The unit file itself never changes, because
it always starts `~/.local/bin/pwr-tray`.

`--svc-start` deliberately leaves an already-running, up-to-date unit alone,
and neither `--svc-start` nor `--svc-restart` rewrites the unit.

---

### HowTo Configure pwr-tray

#### config.ini
- When the program is started w/o a `config.ini`, that file is created with defaults.
- It has three sections:
    * **Settings**: The settings for when plugged in.  Missing/invalid settings are inherited from the defaults. Here are the defaults:
    * **HiBattery**: The settings for when on battery on and not a low battery condition.  Missing/invalid settings are inherited from 'Settings'.  A key absent from both sections uses the HiBattery default: `power_down = False`, `lock_min_list = [10, 20]`, `sleep_min_list = [1, 10]`.
    * **LoBattery**: The settings for when on battery in a low battery condition.  Missing/invalid settings are inherited from 'Settings'.  A key absent from both sections uses the LoBattery default: `power_down = True`, `lock_min_list = [1]`, `sleep_min_list = [1]`.

Here are the current 'Settings' defaults with explanation.
```
    [Settings]
    debug_mode = False                  # more frequent and elaborate logging
    power_down = False                  # power down (rather than suspend)
    turn_off_monitors = False           # turn off monitors after locking screen
    lock_min_list = [15, 30]            # lock minutes choices
    sleep_min_list = [5, 30]            # sleep minutes choices (after lock)
    lo_battery_pct = 10                 # define "low battery" state
    gui_editor = geany                  # gui editor for .ini file
    log_viewer =                        # terminal prefix for "Open Applet Log" (empty = auto-detect)
```
**NOTES**:
* If you have issues with monitors failing to sleep or the system cannot wake when the monitors are off, then disable the `turn_off_monitors` feature.
* You can set `gui_editor = konsole -e vim`, for example, to use vim in a terminal window.  If you don't have `geany` installed, then be sure to change `gui_editor`.
* `log_viewer` is the command prefix `pwr-tray` uses to start the log viewer inside a terminal window, e.g. `log_viewer = xterm -e` or `log_viewer = gnome-terminal --`. When empty it picks the first installed terminal it knows (x-terminal-emulator, gnome-terminal, konsole, kitty, alacritty, foot, xterm, xfce4-terminal, mate-terminal, lxterminal).
* `debug.log` is the file the menu and `-f` follow, and it is capped at 2 MiB. When it grows past that, it is rotated: the current file becomes `debug.log.old`, any previous `debug.log.old` is discarded, and a fresh `debug.log` is started -- so there are never more than two log files. The applet writes this file even when a systemd unit redirected its stdout elsewhere, so the visible log never goes stale.
* Every run brackets itself in that log with `START-UP pwr-tray vX.Y.Z pid N` and `ENDED pwr-tray vX.Y.Z pid N`, so the version and the process id can be read off the log itself. A `START-UP` with no `ENDED` below it was killed (`SIGKILL`, a crash); an `ENDED` with no new `START-UP` is simply the end of a run.
* Edits to `config.ini` are picked up while `pwr-tray` is running; there is no need to restart it.
* Your picks of mode, timeouts, etc. are saved to disk when changed, and restored on the next start.
* Items may be absent depending on the mode and battery state.
- **NOTE**: when in LoBattery, SleepAfterLock becomes the effective mode. The icon will change per your selection and the battery state.

#### commands.yaml (DE commands)
On each startup, `pwr-tray` copies its built-in `commands.yaml` to `~/.config/pwr-tray/commands.yaml` so you can always see the current defaults. To customize DE commands (e.g., fix a logoff command, change the locker, add a new DE):

1. Copy `commands.yaml` to `my-commands.yaml` in the same folder:
   ```
   cp ~/.config/pwr-tray/commands.yaml ~/.config/pwr-tray/my-commands.yaml
   ```
2. Edit `my-commands.yaml` as needed.
3. Restart `pwr-tray`.

`pwr-tray` loads `my-commands.yaml` if it exists, otherwise falls back to the built-in `commands.yaml`. The `my-commands.yaml` file is never overwritten by `pwr-tray`.

The config uses a three-layer merge: **defaults** → **session_type** (x11 or wayland) → **desktop**. Later layers override earlier ones. Desktop entries are keyed by compound names like `i3-x11` or `kde-wayland`; the part before the last `-` is matched against environment variables (`$XDG_CURRENT_DESKTOP`, `$XDG_SESSION_DESKTOP`, `$DESKTOP_SESSION`), and the suffix must match `$XDG_SESSION_TYPE`.

---

## Running pwr-tray as a systemd Service

Instead of your DE/WM's own autostart, you can run `pwr-tray` as a `systemd --user` service. That restarts the applet if it exits and keeps its logging consistent regardless of how the session was started. The `--svc-*` verbs require a reachable `systemd --user` manager: without one they refuse, and starting pwr-tray is your DE/WM's job (see the per-DE notes). The units ship inside the package, so installing one is a single command:

```
pwr-tray --svc-status      # installed? enabled? running?
pwr-tray --svc-install     # write the unit for this desktop and enable it
pwr-tray --svc-start       # start it now (enables it if disabled)
pwr-tray --svc-restart     # restart it (loads new code)
pwr-tray --svc-stop        # stop it; it comes back at your next login
pwr-tray --svc-uninstall   # stop, disable and delete the unit
```

**The unit always runs `~/.local/bin/pwr-tray`.** It is the same file on every
machine: no install path is recorded, so it does not matter which shell, which
virtualenv, or which `pipx` invocation you installed from. That one path is the
only thing to keep right, and `pipx install pwr-tray` (or `pipx install
--editable .` from a checkout) creates it. If it does not exist, `--svc-install`,
`--svc-start` and `--svc-restart` refuse and say so, because such a unit could
only fail; `--svc-status`, `--svc-stop` and `--svc-uninstall` keep working.

`--svc-install` is the only verb that commits the machine to a systemd unit:
it writes the shipped unit for the detected desktop, loading your live
`WAYLAND_DISPLAY`/`DISPLAY`, reloads the manager and enables it. It does not
start the applet, and it will not overwrite a unit you have edited without
showing the difference first. `--yes` answers the question in advance;
`--force` replaces or deletes a unit pwr-tray did not write. `--svc-start` and
`--svc-restart` operate only on an installed unit and never write it;
`--svc-start` enables a disabled unit, so it is the one-command "run now and
come back at login", and an already-active unit is left alone. `--svc-restart`
is how an update reaches the running applet: a `pipx upgrade` or a `git pull`
changes the code, not the unit file. A unit that runs anything other than
`~/.local/bin/pwr-tray` (a hand edit, or one installed by an older pwr-tray
that recorded an absolute path) is refused by start and restart -- run
`--svc-install` to replace it, because rewriting the unit is that verb's
decision.

`--svc-status` prints the launcher, where it points, and what that is (an
editable install of a directory, a released wheel, ...), so what runs is never
guesswork:

```
pwr-tray.service: active (enabled)
  unit: /home/you/.config/systemd/user/pwr-tray.service
  runs: /home/you/.local/bin/pwr-tray
        -> /home/you/.local/share/pipx/venvs/pwr-tray/bin/pwr-tray
        (editable install of /home/you/Projects/pwr-tray)
```

A launcher that points at a **frozen copy** of a checkout -- `pip install .`
inside a project virtualenv copies the package into that virtualenv's
`site-packages` -- is called out there and by `--svc-restart`: such a copy
keeps running the code it was installed with, so a restart would change
nothing. Reinstall it editable to fix that.

The units append stdout/stderr to the applet's own log, `~/.config/pwr-tray/debug.log`, so the tray menu and `-f/--follow-log` always show what the applet is actually writing. `--svc-install` also creates that directory, and says so before it does. systemd does not create it: it fails every process of the unit with `EXIT_STDOUT` (`status=209/STDOUT`, "Failed to set up standard output") *before* `ExecStartPre=` is reached, so a unit installed without it never starts.

`--svc-status` is meant for scripts: it exits `0` when the service is running, `1` when it is installed but stopped or failed, and `2` when no unit is installed. It names a failed unit as `failed` and points at `journalctl --user -xeu pwr-tray.service`.

```
pwr-tray --svc-status || { pwr-tray --svc-install --yes && pwr-tray --svc-start --yes; }
```
Because `--svc-start` requires an installed unit, the fallback installs (or confirms the install is current) first.

The same units are readable in the source tree and in the wheel under [`pwr_tray/resources/systemd/`](pwr_tray/resources/systemd/). To install one by hand, copy it as `~/.config/systemd/user/pwr-tray.service`, then:

```
mkdir -p ~/.config/pwr-tray     # --svc-install does this for you
systemctl --user daemon-reload
systemctl --user enable --now pwr-tray.service
```

Example unit for **sway / Wayland** (as `--svc-install` would render it):

```
[Unit]
Description=pwr-tray (power/energy tray applet)
ConditionEnvironment=XDG_CURRENT_DESKTOP=sway
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
Environment=XDG_CURRENT_DESKTOP=sway
Environment=XDG_SESSION_TYPE=wayland
Environment=WAYLAND_DISPLAY=wayland-1
ExecStartPre=/bin/sleep 1
ExecStart=%h/.local/bin/pwr-tray --de sway-wayland
Restart=always
RestartSec=5
RestartPreventExitStatus=2 3
StandardOutput=append:%h/.config/pwr-tray/debug.log
StandardError=inherit

[Install]
WantedBy=graphical-session.target
```

> The service examples above and below have not been run on every desktop, so
> review them before use.

### Example systemd Service Config for other DEs/WMs

* [i3](pwr_tray/resources/systemd/pwr-tray-i3.service)
* [Hyprland](pwr_tray/resources/systemd/pwr-tray-hyprland.service)
* [niri](pwr_tray/resources/systemd/pwr-tray-niri.service)
* [KDE X11](pwr_tray/resources/systemd/pwr-tray-kde-x11.service)
* [KDE Wayland](pwr_tray/resources/systemd/pwr-tray-kde-wayland.service)
* [XFCE](pwr_tray/resources/systemd/pwr-tray-xfce.service)
* [Cinnamon](pwr_tray/resources/systemd/pwr-tray-cinnamon.service)
* [MATE](pwr_tray/resources/systemd/pwr-tray-mate.service)
* [LXQt](pwr_tray/resources/systemd/pwr-tray-lxqt.service)

### systemd Service Caveats

* A `systemd --user` service is **not tied to a DE/WM**. With `WantedBy=graphical-session.target` it starts in *any* graphical session on your account, so on a machine where you switch between DEs it will also launch under the others (and, with `--de` forced, then act on the wrong session). To limit it, bind it to a WM-specific target or add a condition line such as `ConditionEnvironment=XDG_CURRENT_DESKTOP=sway`.
* `systemd --user` does not inherit your session environment, so the variables above are set explicitly. `--svc-install` fills in the one that cannot be guessed: the `WAYLAND_DISPLAY`/`DISPLAY` your session actually uses. The executable is always `%h/.local/bin/pwr-tray`.
* Do **not** enable the service *and* a DE/WM autostart for the same session. A second launch exits with "another pwr-tray is already running" instead of starting a duplicate. The units pass `RestartPreventExitStatus=2 3`, so that refusal (exit 3) and a failed startup (exit 2: unknown desktop, missing resource, or a required command absent from `PATH`) are reported as a unit failure rather than retried; only transient exits are restarted, and `StartLimitIntervalSec`/`StartLimitBurst` stop a crash loop after five attempts in a minute. Pick one supervisor per session -- `--svc-start` notices an applet that is already running outside systemd and refuses with that explanation instead of starting a unit that would exit 3.
* Once the start rate limiter has tripped, the unit stays `failed` and a bare `systemctl --user start` is refused as "start request repeated too quickly" until the interval passes. `--svc-start` clears that first (`systemctl --user reset-failed`), so it is the reliable way to retry after fixing whatever failed -- including a log directory that a hand install forgot to create.
* `--replace` is for an intentional hand-over (`setsid pwr-tray --replace` from a terminal). Do **not** put it in a unit's `ExecStart`: two supervisors that both pass it would keep terminating each other's instance.
* Keep the standard `PATH` entries -- `pwr-tray` calls `systemctl`, `loginctl`, `swayidle`, `swaymsg`, and friends from `/usr/bin`.

---

## Per-DE Specific Notes

### i3wm Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `xscreensaver`, `xfce4-power-manager`, etc.) when running `i3` whether started by `systemd` or `i3/config` or whatever; defeat the X11 defaults somehow such as in `~/.config/i3/config`:
```
        exec --no-startup-id xset s off ; xset s noblank ; xset -dpms
```
* **Recommended**: Install `xss-lock` and configure it in your i3 config to handle lid close and power button events:
```
set $screenlock i3lock -t -i ~/.config/pwr-tray/lockpaper.png --ignore-empty-password --show-failed-attempts
exec --no-startup-id xss-lock --transfer-sleep-lock -- $screenlock --nofork
bindsym XF86PowerOff exec --no-startup-id $screenlock && systemctl suspend
bindsym $mod+Escape exec --no-startup-id $screenlock  # create shortcut to lock screen only
```
* Edit `/etc/systemd/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, set each action to `suspend`, and then either reboot or restart `systemd-logind`.
* Finally, start `pwr-tray` as a systemd service (see above) or some other way. For example, a simplest case using `i3status`, though it may depend on your status bar:
```
        bar {
            status_command i3status
            tray_output primary
        }
        exec_always --no-startup-id ~/.local/bin/pwr-tray
```
* If you use `polybar` for status, then it may be best to run `pwr-tray` from polybar's 'launch' script; e.g., `sleep 1.5 && setsid ~/.local/bin/pwr-tray &`;  the delay may be need to allow time for the tray to become ready.

### sway Specific Notes
* Uninstall or disable any **other** `swayidle` instances or competing energy savers. `pwr-tray` manages its own `swayidle` process and will kill any stray instances on startup.
* **NOTE**: on `sway`, `pwr-tray` cannot read the idle time directly; instead, it manages a `swayidle` process whose arguments vary with your settings.
* Edit `/etc/systemd/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, and set each action to `suspend`; then either reboot or restart `systemd-logind`.  That enables the ever-running `swayidle` to handle the suspend / resume events.
* Again, start `pwr-tray` as a systemd service (see above) or find another way; for example, adding to sway's config: `exec_always --no-startup-id sleep 2 && ~/.local/bin/pwr-tray` -- a delay may be required to let the tray initialize.

### Hyprland Specific Notes
* Disable any other idle managers. `pwr-tray` manages its own `swayidle` process.
* Start `pwr-tray` as a systemd service (see above) or, for example, via Hyprland's `exec-once`:
```
exec-once = sleep 2 && ~/.local/bin/pwr-tray
```
* Screen locking uses `swaylock`; monitor control uses `hyprctl dispatch dpms`.

### niri Specific Notes
* `pwr-tray` ships a `niri-wayland` profile in `commands.yaml`. It is selected from `$XDG_CURRENT_DESKTOP`/`$XDG_SESSION_DESKTOP`, or force it with `--de niri-wayland`.
* Install `niri`, `swaylock`, and `swayidle` (1.8+). As with sway and Hyprland, disable any other idle manager: `pwr-tray` manages its own `swayidle` process and kills stray instances on startup.
* Monitor blanking uses `niri msg action power-off-monitors`. niri has no "monitors on" action because keyboard or pointer input wakes the display, so `monitors_on` is intentionally empty.
* Log off and config reload use `niri msg action quit` and `niri msg action reload-config`.
* niri handles the power key itself; the `handle-power-key` inhibitor it registers is ignored when deciding whether to show the StopSign icon.
* Start `pwr-tray` as a systemd service (see above) or from niri's `spawn-at-startup` with a short delay:
```
spawn-at-startup "sh" "-c" "sleep 2 && ~/.local/bin/pwr-tray"
```

### KDE Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving", "Suspend session", etc., except keep the "Button events handling" and make it as you wish (e.g., "When power button pressed", "Sleep").
* Start it as a systemd service (see above), or in Settings/AutoStart add the full path of `~/.local/bin/pwr-tray`.
* `qdbus` (or `qdbus6` on Plasma 6) is required for X11; `pwr-tray` auto-detects which is available.
* On **KDE Wayland**, `swayidle` is required (install it if missing). `pwr-tray` manages `swayidle` for idle timeout handling. Locking uses `loginctl lock-session`.
* On **KDE X11**, idle time is read via `xprintidle` and screen locking uses `loginctl lock-session`.

### Other DEs (XFCE, Cinnamon, MATE, LXQt)
* These DEs have built-in configs that should work out of the box on X11.
* Disable any competing energy saving features in the DE's own power settings.
* Screen locking uses `loginctl lock-session` (the DE provides the lock handler).
* If the built-in commands don't work for your setup, create a `my-commands.yaml` override (see above).
