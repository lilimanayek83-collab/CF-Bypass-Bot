import glob
import logging
import os
import time

from botasaurus.browser import browser, Driver, Wait
import config

log = logging.getLogger("scraper")
STEP = {"name": "idle"}
DEBUG = {"shot": None}

# ---------------------------------------------------------------- JS snippets
# token only counts if its widget is actually visible (hidden login/report forms have their own inputs)
TOKEN_JS = """
for (const i of document.querySelectorAll('input[name*="turnstile"], input[name*="cf-chl"]')) {
  if (!i.value) continue;
  const p = i.parentElement;
  if (p && p.getBoundingClientRect().width > 50 && p.offsetParent !== null) return i.value;
}
return '';
"""

WIDGET_RECT_JS = """
for (const i of document.querySelectorAll('input[name*="turnstile"]')) {
  const p = i.parentElement;
  if (!p) continue;
  const b = p.getBoundingClientRect();
  if (b.width > 50 && b.height > 30 && p.offsetParent !== null) return {x: b.x, y: b.y, w: b.width, h: b.height};
}
const f = [...document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], .cf-turnstile')]
          .find(e => e.getBoundingClientRect().width > 50);
if (f) { const b = f.getBoundingClientRect(); return {x: b.x, y: b.y, w: b.width, h: b.height}; }
return null;
"""

DIAG_JS = """
return JSON.stringify({
  widget_visible: !!(function () {
    for (const i of document.querySelectorAll('input[name*="turnstile"]')) {
      const p = i.parentElement; if (p && p.offsetParent !== null && p.getBoundingClientRect().width > 50) return true;
    } return false; })(),
  login_form_visible: !!(function () { const i = document.querySelector('input[name="log"]');
      return i && i.offsetParent !== null; })(),
  ready: document.readyState
});
"""

# profile icon (top right). Picks visible links/buttons near the top, prefers login/account-like names, rightmost wins
LOGIN_ICON_JS = """
const re = /login|log-in|sign|account|profile|user|member/i;
const vis = e => { const r = e.getBoundingClientRect();
  return r.width > 8 && r.height > 8 && r.top >= 0 && r.top < 140 && e.offsetParent !== null; };
const all = [...document.querySelectorAll('a, button, [role=button]')].filter(vis);
const named = all.filter(e => re.test((e.className || '') + ' ' + (e.id || '') + ' ' +
  (e.getAttribute('aria-label') || '') + ' ' + (e.title || '') + ' ' + (e.getAttribute('href') || '')));
const pool = named.length ? named : all;
pool.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
const e = pool[0];
if (!e) return null;
if (args && args.click) { e.click(); }
const r = e.getBoundingClientRect();
return {x: r.x, y: r.y, w: r.width, h: r.height, cands: pool.slice(0, 4).map(c => c.outerHTML.slice(0, 140))};
"""

LOGIN_FORM_VISIBLE_JS = """
const i = document.querySelector('input[name="log"]');
return !!(i && i.offsetParent !== null && i.getBoundingClientRect().width > 0);
"""

REMEMBER_JS = """
const c = document.querySelector('input[name="rememberme"]');
if (!c) return 'no checkbox';
if (!c.checked) { (c.closest('label') || c).click(); }
if (!c.checked) { c.click(); }
return c.checked;
"""

SUBMIT_JS = """
const i = document.querySelector('input[name="log"]');
let b = args && args.s ? document.querySelector(args.s) : null;
const f = i && i.form;
if (!b && f) b = [...f.querySelectorAll('button, input[type=submit]')]
    .find(x => /log\\s*in|sign\\s*in/i.test(x.innerText || x.value || '')) || f.querySelector('button[type=submit], input[type=submit]');
if (!b) b = [...document.querySelectorAll('button, input[type=submit], a')]
    .find(x => x.offsetParent !== null && /^\\s*log\\s*in\\s*$/i.test(x.innerText || x.value || ''));
if (!b) return 'no submit button';
b.click();
return 'clicked';
"""

TARGET_JS = """
const e = document.querySelector(args.s);
if (!e) return 'button NOT in DOM';
const r = e.getBoundingClientRect();
const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
return JSON.stringify({rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
  display: getComputedStyle(e).display,
  covered_by: top === e || e.contains(top) ? 'nothing' : (top ? top.outerHTML.slice(0, 160) : 'null')});
"""

FIND_LINK_JS = """
for (const a of document.querySelectorAll(args.s)) {
  const h = a.href;
  if (!h || !/^https?:/.test(h) || a.offsetParent === null) continue;
  if (/login|register|signup|password|javascript:/i.test(h)) continue;
  if (h.split('#')[0] === location.href.split('#')[0]) continue;
  return h;
}
return '';
"""


# ---------------------------------------------------------------- helpers
def _step(name):
    STEP["name"] = name
    log.info("STEP: %s", name)


def _state(driver):
    try:
        return driver.run_js("return document.title + ' | ' + location.href")
    except Exception:
        return "?"


