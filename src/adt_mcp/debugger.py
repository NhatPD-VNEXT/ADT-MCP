"""The ABAP debugger protocol over ADT REST, as spoken by SAP cloud tenants.

Verified against an S/4HANA Cloud tenant: ``/sap/bc/adt/discovery`` advertises
the full debugger tree (listeners, breakpoints, stack, variables, actions,
watchpoints), and ``/sap/bc/adt/compatibility/graph`` declares
``COM.SAP.ADT.DEBUGGER :: userRequestDebugging`` — the user-scoped external
debugging this module drives — along with ``detach``, ``dynamicBreakpoints``
and ``runToLineAndJumpToLine``.

Every call after the listener must be ``stateful=True``. Without it the server
has no debug session to talk about and answers 404 ``noSessionAttached``.
"""
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

from .adt_client import object_root_path
from .debug_pool import DISCOVERY, DebugError, DebugSession

ADT = "/sap/bc/adt"
BASE = f"{ADT}/debugger"
BREAKPOINTS = f"{BASE}/breakpoints"
LISTENERS = f"{BASE}/listeners"
STACK = f"{BASE}/stack"
ACTIONS = f"{BASE}/actions"
CLASSRUN = f"{ADT}/oo/classrun"

# Identifies this server to SAP. Must differ from Eclipse's so our breakpoints
# and the ones in your IDE cannot be mistaken for each other.
IDE_ID = "adt-mcp"

# getVariables insists Accept matches Content-Type.
VARIABLES_CT = ("application/vnd.sap.as+xml;charset=UTF-8;"
                "dataname=com.sap.adt.debugger.Variables")

STEP_TYPES = ("stepInto", "stepOver", "stepReturn", "stepContinue",
              "stepRunToLine", "stepJumpToLine")

_START = re.compile(r"#start=(\d+)")


# --- tiny XML helpers -------------------------------------------------------

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(data: bytes):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _attrs(el) -> dict:
    return {_localname(k): v for k, v in el.attrib.items()}


def _iter_named(data: bytes, name: str):
    root = _parse(data)
    if root is None:
        return
    for el in root.iter():
        if _localname(el.tag) == name:
            yield el


def _fields(el) -> dict:
    """Child elements and attributes of a node, keyed upper-case.

    Both are read because the payload shape varies by release: some tenants put
    stack data in attributes, others in child elements.
    """
    out = {_localname(c.tag).upper(): (c.text or "").strip() for c in el}
    for k, v in _attrs(el).items():
        out.setdefault(k.upper(), v)
    return out


# --- identity ---------------------------------------------------------------

def terminal_id(user: str) -> str:
    """Deterministic terminal id for a user.

    It must be deterministic: SAP keys an external breakpoint on
    (user, terminalId, ideId). An id that changed per start would orphan every
    breakpoint set by the previous run — impossible to delete, still able to
    trap a real session.
    """
    digest = hashlib.sha1(user.strip().upper().encode("utf-8")).hexdigest()
    return f"adtmcp-{digest[:12]}"


def identity_params(user: str, scope: str = "") -> dict:
    """Who SAP should consider the debugging client.

    `scope` is only sent where SAP demands it — the breakpoint resource rejects
    a DELETE without it (400 ExceptionParameterNotFound), while the listener
    resource does not take it at all.
    """
    params = {"debuggingMode": "user", "requestUser": user.upper(),
              "terminalId": terminal_id(user), "ideId": IDE_ID}
    if scope:
        params["scope"] = scope
    return params


# --- source helpers ---------------------------------------------------------

