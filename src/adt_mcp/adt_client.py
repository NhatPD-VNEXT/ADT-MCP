"""HTTP client to fetch ABAP source from SAP ADT."""
import base64
import html
import re
import fnmatch
import difflib
import xml.etree.ElementTree as ET
import httpx
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, quote
from .registry import System


def check_write(system: System, package: str,
                object_name: str = "") -> str | None:
    """Safety gate: writes require allow_write + a whitelisted target package.

    When ``system.write_objects`` is set, the object name must additionally
    match one of its patterns (fnmatch, case-insensitive) — an allow-list that
    restricts which objects agents may touch. Left unset (None) it imposes no
    object-level restriction, preserving prior behaviour.
    """
    if not system.allow_write:
        return (f"Error: writes disabled for system {system.name!r} "
                f"(set allow_write=true in systems.json)")
    pats = system.write_packages or ["Z*", "$TMP"]
    pkg = (package or "").upper()
    if not pkg:
        return "Error: could not determine target package"
    if not any(fnmatch.fnmatch(pkg, p.upper()) for p in pats):
        return f"Error: package {package!r} not in write_packages {pats}"
    if system.write_objects is not None:
        obj = (object_name or "").upper()
        if not any(fnmatch.fnmatch(obj, p.upper())
                   for p in system.write_objects):
            return (f"Error: object {object_name!r} not in write_objects "
                    f"{system.write_objects}")
    return None


def base_url(url: str) -> str:
    """Normalize a system URL to scheme://host, dropping any path/fragment
    (e.g. a Fiori launchpad URL like '.../ui#Shell-home')."""
    p = urlsplit(url if "//" in url else "https://" + url)
    return f"{p.scheme}://{p.netloc}"

OBJECT_PATHS = {
    "CLAS": "/sap/bc/adt/oo/classes/{name}/source/main",
    "INTF": "/sap/bc/adt/oo/interfaces/{name}/source/main",
    "PROG": "/sap/bc/adt/programs/programs/{name}/source/main",
    "INCL": "/sap/bc/adt/programs/includes/{name}/source/main",
    "FUGR": "/sap/bc/adt/functions/groups/{group}/fmodules/{name}/source/main",
    "DDLS": "/sap/bc/adt/ddic/ddl/sources/{name}/source/main",
    "BDEF": "/sap/bc/adt/bo/behaviordefinitions/{name}/source/main",
    "SRVD": "/sap/bc/adt/ddic/srvd/sources/{name}/source/main",
    "TABL": "/sap/bc/adt/ddic/tables/{name}/source/main",
    "VIEW": "/sap/bc/adt/ddic/views/{name}/source/main",
    "STRU": "/sap/bc/adt/ddic/structures/{name}/source/main",
    "DDLX": "/sap/bc/adt/ddic/ddlx/sources/{name}/source/main",
    "DCLS": "/sap/bc/adt/acm/dcl/sources/{name}/source/main",
}

# Accept aliases for object types
OBJECT_TYPE_ALIASES = {"STRUCT": "STRU"}


def build_rename_map(names: list[str], suffix: str) -> dict[str, str]:
    """{ORIG_NAME.upper(): ORIG_NAME.upper()+suffix.upper()} for clone rename."""
    suf = (suffix or "").upper()
    return {n.upper(): n.upper() + suf for n in names}


def rewrite_references(source: str, rename_map: dict[str, str]) -> str:
    """Replace names from rename_map with their target, matching on word
    boundaries, case-insensitive. Longest names first to avoid prefix
    collisions (e.g. ZI_FUN_MF902 before ZI_FUN)."""
    if not source or not rename_map:
        return source
    for old in sorted(rename_map, key=len, reverse=True):
        source = re.sub(rf"\b{re.escape(old)}\b", rename_map[old],
                        source, flags=re.IGNORECASE)
    return source


# Creation order by dependency (DOMA->...->SRVB). Activation is batched at the
# end so order within a group (e.g. multiple DDLS) need not be perfect.
CLONE_ORDER = ["DOMA", "DTEL", "PROG", "TABL", "DDLS", "DDLX", "DCLS",
               "INTF", "CLAS", "BDEF", "SRVD", "SRVB"]

# Types that can only be created as a shell (content not copied) -> skip in v1.
SKIP_CLONE_TYPES = {"DTEL", "DOMA"}


def clone_short_type(node_type: str) -> str:
    """nodestructure OBJECT_TYPE ('DDLS/DF') -> CREATE_TYPES key ('DDLS')."""
    return (node_type or "").split("/")[0].upper()


CLASS_INCLUDES = {"definitions", "implementations", "macros", "testclasses"}


def object_root_path(object_type: str, name: str,
                     function_group: str | None = None) -> str:
    """ADT object root path (no /source/main, no host), for lock/unlock/activate."""
    ot = OBJECT_TYPE_ALIASES.get(object_type.upper(), object_type.upper())
    if ot == "FUGR":
        if not function_group:
            raise ValueError("FUGR requires function_group")
        return (f"/sap/bc/adt/functions/groups/{function_group.upper()}"
                f"/fmodules/{name.upper()}")
    if ot == "SRVB":
        return f"/sap/bc/adt/businessservices/bindings/{name.upper()}"
    if ot not in OBJECT_PATHS:
        raise ValueError(f"invalid object_type {object_type!r}")
    return OBJECT_PATHS[ot].format(
        name=name.upper(), group=(function_group or "").upper()
    ).rsplit("/source/main", 1)[0]


def parse_lock_result(data: bytes) -> tuple[str, str]:
    """Return (lock_handle, modification_support) from a LOCK response."""
    if not data:
        return "", ""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return "", ""
    handle, mod = "", ""
    for el in root.iter():
        ln = _localname(el.tag)
        if ln == "LOCK_HANDLE":
            handle = (el.text or "").strip()
        elif ln == "MODIFICATION_SUPPORT":
            mod = (el.text or "").strip()
    return handle, mod


def parse_lock_handle(data: bytes) -> str:
    """Extract the lock handle from a LOCK response."""
    return parse_lock_result(data)[0]


def parse_adt_exception(data: bytes) -> tuple[str, str]:
    """Return (exception_type_id, message) from an ADT error payload.

    ADT reports failures as <exc:exception> with a <type id="..."/> and a
    <message>text</message>. Returns ("", "") when the body is missing or not
    such an XML document.
    """
    if not data:
        return "", ""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return "", ""
    type_id, message = "", ""
    for el in root.iter():
        ln = _localname(el.tag)
        if ln == "type" and not type_id:
            type_id = (el.attrib.get("id") or "").strip()
        elif ln == "message" and not message:
            message = (el.text or "").strip()
    return type_id, message


def parse_activation(data: bytes) -> str:
    """Return 'OK' or an error string from an activation response."""
    if not data:
        return "OK"
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return "OK"
    errors = []
    for el in root.iter():
        if _localname(el.tag) in ("msg", "message"):
            a = {_localname(k): v for k, v in el.attrib.items()}
            sev = (a.get("severity") or a.get("type") or "").upper()
            text = a.get("shortText") or (el.text or "")
            if sev and sev[0] in ("E", "A", "X"):
                errors.append(text.strip() or sev)
    return "Error: activation failed: " + "; ".join(errors) if errors else "OK"


def parse_check_run(data: bytes) -> list[dict]:
    """Parse a checkruns response into message dicts.

    Each message: {type (E/W/I/...), text, uri, line}. type is the severity
    letter ADT returns (E=error, W=warning, I=info, S=success).
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _localname(el.tag) != "checkMessage":
            continue
        a = {_localname(k): v for k, v in el.attrib.items()}
        uri = a.get("uri", "")
        line = ""
        m = re.search(r"start=(\d+)", uri)
        if m:
            line = m.group(1)
        out.append({
            "type": (a.get("type") or "").upper(),
            "text": a.get("shortText") or (el.text or "").strip(),
            "uri": uri,
            "line": line,
        })
    return out


def parse_aunit_result(data: bytes) -> list[dict]:
    """Parse an ABAP Unit runResult into per-method dicts.

    Each: {class, method, time, alerts:[{severity, kind, title, details:[..]}]}.
    A method with no alerts passed; alerts of severity critical/fatal are
    failures (warnings keep a lower severity).
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for tclass in root.iter():
        if _localname(tclass.tag) != "testClass":
            continue
        ca = {_localname(k): v for k, v in tclass.attrib.items()}
        cname = ca.get("name", "")
        for tm in tclass.iter():
            if _localname(tm.tag) != "testMethod":
                continue
            ma = {_localname(k): v for k, v in tm.attrib.items()}
            alerts = []
            for al in tm.iter():
                if _localname(al.tag) != "alert":
                    continue
                aa = {_localname(k): v for k, v in al.attrib.items()}
                title, details = "", []
                for sub in al.iter():
                    sln = _localname(sub.tag)
                    if sln == "title":
                        title = (sub.text or "").strip()
                    elif sln == "detail":
                        da = {_localname(k): v for k, v in sub.attrib.items()}
                        d = da.get("text") or (sub.text or "")
                        if d.strip():
                            details.append(d.strip())
                alerts.append({"severity": aa.get("severity", ""),
                               "kind": aa.get("kind", ""),
                               "title": title, "details": details})
            out.append({"class": cname, "method": ma.get("name", ""),
                        "time": ma.get("executionTime", ""), "alerts": alerts})
    return out


def parse_data_preview(data: bytes) -> dict:
    """Parse a datapreview tableData response (column-oriented) into rows.

    Returns {columns:[name..], rows:[[val..]..], total:int}. The payload lists
    one <columns> block per column (metadata + a dataSet of values); rows are
    rebuilt by zipping the columns position-wise.
    """
    out = {"columns": [], "rows": [], "total": 0}
    if not data:
        return out
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    col_values: list[list[str]] = []
    for el in root.iter():
        ln = _localname(el.tag)
        if ln == "totalRows":
            try:
                out["total"] = int((el.text or "0").strip())
            except ValueError:
                out["total"] = 0
        elif ln == "columns":
            name, values = "", []
            for sub in el.iter():
                sln = _localname(sub.tag)
                if sln == "metadata":
                    a = {_localname(k): v for k, v in sub.attrib.items()}
                    name = a.get("name", "")
                elif sln == "data":
                    values.append(sub.text or "")
            out["columns"].append(name)
            col_values.append(values)
    nrows = max((len(v) for v in col_values), default=0)
    out["rows"] = [[col_values[c][i] if i < len(col_values[c]) else ""
                    for c in range(len(col_values))] for i in range(nrows)]
    return out


def parse_trace_runs(data: bytes) -> list[dict]:
    """Parse the ABAP profiler results feed into run dicts.

    Each: {uri, title, date, runtime, runtime_abap, runtime_db, state}.
    Times are microseconds as reported by the trace.
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    def _int(s):
        try:
            return int((s or "").strip())
        except ValueError:
            return 0
    out = []
    for e in root.iter():
        if _localname(e.tag) != "entry":
            continue
        run = {"uri": "", "title": "", "date": "", "runtime": 0,
               "runtime_abap": 0, "runtime_db": 0, "state": ""}
        for c in e.iter():
            ln = _localname(c.tag)
            if ln == "id":
                run["uri"] = (c.text or "").strip()
            elif ln == "title" and not run["title"]:
                run["title"] = (c.text or "").strip()
            elif ln == "published":
                run["date"] = (c.text or "").strip()
            elif ln == "runtime":
                run["runtime"] = _int(c.text)
            elif ln == "runtimeABAP":
                run["runtime_abap"] = _int(c.text)
            elif ln == "runtimeDatabase":
                run["runtime_db"] = _int(c.text)
            elif ln == "state":
                a = {_localname(k): v for k, v in c.attrib.items()}
                run["state"] = a.get("text", "") or a.get("value", "")
        if run["uri"]:
            out.append(run)
    return out


def parse_trace_hitlist(data: bytes) -> list[dict]:
    """Parse a trace hitlist into entries.

    Each: {description, gross_time, gross_pct, net_time, net_pct, program}.
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    def _i(s):
        try:
            return int((s or "0").strip())
        except ValueError:
            return 0
    def _f(s):
        try:
            return float((s or "0").strip())
        except ValueError:
            return 0.0
    out = []
    for e in root.iter():
        if _localname(e.tag) != "entry":
            continue
        a = {_localname(k): v for k, v in e.attrib.items()}
        row = {"description": a.get("description", ""), "gross_time": 0,
               "gross_pct": 0.0, "net_time": 0, "net_pct": 0.0, "program": ""}
        for c in e:
            cln = _localname(c.tag)
            ca = {_localname(k): v for k, v in c.attrib.items()}
            if cln == "grossTime":
                row["gross_time"] = _i(ca.get("time"))
                row["gross_pct"] = _f(ca.get("percentage"))
            elif cln == "traceEventNetTime":
                row["net_time"] = _i(ca.get("time"))
                row["net_pct"] = _f(ca.get("percentage"))
            elif cln == "callingProgram" and not row["program"]:
                row["program"] = ca.get("name", "")
        out.append(row)
    return out