def _diag(driver):
    try:
        return driver.run_js(DIAG_JS)
    except Exception as e:
        return f"diag failed: {e}"


def _screenshot(driver):
    try:
        driver.save_screenshot()
        files = glob.glob("output/screenshots/*")
        DEBUG["shot"] = max(files, key=os.path.getmtime) if files else None
    except Exception as e:
        log.warning("screenshot failed: %s", e)


def _wait_ready(driver: Driver, seconds: float = 10):
    """Let the page's own JS bind its handlers (ads can keep readyState from completing, so cap it)."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if driver.run_js("return document.readyState") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(2)


def _is_logged_in(driver: Driver) -> bool:
    try:
        return any(k.startswith("wordpress_logged_in") for k in driver.get_cookies_dict())
    except Exception:
        return False


# ---------------------------------------------------------------- Turnstile
def _cdp_click(driver: Driver, x: float, y: float):
    """Real mouse events at viewport coordinates. Chrome routes them into the cross-origin Turnstile iframe."""
    from botasaurus.browser import cdp
    ev = cdp.input_.dispatch_mouse_event
    btn = cdp.input_.MouseButton.LEFT
    sx, sy = x - 60, y - 25
    for i in range(1, 7):
        driver.run_cdp_command(ev(type_="mouseMoved", x=sx + (x - sx) * i / 6, y=sy + (y - sy) * i / 6))
        time.sleep(0.03)
    time.sleep(0.15)
    driver.run_cdp_command(ev(type_="mousePressed", x=x, y=y, button=btn, click_count=1))
    time.sleep(0.08)
    driver.run_cdp_command(ev(type_="mouseReleased", x=x, y=y, button=btn, click_count=1))


def _wait_token(driver: Driver, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if driver.run_js(TOKEN_JS):
            return True
        time.sleep(0.5)
    return False


def _click_turnstile(driver: Driver):
    """Click the Turnstile checkbox (left side of the widget box)."""
    r = driver.run_js(WIDGET_RECT_JS)
    if not r:
        log.warning("no visible Turnstile widget to click")
        return
    x, y = r["x"] + 30, r["y"] + r["h"] / 2
    log.info("widget box %s -> clicking at (%d, %d)", [round(r[k]) for k in "xywh"], x, y)
    try:
        _cdp_click(driver, x, y)
        log.info("CDP click sent")
        return
    except Exception as e:
        log.warning("CDP click failed: %s", e)
    try:
        driver.enable_human_mode()
        iframe = driver.get_element_at_point(int(r["x"] + r["w"] / 2), int(y))
        iframe.get_element_at_point(30, int(r["h"] / 2)).click()
        log.info("element click sent")
    except Exception as e:
        log.warning("element click failed: %s", e)
    finally:
        try:
            driver.disable_human_mode()
        except Exception:
            pass


def _bypass(driver: Driver):
    """Botasaurus' built-in Cloudflare/Turnstile handler."""
    fn = getattr(driver, "detect_and_bypass_cloudflare", None)
    if not fn:
        log.warning("driver has no detect_and_bypass_cloudflare")
        return
    try:
        fn()
    except Exception as e:
        log.warning("built-in bypass failed: %s", e)


def _solve_turnstile(driver: Driver, label: str):
    """Wait for the visible Turnstile to give a token; click it / use the built-in bypass if it doesn't."""
    if _wait_token(driver, 6):
        return
    _step(f"{label}: clicking Turnstile checkbox")
    _click_turnstile(driver)
    time.sleep(3)
    driver.save_screenshot()  # shows whether the checkbox got ticked (path is printed)
    if _wait_token(driver, 20):
        return
    _step(f"{label}: trying built-in Cloudflare bypass")
    _bypass(driver)
    if _wait_token(driver, config.TURNSTILE_TIMEOUT):
        return
    log.info("turnstile failed: %s", _diag(driver))
    _screenshot(driver)
    raise Exception(f"Turnstile not solved during {label} (see screenshot)")