def locate_statement(source: str, needle: str, occurrence: int = 1) -> int:
    """1-based line of the nth line containing `needle`, comments skipped.

    Comment lines are skipped because a breakpoint only sticks on an executable
    statement — landing on a comment is the most common way to get a breakpoint
    that SAP silently refuses.
    """
    want = (needle or "").strip().lower()
    if not want:
        raise ValueError("statement is empty")
    hits = 0
    for i, line in enumerate(source.split("\n"), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith('"'):
            continue
        if want in stripped.lower():
            hits += 1
            if hits >= max(1, occurrence):
                return i
    raise ValueError(f"no executable line contains {needle!r} "
                     f"(occurrence {occurrence})")


def breakpoint_uri(object_type: str, name: str, line: int,
                   function_group: str | None = None) -> str:
    return f"{object_root_path(object_type, name, function_group)}/source/main" \
           f"#start={int(line)}"


# --- breakpoints ------------------------------------------------------------

def _breakpoint_element(bp: dict) -> str:
    kind = (bp.get("kind") or "line").strip().lower()
    if kind == "line":
        target = (bp.get("uri") or "").strip()
        if not target:
            raise ValueError("breakpoint kind='line' needs 'uri'")
        out = (f'<breakpoint kind="line" enabled="true" '
               f'adtcore:uri={quoteattr(target)}')
        condition = (bp.get("condition") or "").strip()
        if condition:
            out += f" condition={quoteattr(condition)}"
        return out + "/>"
    if kind == "exception":
        cls = (bp.get("exception") or "").strip()
        if not cls:
            raise ValueError("breakpoint kind='exception' needs 'exception'")
        return (f'<breakpoint kind="exception" enabled="true" '
                f'exceptionClass={quoteattr(cls.upper())}/>')
    if kind == "statement":
        stmt = (bp.get("statement") or "").strip()
        if not stmt:
            raise ValueError("breakpoint kind='statement' needs 'statement'")
        return (f'<breakpoint kind="statement" enabled="true" '
                f'statement={quoteattr(stmt.upper())}/>')
    raise ValueError(f"unsupported kind {kind!r} (line/exception/statement)")


def build_breakpoints_body(breakpoints: list[dict], user: str) -> str:
    inner = "".join(_breakpoint_element(bp) for bp in breakpoints)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<dbg:breakpoints xmlns:dbg="http://www.sap.com/adt/debugger" '
            'xmlns:adtcore="http://www.sap.com/adt/core" '
            'debuggingMode="user" scope="external" '
            f'requestUser={quoteattr(user.upper())} '
            f'terminalId={quoteattr(terminal_id(user))} '
            f'ideId={quoteattr(IDE_ID)}>{inner}</dbg:breakpoints>')


def parse_breakpoints(data: bytes) -> list[dict]:
    out = []
    for el in _iter_named(data, "breakpoint"):
        a = _attrs(el)
        out.append({"id": a.get("id", ""), "uri": a.get("uri", ""),
                    "kind": a.get("kind", ""),
                    "error": a.get("errorMessage", "")})
    return out


def set_breakpoints(session: DebugSession, breakpoints: list[dict],
                    user: str) -> list[dict]:
    """Set external breakpoints. Raises if SAP rejected any of them.

    SAP answers HTTP 200 even when it refused: the reason sits in an
    ``errorMessage`` attribute. This is the one place where the body has to be
    read to know whether the call worked.
    """
    if not breakpoints:
        raise ValueError("need at least one breakpoint")
    body = build_breakpoints_body(breakpoints, user)
    resp = session.post(BREAKPOINTS, body=body, content_type="application/xml",
                        action="set breakpoint")
    rows = parse_breakpoints(resp.content)
    failed = [r["error"] for r in rows if r["error"]]
    if failed:
        raise DebugError(resp.status_code, "; ".join(failed),
                         session.system.name, "set breakpoint")
    if not rows:
        raise DebugError(resp.status_code,
                         "SAP accepted the request but returned no breakpoint "
                         "— is the line an executable statement?",
                         session.system.name, "set breakpoint")
    return rows


def clear_breakpoints(session: DebugSession, user: str) -> None:
    """Ask SAP to hold no breakpoints for this (user, terminalId, ideId).

    Discovery advertises this resource under a *synchronise* relation, which
    means the body replaces the whole set rather than adding to it — so an
    empty body should clear ours, and only ours: Eclipse's breakpoints carry a
    different ideId.

    Measured: the tenant answers 200. NOT measured: that the breakpoints are
    really gone afterwards — the GET on this resource returns an empty body on
    this release, and DELETE answers 200 for ids that no longer exist, so
    neither can confirm it. Treat this as a safety net stacked on top of the
    per-id deletes, never as the only cleanup.
    """
    session.post(BREAKPOINTS, body=build_breakpoints_body([], user),
                 content_type="application/xml",
                 action="clear breakpoints")


