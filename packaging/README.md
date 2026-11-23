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
| [`synology/`](synology/) | Synology: SynoCommunity spksrc package and Container Manager project |
| [`qnap/`](qnap/) | QNAP: QPKG (QDK) and Container Station application |
| [`asustor/`](asustor/) | Asustor: App Central APK and Portainer stack |
| [`terramaster/`](terramaster/) | TerraMaster TOS 6 Docker Manager project |
| [`ugreen/`](ugreen/) | UGREEN UGOS Pro Docker project |
| [`openmediavault/`](openmediavault/) | openmediavault compose plugin |

Each folder mirrors the path layout of the store's own repository, so a
submission is a copy of the folder's contents. `tests/test_packaging.py` and
`tests/test_nas_packaging.py` keep their versions, image tags and ports in step.
The NAS install guide is [docs/NAS.md](../docs/NAS.md).