def parse_trace_dbaccesses(data: bytes) -> dict:
    """Parse a trace dbAccesses response.

    Returns {total_db_time, accesses:[{table, statement, type, total_count,
    buffered_count, db_time, ratio, program}]}.
    """
    out = {"total_db_time": 0, "accesses": []}
    if not data:
        return out
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    def _i(s):
        try:
            return int((s or "0").strip())
        except ValueError:
            return 0
    def _f(s):
        try:
            return float((s or "0").strip())
        except ValueError:
            return 0.0
    out["total_db_time"] = _i({_localname(k): v
                               for k, v in root.attrib.items()}.get("totalDbTime"))
    for e in root.iter():
        if _localname(e.tag) != "dbAccess":
            continue
        a = {_localname(k): v for k, v in e.attrib.items()}
        row = {"table": a.get("tableName", ""), "statement": a.get("statement", ""),
               "type": a.get("type", ""), "total_count": _i(a.get("totalCount")),
               "buffered_count": _i(a.get("bufferedCount")), "db_time": 0,
               "ratio": 0.0, "program": ""}
        for c in e:
            cln = _localname(c.tag)
            ca = {_localname(k): v for k, v in c.attrib.items()}
            if cln == "accessTime":
                row["db_time"] = _i(ca.get("database") or ca.get("total"))
                row["ratio"] = _f(ca.get("ratioOfTraceTotal"))
            elif cln == "callingProgram" and not row["program"]:
                row["program"] = ca.get("name", "")
        out["accesses"].append(row)
    return out


def parse_release_state(data: bytes) -> dict:
    """Parse an apireleases response into a release-state dict.

    Returns {object: {name,type}, contracts: [{contract, state,
    stateDescription, cloud, keyUser, successors:[name]}],
    anyContractReleased: bool}.
    """
    text = (data or b"").decode("utf-8", "replace").strip()
    # The endpoint sometimes returns the XML JSON-quoted / HTML-escaped.
    if text[:1] == '"' and text[-1:] == '"':
        text = text[1:-1].encode().decode("unicode_escape")
    if "&lt;" in text:
        text = html.unescape(text)
    out: dict = {"object": {}, "contracts": [], "anyContractReleased": False}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for el in root.iter():
        ln = _localname(el.tag)
        a = {_localname(k): v for k, v in el.attrib.items()}
        if ln == "releasableObject":
            out["object"] = {"name": a.get("name", ""),
                             "type": a.get("type", ""),
                             "uri": a.get("uri", "")}
        elif ln.endswith("Release") and ln != "apiRelease":
            c = {"contract": a.get("contract", "") or ln.replace("Release", ""),
                 "state": "", "stateDescription": "",
                 "cloud": a.get("useInSAPCloudPlatform", "") == "true",
                 "keyUser": a.get("useInKeyUserApps", "") == "true",
                 "successors": []}
            # Read only the contract's own <status> (a direct child). The
            # nested <stateTransitions> lists the *allowed next* states as more
            # <status> elements (e.g. RELEASED, NOT_RELEASED); descending into
            # them with el.iter() would overwrite the real state with the last
            # transition option — making a RELEASED API look NOT_RELEASED.
            for ch in el:
                cln = _localname(ch.tag)
                ca = {_localname(k): v for k, v in ch.attrib.items()}
                if cln == "status" and not c["state"]:
                    c["state"] = ca.get("state", "")
                    c["stateDescription"] = ca.get("stateDescription", "")
                elif cln in ("successor", "successors"):
                    for s in ch.iter():
                        sa = {_localname(k): v for k, v in s.attrib.items()}
                        if _localname(s.tag) == "successor" and sa.get("name"):
                            c["successors"].append(sa["name"])
            out["contracts"].append(c)
        elif ln == "apiCatalogData":
            out["anyContractReleased"] = \
                a.get("isAnyContractReleased", "") == "true"
    return out


def parse_transports(data: bytes) -> list[dict]:
    """Parse a CTS transportorganizertree response into request dicts.

    Each: {number, description, type, status, target, owner}. type 'K' is a
    workbench request, 'W'/'C' customizing; status 'D' is modifiable, 'R'
    released. owner falls back from the request's adtcore:createdBy to a nested
    task's tm:owner. An empty <tm:root/> (user has no open requests) yields [].
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _localname(el.tag) != "request":
            continue
        a = {_localname(k): v for k, v in el.attrib.items()}
        owner = a.get("createdBy", "") or a.get("owner", "")
        if not owner:
            for task in el:
                if _localname(task.tag) == "task":
                    ta = {_localname(k): v for k, v in task.attrib.items()}
                    owner = ta.get("owner", "") or ta.get("createdBy", "")
                    if owner:
                        break
        out.append({
            "number": a.get("number", ""),
            "description": a.get("desc", ""),
            "type": a.get("type", ""),
            "status": a.get("status", ""),
            "target": a.get("target", ""),
            "owner": owner,
        })
    return out


def build_transport_body(description: str, req_type: str = "K",
                         target: str = "", owner: str = "") -> str:
    """POST body to create a CTS request (tm:useraction='newrequest')."""
    task = f'<tm:task tm:owner="{owner}"/>' if owner else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<tm:root xmlns:tm="http://www.sap.com/cts/adt/tm" '
            f'tm:useraction="newrequest">\n'
            f'  <tm:request tm:desc="{description}" tm:type="{req_type}" '
            f'tm:target="{target}" tm:cts_project="">\n'
            f'    {task}\n  </tm:request>\n</tm:root>')


def parse_netscape_cookies(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def is_login_page(resp: httpx.Response) -> bool:
    """A 200 response that is actually a SAML/SSO login page, not ADT content.

    SAP cloud (S/4HANA, BTP) answers an expired session with HTTP 200 carrying
    an HTML SAML auto-submit form instead of the requested ADT payload. ADT
    source/discovery responses are text/plain or */xml, never text/html.
    """
    ctype = resp.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        return True
    body = resp.text
    return "SAMLRequest" in body or "saml2/idp" in body.lower()


def _localname(tag: str) -> str:
    """Strip XML namespace from a tag, e.g. '{ns}name' -> 'name'."""
    return tag.rsplit("}", 1)[-1]


def parse_nodestructure(data: bytes) -> list[dict]:
    """Parse a repository/nodestructure response into object dicts."""
    if not data:
        return []
    root = ET.fromstring(data)
    out = []
    for node in root.iter():
        if _localname(node.tag) != "SEU_ADT_REPOSITORY_OBJ_NODE":
            continue
        fields = {_localname(c.tag): (c.text or "").strip() for c in node}
        name = fields.get("OBJECT_NAME", "")
        if not name:
            continue
        out.append({
            "type": fields.get("OBJECT_TYPE", ""),
            "name": name,
            "uri": fields.get("OBJECT_URI", ""),
            "description": fields.get("DESCRIPTION", ""),
        })
    return out


def parse_search(data: bytes) -> list[dict]:
    """Parse an informationsystem/search response into object dicts."""
    if not data:
        return []
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        attrs = {_localname(k): v for k, v in el.attrib.items()}
        name = attrs.get("name")
        if not name or "type" not in attrs:
            continue
        out.append({
            "name": name,
            "type": attrs.get("type", ""),
            "uri": attrs.get("uri", ""),
            "package": attrs.get("packageName", ""),
            "description": attrs.get("description", ""),
        })
    return out


def extract_method(source: str, method: str) -> str | None:
    """Return the METHOD <method> … ENDMETHOD block from class source."""
    lines = source.splitlines()
    start = re.compile(rf"^\s*METHOD\s+{re.escape(method)}\s*\.", re.IGNORECASE)
    end = re.compile(r"^\s*ENDMETHOD\s*\.", re.IGNORECASE)
    for i, line in enumerate(lines):
        if start.match(line):
            for j in range(i, len(lines)):
                if end.match(lines[j]):
                    return "\n".join(lines[i:j + 1])
            return "\n".join(lines[i:])
    return None


def parse_revision_feed(data: bytes) -> list[dict]:
    """Parse an ADT revisions atom feed into version dicts."""
    if not data:
        return []
    root = ET.fromstring(data)
    out = []
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        rev = {"version": "", "title": "", "date": "", "author": "",
               "transport": "", "uri": ""}
        for ch in entry:
            ln = _localname(ch.tag)
            if ln == "id":
                rev["version"] = (ch.text or "").strip()
            elif ln == "title":
                rev["title"] = (ch.text or "").strip()
            elif ln == "updated":
                rev["date"] = (ch.text or "").strip()
            elif ln == "author":
                for a in ch:
                    if _localname(a.tag) == "name":
                        rev["author"] = (a.text or "").strip()
            elif ln == "content":
                attrs = {_localname(k): v for k, v in ch.attrib.items()}
                rev["uri"] = attrs.get("src", "")
            elif ln == "link":
                attrs = {_localname(k): v for k, v in ch.attrib.items()}
                if "transportrequests" in attrs.get("type", ""):
                    rev["transport"] = attrs.get("name", "")
        out.append(rev)
    return out


# Friendly key -> feed title as it appears in the /sap/bc/adt/feeds catalog.
# Covers the feeds SAP's "Read ABAP Feeds" MCP tool exposes (gateway error log,
# system messages, dumps) plus the other monitoring feeds the catalog offers.
FEED_ALIASES = {
    "gateway_log": "SAP Gateway Error Log",
    "system_messages": "ABAP System Messages",
    "dumps": "ABAP Runtime Errors",
    "event_log": "Enterprise Event Enablement Error Log",
    "uri_errors": "URI Creation Errors",
    "atc": "ATC Findings",
    "contract_violations": "ABAP Contract Check Violations",
}


def parse_feed_catalog(data: bytes) -> list[dict]:
    """Parse the /sap/bc/adt/feeds catalog into {title, href, summary} dicts.

    Each atom:entry describes one feed; its href (atom:link/@href, falling back
    to atom:content/@src or atom:id) is the URL to GET that feed's entries.
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        title = summary = href = src = idv = ""
        for ch in entry:
            ln = _localname(ch.tag)
            a = {_localname(k): v for k, v in ch.attrib.items()}
            if ln == "title" and not title:
                title = (ch.text or "").strip()
            elif ln == "summary":
                summary = (ch.text or "").strip()
            elif ln == "link" and not href:
                href = a.get("href", "")
            elif ln == "content" and not src:
                src = a.get("src", "")
            elif ln == "id" and not idv:
                idv = (ch.text or "").strip()
        link = href or src or idv
        if title and link:
            out.append({"title": title, "href": link, "summary": summary})
    return out


