import httpx
from starlette.testclient import TestClient

from adt_mcp.adt_client import ADTClient, parse_activation
from adt_mcp.registry import System, SystemRegistry
from adt_mcp.server import SessionKeeper, build_server

# Activation log as ADT returns it: the text sits in <shortText><txt>.
ACT_ERROR = b'''<?xml version="1.0" encoding="utf-8"?>
<chkl:messages xmlns:chkl="http://www.sap.com/abapxml/checklist">
  <msg objDescr="Table ZT" type="E" line="65"
       href="/sap/bc/adt/ddic/tables/zt/source/main#start=65,32">
    <shortText><txt>LINENO is a reserved word</txt></shortText>
  </msg>
  <msg objDescr="Table ZT" type="W" line="0"
       href="/sap/bc/adt/ddic/tables/zt/source/main#start=6,3">
    <shortText><txt>Key must have the type Inverted Individual</txt></shortText>
  </msg>
</chkl:messages>'''

# Captured from VNEXT (2026-09-27). line="1"/"2" is the message index in the
# DDIC log; the source position is only in href.
ACT_REAL_DDIC = b'''<?xml version="1.0" encoding="utf-8"?><chkl:messages xmlns:chkl="http://www.sap.com/abapxml/checklist"><chkl:properties checkExecuted="true" activationExecuted="true" generationExecuted="false"/><msg objDescr="Database Table ZDIAGT1027A" type="E" line="1" href="/sap/bc/adt/ddic/tables/zdiagt1027a/source/main#start=6,13;end=6,24" code="DD_ABAP_LANG_VERS(013)"><shortText><txt>New ABAP Language version not allowed for object TABL ZDIAGT1027A</txt></shortText><t100Key msgid="DD_ABAP_LANG_VERS" msgno="013" msgv1="TABL" msgv2="ZDIAGT1027A"/></msg><msg objDescr="Database Table ZDIAGT1027A" type="E" line="2" href="/sap/bc/adt/ddic/tables/zdiagt1027a/source/main#start=6,13;end=6,24" code="D0(408)"><shortText><txt>TABL ZDIAGT1027A was not activated</txt></shortText></msg></chkl:messages>'''

CHECK_WARNING = b'''<chkrun:checkRunReports xmlns:chkrun="http://www.sap.com/adt/checkrun"><chkrun:checkReport><chkrun:checkMessageList><chkrun:checkMessage chkrun:uri="/sap/bc/adt/ddic/tables/zt/source/main#start=6,3" chkrun:type="W" chkrun:shortText="Key must be inverted individual"/></chkrun:checkMessageList></chkrun:checkReport></chkrun:checkRunReports>'''

ACT_WARNING = b'''<chkl:messages xmlns:chkl="http://www.sap.com/abapxml/checklist">
  <msg type="W" line="6"><shortText><txt>Key must be inverted</txt></shortText></msg>
</chkl:messages>'''

LOGIN_PAGE = httpx.Response(
    200, headers={"content-type": "text/html"},
    text='<html><form><input name="SAMLRequest"></form></html>')


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


def _write_handler(activation, check=b""):
    """Mock tenant: create/lock/put/unlock succeed, activation returns
    `activation` (bytes body or an httpx.Response), check-runs return
    `check`."""
    def handler(request):
        url = str(request.url)
        if "checkruns" in url:
            return httpx.Response(200, content=check)
        if request.method == "GET" and "discovery" in url:
            return httpx.Response(200, headers={"x-csrf-token": "T"})
        if "abaplanguageversions" in url:
            return httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(
                200, content=b'<r><adtcore:packageRef xmlns:adtcore="x" '
                             b'adtcore:name="ZPKG"/></r>')
        if "_action=LOCK" in url:
            return httpx.Response(
                200, content=b'<a><DATA><LOCK_HANDLE>LH</LOCK_HANDLE></DATA></a>')
        if "/activation" in url:
            if isinstance(activation, httpx.Response):
                return activation
            return httpx.Response(200, content=activation)
        return httpx.Response(200)
    return handler


def test_activation_error_carries_message_text_and_line():
    result = parse_activation(ACT_ERROR)
    assert result.startswith("Error: activation failed (1 error(s), 1 warning(s))")
    assert "E:65 [Table ZT]: LINENO is a reserved word" in result
    # line="0" falls back to the position in href
    assert "W:6 [Table ZT]: Key must have the type Inverted Individual" in result


def test_real_ddic_activation_log_uses_source_line_from_href():
    result = parse_activation(ACT_REAL_DDIC)
    assert result.startswith("Error: activation failed (2 error(s), 0 warning(s))")
    assert ("- E:6 [Database Table ZDIAGT1027A]: New ABAP Language version "
            "not allowed for object TABL ZDIAGT1027A") in result
    assert "E:1 " not in result and "E:2 " not in result


def test_update_source_reports_warnings_only_the_check_run_sees():
    result = _client(_write_handler(b"", check=CHECK_WARNING)).update_source(
        _system(), "TABL", "ZT", "define table zt {}")
    assert result.startswith("OK with 1 warning(s):")
    assert "W:6: Key must be inverted individual" in result


def test_update_source_clean_stays_plain_ok():
    result = _client(_write_handler(b"")).update_source(
        _system(), "TABL", "ZT", "define table zt {}")
    assert result == "OK"


def test_activation_warnings_are_reported_on_success():
    result = parse_activation(ACT_WARNING)
    assert result.startswith("OK with 1 warning(s):")
    assert "W:6: Key must be inverted" in result
    assert parse_activation(b"") == "OK"


