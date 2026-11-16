# Packaging

Store and package-manager packaging for ROMarr. Status, validation and submission
steps for all of them: [docs/STORES.md](../docs/STORES.md).

| Folder | Store |
|---|---|
| [`flatpak/`](flatpak/) | Flathub (Linux, Steam Deck, ChromeOS) |
| [`proxmox/`](proxmox/) | Proxmox VE Community Scripts (community-scripts format) |
| [`truenas/`](truenas/) | TrueNAS SCALE Community train, plus an Install-via-YAML file |
| [`casaos/`](casaos/) | CasaOS / ZimaOS AppStore |
| [`umbrel/`](umbrel/) | Umbrel App Store |
| [`winget/`](winget/) | Windows Package Manager |
| [`homebrew/`](homebrew/) | Homebrew (macOS and Linux) |

Each folder mirrors the path layout of the store's own repository, so a
submission is a copy of the folder's contents. `tests/test_packaging.py` keeps
their versions, image tags and ports in step.
