# ADT MCP (Python)

Multi-system MCP server for reading and writing ABAP source via SAP ADT,
with a local web admin to configure systems. One process serves both the
MCP endpoint (`/mcp`) and the web admin (`/`).

## Install (Windows)

```bat
git clone <repo-url> adt-mcp
cd adt-mcp
install.bat        REM creates .venv, installs everything, verifies the import
run.bat            REM starts the server and opens the web admin
```

Only prerequisite: **Python 3.10+** on the machine. `install.bat` finds it via
`py -3` or `python` and skips the Microsoft Store stub; it creates a project
`.venv` so nothing global is touched. Flags:

| Flag | Effect |
| --- | --- |
| `--no-venv` | Install into the Python already on PATH instead of `.venv` |
| `--no-browser` | Skip Playwright (only fine if every system uses basic auth) |

Playwright is installed by default because cookie systems log in through a
browser. It drives the machine's own Chrome/Edge, so there is no browser
download; if neither is installed, run
`.venv\Scripts\python -m playwright install chromium`.

### Configure the new machine

`systems.json` and `cookies/` are gitignored — they hold credentials and are
**never** in the repo. After `install.bat`, pick one:

- **Add the system in the web admin** (`run.bat` → http://127.0.0.1:8765) and
  log in once with the browser flow. Nothing secret has to be copied.
- **Copy `systems.json` + `cookies\` by hand** from a working machine (USB,
  password manager, internal share — not email/chat). Fix the absolute
  `cookie_file` paths afterwards, or re-login from the admin.

Until that is done the server runs and the admin opens, but `list_systems`
reports no systems.

## Install (manual / non-Windows)

```bash
cd adt-mcp
python -m pip install -e ".[refresh]"   # -e matters: config, web/ and cookies/
                                        # are read from the checkout.
                                        # [refresh] = Playwright cookie login
```

## Run

```bash
python -m adt_mcp        # or: adt-mcp
# → http://127.0.0.1:8765  (MCP at /mcp, admin at /)
```

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADT_MCP_PORT` | `8765` | Port for MCP + web admin (`run.bat` follows it) |
| `ADT_MCP_HOME` | the checkout | Folder holding `systems.json`, `web/`, `cookies/` |
| `ADT_MCP_SYSTEMS` | `<home>/systems.json` | Explicit path to the systems config |
| `ADT_MCP_TOOLS` | `full` | `core` exposes only the essential tools |
| `ADT_MCP_BROWSER` | `chrome` | Browser channel for cookie login (`msedge`/`chromium`) |
| `ADT_MCP_CDP` | `http://127.0.0.1:9222` | Chrome DevTools endpoint for `mode: "cdp"` |

Set `ADT_MCP_HOME` when the package is installed non-editable or run as a
service from another working directory.

Open http://127.0.0.1:8765 to add SAP systems (URL, client, language, auth).
Config is stored in `systems.json` (gitignored). See `systems.example.json`.
Cookie systems can be (re)authenticated from the web admin via a browser login.

## Connect Claude Code

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "sap-adt": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

## Tools

Read / navigate:
- `list_systems`, `list_package`, `search_objects`
- `get_source`, `get_source_by_uri`, `get_class_method_source`,
  `get_class_include`, `get_object_structure`, `get_package_source`
- `grep_package`, `find_references` (where-used), `cds_dependencies`
- `get_context` (object + compressed dependencies: CDS/BDEF/CLAS)
- `get_revisions`, `get_revision_source`, `compare_source`
- `syntax_check`, `run_unit_tests` (ABAP Unit), `data_preview` (CDS/SQL data)
- `trace_start`, `trace_list`, `trace_analyze` (ABAP profiler: CPU hotspots + DB accesses)
- `list_dumps`, `get_dump` (ST22 runtime dumps: liệt kê + đọc chi tiết để phân tích lỗi)

Write (gated by safety, see below):
- `update_source`, `update_class_include`, `activate`
- `create_object` (CLAS / INTF / DDLS / DDLX / BDEF / SRVD / SRVB / TABL)
- `clone_package` (clone toàn bộ object của một package sang package đích, thêm suffix `_VN` + sửa tham chiếu chéo trong source; dry-run mặc định)

Cookie maintenance: `refresh_cookies_for`.

## Write safety

Writes are **off by default**. Per system in `systems.json`:
- `allow_write: true` — required to enable any create/update.
- `write_packages: ["Z*", "$TMP"]` — target package must match (default).

Delete is intentionally **not** supported.

## Token economy

Tool schemas are sent to the model on every turn. Set `ADT_MCP_TOOLS=core`
to expose only the essential ~16 tools (smaller schema); default `full`
exposes all 29. Descriptions are kept terse.

```bash
ADT_MCP_TOOLS=core python -m adt_mcp
```

## Security

- `systems.json`, `cookies/`, `*-cookies.txt` hold session secrets and are
  gitignored — never commit them.
- The server binds `127.0.0.1` only.
- **Stored passwords are plaintext.** A `username`/`password` is only kept to
  enable headless cookie refresh (`refresh_cookies_for`). For real systems
  prefer the cookie flows that store **no** password:
  - `mode: "browser"` — log in once in a visible browser; only session cookies
    are saved (the persistent profile keeps SSO so re-login is rare).
  - `mode: "cdp"` — attach to your already-authenticated Chrome.
  If you must keep a password, store it in an OS keyring / secrets manager and
  inject it into `systems.json` at deploy time rather than committing it.
