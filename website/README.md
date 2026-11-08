# ROMarr website

A dependency-free static showcase for [romarr.app](https://romarr.app/). The page uses the interactive-search screenshot from `docs/img/interactive-search-live.png` and keeps shipped integrations separate from proposed cross-project direction.

The visual system follows RomM's warm cream, ink, and violet palette with a locally hosted Silkscreen headline font. The font's OFL license is included at `assets/fonts/OFL-Silkscreen.txt`.

For a local preview from the repository root:

```sh
python -m http.server 8000 --directory website
```

The site has no build step or third-party runtime scripts. Keep `website/assets/romarr-search.png` in sync with the product screenshot when the interface changes.
