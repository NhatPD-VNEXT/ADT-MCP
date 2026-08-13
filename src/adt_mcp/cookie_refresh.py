"""Refresh SAP session cookies via headless SAML/IAS login (Playwright).

Adapted from the project's standalone getcookie.py. Performs a browser login
with username/password against the system's IdP, then writes the session
cookies the ADT client needs to a Netscape cookie file.

Playwright is an optional dependency:
    pip install playwright
    python -m playwright install chromium
"""
import os
import re
import time
from pathlib import Path

from .paths import cookies_dir

DEFAULT_TIMEOUT_MS = 30000

# Per-platform: which cookies actually carry the ADT session.
COOKIE_TARGETS = {
    "abap_btp": [
        ("__VCAP_ID__", lambda n: n == "__VCAP_ID__"),
        ("JSESSIONID", lambda n: n == "JSESSIONID"),
    ],
    "s4hana_cloud": [
        ("SAP_SESSIONID_<SYS>_<CLI>",
         lambda n: re.fullmatch(r"SAP_SESSIONID_[A-Z0-9]+_\d+", n) is not None),
        ("sap-usercontext", lambda n: n == "sap-usercontext"),
    ],
}


def detect_mode(url: str) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if "s4hana.cloud.sap" in host or "sapbydesign" in host:
        return "s4hana_cloud"
    return "abap_btp"


def _fill_ias_form(page, username: str, password: str) -> None:
    user_selectors = [
        'input[name="j_username"]', 'input#j_username',
        'input[name="username"]', 'input[type="email"]',
        'input[autocomplete="username"]', 'input[type="text"]',
    ]
    pass_selectors = [
        'input[name="j_password"]', 'input#j_password',
        'input[name="password"]', 'input[type="password"]',
        'input[autocomplete="current-password"]',
    ]
    submit_selectors = [
        'button[type="submit"]', 'input[type="submit"]',
        'button#logOnFormSubmit',
        'button:has-text("Continue")', 'button:has-text("Sign in")',
        'button:has-text("Log On")',
    ]

    def first_visible(selectors, ctx=None):
        target = ctx if ctx is not None else page
        for sel in selectors:
            try:
                loc = target.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def scan(selectors):
        found = first_visible(selectors)
        if found:
            return found
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            found = first_visible(selectors, ctx=frame)
            if found:
                return found
        return None

    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass

    user_field = scan(user_selectors)
    if user_field is None:
        page.wait_for_timeout(3000)
        user_field = scan(user_selectors)
    if user_field is None:
        raise RuntimeError("Could not locate username field on login page.")
    user_field.fill(username)

    pass_field = scan(pass_selectors)
    if pass_field is None:
        btn = scan(submit_selectors)
        if btn is not None:
            btn.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
        pass_field = scan(pass_selectors)
    if pass_field is None:
        raise RuntimeError("Could not locate password field on login page.")
    pass_field.fill(password)

    btn = scan(submit_selectors)
    if btn is None:
        pass_field.press("Enter")
    else:
        btn.click()


def _wait_for_target_cookies(context, url: str, timeout_ms: int) -> list:
    targets = COOKIE_TARGETS[detect_mode(url)]
    target_host = url.split("//", 1)[1].split("/", 1)[0].lower()
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        cookies = context.cookies()
        found = sum(
            1 for _, pred in targets
            if any(pred(c.get("name", "")) and
                   target_host in (c.get("domain", "") or "").lower()
                   for c in cookies)
        )
        if found >= len(targets):
            return cookies
        time.sleep(0.3)
    return context.cookies()


def _write_netscape(path: Path, cookies: list, url: str) -> int:
    target_host = url.split("//", 1)[1].split("/", 1)[0].lower()
    mode = detect_mode(url)
    targets = COOKIE_TARGETS[mode]
    domain_field = f"https://{target_host}" if mode == "abap_btp" else target_host

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", ""]
    written = 0
    for _, pred in targets:
        matches = [c for c in cookies
                   if pred(c.get("name", "")) and
                   target_host in (c.get("domain", "") or "").lower()]
        if not matches:
            continue
        c = max(matches, key=lambda x: int(x.get("expires") or 0))
        secure = "TRUE" if c.get("secure") else "FALSE"
        lines.append("\t".join([domain_field, "FALSE", c.get("path", "/") or "/",
                                secure, "0", c.get("name", ""), c.get("value", "")]))
        written += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def _profile_dir() -> str:
    """Persistent browser profile so the SSO session is remembered across
    logins (next login goes straight through without retyping)."""
    d = Path(cookies_dir()) / ".browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _open_persistent(p, headless: bool, timeout_ms: int):
    """Launch the user's installed browser (Edge → Chrome → bundled Chromium)
    with a persistent profile. Returns the BrowserContext."""
    user_data_dir = _profile_dir()
    last_err = None
    # Prefer installed channels so it looks/behaves like the user's browser
    # and can use Windows integrated auth / saved passwords. Chrome first
    # (override with ADT_MCP_BROWSER=msedge|chrome|chromium).
    pref = os.environ.get("ADT_MCP_BROWSER", "").strip().lower()
    order = {"msedge": ("msedge",), "chrome": ("chrome",),
             "chromium": (None,)}.get(pref, ("chrome", "msedge", None))
    for channel in order:
        try:
            kwargs = {"user_data_dir": user_data_dir, "headless": headless,
                      "args": ["--no-first-run", "--no-default-browser-check"]}
            if channel:
                kwargs["channel"] = channel
            ctx = p.chromium.launch_persistent_context(**kwargs)
            ctx.set_default_timeout(timeout_ms)
            return ctx
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"could not launch any browser: {last_err}")