# ---------------------------------------------------------------- login
def _open_login_modal(driver: Driver) -> bool:
    for attempt in range(3):
        if config.LOGIN_ICON_SELECTOR:
            driver.select(config.LOGIN_ICON_SELECTOR, wait=Wait.LONG).click()
        else:
            r = driver.run_js(LOGIN_ICON_JS, {"click": attempt > 0})  # 2nd+ attempt: plain DOM click
            if not r:
                raise Exception("Profile icon not found (set LOGIN_ICON_SELECTOR in .env)")
            if attempt == 0:
                log.info("profile icon candidates: %s", r["cands"])
                try:
                    _cdp_click(driver, r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
                except Exception as e:
                    log.warning("icon CDP click failed: %s", e)
        end = time.time() + 8
        while time.time() < end:
            if driver.run_js(LOGIN_FORM_VISIBLE_JS):
                return True
            time.sleep(0.5)
        log.info("login form not visible after click %d: %s", attempt + 1, _diag(driver))
    return False


def _login(driver: Driver):
    if not (config.SITE_USER and config.SITE_PASS):
        raise Exception("Login needed: set SITE_USER and SITE_PASS in .env")

    _step("login 1/4: opening profile popup")
    if not _open_login_modal(driver):
        _screenshot(driver)
        raise Exception("Login popup did not open (see screenshot; try LOGIN_ICON_SELECTOR)")

    _step("login 2/4: Cloudflare check")
    _solve_turnstile(driver, "login")

    _step("login 3/4: filling username/password + Remember Me")
    driver.type('input[name="log"]', config.SITE_USER)
    driver.type('input[name="pwd"]', config.SITE_PASS)
    log.info("remember me checked: %s", driver.run_js(REMEMBER_JS))
    time.sleep(0.5)

    _step("login 4/4: clicking Log in")
    log.info("submit: %s", driver.run_js(SUBMIT_JS, {"s": config.LOGIN_SUBMIT_SELECTOR or None}))
    end = time.time() + 25
    while time.time() < end:
        if _is_logged_in(driver):
            log.info("logged in")
            return
        time.sleep(0.5)
    _screenshot(driver)
    raise Exception("Login failed (check SITE_USER / SITE_PASS; see screenshot)")


# ---------------------------------------------------------------- download modal
def _click_download(driver: Driver):
    if config.DOWNLOAD_BTN_SELECTOR:
        el = driver.select(config.DOWNLOAD_BTN_SELECTOR, wait=Wait.VERY_LONG)
    else:
        el = driver.get_element_with_exact_text("Download", wait=Wait.VERY_LONG)
    if el is None:
        raise Exception("Download button not found (set DOWNLOAD_BTN_SELECTOR)")
    el.click()


def _find_link(driver: Driver):
    return driver.run_js(FIND_LINK_JS, {"s": config.DOWNLOAD_LINK_SELECTOR}) or None


def _open_modal(driver: Driver) -> bool:
    """Click Download until the Turnstile widget (or the link itself) shows up."""
    if config.DOWNLOAD_BTN_SELECTOR:
        try:
            log.info("button check: %s", driver.run_js(TARGET_JS, {"s": config.DOWNLOAD_BTN_SELECTOR}))
        except Exception as e:
            log.info("button check failed: %s", e)
    for attempt in range(3):
        if attempt == 0 or not config.DOWNLOAD_BTN_SELECTOR:
            _click_download(driver)
        else:
            driver.run_js("const e = document.querySelector(args.s); if (e) e.click();",
                          {"s": config.DOWNLOAD_BTN_SELECTOR})
        end = time.time() + 8
        while time.time() < end:
            if driver.run_js(WIDGET_RECT_JS) or _find_link(driver):
                return True
            time.sleep(0.5)
        log.info("modal not visible after click %d: %s", attempt + 1, _diag(driver))
    return False


def _wait_link(driver: Driver, seconds: float = 20):
    end = time.time() + seconds
    while time.time() < end:
        link = _find_link(driver)
        if link:
            return link
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------- main job
@browser(
    profile=config.PROFILE_NAME,       # keeps the login cookies between runs
    tiny_profile=True,
    reuse_driver=True,
    wait_for_complete_page_load=False,  # page is full of ad iframes that never finish loading
    headless=config.HEADLESS,
    proxy=config.PROXY,
    max_retry=config.MAX_RETRY,
    close_on_crash=True,
    raise_exception=True,
    create_error_logs=False,
    output=None,
)
def get_download(driver: Driver, url: str):
    DEBUG["shot"] = None

    _step("1/6 opening page")
    driver.google_get(url)
    time.sleep(3)
    if "just a moment" in _state(driver).lower():
        _step("1/6 Cloudflare page challenge, bypassing")
        driver.google_get(url, bypass_cloudflare=True)
    _step("2/6 page opened: " + _state(driver))
    _wait_ready(driver)

    if not _is_logged_in(driver):
        _login(driver)
        _step("2/6 logged in, reopening page")
        driver.google_get(url)
        time.sleep(3)
        _wait_ready(driver)
    else:
        log.info("already logged in (saved session)")

    _step("3/6 clicking Download")
    if not _open_modal(driver):
        _screenshot(driver)
        raise Exception("Download modal did not appear (see screenshot)")
    log.info("modal open: %s", _diag(driver))

    link = _find_link(driver)
    if not link:
        _step("4/6 waiting for Turnstile")
        _solve_turnstile(driver, "download")
        _step("5/6 Turnstile ok, waiting for download link")
        link = _wait_link(driver)
    if not link:
        log.info("no link: %s", _diag(driver))
        _screenshot(driver)
        raise Exception("Download link not found after verification (see screenshot)")
    _step("6/6 got link")

    return {
        "url": link,
        "cookies": driver.get_cookies_dict(),
        "ua": driver.run_js("return navigator.userAgent"),
        "referer": driver.current_url,
    }
