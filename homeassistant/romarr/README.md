# ROMarr — Home Assistant add-on

Runs the stock ROMarr image as a Home Assistant add-on. Options set in the
add-on configuration page become ROMarr's environment: every key in the
add-on's options is upper-cased and read at start (`prowlarr_url` becomes
`PROWLARR_URL`), so anything the [main README](../../README.md#configuration)
documents can be set here.

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories**
2. Add `https://github.com/BlizzHacker/romarr`
3. Install **ROMarr**, set at least `romarr_password`, start it.
4. Open `http://homeassistant.local:6868`.

ROMs live under `/media/roms` by default — the add-on maps Home Assistant's
`media` share read-write, which is also where a RomM or Jellyfin add-on can
see them.

*The add-on packaging exists because [Questarr](https://github.com/Doezer/Questarr)
proved game-*arrs belong in the Home Assistant store. Credit where due.*