def interactive_login(url: str, cookie_file: str,
                      timeout_ms: int = 180000) -> str:
    """Open the user's installed browser at `url` with a remembered profile,
    let them log in to IAS (only needed the first time), then capture and
    write session cookies. No credentials are stored.

    Returns a human-readable result string. Requires Playwright + a
    Chromium-based browser (Edge/Chrome installed, or bundled chromium).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("Error: Playwright not installed — run "
                "`pip install playwright && python -m playwright install chromium`")

    target_host = url.split("//", 1)[1].split("/", 1)[0]
    try:
        with sync_playwright() as p:
            context = _open_persistent(p, headless=False, timeout_ms=timeout_ms)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url)
                # Wait until the ADT session cookies appear. If the persistent
                # profile is already authenticated, this returns immediately.
                cookies = _wait_for_target_cookies(context, url, timeout_ms)
            finally:
                context.close()
    except Exception as e:
        return f"Error: interactive login failed: {e}"

    n = _write_netscape(Path(cookie_file), cookies, url)
    if n == 0:
        return (f"Error: no session cookies captured for {target_host} "
                f"(login not completed in time?)")
    return f"OK: captured {n} session cookies"


def cdp_capture(url: str, cookie_file: str,
                cdp_url: str = "http://127.0.0.1:9222",
                timeout_ms: int = 60000) -> str:
    """Attach to YOUR already-running Chrome (started with
    --remote-debugging-port) and capture the live session cookies — uses the
    real browser you are logged into. No new window, no stored password.

    Start Chrome first, e.g.:
        chrome.exe --remote-debugging-port=9222
                   --user-data-dir="%LOCALAPPDATA%\\Google\\Chrome\\User Data"
    (Close all Chrome windows on that profile before launching with the flag.)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("Error: Playwright not installed — run "
                "`pip install playwright && python -m playwright install chromium`")

    cdp_url = cdp_url.replace("localhost", "127.0.0.1")
    target_host = url.split("//", 1)[1].split("/", 1)[0]
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return (f"Error: cannot attach to Chrome at {cdp_url} — start "
                        f"Chrome with --remote-debugging-port=9222 first ({e})")
            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(url)
                cookies = _wait_for_target_cookies(ctx, url, timeout_ms)
                try:
                    page.close()
                except Exception:
                    pass
            finally:
                browser.close()  # disconnects CDP, does not close your Chrome
    except Exception as e:
        return f"Error: CDP capture failed: {e}"

    n = _write_netscape(Path(cookie_file), cookies, url)
    if n == 0:
        return (f"Error: no session cookies for {target_host} in the attached "
                f"Chrome — are you logged into that system in this browser?")
    return f"OK: captured {n} session cookies"


def refresh_cookies(url: str, username: str, password: str, cookie_file: str,
                    headless: bool = True,
                    timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """Log in via SAML/IAS and write fresh session cookies to cookie_file.

    Returns a human-readable result string. Requires Playwright + chromium.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("Error: Playwright not installed — run "
                "`pip install playwright && python -m playwright install chromium`")

    target_host = url.split("//", 1)[1].split("/", 1)[0]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                page.goto(url)
                try:
                    page.wait_for_url(
                        lambda u: ("accounts" in u or "ias" in u
                                   or "login" in u.lower() or "saml" in u.lower()
                                   or target_host not in u),
                        timeout=timeout_ms)
                except Exception:
                    pass

                on_login = (target_host not in page.url
                            or "login" in page.url.lower()
                            or "saml" in page.url.lower())
                if on_login:
                    try:
                        idp = page.get_by_text(
                            "Default Identity Provider", exact=False).first
                        if idp.count() > 0 and idp.is_visible():
                            idp.click()
                            page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        pass
                    _fill_ias_form(page, username, password)
                    page.wait_for_url(lambda u: target_host in u,
                                      timeout=timeout_ms)

                cookies = _wait_for_target_cookies(context, url, timeout_ms)
            finally:
                browser.close()
    except Exception as e:
        return f"Error: login failed: {e}"

    n = _write_netscape(Path(cookie_file), cookies, url)
    if n == 0:
        return (f"Error: login completed but no session cookies captured "
                f"for {target_host} — check credentials/IdP")
    return f"OK: captured {n} session cookies"
