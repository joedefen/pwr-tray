# pwr-tray

`pwr-tray` is a GTK5 Tray Applet for Power/Energy Saving and System/DE Controls; currently supported/tested DEs are: i3wm, swaywm, and KDE on X11. `systemd` is required. The `pwr-tray` menu will look similar to:

<p align="center">
  <img src="https://github.com/joedefen/pwr-tray/blob/main/images/pwr-tray-screenshot.png?raw=true" alt="screenshot">
</p>


With just a right-click and a left-click, you can do most operations such as change to Presentation Mode, change screen lock and sleep timeouts, log off, lock and blank your monitors, and more. The `pwr-tray` icon changes based on state:

* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/FullSun-v03.svg?raw=true" alt="FullSun" width="24" height="24"> Presentation Mode (i.e., the full sun)
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/SettingSun-v03.svg?raw=true" alt="SettingSun" width="24" height="24"> SleepAfterLock Mode (i.e., the setting sun)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/RisingMoon-v03.svg?raw=true" alt="RisingMoon" width="24" height="24"> SleepAfterLock Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/Unlocked-v03.svg?raw=true" alt="Unlocked" width="24" height="24"> LockOnly Mode  (i.e., the unlocked lock)
    * <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/UnlockedMoon-v03.svg?raw=true" alt="UnlockedMoon" width="24" height="24"> LockOnly Mode and Locking Screen Soon
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/GoingDown-v03.svg?raw=true" alt="GoingDown" width="24" height="24"> LowBattery State (going down).
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/PlayingNow-v03.svg?raw=true" alt="PlayingNow" width="24" height="24"> Inhibited by playing media.
* <img src="https://github.com/joedefen/pwr-tray/blob/main/src/pwr_tray/resources/StopSign-v03.svg?raw=true" alt="StopSign" width="24" height="24"> Inhibited by systemd inhibitors.

---

### HowTo Install and Start pwr-tray
* Basically: `pipx install pwr-tray` (exactly how depends on your installation and its state)
* Manually run as `pwr-tray -o`:
    * Creates config (in `~/.config/pwr-tray/config.ini`).
    * Shows system-level commands (DE dependent) that must be installed if missing. Note:
      * `systemctl` is always required.
      * Optionally, install `playerctl` if you wish playing media to inhibit screen saving and sleeping.
    * If you find you are missing the QT5 foundation, then you'll need to install that; examples:
        * `sudo apt install qt5-default` # if debian based
        * `sudo pacman -S qt5-base` # if arch based
        * `sudo dnf install qt5-qtbase` # if fedora based

* Then, follow the "Per-DE Specific Notes" below to ensure proper operation. To just kick the tires, you can defer this until ready to go forward.
* Read the other sections for customization and everyday use.
* From the CLI, you can start/restart pwr-tray in the background with `setsid pwr-tray`; typically, you will "autostart" `pwr-tray` when you log in however your DE/WM manages autostarts.
* `pwr-tray -e` edits the config file (`~/.config/pwr-ini/config.ini`)
* `pwr-tray -f` tails the log file (`~/.config/pwr-ini/debug.log`)

---

### HowTo Use pwr-tray
Open the `pwr-tray' menu with a right-click. Then left-click a line to have an effect ...

Choose from three *major power modes* (to control the effects of timeouts):
- **🅟 Presentation ⮜** -  Keeps the screen unlocked/on and system up.
- **🅛 LockOnly ⮜** - Keeps the system up, but the screen may lock.
- **🅢 SleepAfterLock ⮜** - Allows screen locking and system to go down (the "normal" mode).

Ory choose from various locking/blanking/DE operations:
- **▷ Lock Screen** - locks the screen immediately.
- **▷ Blank Monitors** - blanks the screen after locking the screen.
- **▷ Reload i3** - various DE-dependent actions.
- **▷ Log Off** - terminate your user session.

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Next, you may see:
- **🗲 Plugged In** (or HiBattery or LoBattery). Shows the state of the battery.
- **♺ Chg Screen Idle: 15m->30m** - change the time to start the screen saver; each time clicked, it changes to the next choice.
- **♺ Chg System Idle: 5m->30m** - change the time to take the system down; clicking selects the next choice.
- **🎝 PlayerCtl** - shows the state (not installed, enabled, disabled); if installed, a click toggles whether playing media inhibits screen locking and sleeping.

Or act on the applet itself:
- **🖹  Edit Applet Config** - edit the applet's .ini file.
- **☓ Quit this Applet** -  exit applet.
- **↺ Restart this Applet** - restart applet.


---

### Testing pwr-tray
- Running `pwr-tray --quick` reduces the lock and sleep timeout to 1 minute (although you can 'click' the current value to try others), and `--quick` runs double-time (so 1 minute timers expire in 30s per the wall clock).
- You can run in various modes, but the default, `SleepAfterLock`, exercises the most code paths.
- Then, ensure closing the lid, hitting the power button, etc., have the desired effects.
- To test systemd inhibits: create a test inhibit with `systemd-inhibit --why="Prevent sleep for demonstration" sleep infinity`
- To test Hi/Lo Battery states (only on a system w/o a battery), click the battery state which artificially changes to HiBattery or LoBattery states for testing behaviors in those states.

---

### HowTo Configure pwr-tray
- When the program is started w/o a `config.ini`, that file is created with defaults.
- It has three sections:
    * **Settings**: The settings for when plugged in.  Missing/invalid settings are inherited from the defaults. Here are the defaults:
    * **HiBattery**: The settings for when on battery on and not a low battery condition.  Missing/invalid settings are inherited from 'Settings'.
    * **LoBattery**: The settings for when on battery in a low battery condition.  Missing/invalid settings are inherited from 'Settings'.

Here are the current 'Settings' defaults with explanation.
```
    [Settings]
    i3lock_args = -t -i ./lockpaper.png # arguments when running i3lock for wallpaper
    debug_mode = False                  # more frequent and elaborate logging
    power_down = False                  # power down (rather than suspend)
    turn_off_monitors = False           # turn off monitors after locking screen
    lock_min_list = [15, 30]            # lock minutes choices
    sleep_min_list = [5, 30]            # sleep minutes choices (after lock)
    lo_battery_pct = 10                 # define "low battery" state
    gui_editor = geany                  # gui editor for .ini file
