"""Entry point: `python -m romarr`."""
import json
import logging
import os
from pathlib import Path


def load_ha_options(path: str = "/data/options.json") -> dict[str, str]:
    """Home Assistant's configuration, translated into the environment.

    An HA add-on's options arrive as /data/options.json, not as environment
    variables. Reading it here -- upper-cased keys, scalars only -- makes the
    stock image a working add-on with no shell shim in between, and costs
    nothing anywhere else because the file simply does not exist outside HA.
    The real environment wins over the file: a variable someone set on the
    container is the more deliberate act.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool)) and str(value) != "":
            name = str(key).upper()
            if name not in os.environ:
                out[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return out


def resolve_port(env: dict[str, str] | None = None) -> int:
    """Resolve the HTTP listening port from ROMARR_PORT, WEBUI_PORT or PORT.

    The Dockerfile sets ``ENV ROMARR_PORT=6868``, so in containers where an
    operator sets ``WEBUI_PORT`` or ``PORT`` (common in Gluetun and *arr
    compose stacks) we honour that override when ``ROMARR_PORT`` is still at
    its default, and sync ``ROMARR_PORT`` so Docker's healthcheck probes the
    actual listening port.
    """
    e = env if env is not None else os.environ
    raw_romarr = str(e.get("ROMARR_PORT") or "").strip().strip("\"'")
    raw_alt = (str(e.get("WEBUI_PORT") or "").strip().strip("\"'")
               or str(e.get("PORT") or "").strip().strip("\"'"))
    chosen = raw_romarr
    if (not chosen or chosen == "6868") and raw_alt:
        chosen = raw_alt
    if not chosen:
        chosen = "6868"
    try:
        port = int(chosen)
        if not (1 <= port <= 65535):
            raise ValueError("port out of range")
    except ValueError:
        logging.getLogger(__name__).warning(
            "Invalid port %r; falling back to 6868", chosen)
        port = 6868
    if env is None:
        os.environ["ROMARR_PORT"] = str(port)
    return port


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ha = load_ha_options()
    if ha:
        os.environ.update(ha)
        logging.getLogger(__name__).info(
            "Home Assistant options loaded: %s", ", ".join(sorted(ha)))

    from .app import serve
    from .store import StateUnreadable
    try:
        serve(port=resolve_port())
    except StateUnreadable as err:
        # One line an operator can act on, not a traceback. Exit 1 so Docker
        # marks the container failed and `restart: unless-stopped` does not
        # hide it: a permission problem does not fix itself on the next boot,
        # and a container that restart-loops visibly is better than one that
        # comes up green having thrown the install's credentials away.
        logging.getLogger(__name__).error("%s", err)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
