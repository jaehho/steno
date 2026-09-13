# packaging

`PKGBUILD` builds the Arch package from this checkout (`make install`). It
depends on GStreamer as well as GTK, because archive playback uses it directly —
GTK's own media backend is not packaged on Arch.

`dev.jaeho.Steno.desktop` is the launcher entry, with actions for recording and
quitting. `make desktop` installs it to `~/.local/share/applications`, which is
what makes rofi list it.

Starting at login is opt-in and per user: the window menu's "Start at login",
`steno autostart on`, or `make autostart` write `~/.config/autostart/steno.desktop`
(`steno.autostart`). The package installs nothing into `/etc/xdg/autostart`, since
that would start recording detected calls for every account on the machine;
`steno.install` says how to turn it on. There is no systemd unit — it needs a
display, so the session should own it.

`aur/steno-git/` is the AUR package. `make aur` regenerates its `.SRCINFO`.

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
