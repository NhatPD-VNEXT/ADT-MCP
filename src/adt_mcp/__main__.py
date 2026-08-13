"""Entry point: `python -m adt_mcp` runs MCP + web admin on one port."""
import atexit
import logging
import os

import httpx
from uvicorn.config import LOGGING_CONFIG

from .paths import systems_path
from .registry import SystemRegistry
from .adt_client import ADTClient
from .server import build_server

_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging() -> None:
    """Prefix every log line with a timestamp.

    FastMCP builds uvicorn's Config without a custom log_config, so uvicorn's
    default formatters (which omit asctime) are used and its access logger does
    not propagate to root. Patch uvicorn's own formatters in place, and set a
    timestamped root format too (covers httpx 'HTTP Request' lines and ours).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt=_DATEFMT,
    )
    fmts = LOGGING_CONFIG["formatters"]
    fmts["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
    fmts["default"]["datefmt"] = _DATEFMT
    fmts["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - '
        '"%(request_line)s" %(status_code)s')
    fmts["access"]["datefmt"] = _DATEFMT


def main() -> None:
    configure_logging()
    port = int(os.environ.get("ADT_MCP_PORT", "8765"))

    registry = SystemRegistry(systems_path())
    adt = ADTClient(httpx.Client(timeout=30.0, verify=True))
    mcp = build_server(registry, adt)
    mcp.settings.port = port
    mcp.settings.host = "127.0.0.1"
    logging.getLogger("adt_mcp").info(
        "ADT MCP on http://127.0.0.1:%s  (MCP at /mcp, admin at /)", port)

    # Shutdown must delete every external breakpoint this server set and release
    # any debuggee still stopped. A leftover breakpoint pops the debugger open
    # on the user's real session later; an abandoned debuggee keeps one of the
    # tenant's work processes. Registered with atexit as well as the finally,
    # because Ctrl+C on Windows does not always come back through mcp.run().
    manager = getattr(mcp, "debug_manager", None)
    if manager is not None:
        atexit.register(manager.close_all)
    try:
        mcp.run(transport="streamable-http")
    finally:
        if manager is not None:
            manager.close_all()


if __name__ == "__main__":
    main()
