# Owned games, launcher libraries and devices

Open **Wanted → Lists → My games**. Search your connected store libraries
under **Storefronts** and browse your linked ROM libraries under **ROM
libraries**. Each storefront's source status shows its last sync. Use
**Sync lists** after connecting a store. Steam wishlists stay in the
acquisition lists. Xbox and PlayStation activity appears with a **Played**
label.

## Report a Windows or Linux gaming PC

Run this from a ROMarr checkout on the gaming device, and enter your ROMarr
API key when prompted. Use your HTTPS ROMarr address for a remote server.

~~~sh
python scripts/connect_launchers.py --url https://romarr.example
~~~

Run it again after making changes to refresh the device's report. Steam and
Epic can confirm local installations from launcher files. Other launcher
sources may report titles without a confirmed install directory. Reports
include game titles, IDs where supported, and install status. Paths and
store account secrets stay on the device.

For Steam titles with an app ID, **Install here** opens Steam on the device
viewing ROMarr. Choose the target Steam library and drive in Steam. **Play in
Steam** opens the same game's launch flow. On a remote gaming host, open
ROMarr in that host's desktop session to hand off to its local Steam client.
Steam also offers [Remote Downloads](https://help.steampowered.com/en/faqs/view/1025-BD94-12FC-3409) for a PC or Mac with Steam signed in and running.
A Sunshine or Wolf host can stream apps that the host already exposes to
Moonlight.

## LaunchBox and Playnite

Use **ROM libraries → Export to a launcher** for LaunchBox XML or Playnite
JSON. If the server sees your library at `/mnt/roms` and the gaming PC sees
that same library at `R:\ROMs` or a UNC share, enter both mount paths. The
export covers all detected local ROM files that ROMarr can map to a filename.
The launcher needs access to the resulting path.

For Playnite, copy `contrib/playnite/ROMarr` into Playnite's Extensions
directory and restart Playnite. **ROMarr → Configure**
sets the server and API key. **ROMarr → Sync library** imports the
ROM catalogue. **ROMarr → Report this PC's games to ROMarr** sends Playnite's
connected-library catalogue and installed status. Playnite handles its own
provider sign-ins, for instance Amazon Games and GOG.

For LaunchBox, use its ROM import or the sample in `contrib/launchbox`.
Cartridge continues to use its RomM-compatible library for ROM play.

## Free Game Collector

Choose **Show offers** under **Free Game Collector** to see active Epic base-game
giveaways. `ROMARR_GIVEAWAY_COUNTRY` selects the two-letter Epic country
code and defaults to `US`. Follow an offer to claim it in the official store.
Amazon Luna, Steam and GOG open their storefront pages. After claiming a
game, sync the store or report it from Playnite to update My games.
