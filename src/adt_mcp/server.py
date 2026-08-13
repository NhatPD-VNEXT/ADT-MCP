"""FastMCP server wiring registry + ADT client into MCP tools."""
import os
import anyio
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse
from .paths import cookies_dir as _cookies_dir, web_dir as _web_dir
from .registry import System, SystemRegistry
from .adt_client import ADTClient
from .cookie_refresh import refresh_cookies, interactive_login, cdp_capture


def format_systems(systems: list[System]) -> str:
    if not systems:
        return "No systems configured. Open the web admin to add one."
    lines = ["Available systems:"]
    for s in systems:
        lines.append(f"- {s.name}: {s.url} (client {s.client}, auth {s.auth})")
    return "\n".join(lines)


def format_connections(rows: list[dict]) -> str:
    if not rows:
        return "No systems configured. Open the web admin to add one."
    lines = ["System connections:"]
    for r in rows:
        mark = "✅ connected" if r["connected"] else "❌ not connected"
        user = r["user"] or "(unknown)"
        detail = "" if r["connected"] else f" — {r['status']}"
        lines.append(
            f"- {r['name']}: {mark} | user {user} | "
            f"{r['url']} (client {r['client']}, auth {r['auth']}){detail}")
    return "\n".join(lines)


def resolve_and_get(registry: SystemRegistry, adt: ADTClient,
                    system: str, object_type: str, name: str,
                    function_group: str | None) -> str:
    try:
        sys = registry.get(system)
    except KeyError:
        names = ", ".join(s.name for s in registry.list()) or "(none)"
        return f"Error: unknown system {system!r}. Known: {names}"
    return adt.get_source(sys, object_type, name, function_group)


def resolve_and_refresh(registry: SystemRegistry, system: str) -> str:
    """Refresh a cookie system's session via SAML login (Playwright)."""
    try:
        sys = registry.get(system)
    except KeyError:
        names = ", ".join(s.name for s in registry.list()) or "(none)"
        return f"Error: unknown system {system!r}. Known: {names}"
    if sys.auth != "cookie" or not sys.cookie_file:
        return (f"Error: system {system!r} is not a cookie_file system; "
                f"refresh only applies to cookie auth with a cookie_file")
    if not sys.username or not sys.password:
        return (f"Error: system {system!r} needs username and password "
                f"(stored for login) to refresh cookies")
    return refresh_cookies(sys.url, sys.username, sys.password, sys.cookie_file)


CORE_TOOLS = {
    "list_systems", "check_connection", "list_package", "search_objects",
    "get_source",
    "get_source_by_uri", "get_context", "grep_package", "find_references",
    "update_source", "update_class_include", "create_object", "activate",
    "syntax_check", "run_unit_tests", "data_preview", "refresh_cookies_for",
    "clone_package",
}


