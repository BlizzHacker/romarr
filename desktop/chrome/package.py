"""Build a deterministic Chrome Web Store upload ZIP without build dependencies."""
from pathlib import Path
import argparse
import zipfile


def package(destination: Path) -> None:
    """Write only runtime extension files, with fixed order and timestamps."""
    source = Path(__file__).resolve().parent
    files = [source / name for name in ("manifest.json", "popup.html", "popup.css", "popup.js", "validation.js")]
    files += sorted((source / "icons").glob("*.png"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            entry = zipfile.ZipInfo(path.relative_to(source).as_posix(), (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    package(parser.parse_args().output)
