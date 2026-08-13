"""Isolated ABAP sessions for the debugger — one per (system, channel).

Why this file exists at all: adt-mcp normally runs every system and every tool
through ONE shared ``httpx.Client`` (see ``__main__.py``). That is fine for
stateless reads and for the short LOCK→PUT→UNLOCK write sequence, but it cannot
host a debug session. A debug session must stay stateful across many calls, and
**any request carrying ``X-sap-adt-sessiontype: stateless`` on the same ABAP
session silently kills it** — SAP reports nothing, the next call simply 404s.

On this platform the ABAP security session *is* the ``SAP_SESSIONID`` cookie,
so "a separate session" means "a separate login". Measured on the S/4HANA Cloud
tenant this was built against: every fresh login mints a different
``SAP_SESSIONID``, so channels really are independent. That is the cloud-specific
price — the on-prem sibling gets isolation for free, because basic auth makes
the server hand out a session per client.

Channels:

* ``DEBUG`` — listener plus the attached debug session. Stateful, held for
  minutes at a time. Gets its own login.
* ``EXEC`` — runs the code being debugged. Own login, because SAP holds the
  HTTP request that is stopped at a breakpoint: sharing this channel would
  freeze whatever else was using that session until the debuggee is released.
* ``CONTROL`` — set/delete breakpoints. These are stateless calls, and a
  breakpoint is keyed on (user, terminalId, ideId) rather than on a session, so
  this channel deliberately reuses the system's normal cookie file. No extra
  login, and nothing it does can disturb ``DEBUG``.
"""
import json
import os
import threading
from urllib.parse import urlsplit

import httpx

from .adt_client import (base_url, is_login_page, parse_adt_exception,
                         parse_netscape_cookies)
from .paths import cookies_dir
from .registry import System

DEBUG = ""
EXEC = "exec"
CONTROL = "control"

DISCOVERY = "/sap/bc/adt/discovery"
SYSTEM_INFO = "/sap/bc/adt/core/http/systeminformation"
SYSTEM_INFO_CT = "application/vnd.sap.adt.core.http.systeminformation.v1+json"

# Connect fast-fails so a dead host is obvious; read/write follow the caller's
# timeout because a debuggee stopped at a breakpoint holds its request open.
_CONNECT_TIMEOUT = 10.0


class DebugError(Exception):
    """An ADT call the debugger made failed. Carries enough to explain itself."""

    def __init__(self, status: int, message: str, system: str, action: str):
        self.status = status
        self.message = message
        self.system = system
        self.action = action
        where = f"HTTP {status}" if status else "no response"
        super().__init__(f"Error: {action} failed on system {system!r} "
                         f"({where}): {message}")


def explain(status: int, content: bytes) -> str:
    """Human-readable reason out of an ADT error body."""
    exc_type, msg = parse_adt_exception(content)
    if msg:
        return f"{msg} [{exc_type}]" if exc_type else msg
    text = (content or b"").decode("utf-8", "replace").strip()
    return text[:300] or f"HTTP {status}"


def read_cookies(system: System, cookie_file: str | None) -> dict:
    """Session cookies for a system, from an explicit file or its config."""
    if system.cookie_string and not cookie_file:
        out = {}
        for part in system.cookie_string.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    path = cookie_file or system.cookie_file
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return parse_netscape_cookies(f.read())
    return {}


def key(name: str, channel: str = DEBUG) -> str:
    """Lookup key of a (system, channel) pair."""
    return f"{name}#{channel}" if channel else name


def channel_cookie_file(system: System, channel: str) -> str | None:
    """Where a channel's cookies live.

    CONTROL shares the system's own cookie file on purpose (see module docs);
    the other channels each need their own SAP_SESSIONID, hence their own file.
    """
    if channel == CONTROL or system.auth != "cookie":
        return system.cookie_file
    return os.path.join(cookies_dir(), f"{system.name}.{channel or 'debug'}.txt")


