# packaging

`PKGBUILD` builds the Arch package from this checkout (`make install`). It
depends on GStreamer as well as GTK, because archive playback uses it directly —
GTK's own media backend is not packaged on Arch.

`dev.jaeho.Steno.desktop` is the launcher entry, with actions for recording and
quitting. `make desktop` installs it to `~/.local/share/applications`, which is
what makes rofi list it.

`steno-autostart.desktop` starts the listener at login with `--background`: no
window, just detection. The app has to be running to notice a meeting, and the
person who just logged in did not ask to look at their archive. `make autostart`
links it; the package installs it to `/etc/xdg/autostart`. There is no systemd
unit — it needs a display, so the session should own it.

`install-hypr.py` adds one line to `hyprland.lua`'s start block (`make hypr`).
Hyprland does not read `~/.config/autostart`, and this config starts its daemons
there, so that is where the listener belongs. It writes an absolute path,
because the compositor's environment usually lacks `~/.local/bin`.

`install-waybar.py` adds the recording indicator to an existing waybar config
(`make waybar`). It edits the JSONC as text rather than reparsing it, because
reparsing would delete the comments the user wrote. Both edits are idempotent,
both files are backed up first, and `--remove` takes it back out. The module's
icons come from `steno.state.ICONS` and its colours from the user's own palette,
so neither can drift from the app.
