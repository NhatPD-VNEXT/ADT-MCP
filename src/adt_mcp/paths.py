"""Where the project's on-disk data lives: systems.json, web/, cookies/.

The package uses a src-layout, so when it is imported from a checkout
(editable install, PYTHONPATH=src, or pytest) the project root is three
levels above this file. A plain non-editable ``pip install .`` puts the
package in site-packages and that assumption silently points at the
interpreter's library folder, so fall back to the working directory and
allow an explicit ``ADT_MCP_HOME`` override for service deployments.
"""
import os

_SRC_LAYOUT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def project_root() -> str:
    """Directory that holds systems.json, web/ and cookies/."""
    home = os.environ.get("ADT_MCP_HOME", "").strip()
    if home:
        return os.path.abspath(home)
    if os.path.isdir(os.path.join(_SRC_LAYOUT_ROOT, "web")):
        return _SRC_LAYOUT_ROOT
    return os.getcwd()


def systems_path() -> str:
    """Path of the systems config file (ADT_MCP_SYSTEMS wins)."""
    override = os.environ.get("ADT_MCP_SYSTEMS", "").strip()
    return override or os.path.join(project_root(), "systems.json")


def web_dir() -> str:
    """Directory of the web admin's static files."""
    return os.path.join(project_root(), "web")


def cookies_dir() -> str:
    """Directory for captured cookie files; created on demand."""
    d = os.path.join(project_root(), "cookies")
    os.makedirs(d, exist_ok=True)
    return d