def delete_breakpoint(session: DebugSession, breakpoint_id: str,
                      user: str) -> None:
    ident = (breakpoint_id or "").strip()
    if not ident:
        raise ValueError("missing breakpoint id")
    path = f"{BREAKPOINTS}/{urllib.parse.quote(ident, safe='')}"
    session.request("DELETE", path,
                    params=identity_params(user, scope="external"),
                    action=f"delete breakpoint {ident[:40]}")


# --- listener ---------------------------------------------------------------

def parse_debuggee(data: bytes) -> dict | None:
    """The debuggee that got caught, or None when the listener timed out."""
    if not data.strip():
        return None
    root = _parse(data)
    if root is None:
        return None
    f = {}
    for el in root.iter():
        if len(el) == 0 and el.text is not None:
            f[_localname(el.tag).upper()] = el.text.strip()
    ident = f.get("DEBUGGEE_ID", "")
    if not ident:
        return None
    target = f.get("URI", "")
    # The line comes from the URI: LINE_CURR is an include coordinate, not the
    # line of the source you are reading.
    m = _START.search(target)
    return {"id": ident,
            "user": f.get("DEBUGGEE_USER", ""),
            "name": f.get("NAME", ""),
            "type": f.get("TYPE", ""),
            "uri": target,
            "line": int(m.group(1)) if m else 0,
            "program": f.get("PRG_CURR", ""),
            "attachable": f.get("IS_ATTACH_IMPOSSIBLE", "false").lower() != "true"}


def listen(session: DebugSession, user: str, seconds: int = 30) -> dict | None:
    """Wait for a debuggee to stop at a breakpoint. None on timeout.

    Long poll: SAP holds the request for up to `seconds` and then answers with
    an empty body. The HTTP timeout has to be wider or httpx cuts it off first.
    """
    params = dict(identity_params(user), timeout=str(seconds))
    resp = session.post(LISTENERS, accept="application/vnd.sap.as+xml",
                        stateful=True, params=params,
                        action="wait for a debuggee", timeout=seconds + 30)
    return parse_debuggee(resp.content)


def stop_listener(session: DebugSession, user: str) -> None:
    """Deregister the listener server-side (capability `listenerDeactivation`).

    Best effort: dropping the local thread is what really stops us listening.
    """
    try:
        session.request("DELETE", LISTENERS, params=identity_params(user),
                        action="stop listener", check=False)
    except DebugError:
        pass


# --- attached session -------------------------------------------------------

def attach(session: DebugSession, debuggee_id: str, user: str) -> None:
    """Attach to a caught debuggee. Every later call must use this session."""
    ident = (debuggee_id or "").strip()
    if not ident:
        raise ValueError("missing debuggee_id")
    session.post(BASE, stateful=True,
                 params={"method": "attach", "debuggeeId": ident,
                         "dynproDebugging": "true", "debuggingMode": "user",
                         "requestUser": user.upper()},
                 action=f"attach to debuggee {ident[:16]}")


def get_stack(session: DebugSession) -> list[dict]:
    """The current call stack. Line numbers come from the URI, not from LINE."""
    resp = session.get(STACK, stateful=True,
                       params={"method": "getStack", "emode": "_",
                               "semanticURIs": "true"},
                       action="read the call stack")
    root = _parse(resp.content)
    if root is None:
        return []
    out = []
    for el in root.iter():
        if _localname(el.tag).upper().replace("_", "") != "STACKENTRY":
            continue
        f = _fields(el)
        target = f.get("URI", "")
        m = _START.search(target)
        out.append({"program": f.get("PROGRAM") or f.get("PROGRAMNAME", ""),
                    "include": f.get("INCLUDE") or f.get("INCLUDENAME", ""),
                    "line": int(m.group(1)) if m else int(f.get("LINE") or 0),
                    "event": f.get("EVENT") or f.get("EVENTNAME", ""),
                    "event_type": f.get("EVENTTYPE", ""),
                    "uri": target})
    return out