class DebugSession:
    """One ADT session on one channel of one system.

    Not safe for concurrent use — ``DebugSessionPool.lock()`` serialises access.

    ``stateful`` is deliberately tri-state on every call:

    * ``True``  → send ``stateful``: keeps/creates the debug session.
    * ``None``  → send no session-type header at all (the safe default). SAP
      leaves an existing stateful session alone.
    * ``False`` → send ``stateless``, which **ends** any stateful session on this
      cookie. That is destructive, so it is never the default; only the detach
      fallback asks for it on purpose.
    """

    def __init__(self, system: System, cookie_file: str | None = None,
                 timeout: float = 60.0):
        self.system = system
        self.base = base_url(system.url)
        kwargs = {}
        if system.auth != "cookie":
            kwargs["auth"] = httpx.BasicAuth(system.username or "",
                                             system.password or "")
        self._client = httpx.Client(
            base_url=self.base,
            timeout=httpx.Timeout(connect=min(_CONNECT_TIMEOUT, timeout),
                                  read=timeout, write=timeout, pool=timeout),
            # Never follow redirects: an expired cookie answers 302 to the IdP,
            # and following it would turn a dead session into a cheerful 200
            # carrying a login page. Let the 302 surface as the error it is.
            follow_redirects=False,
            **kwargs)
        if system.auth == "cookie":
            host = urlsplit(self.base).hostname or ""
            for k, v in read_cookies(system, cookie_file).items():
                self._client.cookies.set(k, v, domain=host)
        self._token = ""
        self._token_lock = threading.Lock()

    # --- CSRF ---

    def _fetch_token(self, action: str) -> str:
        """Fetch a CSRF token. Call while holding _token_lock.

        Note it goes out with no session-type header: sending ``stateless`` here
        would kill the very debug session the following POST belongs to.
        """
        try:
            resp = self._client.get(
                DISCOVERY, params=self._params(None),
                headers={"X-CSRF-Token": "fetch",
                         "Accept": "application/atomsvc+xml"})
        except httpx.HTTPError as e:
            raise DebugError(0, f"could not fetch CSRF token: {e}",
                             self.system.name, action) from e
        self._token = resp.headers.get("x-csrf-token", "")
        return self._token

    def _params(self, extra: dict | None) -> dict:
        params = {"sap-client": self.system.client,
                  "sap-language": self.system.language}
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _headers(self, accept: str, content_type: str | None,
                 stateful: bool | None, token: str | None) -> dict:
        headers = {"Accept": accept}
        if stateful is not None:
            headers["X-sap-adt-sessiontype"] = ("stateful" if stateful
                                                else "stateless")
        if content_type:
            headers["Content-Type"] = content_type
        if token:
            headers["X-CSRF-Token"] = token
        return headers

    # --- request ---

    def request(self, method: str, path: str, *, accept: str = "*/*",
                body: str | bytes | None = None,
                content_type: str | None = None,
                stateful: bool | None = None, params: dict | None = None,
                action: str = "", check: bool = True,
                timeout: float | None = None) -> httpx.Response:
        needs_token = method.upper() not in ("GET", "HEAD")
        with self._token_lock:
            token = (self._token or self._fetch_token(action)) if needs_token else None
        content = body.encode("utf-8") if isinstance(body, str) else body

        def send(tok):
            kw = {"timeout": timeout} if timeout is not None else {}
            return self._client.request(
                method, path, params=self._params(params),
                headers=self._headers(accept, content_type, stateful, tok),
                content=content, **kw)

        try:
            resp = send(token)
            if needs_token and resp.status_code == 403 and \
                    resp.headers.get("x-csrf-token", "").lower() == "required":
                with self._token_lock:
                    self._token = ""
                    fresh = self._fetch_token(action)
                resp = send(fresh)
        except httpx.HTTPError as e:
            raise DebugError(0, str(e), self.system.name, action) from e

        if check:
            self.check(resp, action)
        return resp

    def check(self, resp: httpx.Response, action: str) -> None:
        """Raise unless the response is a success. Names session expiry."""
        if resp.status_code in (301, 302, 303, 307, 308) or \
                (resp.status_code == 200 and is_login_page(resp)):
            raise DebugError(resp.status_code,
                             "session expired (redirected to the IdP login) — "
                             "run refresh_cookies_for and start over",
                             self.system.name, action)
        if resp.status_code in (401, 403):
            raise DebugError(resp.status_code,
                             "not authorised — cookie expired, or this user "
                             "lacks the ABAP debug authorisation",
                             self.system.name, action)
        if not 200 <= resp.status_code < 300:
            raise DebugError(resp.status_code, explain(resp.status_code,
                                                       resp.content),
                             self.system.name, action)

    def get(self, path: str, **kw) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> httpx.Response:
        kw.setdefault("accept", "application/xml")
        return self.request("POST", path, **kw)

    def delete(self, path: str, **kw) -> httpx.Response:
        return self.request("DELETE", path, **kw)

    def system_info(self) -> dict | None:
        """{systemID, userName, userFullName, client, language} or None.

        Doubles as the liveness probe for a channel: a dead cookie cannot
        produce this, it produces a login page.
        """
        try:
            resp = self._client.get(SYSTEM_INFO, params=self._params(None),
                                    headers={"Accept": SYSTEM_INFO_CT})
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or is_login_page(resp):
            return None
        try:
            info = json.loads(resp.text)
        except ValueError:
            return None
        return info if isinstance(info, dict) and info.get("userName") else None

    def close(self) -> None:
        self._client.close()


