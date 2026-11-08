# ROMarr website

A dependency-free static showcase for [romarr.app](https://romarr.app/), served directly from `website/`.

The design is a custom dark workbench: graphite, steel, and blue; system UI typography; no downloaded or embedded display font; a restrained treatment of the product screenshot; and an original route diagram that shows optional handoffs across ROM Hub, ROMarr, libraries, and players.

The page presents RomM as our favorite while linking Gaseous and Retrom as alternate libraries, and Playnite and LaunchBox as player choices. It introduces Cartridge, Yarr.It, ROM Hub, the ROM Hub plugin projects, Move Weight's self-hosting guide, Jellyfin, Seerr, and adjacent self-hosted media tools. ROM Hub's plugin catalog remains the source of truth for current install status.

For a local preview from the repository root:

```sh
python -m http.server 8000 --directory website
```

The site has no build step or third-party runtime scripts. Keep `website/assets/romarr-search.png` in sync with the product screenshot when the interface changes.