```
**NOTES**:
* If you have issues with monitors failing to sleep or the system cannot wake when the monitors are off, then disable the `turn_off_monitors` feature.
* You can set `gui_editor = konsole -e vim`, for example, to use vim in a terminal window.  If you don't have `geany` installed, then be sure to change `gui_editor`.
* `pwr-tray` changes directory to `~/.config/pwr-tray`.
* If its .ini file is missing, it is created and `lockpaper.png` is copied there too.
* Your picks of mode, timeouts, etc. are saved to disk when changed, and restored on the next start.
* Items may be absent depending on the mode and battery state.
- **NOTE**: when in LoBattery, SleepAfterLock becomes the effective mode. The icon will change per your selection and the battery state.

---

## Per-DE Specific Notes

### i3wm Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `xscreensaver`, `xfce4-power-manager`, etc.) when running `i3` whether started by `systemd` or `i3/config` or whatever; defeat the X11 defaults somehow such as in `~/.config/i3/config`:
```
        exec --no-startup-id xset s off ; xset s noblank ; xset -dpms
```
* Edit `/etc/systemd/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, set each action to `suspend`, and then either reboot or restart `systemd-logind`.  That enables `xss-lock` to handle those keys.
* In your config, arrange for the power key (when set to suspend) to also have the system locked on power up with:
```
set $screenlock i3lock -t -i ~/.config/pwr-tray/lockpaper.png --ignore-empty-password --show-failed-attempts
exec --no-startup-id xss-lock --transfer-sleep-lock -- $screenlock --nofork
bindsym XF86PowerOff exec --no-startup-id $screenlock && systemctl suspend
bindsym $mod+Escape exec --no-startup-id $screenlock  # create shortcut to lock screen only

```
* Finally, start your pwr-tray somehow. Below is a simplest case using `i3status`, but it may depend on your status bar:
```
        bar { 
            status_command i3status
            tray_output primary
        }
        exec_always --no-startup-id ~/.local/bin/pwr-tray
```
* If you use `polybar` for status, then it may be best to run `pwr-tray` from polybar's 'launch' script; e.g., `sleep 1.5 && setsid ~/.local/bin/pwr-tray &`;  the delay may be need to allow time for the tray to become ready.

### sway Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `swayidle`, `xfce4-power-manager`, etc.) when running `sway` whether started by `systemd` or `sway/config` or whatever.
* **NOTE**: on `sway`, `pwr-tray` cannot read the idle time and do its usual micromanagement; instead, it runs a `swayidle` command whose arguments may vary with your settings.
* Edit `/etc/system/logind.conf` and uncomment `HandlePowerKey=` and `HandleLidSwitch=`, and set each action to `suspend`; then either reboot or restart `systemd-logind`.  That enables the ever-running `swayidle` to handle the suspend / resume events.
* Again, find a way to start `pwr-tray`; perhaps adding to sway's config: `exec_always --no-startup-id sleep 2 && ~/.local/bin/pwr-tray`; a delay may be required to let the tray initialize.

### KDE (X11) Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving", "Suspend session", etc., except keep the "Button events handling" and make it as you wish (e.g., "When power button pressed", "Sleep").
* In Settings/AutoStart, add the full path of `~/.local/bin/pwr-tray`.