class DebugSessionPool:
    """One DebugSession per (system, channel), each with its own lock.

    Sessions are built lazily, and a cookie channel that has no live session
    logs in to mint one. Logins are serialised: they drive a real browser
    profile, which is not safe to run twice at once.
    """

    def __init__(self, login=None):
        # Injectable for tests; defaults to the real Playwright logins.
        self._login = login or _default_login
        self._sessions: dict[str, DebugSession] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._info: dict[str, dict] = {}
        self._guard = threading.Lock()
        self._create_lock = threading.RLock()

    # --- sessions ---

    def _build(self, system: System, channel: str) -> DebugSession:
        path = channel_cookie_file(system, channel)
        timeout = float(getattr(system, "debug_timeout", 600) or 600)
        session = DebugSession(system, cookie_file=path, timeout=timeout)
        if system.auth != "cookie":
            return session
        info = session.system_info()
        if info is not None:
            self._remember(system, info)
            return session
        # No live session on this channel — mint one. CONTROL shares the
        # system's own cookie file, so refreshing it here also un-sticks the
        # normal tools; that is intended, not a side effect to avoid.
        session.close()
        failure = self._login(system, path)
        if failure:
            raise DebugError(0, failure, system.name,
                             f"open debug channel {channel or 'debug'!r}")
        session = DebugSession(system, cookie_file=path, timeout=timeout)
        info = session.system_info()
        if info is None:
            session.close()
            raise DebugError(0, "logged in but the session is still not usable",
                             system.name,
                             f"open debug channel {channel or 'debug'!r}")
        self._remember(system, info)
        return session

    def _remember(self, system: System, info: dict) -> None:
        with self._guard:
            self._info[system.name] = info

    def session(self, system: System, channel: str = DEBUG) -> DebugSession:
        name = key(system.name, channel)
        with self._guard:
            existing = self._sessions.get(name)
        if existing is not None:
            return existing
        with self._create_lock:
            with self._guard:
                existing = self._sessions.get(name)
            if existing is not None:
                return existing
            session = self._build(system, channel)
            with self._guard:
                self._sessions[name] = session
            return session

    def get_existing(self, name: str, channel: str = DEBUG) -> DebugSession | None:
        """The live session of a (system, channel), or None. Never builds one —
        used at shutdown, where minting a login would be absurd."""
        with self._guard:
            return self._sessions.get(key(name, channel))

    def invalidate(self, name: str, channel: str = DEBUG) -> None:
        with self._guard:
            session = self._sessions.pop(key(name, channel), None)
        if session:
            session.close()

    # --- identity ---

    def identity(self, system: System) -> dict:
        """(user, terminalId, ideId) this server debugs as.

        The ABAP user has to come from the tenant. ``System.username`` holds the
        IAS login (an e-mail address on cloud), and an external breakpoint is
        keyed on the ABAP user — using the e-mail sets breakpoints that simply
        never fire.
        """
        with self._guard:
            info = self._info.get(system.name)
        if info is None:
            self.session(system, CONTROL)
            with self._guard:
                info = self._info.get(system.name)
        if info is None:
            raise DebugError(0, "could not determine the ABAP user",
                             system.name, "identify debug user")
        return info

    # --- locks ---

    def lock_by_name(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def lock(self, system: System, channel: str = DEBUG) -> threading.Lock:
        """Serialise access to one channel of one system; other channels and
        other systems are unaffected."""
        return self.lock_by_name(key(system.name, channel))

    def close_all(self) -> None:
        with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def _default_login(system: System, cookie_file: str | None) -> str:
    """Mint a fresh SAP_SESSIONID into cookie_file. Returns "" or an error."""
    if system.auth != "cookie":
        return ""
    if not cookie_file:
        return (f"system {system.name!r} has no cookie_file; the debugger needs "
                f"one per channel")
    from .cookie_refresh import interactive_login, refresh_cookies
    if system.username and system.password:
        result = refresh_cookies(system.url, system.username, system.password,
                                 cookie_file)
        if result.startswith("OK"):
            return ""
    result = interactive_login(system.url, cookie_file)
    return "" if result.startswith("OK") else result
