# pwr-tray
A GTK Tray Applet for Power/Energy Saving and System/DE Controls 

### Install
**Details TBD**
* Basically: `pipx install pwr-tray`
* Plus, various executables.
* Manually run as `pwr-tray` to create config (in `~/.config/pwr-tray/config.ini`)
#### System Python Modules are needed
* On Debian:
    * gir1.2-appindicator3-0.1 python3-gi
    * python3-gi-cairo gir1.2-gtk-3.0 libappindicator3-1
* On Debian 12 and others?, if the above is not working, try:
    * gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
    * python3-gi python3-gi-cairo gir1.2-gtk-3.0 ayatanalibappindicator3-1

* On Arch:
    * libappindicator-gtk3 python-gobject python-cairo




### Manual Launch
- For foreground in terminal, run `pwr-tray`
- In background, run `setsid pwr-tray >/dev/null 2>&1 &`

### Output for Debugging 
- unless stdout is a tty, say for debugging, ~/.config/pwr-tray/debug.log is used instead of stdout
- if we create a log file, its size is limited to 512K and then it is truncated
- TBD: more info (`tail -F`, `ssh`, whatever)

### Menu Options
Choose from three *major power modes* (to limit the effect of timeouts):
- **☀ PRESENTATION Mode** -  Keeps the monitors on and system up.
- **☀ NO-SLEEP Mode** - Keeps the system up, but not the monitors on.
- **☀ NORMAL Mode** - Allows the monitors off and system to go down (the default).

Or choose to *start the screen saver / lock*:
- **▷ Start Screensaver** - start the screensaver (configure your screensaver separately, as you wish, but it should not be "None").
- **▷ Blank Monitors** - Blanks the screen immediately and locks the screen (using your screen saver).

Or choose a new *system state*:
- **▼ Suspend System** - suspends the system immediately.
- **▼ Reboot System** - reboots the system immediately.
- **▼ Poweroff System** - power down the system immediately.

Or choose new controls values: 
- **♺ Chg Screen Idle: 10m->30m** - change the time to start the screen saver; each time clicked, it changes to the next one; you can fix the timeout or choose a list of them from the command line options; the default list is [10m, 30m].
- **♺ Chg System Idle: 30m->60m** - change the time to take the system down; clicking selects the next value; change on the command line; the default is [30m, 60m].
- **♺ Chg Down State: Suspend** - toggle how the system goes down when System Idle expires, Suspend or PowerOff;  you can select the default from the command line.

**Notes:**
- only the menu options that can have effect are shown (e.g., you cannot see "Presentation Mode" if in "Presentation Mode", you cannot see "Chg Screen Idle" if there is only one possibility, etc).


### i3wm Specific Notes
* Uninstall or disable all competing energy saving programs (e.g., `xscreensaver`, `xfce4-power-manage`, etc.) which running `i3` whether started by `systemd` or `i3/config` or whatever.
* Edit `/etc/system/logind.conf` and uncomment `HandlePowerKey=`, `HandlePowerKey=`, `HandlePowerKey=`, and `HandlePowerKey=`, and set the action to `suspend` (reboot or restart `systemd-logind`).  That enables `xss-switch` to handle those keys.
* In `~/.config/i3/config`, configure something like:
```
set $screenlock i3lock -t -i ~/.config/pwr-tray/lockpaper.png --ignore-empty-password --show-failed-attempts
exec --no-startup-id xss-lock --transfer-sleep-lock -- $screenlock --nofork
            
```
That creates handlers for those special key presses that is compatible with `pwr-tray` defaults. Vary if needed (but you might start with this suggestion).

### KDE (X11) Specific Notes
* In Settings/Energy Saving, disable "Screen Energy Saving", "Suspend session", etc., except keep the "Button events handling" and make it as you wish (e.g., "When power button pressed", "Sleep").