def build_server(registry: SystemRegistry, adt: ADTClient) -> FastMCP:
    mcp = FastMCP("adt-mcp", host="127.0.0.1")
    mcp.registry = registry  # type: ignore[attr-defined]
    mcp.adt = adt            # type: ignore[attr-defined]

    # Token economy: ADT_MCP_TOOLS=core loads only the essential tools so the
    # always-on tool schema is ~40% smaller. Default "full" loads everything.
    mode = os.environ.get("ADT_MCP_TOOLS", "full").lower()
    enabled = CORE_TOOLS if mode == "core" else None  # None = all

    def tool(name: str):
        def deco(fn):
            return mcp.tool()(fn) if (enabled is None or name in enabled) else fn
        return deco

    @tool("list_systems")
    def list_systems() -> str:
        """List configured SAP systems available for source retrieval."""
        return format_systems(registry.list())

    @tool("check_connection")
    def check_connection() -> str:
        """Test connectivity of all configured SAP systems (like the web Test).

        Probes each system's ADT discovery endpoint and reports whether it is
        connected and which user the connection authenticates as.
        """
        return format_connections(adt.check_connections(registry.list()))

    @tool("get_source")
    def get_source(system: str, object_type: str, name: str,
                   function_group: str | None = None) -> str:
        """Fetch ABAP source. object_type CLAS/PROG/INTF/INCL/FUGR/DDLS/DDLX/BDEF/SRVD/TABL/VIEW/STRU; FUGR needs function_group."""
        return resolve_and_get(registry, adt, system, object_type,
                               name, function_group)

    def _resolve(system: str):
        """Return (System, None) or (None, error_text)."""
        try:
            return registry.get(system), None
        except KeyError:
            names = ", ".join(s.name for s in registry.list()) or "(none)"
            return None, f"Error: unknown system {system!r}. Known: {names}"

    def _fmt_objects(objs) -> str:
        if isinstance(objs, str):
            return objs
        if not objs:
            return "(none)"
        lines = []
        for o in objs:
            desc = f" — {o['description']}" if o.get("description") else ""
            lines.append(f"{o['type']:<10} {o['name']}{desc}\t[{o['uri']}]")
        return "\n".join(lines)

    @tool("list_package")
    def list_package(system: str, package: str, recursive: bool = False) -> str:
        """List objects and subpackages inside an ABAP package.

        Returns one object per line: TYPE NAME — description [uri].
        Set recursive=true to descend into subpackages.
        """
        sys, err = _resolve(system)
        if err:
            return err
        return _fmt_objects(adt.list_package(sys, package, recursive))

    @tool("search_objects")
    def search_objects(system: str, query: str, max_results: int = 20) -> str:
        """Search ABAP objects by name/wildcard (e.g. 'ZCL_ORDER*')."""
        sys, err = _resolve(system)
        if err:
            return err
        return _fmt_objects(adt.search_objects(sys, query, max_results))

    @tool("get_source_by_uri")
    def get_source_by_uri(system: str, uri: str) -> str:
        """Fetch ABAP source for an object by its ADT URI (from list/search)."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_source_by_uri(sys, uri)

    @tool("get_class_method_source")
    def get_class_method_source(system: str, class_name: str,
                                method: str) -> str:
        """Fetch a single METHOD…ENDMETHOD block from a class."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_class_method_source(sys, class_name, method)

    @tool("get_class_include")
    def get_class_include(system: str, class_name: str, include: str) -> str:
        """Fetch a class include: definitions | implementations | macros | testclasses."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_class_include(sys, class_name, include)

    @tool("get_object_structure")
    def get_object_structure(system: str, class_name: str) -> str:
        """List the declared method names of a class (outline)."""
        sys, err = _resolve(system)
        if err:
            return err
        res = adt.object_structure(sys, class_name)
        if isinstance(res, str):
            return res
        return "\n".join(res) if res else "(no methods declared)"

    @tool("get_package_source")
    def get_package_source(system: str, package: str,
                           max_objects: int = 50) -> str:
        """Concatenated source of all source-bearing objects in a package."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_package_source(sys, package, max_objects)

    @tool("grep_package")
    def grep_package(system: str, package: str, pattern: str,
                     ignore_case: bool = False, max_objects: int = 100) -> str:
        """Regex-search the source of objects in a package.

        Returns matches as NAME:line: text.
        """
        sys, err = _resolve(system)
        if err:
            return err
        return adt.grep_package(sys, package, pattern, ignore_case, max_objects)

    @tool("get_revisions")
    def get_revisions(system: str, object_type: str, name: str,
                      function_group: str | None = None,
                      include: str | None = None) -> str:
        """Version history (PROG/CLAS/INTF/FUNC/INCL/DDLS/BDEF/SRVD). CLAS: pass include; FUNC: pass function_group."""
        sys, err = _resolve(system)
        if err:
            return err
        res = adt.get_revisions(sys, object_type, name, function_group, include)
        if isinstance(res, str):
            return res
        if not res:
            return "(no revisions)"
        lines = []
        for r in res:
            tr = f"  TR={r['transport']}" if r.get("transport") else ""
            lines.append(f"{r['date']}  {r['author']}  {r['title'] or r['version']}"
                         f"{tr}\t[{r['uri']}]")
        return "\n".join(lines)

    @tool("get_revision_source")
    def get_revision_source(system: str, version_uri: str) -> str:
        """Fetch source of a specific past version (uri from get_revisions)."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_revision_source(sys, version_uri)

    @tool("compare_source")
    def compare_source(system: str, object_type: str, name: str,
                       version_uri: str, against: str = "current",
                       function_group: str | None = None) -> str:
        """Unified diff between a past version and another version (default: current)."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.compare_source(sys, object_type, name, version_uri,
                                  against, function_group)

    @tool("find_references")
    def find_references(system: str, object_uri: str, line: int = 0,
                        column: int = 0) -> str:
        """Where-used for object_uri (from list/search). Optional line+column for a symbol at that position."""
        sys, err = _resolve(system)
        if err:
            return err
        res = adt.find_references(sys, object_uri, line, column)
        if isinstance(res, str):
            return res
        if not res:
            return "(no references found)"
        lines = []
        for r in res:
            pkg = f" ({r['package']})" if r.get("package") else ""
            lines.append(f"{r['type']:<10} {r['name']}{pkg}\t[{r['uri']}]")
        return "\n".join(lines)

    @tool("cds_dependencies")
    def cds_dependencies(system: str, ddls_name: str) -> str:
        """Upstream deps of a CDS view (FROM/JOIN/ASSOCIATION/COMPOSITION), parsed from source. Downstream: use find_references."""
        sys, err = _resolve(system)
        if err:
            return err
        res = adt.cds_dependencies(sys, ddls_name)
        if isinstance(res, str):
            return res
        if not res:
            return "(no dependencies found)"
        return "\n".join(f"{r['relation']:<12} {r['name']}" for r in res)

    @tool("syntax_check")
    def syntax_check(system: str, object_type: str, name: str,
                     function_group: str | None = None,
                     version: str = "active",
                     source: str | None = None) -> str:
        """ABAP syntax/check-run on an object; reports errors+warnings. Checks `source` if given, else current active source. FUGR needs function_group."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.syntax_check(sys, object_type, name, function_group,
                                version, source)

    @tool("run_unit_tests")
    def run_unit_tests(system: str, object_type: str, name: str) -> str:
        """Run ABAP Unit for an object; reports pass/fail per method + assertion alerts. Types CLAS/PROG/FUGR (FUGR = whole group)."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.run_unit_tests(sys, object_type, name)

    @tool("data_preview")
    def data_preview(system: str, query: str, max_rows: int = 100) -> str:
        """Preview data of a CDS entity or Open SQL SELECT. query = entity name (→ SELECT * FROM it) or a full SELECT. Returns a column/row table."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.data_preview(sys, query, max_rows)

    @tool("trace_start")
    def trace_start(system: str, process_type: str = "http",
                    max_executions: int = 3, expires_minutes: int = 60,
                    title: str = "ai-trace") -> str:
        """Arm an ABAP profiler trace for your next runs, then run the slow workload. process_type: http (Fiori/OData/data_preview) | dialog | batch. Then call trace_list + trace_analyze."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.trace_start(sys, process_type=process_type,
                               max_executions=max_executions,
                               expires_minutes=expires_minutes, title=title)

    @tool("trace_list")
    def trace_list(system: str, max_runs: int = 20) -> str:
        """List your recorded ABAP profiler runs (newest first) with total/ABAP/DB time — pick a uri for trace_analyze."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.trace_list(sys, max_runs)

    @tool("trace_analyze")
    def trace_analyze(system: str, trace_uri: str, top: int = 15) -> str:
        """Digest one trace (uri from trace_list): top time consumers + DB accesses (table/stmt/count/buffered/db_ms) — shows why it's slow."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.trace_analyze(sys, trace_uri, top)

    @tool("list_dumps")
    def list_dumps(system: str, from_date: str = "", to_date: str = "",
                   max_dumps: int = 50) -> str:
        """List recent ABAP runtime dumps (ST22), newest first, with each dump's uri for get_dump. Optional from_date/to_date filter as 'yyyyMMddHHmmss' (e.g. '20260601000000')."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.list_dumps(sys, from_date or None, to_date or None,
                              max_dumps)

    @tool("get_dump")
    def get_dump(system: str, dump_uri: str) -> str:
        """Fetch one runtime dump (uri from list_dumps) as readable text — error analysis, source extract and call stack — for analyzing why ABAP/RAP/OData failed."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_dump(sys, dump_uri)

    @tool("list_feeds")
    def list_feeds(system: str) -> str:
        """List ABAP monitoring feeds the system offers (SAP Gateway Error Log, ABAP System Messages, ABAP Runtime Errors, ATC Findings, ...), each with a friendly alias for read_feed."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.list_feeds(sys)

    @tool("read_feed")
    def read_feed(system: str, feed: str, from_date: str = "", to_date: str = "",
                  user: str = "", object: str = "", max: int = 50) -> str:
        """Read one ABAP feed newest-first. feed = alias (gateway_log/system_messages/dumps/event_log/uri_errors/atc/contract_violations) or a title substring. Optional filters: from_date/to_date ('yyyy-mm-dd'), user, object substring. For full ST22 dump bodies use list_dumps/get_dump."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.read_feed(sys, feed, from_date or None, to_date or None,
                             user or None, object or None, max)

    @tool("list_transports")
    def list_transports(system: str) -> str:
        """List the session user's open (modifiable) transport requests — number, type/status, owner, description. Use the number as `transport` for create_object/update_source."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.list_transports(sys)

    @tool("create_transport")
    def create_transport(system: str, description: str) -> str:
        """Create a workbench transport request (write — requires allow_write); returns the new request number to pass as `transport` to write tools."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.create_transport(sys, description)

    @tool("pretty_print")
    def pretty_print(system: str, source: str) -> str:
        """Format ABAP source via ADT pretty printer (applies the system's keyword-case/indent settings). Returns formatted code."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.pretty_print(sys, source)

    @tool("api_release_state")
    def api_release_state(system: str, object_type: str, name: str,
                          function_group: str | None = None) -> str:
        """Check if an object is released for ABAP Cloud (Clean Core C0–C4 contracts). Types CLAS/INTF/DDLS/TABL/... ; FUGR needs function_group."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.api_release_state(sys, object_type, name, function_group)

    @tool("get_context")
    def get_context(system: str, object_type: str, name: str,
                    depth: int = 1) -> str:
        """Object full source + compressed deps. DDLS: CDS deps recursed to depth. BDEF: behavior-for CDS + impl class. CLAS: superclass + interfaces. Custom expanded, standard listed."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.get_context(sys, object_type, name, depth)

    @tool("update_source")
    def update_source(system: str, object_type: str, name: str, source: str,
                      transport: str | None = None,
                      function_group: str | None = None,
                      activate: bool = True) -> str:
        """Edit + activate an object (write; needs allow_write). Types CLAS/PROG/INTF/INCL/DDLS/DDLX/BDEF/SRVD/TABL/VIEW/STRU/FUGR. activate=False to defer."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.update_source(sys, object_type, name, source, transport,
                                 function_group, activate)

    @tool("update_class_include")
    def update_class_include(system: str, class_name: str, include: str,
                             source: str, transport: str | None = None,
                             activate: bool = True) -> str:
        """Edit a class include: main/definitions/implementations/macros/testclasses (RAP logic in implementations). activate=False to batch."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.update_class_include(sys, class_name, include, source,
                                        transport, activate)

    @tool("activate")
    def activate(system: str, object_type: str, name: str,
                 function_group: str | None = None) -> str:
        """Activate an object (write — requires allow_write)."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.activate(sys, object_type, name, function_group)

    @tool("create_object")
    def create_object(system: str, object_type: str, name: str, package: str,
                      description: str = "", source: str | None = None,
                      transport: str | None = None,
                      service_definition: str | None = None,
                      binding_version: str = "V2") -> str:
        """Create RAP object (write; needs allow_write). Types CLAS/PROG/INTF/DDLS/DDLX/DCLS/DTEL/DOMA/BDEF/SRVD/SRVB/TABL. package must exist; transport for transportable pkgs; source (if given) is written+activated; SRVB needs service_definition."""
        sys, err = _resolve(system)
        if err:
            return err
        return adt.create_object(sys, object_type, name, package, description,
                                 source, transport, service_definition,
                                 binding_version)

    @tool("clone_package")
    def clone_package(system: str, source_package: str, target_package: str,
                      target_system: str | None = None, suffix: str = "_VN",
                      dry_run: bool = True,
                      transport: str | None = None) -> str:
        """Clone mọi object của source_package sang target_package (PHẢI tồn tại sẵn),
        thêm suffix (mặc định _VN) vào mọi tên và sửa tham chiếu chéo trong source.
        dry_run=True (mặc định) chỉ in kế hoạch. target_system bỏ trống = cùng system.
        Bỏ qua DTEL/DOMA (chỉ tạo shell). Cần allow_write trên system đích."""
        src, err = _resolve(system)
        if err:
            return err
        tgt, err2 = (_resolve(target_system) if target_system else (src, None))
        if err2:
            return err2
        return adt.clone_package(src, tgt, source_package, target_package,
                                 suffix, dry_run, transport)

    @tool("refresh_cookies_for")
    async def refresh_cookies_for(system: str) -> str:
        """Refresh expired session cookies (headless login with stored creds). Use when calls report session expired."""
        # resolve_and_refresh drives sync Playwright; FastMCP runs sync tools
        # directly in the event loop thread, where sync_playwright() raises.
        # Offload to a worker thread like the web-admin routes do.
        return await anyio.to_thread.run_sync(resolve_and_refresh, registry, system)

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> HTMLResponse:
        path = os.path.join(_web_dir(), "index.html")
        if not os.path.exists(path):
            return PlainTextResponse("web/index.html missing", status_code=500)
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())

    @mcp.custom_route("/api/systems", methods=["GET"])
    async def api_list(request: Request) -> JSONResponse:
        systems = [
            {"name": s.name, "url": s.url, "client": s.client,
             "language": s.language, "auth": s.auth}
            for s in registry.list()
        ]
        return JSONResponse({"systems": systems})

    @mcp.custom_route("/api/systems", methods=["POST"])
    async def api_upsert(request: Request) -> JSONResponse:
        body = await request.json()
        sys = System(
            name=body["name"], url=body["url"],
            client=body.get("client", "001"),
            language=body.get("language", "EN"),
            auth=body.get("auth", "basic"),
            username=body.get("username"), password=body.get("password"),
            cookie_file=body.get("cookie_file"),
            cookie_string=body.get("cookie_string"))
        registry.upsert(sys)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/systems/{name}", methods=["DELETE"])
    async def api_delete(request: Request) -> JSONResponse:
        registry.delete(request.path_params["name"])
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/systems/{name}/test", methods=["POST"])
    async def api_test(request: Request) -> JSONResponse:
        try:
            sys = registry.get(request.path_params["name"])
        except KeyError:
            return JSONResponse({"result": "Error: unknown system"})
        result = await anyio.to_thread.run_sync(adt.test_connection, sys)
        return JSONResponse({"result": result})

    @mcp.custom_route("/api/systems/login", methods=["POST"])
    async def api_login(request: Request) -> JSONResponse:
        """Create a cookie system by logging in and capturing cookies.

        Body: {name, url, client?, language?, mode: "browser"|"headless",
               username?, password?}. mode=browser opens a visible IAS login
               (no password stored); mode=headless logs in with credentials.
        """
        body = await request.json()
        name = (body.get("name") or "").strip()
        url = (body.get("url") or "").strip()
        if not name or not url:
            return JSONResponse({"result": "Error: name and url are required"})
        mode = body.get("mode", "browser")
        cookie_file = os.path.join(_cookies_dir(), f"{name}.txt")

        if mode == "headless":
            user = (body.get("username") or "").strip()
            pw = body.get("password") or ""
            if not user or not pw:
                return JSONResponse({"result":
                    "Error: headless mode requires username and password"})
            result = await anyio.to_thread.run_sync(
                refresh_cookies, url, user, pw, cookie_file)
        elif mode == "cdp":
            cdp_url = os.environ.get("ADT_MCP_CDP", "http://127.0.0.1:9222")
            result = await anyio.to_thread.run_sync(
                cdp_capture, url, cookie_file, cdp_url)
        else:
            result = await anyio.to_thread.run_sync(
                interactive_login, url, cookie_file)

        if result.startswith("OK"):
            registry.upsert(System(
                name=name, url=url,
                client=body.get("client", "001"),
                language=body.get("language", "EN"),
                auth="cookie",
                username=(body.get("username") or "").strip() or None,
                password=(body.get("password") or "") or None,
                cookie_file=cookie_file, cookie_string=None))
        return JSONResponse({"result": result})

    @mcp.custom_route("/api/systems/{name}/refresh", methods=["POST"])
    async def api_refresh(request: Request) -> JSONResponse:
        """Re-login an existing cookie system and rewrite its cookie file.

        Uses stored username/password if present (headless), otherwise opens
        a visible browser for manual login.
        """
        try:
            sys = registry.get(request.path_params["name"])
        except KeyError:
            return JSONResponse({"result": "Error: unknown system"})
        if sys.auth != "cookie" or not sys.cookie_file:
            return JSONResponse({"result":
                "Error: refresh only applies to cookie_file systems"})
        # Saved credentials → silent headless refresh; only fall back to an
        # interactive browser login if that fails (or no creds are stored).
        if sys.username and sys.password:
            result = await anyio.to_thread.run_sync(
                refresh_cookies, sys.url, sys.username, sys.password,
                sys.cookie_file)
            if not result.startswith("OK"):
                result = await anyio.to_thread.run_sync(
                    interactive_login, sys.url, sys.cookie_file)
        else:
            result = await anyio.to_thread.run_sync(
                interactive_login, sys.url, sys.cookie_file)
        return JSONResponse({"result": result})

    return mcp
