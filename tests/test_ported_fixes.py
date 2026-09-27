import httpx
from starlette.testclient import TestClient

from adt_mcp.adt_client import ADTClient
from adt_mcp.registry import System, SystemRegistry
from adt_mcp.server import build_server


def _system(**overrides):
    values = dict(
        name="VNEXT", url="https://h.example", client="080", language="JA",
        auth="basic", username="u", password="p", cookie_file=None,
        cookie_string=None, allow_write=True, write_packages=None,
    )
    values.update(overrides)
    return System(**values)


def _client(handler):
    return ADTClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_registry_lookup_is_case_insensitive(tmp_path):
    registry = SystemRegistry(str(tmp_path / "systems.json"))
    registry.upsert(_system())
    assert registry.get("vnext").name == "VNEXT"


def test_streamable_http_ignores_stale_session_after_restart(tmp_path):
    registry = SystemRegistry(str(tmp_path / "systems.json"))
    registry.upsert(_system())
    mcp = build_server(registry, ADTClient(httpx.Client()))
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-session-id": "stale-session-from-previous-process",
    }
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    with TestClient(
            mcp.streamable_http_app(),
            base_url="http://127.0.0.1:8000") as client:
        response = client.post("/mcp", headers=headers, json=request)

    assert response.status_code == 200
    assert "Session not found" not in response.text


def test_create_uses_package_preferred_abap_language_version():
    seen = {"body": ""}
    versions = b'''<nameditem:namedItemList
        xmlns:nameditem="http://www.sap.com/adt/nameditem">
      <nameditem:namedItem><nameditem:name>standard</nameditem:name></nameditem:namedItem>
      <nameditem:namedItem><nameditem:name>cloudDevelopment</nameditem:name></nameditem:namedItem>
    </nameditem:namedItemList>'''

    def handler(request):
        url = str(request.url)
        if "abaplanguageversions" in url:
            return httpx.Response(200, content=versions)
        if "discovery" in url:
            return httpx.Response(200, headers={"x-csrf-token": "T"})
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(201)

    result = _client(handler).create_object(
        _system(), "CLAS", "ZCL_A", "$TMP", "description")
    assert result.startswith("OK: created CLAS ZCL_A")
    assert 'adtcore:abapLanguageVersion="standard"' in seen["body"]


def test_update_refetches_csrf_and_retries_put():
    state = {"fetches": 0, "puts": []}

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and "discovery" in url:
            state["fetches"] += 1
            return httpx.Response(
                200, headers={"x-csrf-token": f'T{state["fetches"]}'})
        if request.method == "GET":
            return httpx.Response(
                200, content=b'<r><adtcore:packageRef xmlns:adtcore="x" '
                             b'adtcore:name="ZPKG"/></r>')
        if "_action=LOCK" in url:
            return httpx.Response(
                200, content=b'<a><DATA><LOCK_HANDLE>LH</LOCK_HANDLE></DATA></a>')
        if request.method == "PUT":
            token = request.headers.get("x-csrf-token")
            state["puts"].append(token)
            if token == "T1":
                return httpx.Response(403, text="CSRF Token Validation Failed")
            return httpx.Response(200)
        if "_action=UNLOCK" in url:
            return httpx.Response(200)
        return httpx.Response(404)

    result = _client(handler).update_source(
        _system(), "CLAS", "ZCL_A", "x", activate=False)
    assert result.startswith("OK: updated")
    assert state["puts"] == ["T1", "T2"]


def test_update_refetches_csrf_and_retries_lock():
    state = {"fetches": 0, "locks": []}

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and "discovery" in url:
            state["fetches"] += 1
            return httpx.Response(
                200, headers={"x-csrf-token": f'T{state["fetches"]}'})
        if request.method == "GET":
            return httpx.Response(
                200, content=b'<r><adtcore:packageRef xmlns:adtcore="x" '
                             b'adtcore:name="ZPKG"/></r>')
        if "_action=LOCK" in url:
            token = request.headers.get("x-csrf-token")
            state["locks"].append(token)
            if token == "T1":
                return httpx.Response(403, text="CSRF Token Validation Failed")
            return httpx.Response(
                200, content=b'<a><DATA><LOCK_HANDLE>LH</LOCK_HANDLE></DATA></a>')
        if request.method == "PUT" or "_action=UNLOCK" in url:
            return httpx.Response(200)
        return httpx.Response(404)

    result = _client(handler).update_source(
        _system(), "CLAS", "ZCL_A", "x", activate=False)
    assert result.startswith("OK: updated")
    assert state["locks"] == ["T1", "T2"]


def test_write_keeps_cookie_rotated_during_csrf_fetch():
    seen = {"lock_cookie": ""}

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and "discovery" in url:
            return httpx.Response(
                200, headers={
                    "x-csrf-token": "T",
                    "set-cookie": "SAP_SESSIONID=FETCHED; Path=/",
                })
        if request.method == "GET":
            return httpx.Response(
                200, content=b'<r><adtcore:packageRef xmlns:adtcore="x" '
                             b'adtcore:name="ZPKG"/></r>')
        if "_action=LOCK" in url:
            seen["lock_cookie"] = request.headers.get("cookie", "")
            return httpx.Response(
                200, content=b'<a><DATA><LOCK_HANDLE>LH</LOCK_HANDLE></DATA></a>')
        if request.method == "PUT" or "_action=UNLOCK" in url:
            return httpx.Response(200)
        return httpx.Response(404)

    system = _system(
        auth="cookie", username=None, password=None,
        cookie_string="SAP_SESSIONID=ORIG")
    result = _client(handler).update_source(
        system, "CLAS", "ZCL_A", "x", activate=False)
    assert result.startswith("OK: updated")
    assert "FETCHED" in seen["lock_cookie"]
    assert "ORIG" not in seen["lock_cookie"]