def parse_feed_entries(data: bytes) -> list[dict]:
    """Parse a single ADT feed (gateway log, system messages, …) into entries.

    Each: {title, uri, author, date, summary}. Mirrors parse_dumps_feed — the
    entry body is carried in atom:summary; the id is a logical key.
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        d = {"title": "", "uri": "", "author": "", "date": "", "summary": ""}
        for ch in entry:
            ln = _localname(ch.tag)
            if ln == "title" and not d["title"]:
                d["title"] = (ch.text or "").strip()
            elif ln == "id" and not d["uri"]:
                d["uri"] = (ch.text or "").strip()
            elif ln in ("updated", "published") and not d["date"]:
                d["date"] = (ch.text or "").strip()
            elif ln == "author":
                for a in ch:
                    if _localname(a.tag) == "name":
                        d["author"] = (a.text or "").strip()
            elif ln == "summary":
                d["summary"] = (ch.text or "").strip()
        if d["title"] or d["uri"]:
            out.append(d)
    return out


def parse_dumps_feed(data: bytes) -> list[dict]:
    """Parse the ABAP runtime dumps (ST22) atom feed into dump dicts.

    Each: {uri, title, author, date, categories, summary} where categories is a
    list of (term, label) pairs (runtime error, terminated program, …) and
    `summary` is the full dump HTML embedded in the feed entry. `uri` is the
    atom:id — a logical key (the atom:link hrefs are adt:// SAP GUI links, not
    HTTP-fetchable), used to match the entry again in get_dump.
    """
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        d = {"uri": "", "title": "", "author": "", "date": "",
             "categories": [], "summary": ""}
        for ch in entry:
            ln = _localname(ch.tag)
            if ln == "id":
                d["uri"] = (ch.text or "").strip()
            elif ln == "title" and not d["title"]:
                d["title"] = (ch.text or "").strip()
            elif ln in ("updated", "published") and not d["date"]:
                d["date"] = (ch.text or "").strip()
            elif ln == "author":
                for a in ch:
                    if _localname(a.tag) == "name":
                        d["author"] = (a.text or "").strip()
            elif ln == "category":
                attrs = {_localname(k): v for k, v in ch.attrib.items()}
                term = attrs.get("term", "")
                if term:
                    d["categories"].append((term, attrs.get("label", "")))
            elif ln == "summary":
                d["summary"] = ch.text or ""
        if d["uri"]:
            out.append(d)
    return out


def html_to_text(html: str) -> str:
    """Flatten an ADT dump HTML page to readable plain text for analysis."""
    if not html:
        return ""
    import html as _html
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(td|th)\s*>", "\t", text)   # keep table cells apart
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table)\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _html.unescape(text).replace("\xa0", " ")
    lines, blank = [], False
    for ln in text.splitlines():
        ln = ln.rstrip()
        if not ln.strip():
            if not blank:
                lines.append("")
            blank = True
        else:
            lines.append(ln)
            blank = False
    return "\n".join(lines).strip()


REVISION_PATHS = {
    "PROG": "/sap/bc/adt/programs/programs/{name}/source/main/versions",
    "INTF": "/sap/bc/adt/oo/interfaces/{name}/includes/main/versions",
    "INCL": "/sap/bc/adt/programs/includes/{name}/source/main/versions",
    "DDLS": "/sap/bc/adt/ddic/ddl/sources/{name}/source/main/versions",
    "BDEF": "/sap/bc/adt/bo/behaviordefinitions/{name}/source/main/versions",
    "SRVD": "/sap/bc/adt/ddic/srvd/sources/{name}/source/main/versions",
}


def revision_url(object_type: str, name: str,
                 function_group: str | None = None,
                 include: str | None = None) -> str:
    ot = OBJECT_TYPE_ALIASES.get(object_type.upper(), object_type.upper())
    n = name.upper()
    if ot == "CLAS":
        return (f"/sap/bc/adt/oo/classes/{n}/includes/"
                f"{(include or 'main').lower()}/versions")
    if ot in ("FUGR", "FUNC"):
        if not function_group:
            raise ValueError("function_group required for function revisions")
        return (f"/sap/bc/adt/functions/groups/{function_group.upper()}"
                f"/fmodules/{n}/source/main/versions")
    if ot in REVISION_PATHS:
        return REVISION_PATHS[ot].format(name=n)
    raise ValueError(
        f"unsupported object_type for revisions {object_type!r}; valid: "
        f"PROG, CLAS, INTF, FUNC, INCL, DDLS, BDEF, SRVD")


def parse_cds_dependencies(source: str) -> list[dict]:
    """Extract upstream dependencies from CDS/DDL source text.

    Returns dicts {relation, name} for FROM / JOIN / ASSOCIATION / COMPOSITION
    targets. Pure text parse — works wherever the source can be read.
    """
    out, seen = [], set()
    patterns = [
        ("FROM", r"\bfrom\s+([A-Za-z_/][\w/]*)"),
        ("PROJECTION", r"\bprojection\s+on\s+([A-Za-z_/][\w/]*)"),
        ("JOIN", r"\bjoin\s+([A-Za-z_/][\w/]*)"),
        ("ASSOCIATION", r"\bassociation(?:\s*\[[^\]]*\])?\s+to\s+(?:parent\s+)?([A-Za-z_/][\w/]*)"),
        ("COMPOSITION", r"\bcomposition(?:\s*\[[^\]]*\])?\s+of\s+([A-Za-z_/][\w/]*)"),
    ]
    for relation, pat in patterns:
        for m in re.finditer(pat, source, re.IGNORECASE):
            name = m.group(1)
            if name.lower() in ("select", "as"):
                continue
            key = (relation, name.upper())
            if key not in seen:
                seen.add(key)
                out.append({"relation": relation, "name": name})
    return out


def parse_bdef_dependencies(source: str) -> dict:
    """Extract the artifacts a RAP behavior definition leans on.

    Returns {"entities": [...], "classes": [...]}: the CDS entities behind
    `define behavior for <Entity>` and the behavior-pool classes named in
    `... implementation in class <Class>`. Pure text parse.
    """
    entities, ent_seen = [], set()
    for m in re.finditer(r"\bdefine\s+behavior\s+for\s+([A-Za-z_/]\w*)",
                         source, re.IGNORECASE):
        n = m.group(1)
        if n.upper() not in ent_seen:
            ent_seen.add(n.upper())
            entities.append(n)
    classes, cls_seen = [], set()
    for m in re.finditer(r"\bimplementation\s+in\s+class\s+([A-Za-z_/]\w*)",
                         source, re.IGNORECASE):
        n = m.group(1)
        if n.upper() not in cls_seen:
            cls_seen.add(n.upper())
            classes.append(n)
    return {"entities": entities, "classes": classes}


def parse_class_dependencies(source: str) -> dict:
    """Extract a class's superclass and implemented interfaces from source.

    Returns {"superclass": name|None, "interfaces": [...]}. Pure text parse.
    """
    superclass = None
    m = re.search(r"\binheriting\s+from\s+([A-Za-z_/]\w*)", source,
                  re.IGNORECASE)
    if m:
        superclass = m.group(1)
    interfaces, seen = [], set()
    for m in re.finditer(r"^\s*interfaces\s+([A-Za-z_/]\w*)", source,
                         re.IGNORECASE | re.MULTILINE):
        n = m.group(1)
        if n.upper() not in seen:
            seen.add(n.upper())
            interfaces.append(n)
    return {"superclass": superclass, "interfaces": interfaces}


def compress_source(object_type: str, source: str) -> str:
    """Strip a dependency's source down to the parts that matter for context.

    DDLS/CDS: drop @annotations and // comments (keep define/select/fields).
    CLAS: keep the DEFINITION part (signatures), drop method implementations.
    Other types: returned as-is (already concise).
    """
    ot = object_type.upper()
    if ot == "DDLS":
        keep = []
        for line in source.splitlines():
            s = line.strip()
            if not s or s.startswith("@") or s.startswith("//"):
                continue
            keep.append(line.rstrip())
        return "\n".join(keep)
    if ot == "CLAS":
        m = re.search(r"^\s*class\s+\S+\s+implementation", source,
                      re.IGNORECASE | re.MULTILINE)
        if m:
            return source[:m.start()].rstrip()
        return source
    return source


def parse_usage_references(data: bytes) -> list[dict]:
    """Parse a usageReferences response into where-used dicts."""
    if not data:
        return []
    root = ET.fromstring(data)
    out = []
    for ref in root.iter():
        if _localname(ref.tag) != "referencedObject":
            continue
        rattrs = {_localname(k): v for k, v in ref.attrib.items()}
        info = {"name": "", "type": "", "uri": rattrs.get("uri", ""),
                "package": "", "description": "",
                "usage": rattrs.get("usageInformation", "")}
        for child in ref:
            if _localname(child.tag) == "adtObject":
                a = {_localname(k): v for k, v in child.attrib.items()}
                info["name"] = a.get("name", "")
                info["type"] = a.get("type", "")
                info["description"] = a.get("description", "")
                for gc in child:
                    if _localname(gc.tag) == "packageRef":
                        pa = {_localname(k): v for k, v in gc.attrib.items()}
                        info["package"] = pa.get("name", "")
        if info["name"] or info["uri"]:
            out.append(info)
    return out


def list_method_decls(source: str) -> list[str]:
    """Return declared method names (METHODS / CLASS-METHODS) in order."""
    pat = re.compile(r"^\s*(?:CLASS-METHODS|METHODS)\s+([A-Za-z_]\w*)",
                     re.IGNORECASE)
    seen, out = set(), []
    for line in source.splitlines():
        m = pat.match(line)
        if m:
            n = m.group(1).upper()
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


# Create templates: type -> (creation_path, root_elem, ns_decl, adt_type,
#                            content_type, source_capable)
CREATE_TYPES = {
    "PROG": ("/sap/bc/adt/programs/programs", "program:abapProgram",
             'xmlns:program="http://www.sap.com/adt/programs/programs"',
             "PROG/P", "application/*", True),
    "CLAS": ("/sap/bc/adt/oo/classes", "class:abapClass",
             'xmlns:class="http://www.sap.com/adt/oo/classes"',
             "CLAS/OC", "application/*", True),
    "INTF": ("/sap/bc/adt/oo/interfaces", "intf:abapInterface",
             'xmlns:intf="http://www.sap.com/adt/oo/interfaces"',
             "INTF/OI", "application/*", True),
    "DDLS": ("/sap/bc/adt/ddic/ddl/sources", "ddl:ddlSource",
             'xmlns:ddl="http://www.sap.com/adt/ddic/ddlsources"',
             "DDLS/DF", "application/*", True),
    "DDLX": ("/sap/bc/adt/ddic/ddlx/sources", "ddlx:ddlxSource",
             'xmlns:ddlx="http://www.sap.com/adt/ddic/ddlxsources"',
             "DDLX/EX", "application/*", True),
    "BDEF": ("/sap/bc/adt/bo/behaviordefinitions", "blue:blueSource",
             'xmlns:blue="http://www.sap.com/wbobj/blue"',
             "BDEF/BDO", "application/vnd.sap.adt.blues.v1+xml", True),
    "SRVD": ("/sap/bc/adt/ddic/srvd/sources", "srvd:srvdSource",
             'xmlns:srvd="http://www.sap.com/adt/ddic/srvdsources"',
             "SRVD/SRV", "application/*", True),
    "SRVB": ("/sap/bc/adt/businessservices/bindings", "srvb:serviceBinding",
             'xmlns:srvb="http://www.sap.com/adt/ddic/ServiceBindings"',
             "SRVB/SVB", "application/*", False),
    "TABL": ("/sap/bc/adt/ddic/tables", "blue:blueSource",
             'xmlns:blue="http://www.sap.com/wbobj/blue"',
             "TABL/DT", "application/vnd.sap.adt.tables.v2+xml", True),
    # DCLS = access control (CDS DCL source). DTEL/DOMA are property objects,
    # not source-based — create the shell only (source_capable=False).
    "DCLS": ("/sap/bc/adt/acm/dcl/sources", "dcl:dclSource",
             'xmlns:dcl="http://www.sap.com/adt/acm/dclsources"',
             "DCLS/DL", "application/*", True),
    "DTEL": ("/sap/bc/adt/ddic/dataelements", "blue:wbobj",
             'xmlns:blue="http://www.sap.com/wbobj/dictionary/dtel"',
             "DTEL/DE", "application/*", False),
    "DOMA": ("/sap/bc/adt/ddic/domains", "domain:domain",
             'xmlns:domain="http://www.sap.com/dictionary/domain"',
             "DOMA/DD", "application/*", False),
}


def build_creation_body(object_type: str, name: str, package: str,
                        description: str, responsible: str,
                        service_definition: str | None = None,
                        binding_version: str = "V2",
                        language: str = "EN",
                        abap_language_version: str = "cloudDevelopment") -> str:
    ot = object_type.upper()
    path, root, ns, adt_type, _ct, _sc = CREATE_TYPES[ot]
    name = name.upper()
    package = package.upper()
    lang = (language or "EN").upper()
    alv = abap_language_version or "cloudDevelopment"
    # Three attributes are mandatory on ABAP Cloud (S/4HANA Cloud Public, BTP):
    #  - adtcore:language + adtcore:masterLanguage: without them the create POST
    #    fails "deserializing in the simple transformation program <X>" (400).
    #  - adtcore:abapLanguageVersion: without it the server defaults to the
    #    classic version and rejects the write with HTTP 403 / authorization
    #    object S_ABPLNGVS ("not authorized to make changes"). On ABAP Cloud it
    #    must be "cloudDevelopment". Verified against my422346 (DDLS/DDLX/SRVD/
    #    TABL create return 201 only with all three present).
    # adtcore:responsible maps to the ABAP user field XUBNAME (CHAR12). Emit it
    # only when it's a plausible SAP user name (<=12 chars, no '@'); an IAS email
    # login (e.g. "nhatpd@vnext.vn") or any over-long value makes SADT_BLUE_SOURCE
    # fail to deserialize (400, XML offset at the responsible attr). When omitted
    # the server fills it from the session user — verified to return 201.
    resp = (responsible or "").strip()
    resp_attr = (f' adtcore:responsible="{resp}"'
                 if resp and "@" not in resp and len(resp) <= 12 else "")
    head = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<{root} {ns} xmlns:adtcore="http://www.sap.com/adt/core" '
            f'adtcore:description="{description}" adtcore:name="{name}" '
            f'adtcore:type="{adt_type}" adtcore:language="{lang}" '
            f'adtcore:masterLanguage="{lang}" '
            f'adtcore:abapLanguageVersion="{alv}"'
            f'{resp_attr}')
    if ot == "SRVD":
        head += ' srvd:srvdSourceType="S"'
    if ot == "SRVB":
        return (head + '>\n'
                f'  <adtcore:packageRef adtcore:name="{package}"/>\n'
                f'  <srvb:services srvb:name="{name}">\n'
                f'    <srvb:content srvb:version="0001">\n'
                f'      <srvb:serviceDefinition adtcore:name='
                f'"{(service_definition or "").upper()}"/>\n'
                f'    </srvb:content>\n  </srvb:services>\n'
                f'  <srvb:binding srvb:category="0" srvb:type="ODATA" '
                f'srvb:version="{binding_version}">\n'
                f'    <srvb:implementation adtcore:name=""/>\n'
                f'  </srvb:binding>\n</{root}>')
    return (head + '>\n'
            f'  <adtcore:packageRef adtcore:name="{package}"/>\n</{root}>')


class ADTClient:
    def __init__(self, client: httpx.Client):
        self._client = client
        # CSRF tokens are session-scoped; cache per host so writes don't pay an
        # extra discovery round-trip on every POST. Invalidated on a 403 that
        # asks for a fresh token (x-csrf-token: Required).
        self._csrf_cache: dict[str, str] = {}

    def source_url(self, system: System, object_type: str, name: str,
                   function_group: str | None) -> str:
        ot = object_type.upper()
        ot = OBJECT_TYPE_ALIASES.get(ot, ot)
        if ot not in OBJECT_PATHS:
            raise ValueError(
                f"invalid object_type {object_type!r}; "
                f"valid: {', '.join(OBJECT_PATHS)}")
        if ot == "FUGR" and not function_group:
            raise ValueError("FUGR requires function_group")
        path = OBJECT_PATHS[ot].format(
            name=name.upper(),
            group=(function_group or "").upper())
        return (f"{base_url(system.url)}{path}"
                f"?sap-client={system.client}"
                f"&sap-language={system.language}")

    def _cookies_dict(self, system: System) -> dict | None:
        """Session cookies as a dict (for stateful write sequences)."""
        if system.auth != "cookie":
            return None
        if system.cookie_string:
            out = {}
            for part in system.cookie_string.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
        if system.cookie_file:
            with open(system.cookie_file, encoding="utf-8") as f:
                return parse_netscape_cookies(f.read())
        return None

    def _auth_kwargs(self, system: System) -> dict:
        if system.auth == "cookie":
            if system.cookie_string:
                return {"headers": {"Cookie": system.cookie_string}}
            if system.cookie_file:
                with open(system.cookie_file, encoding="utf-8") as f:
                    cookies = parse_netscape_cookies(f.read())
                return {"headers": {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}}
            return {}
        return {"auth": httpx.BasicAuth(system.username or "",
                                        system.password or "")}

    def _get(self, system: System, url: str, accept: str):
        """Low-level GET with auth + accept. Returns httpx.Response or raises."""
        kwargs = self._auth_kwargs(system)
        headers = kwargs.pop("headers", {})
        headers["Accept"] = accept
        return self._client.get(url, headers=headers, **kwargs)

    def _csrf_token(self, system: System, force: bool = False) -> str:
        """Return a CSRF token for the session, cached per host.

        force=True bypasses the cache (e.g. after the server rejected a token).
        """
        key = base_url(system.url)
        if not force and key in self._csrf_cache:
            return self._csrf_cache[key]
        url = f"{key}/sap/bc/adt/discovery"
        kwargs = self._auth_kwargs(system)
        headers = kwargs.pop("headers", {})
        headers["X-CSRF-Token"] = "fetch"
        headers["Accept"] = "application/atomsvc+xml"
        try:
            resp = self._client.get(url, headers=headers, **kwargs)
        except httpx.HTTPError:
            return ""
        token = resp.headers.get("x-csrf-token", "")
        if token:
            self._csrf_cache[key] = token
        return token

    @staticmethod
    def _csrf_rejected(resp) -> bool:
        """True if the server rejected the CSRF token and wants a fresh one."""
        return (resp.status_code == 403
                and resp.headers.get("x-csrf-token", "").lower() == "required")

    def _post_once(self, system: System, url: str, accept: str, token: str,
                   body: bytes | None, content_type: str | None):
        kwargs = self._auth_kwargs(system)
        headers = kwargs.pop("headers", {})
        headers["Accept"] = accept
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["X-CSRF-Token"] = token
        if body is not None:
            kwargs["content"] = body
        return self._client.post(url, headers=headers, **kwargs)

    def _post(self, system: System, url: str, accept: str,
              body: bytes | None = None, content_type: str | None = None):
        """Low-level POST with auth + accept + cached CSRF token.

        On a 403 that asks for a fresh token, the cache is invalidated and the
        POST is retried once with a newly fetched token.
        """
        resp = self._post_once(system, url, accept, self._csrf_token(system),
                               body, content_type)
        if self._csrf_rejected(resp):
            resp = self._post_once(system, url, accept,
                                   self._csrf_token(system, force=True),
                                   body, content_type)
        return resp

    def _fetch_source(self, system: System, url: str, label: str) -> str:
        """GET a source URL; return text or a human-readable error string."""
        try:
            resp = self._get(system, url, "text/plain")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code == 200:
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"(got SAML login page) — refresh cookies and retry")
            return resp.text
        if resp.status_code in (401, 403):
            return (f"Error: auth failed (HTTP {resp.status_code}) — "
                    f"cookie expired or wrong credentials for "
                    f"system {system.name!r}")
        if resp.status_code == 404:
            return f"Error: object not found ({label})"
        return f"Error: HTTP {resp.status_code}: {resp.text[:300]}"

    def get_source(self, system: System, object_type: str, name: str,
                   function_group: str | None = None) -> str:
        try:
            url = self.source_url(system, object_type, name, function_group)
        except ValueError as e:
            return f"Error: {e}"
        return self._fetch_source(system, url, f"{object_type} {name}")

    def test_connection(self, system: System) -> str:
        url = f"{base_url(system.url)}/sap/bc/adt/discovery"
        kwargs = self._auth_kwargs(system)
        headers = kwargs.pop("headers", {})
        try:
            resp = self._client.get(url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code == 200:
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"(got SAML login page) — refresh cookies and retry")
            return "OK"
        if resp.status_code in (401, 403):
            return f"Error: auth failed (HTTP {resp.status_code})"
        return f"Error: HTTP {resp.status_code}"

    def check_connections(self, systems: list[System]) -> list[dict]:
        """Probe every system the way the web 'Test' action does.

        Runs the same /sap/bc/adt/discovery check as test_connection per
        system (concurrently) and returns one dict each:
        {name, url, client, auth, user, status, connected}. `user` is the
        user the connection authenticates as — the configured username for
        basic auth, or the stored login for cookie auth when available.
        """
        if not systems:
            return []

        def probe(s: System) -> dict:
            status = self.test_connection(s)
            return {
                "name": s.name,
                "url": base_url(s.url),
                "client": s.client,
                "auth": s.auth,
                "user": s.username
                        or ("(cookie session)" if s.auth == "cookie" else ""),
                "status": status,
                "connected": status == "OK",
            }

        workers = min(8, len(systems))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(probe, systems))

    # --- Navigation / discovery (v2 Phase 1) ---

    def get_source_by_uri(self, system: System, uri: str) -> str:
        """Fetch source for any object given its ADT URI (from list/search)."""
        if not uri:
            return "Error: empty uri"
        path = uri if "/source/main" in uri else uri.rstrip("/") + "/source/main"
        url = (f"{base_url(system.url)}{path}"
               f"?sap-client={system.client}&sap-language={system.language}")
        return self._fetch_source(system, url, uri)

    def get_class_include(self, system: System, class_name: str,
                          include: str) -> str:
        inc = include.lower()
        if inc not in CLASS_INCLUDES:
            return (f"Error: invalid include {include!r}; "
                    f"valid: {', '.join(sorted(CLASS_INCLUDES))}")
        url = (f"{base_url(system.url)}/sap/bc/adt/oo/classes/"
               f"{class_name.upper()}/includes/{inc}"
               f"?sap-client={system.client}&sap-language={system.language}")
        return self._fetch_source(system, url, f"{class_name} {inc}")

    def get_class_method_source(self, system: System, class_name: str,
                                method: str) -> str:
        source = self.get_source(system, "CLAS", class_name)
        if source.startswith("Error:"):
            return source
        block = extract_method(source, method.upper())
        if block is None:
            return f"Error: method {method} not found in class {class_name}"
        return block

    def object_structure(self, system: System, class_name: str) -> list[str] | str:
        source = self.get_source(system, "CLAS", class_name)
        if source.startswith("Error:"):
            return source
        return list_method_decls(source)

    def list_package(self, system: System, package: str,
                     recursive: bool = False) -> list[dict] | str:
        result: list[dict] = []
        visited: set[str] = set()

        def fetch(pkg: str) -> str | None:
            url = (f"{base_url(system.url)}/sap/bc/adt/repository/nodestructure"
                   f"?parent_type=DEVC/K&parent_name={pkg.upper()}"
                   f"&withShortDescriptions=true")
            try:
                resp = self._post(system, url, "*/*")
            except httpx.HTTPError as e:
                return f"Error: request failed: {e}"
            if resp.status_code != 200:
                return f"Error: HTTP {resp.status_code} listing package {pkg}"
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"— refresh cookies and retry")
            for obj in parse_nodestructure(resp.content):
                obj["package"] = pkg.upper()
                result.append(obj)
                if recursive and obj["type"] == "DEVC/K" \
                        and obj["name"] not in visited:
                    visited.add(obj["name"])
                    err = fetch(obj["name"])
                    if err:
                        return err
            return None

        err = fetch(package)
        if err:
            return err
        return result

    def search_objects(self, system: System, query: str,
                       max_results: int = 20) -> list[dict] | str:
        url = (f"{base_url(system.url)}/sap/bc/adt/repository/"
               f"informationsystem/search?operation=quickSearch"
               f"&query={query}&maxResults={max_results}")
        try:
            resp = self._get(system, url, "application/xml")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code}"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        return parse_search(resp.content)

    def _fetch_sources(self, system: System,
                       objs: list[dict]) -> list[tuple[dict, str]]:
        """Fetch each object's source concurrently, preserving input order.

        Returns (obj, source) pairs; sources that errored keep their error
        string so callers can decide to skip them. Concurrency cuts the
        wall-clock cost of scanning large packages (many small GETs).
        """
        if not objs:
            return []
        workers = min(8, len(objs))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            srcs = ex.map(
                lambda o: self.get_source_by_uri(system, o["uri"]), objs)
            return list(zip(objs, srcs))

    def get_package_source(self, system: System, package: str,
                           max_objects: int = 50) -> str:
        objs = self.list_package(system, package)
        if isinstance(objs, str):
            return objs
        sources = [o for o in objs if o["type"] != "DEVC/K" and o["uri"]]
        if not sources:
            return f"No source objects in package {package}"
        truncated = len(sources) > max_objects
        chunks = []
        for o, src in self._fetch_sources(system, sources[:max_objects]):
            if src.startswith("Error:"):
                continue
            chunks.append(f"* ==== {o['type']} {o['name']} ====\n{src}")
        if not chunks:
            return f"No readable source in package {package}"
        out = "\n\n".join(chunks)
        if truncated:
            out += (f"\n\n* ==== truncated at {max_objects} objects "
                    f"({len(sources)} total) ====")
        return out

    def grep_package(self, system: System, package: str, pattern: str,
                     ignore_case: bool = False, max_objects: int = 100) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return f"Error: invalid pattern: {e}"
        objs = self.list_package(system, package)
        if isinstance(objs, str):
            return objs
        sources = [o for o in objs if o["type"] != "DEVC/K" and o["uri"]]
        matches = []
        for o, src in self._fetch_sources(system, sources[:max_objects]):
            if src.startswith("Error:"):
                continue
            for n, line in enumerate(src.splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{o['name']}:{n}: {line.strip()}")
        if not matches:
            return f"No matches for {pattern!r} in package {package}"
        return "\n".join(matches)

    # --- History / Diff (v2 Phase 2) ---

    def get_revisions(self, system: System, object_type: str, name: str,
                      function_group: str | None = None,
                      include: str | None = None) -> list[dict] | str:
        try:
            path = revision_url(object_type, name, function_group, include)
        except ValueError as e:
            return f"Error: {e}"
        url = (f"{base_url(system.url)}{path}"
               f"?sap-client={system.client}&sap-language={system.language}")
        try:
            resp = self._get(system, url, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code == 200:
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"— refresh cookies and retry")
            return parse_revision_feed(resp.content)
        if resp.status_code == 404:
            return f"Error: object not found ({object_type} {name})"
        return f"Error: HTTP {resp.status_code}"

    def get_revision_source(self, system: System, version_uri: str) -> str:
        if not version_uri:
            return "Error: empty version_uri"
        url = f"{base_url(system.url)}{version_uri}"
        return self._fetch_source(system, url, version_uri)

    def compare_source(self, system: System, object_type: str, name: str,
                       version_uri: str, against: str = "current",
                       function_group: str | None = None) -> str:
        src1 = self.get_revision_source(system, version_uri)
        if src1.startswith("Error:"):
            return src1
        if against == "current":
            src2 = self.get_source(system, object_type, name, function_group)
            label2 = f"{name}@current"
        else:
            src2 = self.get_revision_source(system, against)
            label2 = f"{name}@{against.rsplit('/', 2)[0].rsplit('/', 1)[-1]}"
        if src2.startswith("Error:"):
            return src2
        if src1 == src2:
            return "Sources are identical"
        diff = difflib.unified_diff(
            src1.splitlines(), src2.splitlines(),
            fromfile=f"{name}@revision", tofile=label2, lineterm="")
        return "\n".join(diff)

    # --- Code intelligence (v2 Phase 3) ---

    def find_references(self, system: System, object_uri: str,
                        line: int = 0, column: int = 0) -> list[dict] | str:
        """Where-used list for an object (or a symbol at line/column)."""
        if not object_uri:
            return "Error: object_uri is required"
        uri = object_uri
        if line > 0 and column > 0:
            uri = f"{object_uri}#start={line},{column}"
        url = (f"{base_url(system.url)}/sap/bc/adt/repository/"
               f"informationsystem/usageReferences?uri={quote(uri, safe='')}")
        body = (b'<?xml version="1.0" encoding="ASCII"?>'
                b'<usagereferences:usageReferenceRequest '
                b'xmlns:usagereferences="http://www.sap.com/adt/ris/usageReferences">'
                b'<usagereferences:affectedObjects/>'
                b'</usagereferences:usageReferenceRequest>')
        try:
            resp = self._post(system, url, "application/*", body, "application/*")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code == 200:
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"— refresh cookies and retry")
            return parse_usage_references(resp.content)
        if resp.status_code == 404:
            return f"Error: object not found ({object_uri})"
        return f"Error: HTTP {resp.status_code}: {resp.text[:200]}"

    # --- CDS analysis (v2 Phase 4) ---

    def cds_dependencies(self, system: System, ddls_name: str) -> list[dict] | str:
        """Upstream dependencies of a CDS view, parsed from its DDL source."""
        source = self.get_source(system, "DDLS", ddls_name)
        if source.startswith("Error:"):
            return source
        return parse_cds_dependencies(source)

    # --- Syntax check (ABAP check run) ---

    def syntax_check(self, system: System, object_type: str, name: str,
                     function_group: str | None = None,
                     version: str = "active",
                     source: str | None = None) -> str:
        """Run the ABAP syntax/check-run for an object; return findings text.

        The source to check is embedded as a base64 artifact (the check-run
        reporter checks the supplied content, not the stored version — without
        an artifact the server returns no messages, a false OK). If `source`
        is omitted, the current active source is fetched and checked.

        version: kept for the checkObject element ('active'/'inactive').
        Returns 'OK: no syntax errors' when clean.
        """
        try:
            root_path = object_root_path(object_type, name, function_group)
        except ValueError as e:
            return f"Error: {e}"
        if source is None:
            source = self.get_source(system, object_type, name, function_group)
            if source.startswith("Error:"):
                return source
        artifact_uri = f"{root_path}/source/main"
        encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<chkrun:checkObjectList '
            f'xmlns:chkrun="http://www.sap.com/adt/checkrun" '
            f'xmlns:adtcore="http://www.sap.com/adt/core">'
            f'<chkrun:checkObject adtcore:uri="{root_path}" '
            f'chkrun:version="{version}">'
            f'<chkrun:artifacts>'
            f'<chkrun:artifact '
            f'chkrun:contentType="text/plain; charset=utf-8" '
            f'chkrun:uri="{artifact_uri}">'
            f'<chkrun:content>{encoded}</chkrun:content>'
            f'</chkrun:artifact>'
            f'</chkrun:artifacts>'
            f'</chkrun:checkObject>'
            f'</chkrun:checkObjectList>').encode("utf-8")
        url = (f"{base_url(system.url)}/sap/bc/adt/checkruns"
               f"?reporters=abapCheckRun")
        try:
            resp = self._post(
                system, url, "application/*", body, "application/*")
        except httpx.HTTPError as e:
            return f"Error: check request failed: {e}"
        if resp.status_code != 200:
            return (f"Error: syntax check failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}")
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        msgs = parse_check_run(resp.content)
        errors = [m for m in msgs if m["type"][:1] in ("E", "A", "X")]
        warnings = [m for m in msgs if m["type"][:1] == "W"]
        if not errors and not warnings:
            return f"OK: no syntax errors ({object_type.upper()} {name.upper()})"

        def fmt(m: dict) -> str:
            loc = f":{m['line']}" if m["line"] else ""
            return f"{m['type']}{loc}: {m['text']}"

        lines = [f"{len(errors)} error(s), {len(warnings)} warning(s) "
                 f"in {object_type.upper()} {name.upper()}:"]
        lines += [fmt(m) for m in errors + warnings]
        return "\n".join(lines)

    # --- Pretty printer (ABAP source formatter) ---

    def pretty_print(self, system: System, source: str) -> str:
        """Format ABAP source via the ADT pretty printer (user's settings)."""
        if not source:
            return "Error: empty source"
        url = (f"{base_url(system.url)}/sap/bc/adt/abapsource/prettyprinter"
               f"?sap-client={system.client}")
        try:
            resp = self._post(system, url, "text/plain",
                              source.encode("utf-8"), "text/plain")
        except httpx.HTTPError as e:
            return f"Error: pretty print request failed: {e}"
        if resp.status_code != 200:
            return (f"Error: pretty print failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}")
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        # Empty body => server returned nothing to change; keep the original.
        return resp.text if resp.text else source

    # --- ABAP Unit test run ---

    # Object-set URIs for a unit-test run (group-level for FUGR).
    AUNIT_PATHS = {
        "CLAS": "/sap/bc/adt/oo/classes/{name}",
        "PROG": "/sap/bc/adt/programs/programs/{name}",
        "FUGR": "/sap/bc/adt/functions/groups/{name}",
    }

    def run_unit_tests(self, system: System, object_type: str,
                       name: str) -> str:
        """Run ABAP Unit for a class/program/function group; report pass/fail.

        Returns a per-method summary; failures include their assertion alerts.
        """
        ot = OBJECT_TYPE_ALIASES.get(object_type.upper(), object_type.upper())
        if ot not in self.AUNIT_PATHS:
            return (f"Error: cannot run unit tests for type {object_type!r}; "
                    f"valid: {', '.join(self.AUNIT_PATHS)}")
        uri = self.AUNIT_PATHS[ot].format(name=name.upper())
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<aunit:runConfiguration '
            f'xmlns:aunit="http://www.sap.com/adt/aunit">'
            f'<external><coverage active="false"/></external>'
            # The <options> block is mandatory: without explicit risk levels
            # and durations the server returns an empty runResult (no tests
            # "selected"), which looks like "no tests found". Run them all.
            f'<options>'
            f'<uriType value="semantic"/>'
            f'<testDeterminationStrategy sameProgram="true" '
            f'assignedTests="false" appendAssignedTestsPreview="true"/>'
            f'<testRiskLevels harmless="true" dangerous="true" '
            f'critical="true"/>'
            f'<testDurations short="true" medium="true" long="true"/>'
            f'</options>'
            f'<adtcore:objectSets xmlns:adtcore="http://www.sap.com/adt/core">'
            f'<objectSet kind="inclusive">'
            f'<adtcore:objectReferences>'
            f'<adtcore:objectReference adtcore:uri="{uri}"/>'
            f'</adtcore:objectReferences>'
            f'</objectSet>'
            f'</adtcore:objectSets>'
            f'</aunit:runConfiguration>').encode("utf-8")
        url = f"{base_url(system.url)}/sap/bc/adt/abapunit/testruns"
        ct = "application/vnd.sap.adt.abapunit.testruns.config.v4+xml"
        accept = "application/vnd.sap.adt.abapunit.testruns.result.v2+xml"
        try:
            resp = self._post(system, url, accept, body, ct)
        except httpx.HTTPError as e:
            return f"Error: unit test request failed: {e}"
        if resp.status_code != 200:
            return (f"Error: unit test run failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}")
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        methods = parse_aunit_result(resp.content)
        if not methods:
            return (f"No ABAP Unit tests found for {ot} {name.upper()}")

        def failed(m: dict) -> bool:
            return any(a["severity"].lower() in ("critical", "fatal")
                       for a in m["alerts"])

        n_failed = sum(1 for m in methods if failed(m))
        lines = [f"{len(methods)} test method(s), {n_failed} failed "
                 f"in {ot} {name.upper()}:"]
        for m in methods:
            tag = "FAIL" if failed(m) else "ok"
            t = f" ({m['time']}s)" if m["time"] else ""
            lines.append(f"  [{tag}] {m['class']}->{m['method']}{t}")
            for a in m["alerts"]:
                lines.append(f"      {a['severity']}: {a['title']}")
                for d in a["details"]:
                    lines.append(f"        {d}")
        return "\n".join(lines)

    # --- Data preview (CDS view / Open SQL) ---

    def data_preview(self, system: System, query: str,
                     max_rows: int = 100) -> str:
        """Preview data for a CDS entity name or an Open SQL SELECT.

        A bare entity name is wrapped as `SELECT * FROM <name>`. Returns a
        text table (columns + up to max_rows rows).
        """
        q = (query or "").strip()
        if not q:
            return "Error: empty query"
        if not re.search(r"\bselect\b", q, re.IGNORECASE):
            q = f"SELECT * FROM {q}"
        url = (f"{base_url(system.url)}/sap/bc/adt/datapreview/freestyle"
               f"?rowNumber={max_rows}")
        accept = "application/vnd.sap.adt.datapreview.table.v1+xml"
        try:
            resp = self._post(system, url, accept,
                              q.encode("utf-8"), "text/plain")
        except httpx.HTTPError as e:
            return f"Error: data preview request failed: {e}"
        if resp.status_code != 200:
            return (f"Error: data preview failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}")
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        res = parse_data_preview(resp.content)
        cols, rows = res["columns"], res["rows"]
        if not cols:
            return f"(no columns returned) query: {q}"
        widths = [len(c) for c in cols]
        for r in rows:
            for i, v in enumerate(r):
                widths[i] = max(widths[i], len(v))
        def fmt(vals):
            return " | ".join(v.ljust(widths[i]) for i, v in enumerate(vals))
        out = [fmt(cols), "-+-".join("-" * w for w in widths)]
        out += [fmt(r) for r in rows]
        out.append(f"({len(rows)} row(s) shown, total {res['total']})")
        return "\n".join(out)

    # --- ABAP Profiler (runtime traces) ---

    TRACES_BASE = "/sap/bc/adt/runtime/traces/abaptraces"

    def trace_start(self, system: System, process_type: str = "http",
                    object_type: str = "url", title: str = "ai-trace",
                    max_executions: int = 3, expires_minutes: int = 60,
                    aggregate: bool = False) -> str:
        """Arm an ABAP profiler trace for the caller's next executions.

        After this, run the slow workload (Fiori/OData → process_type 'http';
        a console class → 'dialog'/'batch'; data_preview also counts as 'http').
        Then call trace_list + trace_analyze. The trace auto-expires.
        """
        import datetime
        pbody = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<trc:parameters xmlns:trc="http://www.sap.com/adt/runtime/traces/'
            'abaptraces">'
            '<trc:allMiscAbapStatements value="false"/>'
            '<trc:allProceduralUnits value="true"/>'
            '<trc:allInternalTableEvents value="false"/>'
            '<trc:allDynproEvents value="false"/>'
            f'<trc:description value="{title}"/>'
            f'<trc:aggregate value="{"true" if aggregate else "false"}"/>'
            '<trc:explicitOnOff value="false"/>'
            '<trc:withRfcTracing value="false"/>'
            '<trc:allSystemKernelEvents value="false"/>'
            '<trc:sqlTrace value="false"/>'
            '<trc:allDbEvents value="true"/>'
            '<trc:maxSizeForTraceFile value="30720"/>'
            '<trc:maxTimeForTracing value="1800"/>'
            '</trc:parameters>').encode("utf-8")
        purl = f"{base_url(system.url)}{self.TRACES_BASE}/parameters"
        try:
            pr = self._post(system, purl, "application/xml", pbody,
                            "application/xml")
        except httpx.HTTPError as e:
            return f"Error: trace parameters request failed: {e}"
        if pr.status_code != 200:
            return (f"Error: trace parameters failed (HTTP {pr.status_code}): "
                    f"{pr.text[:200]}")
        pid = pr.headers.get("location", "")
        m = re.search(rf"({re.escape(self.TRACES_BASE)}/parameters/\w+)",
                      pid + " " + pr.text)
        if not m:
            return f"Error: no parametersId returned: {pr.text[:200]}"
        pid = m.group(1)
        expires = (datetime.datetime.utcnow()
                   + datetime.timedelta(minutes=expires_minutes)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
        pt = f"{self.TRACES_BASE}/processtypes/{process_type.lower()}"
        ot = f"{self.TRACES_BASE}/objecttypes/{object_type.lower()}"
        qs = (f"?server=*&description={quote(title)}"
              f"&processType={quote(pt, safe='')}"
              f"&objectType={quote(ot, safe='')}"
              f"&expires={quote(expires, safe='')}"
              f"&maximalExecutions={max_executions}"
              f"&parametersId={quote(pid, safe='')}")
        rurl = f"{base_url(system.url)}{self.TRACES_BASE}/requests{qs}"
        try:
            rr = self._post(system, rurl, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: trace request failed: {e}"
        if rr.status_code not in (200, 201):
            return (f"Error: trace request failed (HTTP {rr.status_code}): "
                    f"{rr.text[:200]}")
        return (f"OK: trace armed (process={process_type}, "
                f"max_executions={max_executions}, expires in "
                f"{expires_minutes}min). Run the workload now, then call "
                f"trace_list and trace_analyze.")

    def trace_list(self, system: System, max_runs: int = 20) -> str:
        """List the caller's recorded ABAP profiler runs (newest first)."""
        url = f"{base_url(system.url)}{self.TRACES_BASE}"
        try:
            resp = self._get(system, url, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: trace list request failed: {e}"
        if resp.status_code != 200:
            return f"Error: trace list failed (HTTP {resp.status_code})"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        runs = parse_trace_runs(resp.content)
        if not runs:
            return ("No trace runs found. Use trace_start, run the workload, "
                    "then retry.")
        runs.sort(key=lambda r: r["date"], reverse=True)  # newest first
        lines = ["Trace runs (time in ms; total / ABAP / DB):"]
        for r in runs[:max_runs]:
            lines.append(
                f"  {r['date']}  {r['title']}  "
                f"{r['runtime']/1000:.1f} / {r['runtime_abap']/1000:.1f} / "
                f"{r['runtime_db']/1000:.1f} ms  [{r['state']}]\t{r['uri']}")
        return "\n".join(lines)

    def trace_analyze(self, system: System, trace_uri: str, top: int = 15,
                      with_system_events: bool = False) -> str:
        """Digest one trace: top time consumers (hitlist) + DB accesses."""
        if not trace_uri:
            return "Error: trace_uri is required"
        se = "true" if with_system_events else "false"
        hurl = f"{base_url(system.url)}{trace_uri}/hitlist?withSystemEvents={se}"
        durl = (f"{base_url(system.url)}{trace_uri}/dbAccesses"
                f"?withSystemEvents={se}")
        try:
            hr = self._get(system, hurl, "application/xml")
            dr = self._get(
                system, durl,
                "application/vnd.sap.adt.runtime.traces.abaptraces.dbaccesses"
                "+xml, application/xml")
        except httpx.HTTPError as e:
            return f"Error: trace analyze request failed: {e}"
        if hr.status_code != 200:
            return f"Error: hitlist failed (HTTP {hr.status_code})"
        if is_login_page(hr):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        hits = parse_trace_hitlist(hr.content)
        out = [f"Trace {trace_uri}", "", "Top time consumers (gross%):"]
        for h in sorted(hits, key=lambda x: x["gross_pct"], reverse=True)[:top]:
            prog = f"  [{h['program']}]" if h["program"] else ""
            out.append(f"  {h['gross_pct']:5.1f}%  {h['gross_time']/1000:8.1f}ms  "
                       f"{h['description']}{prog}")
        if dr.status_code == 200:
            db = parse_trace_dbaccesses(dr.content)
            total_db = db["total_db_time"] or sum(
                a["db_time"] for a in db["accesses"])
            out += ["", f"DB accesses (total {total_db/1000:.1f}ms) "
                    f"— table / stmt / count / buffered / db_ms / ratio:"]
            for a in sorted(db["accesses"], key=lambda x: x["db_time"],
                            reverse=True)[:top]:
                out.append(
                    f"  {a['table']:<28} {a['statement']:<14} "
                    f"{a['total_count']:>6} {a['buffered_count']:>6} "
                    f"{a['db_time']/1000:8.1f} {a['ratio']:5.1f}%")
        return "\n".join(out)

    # --- Runtime dumps (ST22 short dumps) ---

    DUMPS_BASE = "/sap/bc/adt/runtime/dumps"

    def _fetch_dumps_feed(self, system: System, from_date: str | None,
                          to_date: str | None) -> "list[dict] | str":
        """GET the runtime dumps atom feed; return parsed dumps or an error str.

        Each entry already carries the full dump in its `summary` (the
        atom:link hrefs are adt:// SAP GUI links, not HTTP resources).
        """
        params = []
        if from_date:
            params.append(f"from={quote(from_date)}")
        if to_date:
            params.append(f"to={quote(to_date)}")
        qs = ("?" + "&".join(params)) if params else ""
        url = f"{base_url(system.url)}{self.DUMPS_BASE}{qs}"
        try:
            resp = self._get(system, url, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: dumps feed request failed: {e}"
        if resp.status_code in (401, 403):
            return (f"Error: auth failed (HTTP {resp.status_code}) — refresh "
                    f"cookies for system {system.name!r}")
        if resp.status_code != 200:
            return f"Error: dumps feed failed (HTTP {resp.status_code})"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        return parse_dumps_feed(resp.content)

    @staticmethod
    def _dump_error_term(categories: list) -> str:
        """The ABAP runtime error term (category labelled 'runtime error')."""
        for term, label in categories:
            if "runtime error" in (label or "").lower():
                return term
        return categories[0][0] if categories else ""

    def list_dumps(self, system: System, from_date: str | None = None,
                   to_date: str | None = None, max_dumps: int = 50) -> str:
        """List recent ABAP runtime dumps (ST22), newest first.

        from_date/to_date filter the feed; pass timestamps as 'yyyyMMddHHmmss'
        (e.g. '20260601000000'). Each line shows the dump uri for get_dump.
        """
        dumps = self._fetch_dumps_feed(system, from_date, to_date)
        if isinstance(dumps, str):
            return dumps
        if not dumps:
            return ("No runtime dumps found"
                    + (" for that period." if (from_date or to_date) else "."))
        dumps.sort(key=lambda d: d["date"], reverse=True)
        lines = [f"Runtime dumps ({len(dumps)} found, newest first):"]
        for d in dumps[:max_dumps]:
            err = self._dump_error_term(d["categories"])
            tag = f"  [{err}]" if err else ""
            lines.append(
                f"  {d['date']}  {d['author'] or '?':<12}{tag}  "
                f"{d['title']}\n      {d['uri']}")
        return "\n".join(lines)

    def get_dump(self, system: System, dump_uri: str) -> str:
        """Return one runtime dump (uri from list_dumps) as readable text.

        The dump body is embedded in the feed entry's summary, so this matches
        the entry by uri and flattens its HTML (error analysis, source extract,
        call stack) for AI analysis — no second fetch of the adt:// link.
        """
        if not dump_uri:
            return "Error: dump_uri is required"
        key = dump_uri.split("#", 1)[0].split("?", 1)[0].strip()
        # Try the recent feed first; if the dump is older than that window,
        # re-query bounded to its timestamp (first 14 digits of the id).
        windows: list[tuple[str | None, str | None]] = [(None, None)]
        m = re.search(r"/dumps/(\d{14})", key)
        if m:
            windows.append((m.group(1), m.group(1)))
        last_err = None
        for frm, to in windows:
            dumps = self._fetch_dumps_feed(system, frm, to)
            if isinstance(dumps, str):
                last_err = dumps
                continue
            for d in dumps:
                if d["uri"].split("#", 1)[0] == key:
                    summary = d.get("summary", "")
                    if not summary.strip():
                        return ("(dump entry has no summary; error="
                                f"{self._dump_error_term(d['categories'])})")
                    return html_to_text(summary)
        if last_err:
            return last_err
        return (f"Error: dump not found in feed ({key}). It may be outside the "
                f"returned window — retry list_dumps with from_date/to_date "
                f"around the dump's timestamp.")

    # --- API release state (ABAP Cloud / Clean Core) ---

    def api_release_state(self, system: System, object_type: str, name: str,
                          function_group: str | None = None) -> str:
        """Report the API release state of an object (released for ABAP Cloud?)."""
        try:
            root_path = object_root_path(object_type, name, function_group)
        except ValueError as e:
            return f"Error: {e}"
        endpoint = (f"{base_url(system.url)}/sap/bc/adt/apireleases/"
                    f"{quote(root_path, safe='')}"
                    f"?sap-client={system.client}")
        try:
            resp = self._get(
                system, endpoint,
                "application/vnd.sap.adt.apirelease.v10+xml")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code == 404:
            return (f"Error: no release info ({object_type.upper()} "
                    f"{name.upper()}) — object unknown or not releasable")
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code}: {resp.text[:200]}"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        st = parse_release_state(resp.content)
        obj = st["object"] or {"name": name.upper(), "type": object_type.upper()}
        # Drop empty contract-slot placeholders (no state/flags/successors).
        contracts = [c for c in st["contracts"]
                     if c["state"] or c["cloud"] or c["keyUser"]
                     or c["successors"]]
        cloud_ok = any(c["cloud"] and c["state"] == "RELEASED"
                       for c in contracts)
        head = (f"{obj.get('name', name.upper())} "
                f"({obj.get('type', object_type.upper())}) - "
                + ("RELEASED for ABAP Cloud" if cloud_ok
                   else "NOT released for ABAP Cloud"))
        lines = [head]
        if not contracts:
            lines.append("  (no release contracts)")
        for c in contracts:
            succ = (f"  successors: {', '.join(c['successors'])}"
                    if c["successors"] else "")
            lines.append(
                f"  {c['contract']}: {c['state'] or '?'}"
                f" ({c['stateDescription'] or '-'})"
                f"  cloud={'yes' if c['cloud'] else 'no'}"
                f" keyUser={'yes' if c['keyUser'] else 'no'}{succ}")
        return "\n".join(lines)

    # --- ABAP feeds (gateway log, system messages, monitoring) ---

    FEEDS_BASE = "/sap/bc/adt/feeds"

    def _fetch_feed_catalog(self, system: System) -> "list[dict] | str":
        url = f"{base_url(system.url)}{self.FEEDS_BASE}?sap-client={system.client}"
        try:
            resp = self._get(system, url, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: feeds request failed: {e}"
        if resp.status_code in (401, 403):
            return (f"Error: auth failed (HTTP {resp.status_code}) — refresh "
                    f"cookies for system {system.name!r}")
        if resp.status_code != 200:
            return f"Error: feeds catalog failed (HTTP {resp.status_code})"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        return parse_feed_catalog(resp.content)

    def list_feeds(self, system: System) -> str:
        """List the ABAP feeds this system offers (gateway log, system messages…).

        Shows the friendly alias (for read_feed) next to each catalog title.
        """
        catalog = self._fetch_feed_catalog(system)
        if isinstance(catalog, str):
            return catalog
        if not catalog:
            return "No feeds offered by this system."
        title_to_alias = {v: k for k, v in FEED_ALIASES.items()}
        lines = [f"ABAP feeds ({len(catalog)}):"]
        for f in catalog:
            alias = title_to_alias.get(f["title"], "")
            tag = f"  (alias: {alias})" if alias else ""
            lines.append(f"  {f['title']}{tag}\n      {f['href']}")
        return "\n".join(lines)

    def read_feed(self, system: System, feed: str,
                  from_date: str | None = None, to_date: str | None = None,
                  user: str | None = None, object: str | None = None,
                  max: int = 50) -> str:
        """Read one ABAP feed's entries, newest first.

        `feed` accepts an alias (gateway_log, system_messages, dumps, event_log,
        uri_errors, atc, contract_violations) or a case-insensitive substring of
        the catalog title. Filters (from_date/to_date as 'yyyy-mm-dd' prefixes,
        user, object substring) are applied to the parsed entries; `max` caps
        the count. For full ST22 dump bodies use list_dumps / get_dump instead.
        """
        catalog = self._fetch_feed_catalog(system)
        if isinstance(catalog, str):
            return catalog
        title = FEED_ALIASES.get(feed.lower().strip())
        match = None
        for f in catalog:
            if title and f["title"] == title:
                match = f
                break
        if match is None:
            key = feed.lower().strip()
            for f in catalog:
                if key in f["title"].lower():
                    match = f
                    break
        if match is None:
            aliases = ", ".join(sorted(FEED_ALIASES))
            titles = "; ".join(f["title"] for f in catalog)
            return (f"Error: unknown feed {feed!r}. Aliases: {aliases}. "
                    f"Available feeds: {titles}")
        href = match["href"]
        sep = "&" if "?" in href else "?"
        url = f"{base_url(system.url)}{href}{sep}sap-client={system.client}"
        try:
            resp = self._get(system, url, "application/atom+xml;type=feed")
        except httpx.HTTPError as e:
            return f"Error: feed request failed: {e}"
        if resp.status_code != 200:
            return f"Error: feed failed (HTTP {resp.status_code})"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        entries = parse_feed_entries(resp.content)
        # Client-side filters — robust regardless of per-feed query support.
        if user:
            u = user.lower()
            entries = [e for e in entries if u in e["author"].lower()]
        if object:
            o = object.lower()
            entries = [e for e in entries
                       if o in e["title"].lower() or o in e["summary"].lower()]
        if from_date:
            entries = [e for e in entries if e["date"][:10] >= from_date]
        if to_date:
            entries = [e for e in entries if e["date"][:10] <= to_date]
        if not entries:
            return f"No entries in feed {match['title']!r} for those filters."
        entries.sort(key=lambda e: e["date"], reverse=True)
        lines = [f"{match['title']} ({len(entries)} entries, newest first):"]
        for e in entries[:max]:
            snippet = html_to_text(e["summary"]).replace("\n", " ")[:160]
            lines.append(
                f"  {e['date']}  {e['author'] or '?':<12}  {e['title']}"
                + (f"\n      {snippet}" if snippet else ""))
        return "\n".join(lines)

    # --- Transports (CTS) ---

    TRANSPORTS_BASE = "/sap/bc/adt/cts/transportrequests"
    _TR_TREE_ACCEPT = "application/vnd.sap.adt.transportorganizertree.v1+xml"

    def list_transports(self, system: System) -> str:
        """List the session user's open (modifiable) transport requests.

        Reads the CTS transport organizer tree. Each line shows the request
        number to pass as `transport` to create_object/update_source.
        """
        url = (f"{base_url(system.url)}{self.TRANSPORTS_BASE}"
               f"?sap-client={system.client}")
        try:
            resp = self._get(system, url, self._TR_TREE_ACCEPT)
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code in (401, 403):
            return (f"Error: auth failed (HTTP {resp.status_code}) — refresh "
                    f"cookies for system {system.name!r}")
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code}: {resp.text[:200]}"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        rows = parse_transports(resp.content)
        if not rows:
            return "No open transport requests."
        lines = [f"Open transport requests ({len(rows)}):"]
        for r in rows:
            lines.append(
                f"  {r['number']}  [{r['type']}/{r['status']}] "
                f"{r['owner'] or '?':<12}  {r['description']}")
        return "\n".join(lines)

    def create_transport(self, system: System, description: str) -> str:
        """Create a workbench transport request; return its new number.

        A transport isn't package-scoped, so only the allow_write flag gates it
        (not the package/object allow-lists check_write enforces on objects).
        """
        if not system.allow_write:
            return (f"Error: writes disabled for system {system.name!r} "
                    f"(set allow_write=true in systems.json)")
        if not (description or "").strip():
            return "Error: description is required"
        url = f"{base_url(system.url)}{self.TRANSPORTS_BASE}?sap-client={system.client}"
        body = build_transport_body(description, owner=(system.username or ""))
        try:
            resp = self._post(system, url, self._TR_TREE_ACCEPT,
                              body.encode("utf-8"),
                              "application/vnd.sap.adt.transportorganizer.v1+xml")
        except httpx.HTTPError as e:
            return f"Error: request failed: {e}"
        if resp.status_code not in (200, 201):
            return f"Error: create failed (HTTP {resp.status_code}): {resp.text[:300]}"
        if is_login_page(resp):
            return (f"Error: session expired for system {system.name!r} "
                    f"— refresh cookies and retry")
        rows = parse_transports(resp.content)
        number = rows[0]["number"] if rows else ""
        if not number:
            m = re.search(r"(?:tm:number=\"|/transportrequests/)([A-Z0-9]+)",
                          resp.text)
            number = m.group(1) if m else ""
        if not number:
            return f"Error: created but no request number returned: {resp.text[:200]}"
        return f"OK: created transport {number} ({description})"

    # --- Context compression (v2 Phase 6) ---

    @staticmethod
    def _is_custom(name: str) -> bool:
        up = name.upper()
        return up[:1] in ("Z", "Y") or up.startswith("/")

    def get_context(self, system: System, object_type: str, name: str,
                    depth: int = 1, max_objects: int = 20) -> str:
        """Bundle an object's full source + its compressed dependencies.

        DDLS (CDS): recurse FROM/JOIN/ASSOCIATION/COMPOSITION up to `depth`.
        BDEF (RAP behavior): pull the behavior-for CDS (with its own CDS deps)
        and the behavior-pool implementation class.
        CLAS: pull the superclass and implemented interfaces.
        Custom (Z*/Y*/namespaced) deps are fetched and compressed; standard SAP
        objects are listed but not expanded (token economy).
        """
        main = self.get_source(system, object_type, name)
        if main.startswith("Error:"):
            return main
        ot = object_type.upper()
        blocks = [f"* ==== {ot} {name.upper()} (full source) ====", main]
        if ot == "DDLS":
            self._expand_cds_context(system, name, blocks, {name.upper()},
                                     depth, max_objects)
        elif ot == "BDEF":
            self._expand_bdef_context(system, main, blocks, depth, max_objects)
        elif ot == "CLAS":
            self._expand_class_context(system, main, blocks)
        return "\n".join(blocks)

    def _add_dep_block(self, system: System, blocks: list, relation: str,
                       name: str, object_type: str) -> str:
        """Fetch a custom dep, append a compressed block; return its status.

        Standard SAP objects are listed but not fetched. Returns "expanded",
        "standard" or "unresolved" so callers can decide whether to recurse.
        """
        if not self._is_custom(name):
            blocks.append(f"* ---- {relation} {name} (standard, not expanded) ----")
            return "standard"
        src = self.get_source(system, object_type, name)
        if src.startswith("Error:"):
            blocks.append(f"* ---- {relation} {name} (unresolved) ----")
            return "unresolved"
        blocks.append(f"* ---- {relation} {name} [{object_type}, compressed] ----")
        blocks.append(compress_source(object_type, src))
        return "expanded"

    def _expand_cds_context(self, system: System, name: str, blocks: list,
                            visited: set, depth: int, max_objects: int) -> None:
        frontier = [(name, 1)]
        count = 0
        while frontier and count < max_objects:
            cur, lvl = frontier.pop(0)
            deps = self.cds_dependencies(system, cur)
            if isinstance(deps, str):
                continue
            for d in deps:
                dn, up = d["name"], d["name"].upper()
                if up in visited:
                    continue
                visited.add(up)
                count += 1
                if count > max_objects:
                    break
                if not self._is_custom(dn):
                    blocks.append(f"* ---- {d['relation']} {dn} "
                                  f"(standard, not expanded) ----")
                    continue
                src = self.get_source(system, "DDLS", dn)
                otype = "DDLS"
                if src.startswith("Error: object not found"):
                    src = self.get_source(system, "TABL", dn)
                    otype = "TABL"
                if src.startswith("Error:"):
                    blocks.append(f"* ---- {d['relation']} {dn} (unresolved) ----")
                    continue
                blocks.append(f"* ---- {d['relation']} {dn} "
                              f"[{otype}, compressed] ----")
                blocks.append(compress_source(otype, src))
                if otype == "DDLS" and lvl < depth:
                    frontier.append((dn, lvl + 1))

    def _expand_bdef_context(self, system: System, main: str, blocks: list,
                             depth: int, max_objects: int) -> None:
        deps = parse_bdef_dependencies(main)
        visited: set = set()
        for ent in deps["entities"]:
            if self._add_dep_block(system, blocks, "BEHAVIOR_FOR", ent,
                                   "DDLS") == "expanded":
                visited.add(ent.upper())
                self._expand_cds_context(system, ent, blocks, visited,
                                         depth, max_objects)
        for cls in deps["classes"]:
            self._add_dep_block(system, blocks, "IMPL_CLASS", cls, "CLAS")

    def _expand_class_context(self, system: System, main: str,
                              blocks: list) -> None:
        deps = parse_class_dependencies(main)
        if deps["superclass"]:
            self._add_dep_block(system, blocks, "SUPERCLASS",
                                deps["superclass"], "CLAS")
        for intf in deps["interfaces"]:
            self._add_dep_block(system, blocks, "INTERFACE", intf, "INTF")

    # --- Write: stateful primitives (v_write Phase A) ---

    def object_root_url(self, system: System, object_type: str, name: str,
                        function_group: str | None = None) -> str:
        return f"{base_url(system.url)}{object_root_path(object_type, name, function_group)}"

    def _prime_cookies(self, system: System) -> None:
        """Load a cookie system's session into the client's cookie jar, scoped
        to the host. The stateful write sequence then relies on the jar (no
        per-request override) so a session-id the server rotates mid-sequence
        (Set-Cookie on LOCK) is carried into the following PUT/UNLOCK."""
        host = urlsplit(base_url(system.url)).hostname or ""
        jar = self._client.cookies
        for k, v in (self._cookies_dict(system) or {}).items():
            jar.set(k, v, domain=host)

    def _write_kwargs(self, system: System) -> dict:
        """Auth kwargs for the stateful write sequence.

        Cookie systems prime the jar and pass nothing (the jar is the single
        source of truth, respecting server-side session rotation); basic-auth
        systems pass credentials per request.
        """
        if system.auth == "cookie":
            self._prime_cookies(system)
            return {}
        return {"auth": httpx.BasicAuth(system.username or "",
                                        system.password or "")}

    def _lock(self, system: System, root_url: str, token: str,
              wk: dict) -> tuple[str, str | None]:
        url = f"{root_url}?_action=LOCK&accessMode=MODIFY"
        headers = {"X-sap-adt-sessiontype": "stateful", "X-CSRF-Token": token,
                   "Accept": "application/vnd.sap.as+xml;charset=UTF-8;"
                             "dataname=com.sap.adt.lock.result"}
        try:
            resp = self._client.post(url, headers=headers, **wk)
        except httpx.HTTPError as e:
            return "", f"Error: lock request failed: {e}"
        if resp.status_code != 200:
            exc_type, msg = parse_adt_exception(resp.content)
            # ExceptionResourceNoAccess / ...AlreadyLocked on LOCK means the
            # object is enqueue-locked in ANOTHER session — typically open in an
            # Eclipse ADT editor, or a stale lock left by an earlier crashed
            # write (same user, different session). refresh_cookies_for CANNOT
            # release it: a new cookie session does not own the foreign enqueue.
            if exc_type in ("ExceptionResourceNoAccess",
                            "ExceptionResourceAlreadyLocked"):
                detail = f" — server: {msg}" if msg else ""
                return "", (
                    f"Error: cannot lock object for editing (ADT "
                    f"{exc_type}, HTTP {resp.status_code}): it is locked in "
                    f"another session. Most likely it is open in an Eclipse "
                    f"ADT editor, or a stale lock from an earlier write is "
                    f"still held. This is NOT a cookie/session-expiry problem, "
                    f"so refresh_cookies_for will not help — close the object "
                    f"in Eclipse (or wait for the stale lock to time out) and "
                    f"retry.{detail}")
            detail = msg or resp.text[:300]
            return "", f"Error: lock failed (HTTP {resp.status_code}): {detail}"
        handle, _mod = parse_lock_result(resp.content)
        # Note: MODIFICATION_SUPPORT="NoModification" is informational on cloud
        # (local / no version mgmt) and does NOT block writes — a PUT with the
        # handle still succeeds. Only a missing handle is a real failure.
        if not handle:
            return "", f"Error: no lock handle returned: {resp.text[:200]}"
        return handle, None

    def _put_source(self, system: System, source_url: str, source: str,
                    handle: str, transport: str | None, token: str,
                    wk: dict) -> str | None:
        url = f"{source_url}&lockHandle={quote(handle, safe='')}"
        if transport:
            url += f"&corrNr={quote(transport, safe='')}"
        headers = {"X-sap-adt-sessiontype": "stateful", "X-CSRF-Token": token,
                   "Content-Type": "text/plain; charset=utf-8"}
        try:
            resp = self._client.put(url, headers=headers,
                                    content=source.encode("utf-8"), **wk)
        except httpx.HTTPError as e:
            return f"Error: update request failed: {e}"
        if resp.status_code in (200, 201, 202):
            if is_login_page(resp):
                return (f"Error: session expired for system {system.name!r} "
                        f"during write (got SAML login page) — refresh cookies "
                        f"and retry")
            return None
        if resp.status_code in (401, 403):
            return (f"Error: auth failed during write (HTTP {resp.status_code}) "
                    f"for system {system.name!r} — cookie/session expired; "
                    f"refresh cookies and retry")
        return f"Error: update failed (HTTP {resp.status_code}): {resp.text[:300]}"

    def _unlock(self, system: System, root_url: str, handle: str,
                token: str, wk: dict) -> bool:
        """Release the lock; return True if the server confirmed the unlock."""
        url = f"{root_url}?_action=UNLOCK&lockHandle={quote(handle, safe='')}"
        headers = {"X-sap-adt-sessiontype": "stateful", "X-CSRF-Token": token}
        try:
            resp = self._client.post(url, headers=headers, **wk)
            return resp.status_code in (200, 202, 204)
        except httpx.HTTPError:
            return False

    def activate(self, system: System, object_type: str, name: str,
                 function_group: str | None = None) -> str:
        try:
            root_path = object_root_path(object_type, name, function_group)
        except ValueError as e:
            return f"Error: {e}"
        body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<adtcore:objectReferences '
                f'xmlns:adtcore="http://www.sap.com/adt/core">'
                f'<adtcore:objectReference adtcore:uri="{root_path}" '
                f'adtcore:name="{name.upper()}"/>'
                f'</adtcore:objectReferences>').encode("utf-8")
        url = (f"{base_url(system.url)}/sap/bc/adt/activation"
               f"?method=activate&preauditRequested=true")
        try:
            resp = self._post(system, url, "application/xml", body, "application/xml")
        except httpx.HTTPError as e:
            return f"Error: activate request failed: {e}"
        if resp.status_code not in (200, 202):
            return f"Error: activate failed (HTTP {resp.status_code}): {resp.text[:200]}"
        return parse_activation(resp.content)

    def activate_many(self, system: System,
                      refs: list[tuple[str, str, str | None]]) -> str:
        """Activate multiple objects in ONE call so SAP resolves their
        interdependencies. refs: list of (object_type, name, function_group)."""
        if not refs:
            return "OK"
        parts = []
        for ot, name, fg in refs:
            try:
                rp = object_root_path(ot, name, fg)
            # Unknown object type is a programmer error — fail fast with a clear message.
            except ValueError as e:
                return f"Error: {e}"
            parts.append(
                f'<adtcore:objectReference adtcore:uri="{rp}" '
                f'adtcore:name="{name.upper()}"/>')
        body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<adtcore:objectReferences '
                f'xmlns:adtcore="http://www.sap.com/adt/core">'
                f'{"".join(parts)}'
                f'</adtcore:objectReferences>').encode("utf-8")
        url = (f"{base_url(system.url)}/sap/bc/adt/activation"
               f"?method=activate&preauditRequested=true")
        try:
            resp = self._post(system, url, "application/xml",
                              body, "application/xml")
        except httpx.HTTPError as e:
            return f"Error: activate request failed: {e}"
        if resp.status_code not in (200, 202):
            return (f"Error: activate failed (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}")
        return parse_activation(resp.content)

    def object_package(self, system: System, object_type: str,
                       name: str, function_group: str | None = None) -> str | None:
        try:
            root_url = self.object_root_url(system, object_type, name, function_group)
        except ValueError:
            return None
        try:
            resp = self._get(system, root_url, "*/*")
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or is_login_page(resp):
            return None
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return None
        for el in root.iter():
            if _localname(el.tag) == "packageRef":
                a = {_localname(k): v for k, v in el.attrib.items()}
                if a.get("name"):
                    return a["name"]
        return None

    def _edit_sequence(self, system: System, root_url: str, source_url: str,
                       source: str, transport: str | None) -> str | None:
        token = self._csrf_token(system)
        wk = self._write_kwargs(system)
        handle, err = self._lock(system, root_url, token, wk)
        if err:
            return err
        put_err = self._put_source(system, source_url, source, handle,
                                   transport, token, wk)
        unlocked = self._unlock(system, root_url, handle, token, wk)
        if put_err and not unlocked:
            return (put_err + f" — the object may remain locked on system "
                    f"{system.name!r}; run refresh_cookies_for and retry "
                    f"(a fresh session releases the stale lock)")
        return put_err

    def update_source(self, system: System, object_type: str, name: str,
                      source: str, transport: str | None = None,
                      function_group: str | None = None,
                      activate: bool = True) -> str:
        if not system.allow_write:
            return check_write(system, "")
        try:
            root_url = self.object_root_url(system, object_type, name, function_group)
        except ValueError as e:
            return f"Error: {e}"
        pkg = self.object_package(system, object_type, name, function_group)
        gate = check_write(system, pkg or "", name)
        if gate:
            return gate
        source_url = (f"{root_url}/source/main?sap-client={system.client}"
                      f"&sap-language={system.language}")
        err = self._edit_sequence(system, root_url, source_url, source, transport)
        if err:
            return err
        if activate:
            return self.activate(system, object_type, name, function_group)
        return f"OK: updated {object_type.upper()} {name.upper()} (not activated)"

    def update_class_include(self, system: System, class_name: str,
                             include: str, source: str,
                             transport: str | None = None,
                             activate: bool = True) -> str:
        inc = include.lower()
        if inc not in CLASS_INCLUDES and inc != "main":
            return (f"Error: invalid include {include!r}; valid: main, "
                    f"{', '.join(sorted(CLASS_INCLUDES))}")
        if not system.allow_write:
            return check_write(system, "")
        pkg = self.object_package(system, "CLAS", class_name)
        gate = check_write(system, pkg or "", class_name)
        if gate:
            return gate
        root_url = self.object_root_url(system, "CLAS", class_name)
        if inc == "main":
            source_url = (f"{root_url}/source/main?sap-client={system.client}"
                          f"&sap-language={system.language}")
        else:
            source_url = (f"{root_url}/includes/{inc}?sap-client={system.client}"
                          f"&sap-language={system.language}")
        err = self._edit_sequence(system, root_url, source_url, source, transport)
        if err:
            return err
        if activate:
            return self.activate(system, "CLAS", class_name)
        return f"OK: updated {class_name.upper()} include {inc} (not activated)"

    # --- Write: create (v_write Phase B) ---

    def create_object(self, system: System, object_type: str, name: str,
                      package: str, description: str = "",
                      source: str | None = None, transport: str | None = None,
                      service_definition: str | None = None,
                      binding_version: str = "V2") -> str:
        ot = OBJECT_TYPE_ALIASES.get(object_type.upper(), object_type.upper())
        if ot not in CREATE_TYPES:
            return (f"Error: cannot create type {object_type!r}; valid: "
                    f"{', '.join(CREATE_TYPES)}")
        gate = check_write(system, package, name)
        if gate:
            return gate
        path, root, ns, adt_type, content_type, source_capable = CREATE_TYPES[ot]
        if ot == "SRVB" and not service_definition:
            return "Error: SRVB requires service_definition"
        # Pass the raw login as responsible; build_creation_body drops it when
        # it isn't a valid XUBNAME (e.g. an IAS email) and lets the server fill
        # it from the session user.
        responsible = (system.username or "").upper()
        body = build_creation_body(ot, name, package, description, responsible,
                                   service_definition, binding_version,
                                   language=system.language)
        # Pin sap-language on the create URL so SAP writes the description in
        # system.language, matching adtcore:masterLanguage in the body. Without
        # it the description is written in the *session logon language*, which
        # can differ from masterLanguage after a headless re-login picks up the
        # user's SAP default language (e.g. JA) -> HTTP 400 "Language JA for
        # creation of description is not equal to original EN". Every other
        # write path (update_source, update_class_include) already pins it.
        url = (f"{base_url(system.url)}{path}"
               f"?sap-client={system.client}&sap-language={system.language}")
        if transport:
            url += f"&corrNr={quote(transport, safe='')}"
        try:
            resp = self._post(system, url, "application/*",
                              body.encode("utf-8"), content_type)
        except httpx.HTTPError as e:
            return f"Error: create request failed: {e}"
        if resp.status_code not in (200, 201):
            return f"Error: create failed (HTTP {resp.status_code}): {resp.text[:300]}"
        if source and source_capable:
            return self.update_source(system, ot, name, source, transport)
        return f"OK: created {ot} {name.upper()} in {package.upper()}"

    def clone_package(self, source: System, target: System,
                      source_package: str, target_package: str,
                      suffix: str = "_VN", dry_run: bool = True,
                      transport: str | None = None) -> str:
        """Clone all objects of source_package into target_package (must exist),
        append suffix to every name, rewrite cross-references, activate at end."""
        objs = self.list_package(source, source_package)
        if isinstance(objs, str):
            return objs
        if not objs:
            return f"(no objects in package {source_package.upper()})"

        cloneable, skipped = [], []
        for o in objs:
            short = clone_short_type(o["type"])
            if short == "DEVC":
                continue
            if short not in CREATE_TYPES:
                skipped.append((o, short, "type không hỗ trợ create"))
            elif short in SKIP_CLONE_TYPES:
                skipped.append((o, short, "type chỉ tạo shell (v1)"))
            else:
                cloneable.append((o, short))

        rename_map = build_rename_map([o["name"] for o, _ in cloneable], suffix)

        errors = []
        for o, short in list(cloneable):
            if len(rename_map[o["name"].upper()]) > 30:
                errors.append((o, short, "tên đích > 30 ký tự"))
        err_names = {o["name"] for o, _, _ in errors}
        cloneable = [(o, s) for o, s in cloneable if o["name"] not in err_names]
        # Drop over-length objects from rename_map so cross-references are never
        # rewritten to a non-existent (too-long) target name.
        for o, _, _ in errors:
            rename_map.pop(o["name"].upper(), None)

        cloneable.sort(key=lambda os: CLONE_ORDER.index(os[1])
                       if os[1] in CLONE_ORDER else len(CLONE_ORDER))

        lines = [f"Plan: clone {source_package.upper()} -> "
                 f"{target_package.upper()} (suffix={suffix.upper()}), "
                 f"{len(cloneable)} clone, {len(skipped)} skip, "
                 f"{len(errors)} error"]
        for o, short in cloneable:
            lines.append(f"  ✓  {short:<5} {o['name']} -> "
                         f"{rename_map[o['name'].upper()]}")
        for o, short, why in skipped + errors:
            lines.append(f"  ⊘  {short:<5} {o['name']} -> (skip: {why})")

        if dry_run:
            lines.append("Dry run — chưa ghi gì. Đặt dry_run=false để thực thi.")
            return "\n".join(lines)

        gate = check_write(target, target_package)
        if gate:
            return gate

        created_refs: list[tuple[str, str, str | None]] = []
        results = []
        ok = 0
        for o, short in cloneable:
            name, new = o["name"], rename_map[o["name"].upper()]
            desc = o.get("description") or f"Clone of {name}"
            res = self._clone_one(source, target, short, name, new,
                                  target_package, desc, rename_map,
                                  o.get("uri", ""), transport)
            results.append(f"  {short} {new}: {res}")
            if res.startswith("OK"):
                created_refs.append((short, new, None))
                ok += 1

        act = self.activate_many(target, created_refs)
        return ("\n".join(lines[1:]) + "\n--- Execute ---\n"
                + "\n".join(results)
                + f"\nActivate ({len(created_refs)} objects): {act}"
                + f"\nClone complete: {ok} success, "
                + f"{len(skipped)+len(errors)} skipped, "
                + f"{len(cloneable)-ok} failed")

    def _clone_one(self, source: System, target: System, short: str,
                   name: str, new: str, package: str, desc: str,
                   rename_map: dict[str, str], uri: str,
                   transport: str | None) -> str:
        if short == "SRVB":
            sd = self._read_srvb_servicedef(source, uri)
            if sd is None:
                return "FAILED: không đọc được service definition của binding"
            sd_name, version = sd
            sd_new = rename_map.get(sd_name.upper(), sd_name.upper())
            return self.create_object(target, "SRVB", new, package, desc,
                                      service_definition=sd_new,
                                      binding_version=version,
                                      transport=transport)
        if short == "CLAS":
            # A class is a global class (main) plus local includes:
            #   definitions/implementations = local & RAP-handler classes,
            #   macros, testclasses (CCAU = unit tests).
            # Read every part up front so a read error aborts before we create
            # an orphaned shell on the target.
            main_src = self.get_source(source, "CLAS", name)
            if main_src.startswith("Error:"):
                return f"FAILED read class main: {main_src}"
            includes = {}
            for inc in ("definitions", "implementations", "macros",
                        "testclasses"):
                s = self.get_class_include(source, name, inc)
                if not s.startswith("Error:") and s.strip():
                    includes[inc] = rewrite_references(s, rename_map)

            shell = self.create_object(target, "CLAS", new, package, desc,
                                       transport=transport)
            if not shell.startswith("OK"):
                return shell
            # main FIRST: the global class establishes the inactive version that
            # the local includes attach to. Writing a local include (e.g.
            # testclasses/CCAU) before main fails with "does not have any
            # inactive version".
            w = self.update_class_include(
                target, new, "main", rewrite_references(main_src, rename_map),
                transport, activate=False)
            if not w.startswith("OK"):
                return f"FAILED include main: {w}"
            # definitions/implementations carry local & RAP-handler classes →
            # fatal if they fail. macros/testclasses are non-runtime → warn but
            # keep the class so the BO isn't lost over a unit-test include.
            warns = []
            for inc in ("definitions", "implementations", "macros",
                        "testclasses"):
                if inc not in includes:
                    continue
                w = self.update_class_include(target, new, inc, includes[inc],
                                              transport, activate=False)
                if not w.startswith("OK"):
                    if inc in ("definitions", "implementations"):
                        return f"FAILED include {inc}: {w}"
                    warns.append(f"{inc}: {w}")
            return "OK" if not warns else \
                "OK (include warnings: " + "; ".join(warns) + ")"
        src = self.get_source(source, short, name)
        if src.startswith("Error:"):
            return f"FAILED read source: {src}"
        shell = self.create_object(target, short, new, package, desc,
                                   transport=transport)
        if not shell.startswith("OK"):
            return shell
        return self.update_source(target, short, new,
                                  rewrite_references(src, rename_map),
                                  transport, activate=False)

    def _read_srvb_servicedef(self, system: System,
                              uri: str) -> tuple[str, str] | None:
        """Read a binding -> (service definition name, binding version V2/V4)."""
        if not uri:
            return None
        url = f"{base_url(system.url)}{uri}"
        try:
            resp = self._get(system, url, "application/xml")
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or is_login_page(resp):
            return None
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return None
        sd_name, version = None, "V2"
        for el in root.iter():
            ln = _localname(el.tag)
            attrs = {_localname(k): v for k, v in el.attrib.items()}
            if ln == "serviceDefinition" and attrs.get("name"):
                sd_name = attrs["name"]
            if ln == "binding" and attrs.get("version"):
                version = attrs["version"]
        if not sd_name:
            return None
        return sd_name, version