def test_update_source_says_source_saved_but_inactive():
    result = _client(_write_handler(ACT_ERROR)).update_source(
        _system(), "TABL", "ZT", "define table zt {}")
    assert result.startswith("Error: TABL ZT source saved, but it is NOT active")
    assert "LINENO is a reserved word" in result


def test_update_source_returns_activation_warnings():
    result = _client(_write_handler(ACT_WARNING)).update_source(
        _system(), "TABL", "ZT", "define table zt {}")
    assert result.startswith("OK with 1 warning(s):")


def test_create_object_says_object_exists_when_activation_fails():
    result = _client(_write_handler(ACT_ERROR)).create_object(
        _system(), "TABL", "ZT", "ZPKG", "d", source="define table zt {}")
    assert result.startswith("Error: created TABL ZT in ZPKG")
    assert "EXISTS now" in result and "update_source" in result
    assert "NOT active" in result
    assert "LINENO is a reserved word" in result


def test_create_object_success_keeps_warnings():
    result = _client(_write_handler(ACT_WARNING)).create_object(
        _system(), "TABL", "ZT", "ZPKG", "d", source="define table zt {}")
    assert result.startswith("OK: created TABL ZT in ZPKG with 1 warning(s):")


def test_activate_on_login_page_is_session_expired_not_ok():
    result = _client(_write_handler(LOGIN_PAGE)).activate(_system(), "TABL", "ZT")
    assert result.startswith("Error: session expired")


def test_syntax_check_says_which_source_it_checked():
    check_ok = b'<chkrun:checkRunReports xmlns:chkrun="x"/>'

    def handler(request):
        url = str(request.url)
        if "discovery" in url:
            return httpx.Response(200, headers={"x-csrf-token": "T"})
        if "checkruns" in url:
            return httpx.Response(200, content=check_ok)
        seen.append(url)
        return httpx.Response(200, text="define table zt {}")

    seen = []
    adt = _client(handler)
    assert "checked the source passed in (1 lines)" in adt.syntax_check(
        _system(), "TABL", "ZT", source="define table zt {}")
    out = adt.syntax_check(_system(), "TABL", "ZT", version="inactive")
    assert "checked the stored inactive source" in out
    assert seen and "version=inactive" in seen[-1]


# --- SessionKeeper -------------------------------------------------------

class _FakeADT:
    def __init__(self, status="OK"):
        self.status = status
        self.probes = 0

    def test_connection(self, s):
        self.probes += 1
        return self.status


def _keeper(tmp_path, adt, refresh_result="OK: captured 2 session cookies"):
    registry = SystemRegistry(str(tmp_path / "systems.json"))
    registry.upsert(_system(auth="cookie", cookie_file="c.txt"))
    registry.upsert(_system(name="BASIC"))
    refreshes = []

    def refresh(s):
        refreshes.append(s.name)
        return refresh_result
    now = [1000.0]
    keeper = SessionKeeper(registry, adt, refresh=refresh, clock=lambda: now[0])
    return keeper, refreshes, now


def test_keeper_retries_once_after_refresh(tmp_path):
    keeper, refreshes, _ = _keeper(tmp_path, _FakeADT())
    answers = iter(["Error: session expired for system 'VNEXT'", "OK: data"])
    assert keeper.call("VNEXT", lambda: next(answers)) == "OK: data"
    assert refreshes == ["VNEXT"]


def test_keeper_probes_after_idle_and_refreshes_before_call(tmp_path):
    adt = _FakeADT()
    keeper, refreshes, now = _keeper(tmp_path, adt)
    keeper.call("VNEXT", lambda: "OK")
    assert adt.probes == 1              # first call: no history yet
    keeper.call("VNEXT", lambda: "OK")
    assert adt.probes == 1              # not idle: no probe
    now[0] += SessionKeeper.IDLE_SECONDS + 1
    adt.status = "Error: session expired for system 'VNEXT'"
    keeper.call("VNEXT", lambda: "OK")
    assert adt.probes == 2 and refreshes == ["VNEXT"]


def test_keeper_reports_failed_refresh_and_does_not_loop(tmp_path):
    keeper, refreshes, _ = _keeper(
        tmp_path, _FakeADT(), refresh_result="Error: login failed: timeout")
    result = keeper.call(
        "VNEXT", lambda: "Error: session expired for system 'VNEXT'")
    assert "auto refresh failed: Error: login failed: timeout" in result
    assert refreshes == ["VNEXT"]


def test_keeper_treats_rejected_csrf_as_expired_session(tmp_path):
    keeper, refreshes, _ = _keeper(tmp_path, _FakeADT())
    answers = iter(["Error: create failed (HTTP 403): CSRF Token Validation "
                    "Failed", "OK: created TABL ZT in ZPKG"])
    assert keeper.call("VNEXT", lambda: next(answers)).startswith("OK: created")
    assert refreshes == ["VNEXT"]


def test_keeper_leaves_basic_auth_systems_alone(tmp_path):
    adt = _FakeADT()
    keeper, refreshes, _ = _keeper(tmp_path, adt)
    keeper.call("BASIC", lambda: "Error: session expired")
    assert adt.probes == 0 and refreshes == []


def test_wrapped_tools_keep_their_schema(tmp_path):
    registry = SystemRegistry(str(tmp_path / "systems.json"))
    mcp = build_server(registry, ADTClient(httpx.Client()))
    headers = {"accept": "application/json, text/event-stream",
               "content-type": "application/json"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(mcp.streamable_http_app(),
                    base_url="http://127.0.0.1:8000") as client:
        text = client.post("/mcp", headers=headers, json=request).text
    assert '"syntax_check"' in text
    assert '"function_group"' in text and '"version"' in text