def _variables_body(ids: list[str]) -> str:
    inner = "".join(f"<STPDA_ADT_VARIABLE><ID>{quoteattr(i)[1:-1]}</ID>"
                    f"</STPDA_ADT_VARIABLE>" for i in ids)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<asx:abap xmlns:asx="http://www.sap.com/abapxml" version="1.0">'
            f'<asx:values><DATA>{inner}</DATA></asx:values></asx:abap>')


def _parse_variables(data: bytes) -> list[dict]:
    root = _parse(data)
    if root is None:
        return []
    out = []
    for el in root.iter():
        if _localname(el.tag).upper() != "STPDA_ADT_VARIABLE":
            continue
        f = {_localname(c.tag).upper(): (c.text or "").strip() for c in el}
        if not f.get("NAME"):
            continue
        out.append({"name": f.get("NAME", ""), "value": f.get("VALUE", ""),
                    # Newer releases send DECLARED_TYPE_NAME; DECLARED_TYPE is
                    # the older spelling, kept as a fallback.
                    "type": (f.get("DECLARED_TYPE_NAME")
                             or f.get("DECLARED_TYPE", "")),
                    "meta": f.get("META_TYPE", ""),
                    "id": f.get("ID", "")})
    return out


def get_variables(session: DebugSession, names: list[str]) -> list[dict]:
    """Values of named variables at the current stop."""
    wanted = [n.strip() for n in (names or []) if n and n.strip()]
    if not wanted:
        raise ValueError("need at least one variable name, e.g. ['LV_TOTAL']")
    resp = session.post(BASE, stateful=True, params={"method": "getVariables"},
                        body=_variables_body(wanted),
                        content_type=VARIABLES_CT, accept=VARIABLES_CT,
                        action="read variables")
    return _parse_variables(resp.content)


# There is deliberately no "list the variables in scope" call here.
# getChildVariables was tried and measured on the tenant: every parent id
# (@ROOT, @LOCALS, @GLOBALS, ME, …) comes back with the same single entry, ME —
# the server ignores the parent this payload asks for. Rather than ship a tool
# whose name promises enumeration and always answers "ME", name the variables
# you want: read the source first, then call get_variables.


def _no_session(resp) -> bool:
    return b"noSessionAttached" in (resp.content or b"")


def step(session: DebugSession, step_type: str, target_uri: str = "") -> None:
    """Take one step in the attached session.

    Two dialects exist. The older ``/debugger?method=stepOver`` is tried first
    because it is what on-prem systems answer to; cloud discovery advertises
    ``/debugger/actions?action=…`` instead, so that is the fallback. Whichever
    the tenant speaks, one of the two lands.
    """
    kind = (step_type or "").strip()
    if kind not in STEP_TYPES:
        raise ValueError(f"invalid step_type {kind!r} "
                         f"(valid: {', '.join(STEP_TYPES)})")
    needs_uri = kind in ("stepRunToLine", "stepJumpToLine")
    if needs_uri and not (target_uri or "").strip():
        raise ValueError(f"{kind} requires a target uri")

    params = {"method": kind}
    if needs_uri:
        params["uri"] = target_uri.strip()
    resp = session.post(BASE, stateful=True, params=params,
                        action=f"debug {kind}", check=False)
    if 200 <= resp.status_code < 300:
        return
    # A missing debug session is a real answer, not a dialect mismatch — do not
    # burn a second call pretending otherwise.
    if not _no_session(resp) and resp.status_code in (400, 404, 405, 501):
        alt = {"action": kind}
        if needs_uri:
            alt["value"] = target_uri.strip()
        resp = session.post(ACTIONS, stateful=True, params=alt,
                            action=f"debug {kind}", check=False)
    session.check(resp, f"debug {kind}")


def is_attached(session: DebugSession) -> bool:
    """True while the debug session is alive.

    Asking the stack is the only reliable test: the detach paths do not report
    success through their status code, but a dead session answers 404.
    """
    try:
        resp = session.get(STACK, stateful=True,
                           params={"method": "getStack", "emode": "_"},
                           action="check the debug session", check=False)
    except DebugError:
        return False
    return resp.status_code == 200


