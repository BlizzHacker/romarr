# ROMarr website

A dependency-free static showcase for [romarr.app](https://romarr.app/). The page uses the interactive-search screenshot from `docs/img/interactive-search-live.png` and distinguishes shipped integrations from proposed cross-project direction.

For a local preview from the repository root:

```sh
python -m http.server 8000 --directory website
```

The page has no build step or third-party runtime scripts. Keep `website/assets/romarr-search.png` in sync with the product screenshot when the interface changes.
