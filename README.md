# ADT MCP (Python)

Multi-system MCP server for reading and writing ABAP source via SAP ADT,
with a local web admin to configure systems. One process serves both the
MCP endpoint (`/mcp`) and the web admin (`/`).

## Install (Windows)

```bat
git clone https://github.com/NhatPD-VNEXT/ADT-MCP adt-mcp
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

Debug (gated by `allow_debug`, see below):
- `debug_set_breakpoint`, `debug_delete_breakpoint`, `debug_clear_breakpoints`
- `debug_listen`, `debug_poll`, `debug_stop_listener`
- `debug_attach`, `debug_detach`
- `debug_stack`, `debug_variables`, `debug_step`
- `run_class` (IF_OO_ADT_CLASSRUN — Eclipse's F9)

Cookie maintenance: `refresh_cookies_for`.

## Debugging

The tenant must expose the ADT debugger; check with

```
GET /sap/bc/adt/compatibility/graph   → COM.SAP.ADT.DEBUGGER :: userRequestDebugging
```

Enable it per system in `systems.json` — it is **off by default**, separately
from `allow_write`, because `run_class` executes arbitrary ABAP:

```json
"allow_debug": true,
"debug_timeout": 600,
"debug_listen_seconds": 120
```

The flow, in order:

```
debug_set_breakpoint  → statement="…" lets the server find the executable line
debug_listen          → returns at once; the listener runs in the background
run_class             → run the code WHILE the listener waits
debug_poll            → state: listening → caught
debug_attach
debug_stack / debug_variables / debug_step
debug_detach          → releases the debuggee and returns the run's output
```

`debug_variables` needs explicit names — read the source first. There is no
working "list what is in scope" call on this platform: `getChildVariables`
answers with `ME` alone whatever parent it is asked for.

Two things follow from how SAP works, not from choices made here:

- **Run the code while the listener is waiting.** A debuggee is only trapped by
  a listener that was already registered. `debug_listen` therefore returns
  immediately instead of blocking.
- **A breakpoint freezes the HTTP request that started the code.** `run_class`
  runs in the background for that reason, and the console output only arrives
  at `debug_detach` (or a later `debug_poll`).

### Cost on cloud

The ABAP session *is* the `SAP_SESSIONID` cookie, so the debugger's isolated
channels each need their own login. The first `debug_listen` and the first
`run_class` on a system therefore take ~20–25s while a headless login runs;
afterwards the sessions are reused. Channel cookies land in
`cookies/<system>.debug.txt` / `.exec.txt` and are gitignored like the rest.

Breakpoints set here are **external** breakpoints keyed on the ABAP user, so
leaving one behind would pop the debugger open on a real session later. The
server deletes everything it set when it shuts down; `debug_clear_breakpoints`
does it on demand. The `ideId` is `adt-mcp`, distinct from Eclipse's, so your
IDE breakpoints are never touched.

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