def detach(session: DebugSession) -> bool:
    """Release the debuggee so it runs to completion. Never raises.

    The tenant declares a real ``detach`` capability, so that is tried first.
    The fallback is the blunt instrument that on-prem systems need: **ending
    the stateful session**, which SAP treats as "debugger gone, carry on". Any
    stateless request on this cookie does it; discovery is the cheapest.

    Called from `finally` blocks and at shutdown — an abandoned debuggee holds
    one of the tenant's work processes hostage, so failing loudly here would
    make things worse, not better.
    """
    try:
        resp = session.post(BASE, stateful=True, params={"method": "detach"},
                            action="detach", check=False)
        if 200 <= resp.status_code < 300 and not is_attached(session):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        # stateful=False IS the act of killing the session, not an incidental
        # detail of this call.
        session.get(DISCOVERY, stateful=False, action="end the debug session",
                    check=False)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- running code -----------------------------------------------------------

_MISSING = re.compile(r"^Object \S+ of type \w+ does not exist\.")
_FAILURE_PREFIXES = ("Error:", "ABEND")


def _looks_like_failure(text: str) -> bool:
    """classrun answers 200 even when it failed; only the first line tells.

    Substring-scanning the whole body would be wrong: 'Error:' and 'Exception'
    appear all over healthy ABAP console output.
    """
    first = (text.splitlines() or [""])[0].strip()
    return first.startswith(_FAILURE_PREFIXES) or bool(_MISSING.match(first))


def run_class(session: DebugSession, name: str,
              timeout: float | None = None) -> str:
    """Run a class implementing IF_OO_ADT_CLASSRUN; return its console output.

    This is Eclipse's F9. There is no REST endpoint for classic reports with a
    selection screen, and ABAP Cloud has no such reports anyway.
    """
    clean = (name or "").strip()
    if not clean or not re.fullmatch(r"[A-Za-z0-9_/]+", clean):
        raise ValueError(f"invalid class name {name!r}")
    # Accept MUST be text/plain: classrun returns plain console text and answers
    # 406 to the application/xml default.
    resp = session.post(f"{CLASSRUN}/{clean.lower()}", accept="text/plain",
                        timeout=timeout, action=f"run class {clean.upper()}")
    text = resp.text.strip()
    if _looks_like_failure(text):
        raise DebugError(resp.status_code, text[:400], session.system.name,
                         f"run class {clean.upper()}")
    return text


# --- formatting -------------------------------------------------------------

def format_breakpoints(rows: list[dict]) -> str:
    if not rows:
        return "(no breakpoint returned)"
    return "\n".join(
        f"OK: breakpoint set at {r['uri']}\n     id: {r['id']}" for r in rows)


def format_stack(rows: list[dict]) -> str:
    if not rows:
        return "(empty call stack)"
    out = []
    for i, r in enumerate(rows):
        where = r["include"] or r["program"]
        event = f"  {r['event_type']} {r['event']}".rstrip()
        out.append(f"{i:>2}  {where}:{r['line']}{event}")
    return "\n".join(out)


def format_variables(rows: list[dict]) -> str:
    if not rows:
        return "(no variable returned — is the name in scope at this line?)"
    out = []
    for r in rows:
        kind = f"  [{r['type']}]" if r["type"] else ""
        out.append(f"{r['name']} = {r['value']}{kind}")
    return "\n".join(out)


def format_state(state: dict) -> str:
    lines = [f"state: {state['state']}"]
    if state.get("message"):
        lines.append(f"message: {state['message']}")
    d = state.get("debuggee")
    if d:
        lines.append(f"debuggee: {d['name']} ({d['type']}) "
                     f"line {d['line']} — user {d['user']}")
        lines.append(f"uri: {d['uri']}")
        if not d.get("attachable", True):
            lines.append("warning: SAP says this debuggee cannot be attached")
    if state.get("running"):
        lines.append("a background run is still in flight")
    if state.get("output"):
        lines.append(f"\nrun output:\n{state['output']}")
    return "\n".join(lines)
