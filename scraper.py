import glob
import logging
import os
import re
import threading
import time
from urllib.parse import parse_qs, quote_plus, urlparse

from botasaurus.browser import browser, Driver, Wait
import config

log = logging.getLogger("scraper")
CTX = threading.local()          # per-thread (= per browser slot) job state
SHOT_LOCK = threading.Lock()

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
for (const sel of ['#dt-dl-modal .dt-dl-ts-w', '.dt-ts-modal .cf-turnstile']) {
  const c = document.querySelector(sel);
  if (c && c.offsetParent !== null) {
    const b = c.getBoundingClientRect();
    if (b.width > 50 && b.height > 30) return {x: b.x, y: b.y, w: b.width, h: b.height};
  }
}
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

FIND_LINKS_JS = """
const out = [], seen = new Set();
for (const a of document.querySelectorAll(args.s)) {
  const h = a.href;
  if (!h || !/^https?:/.test(h) || a.offsetParent === null) continue;
  if (/login|register|signup|password|javascript:/i.test(h)) continue;
  if (h.split('#')[0] === location.href.split('#')[0]) continue;
  if (seen.has(h)) continue;
  seen.add(h);
  let label = '', n = a;
  for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
    if (n !== a && n.querySelectorAll(args.s).length > 1) break;   // parent holds several links: stop
    const m = (n.innerText || '').match(/(\\d{3,4})\\s*p\\b/i);
    if (m) { label = m[1] + 'p'; break; }
  }
  out.push({url: h, label: label || ('link' + (out.length + 1))});
}
return out;
"""


# ---------------------------------------------------------------- helpers
def _job() -> dict:
    """The dict the bot passed in for the job running on this thread (step name, screenshot path, ...)."""
    j = getattr(CTX, "job", None)
    if j is None:
        j = CTX.job = {}
    return j


def _step(name):
    j = _job()
    j["step"] = name
    log.info("[%s] STEP: %s (+%.1fs)", j.get("tag", "-"), name, time.time() - j.get("t0", time.time()))


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
        with SHOT_LOCK:  # several browsers share output/screenshots, so find "our" file by diffing
            before = set(glob.glob("output/screenshots/*"))
            driver.save_screenshot()
            new = set(glob.glob("output/screenshots/*")) - before
        if new:
            _job()["shot"] = max(new, key=os.path.getmtime)
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
        time.sleep(0.25)
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


def _wait_done(driver: Driver, seconds: float) -> bool:
    """Token present OR the links already showed up (widget is removed once solved)."""
    end = time.time() + seconds
    while time.time() < end:
        if driver.run_js(TOKEN_JS) or _find_links(driver):
            return True
        time.sleep(0.25)
    return False


def _solve_turnstile(driver: Driver, label: str):
    """Let Turnstile auto-solve first; then click its checkbox a few times (it may not be ready on the first click)."""
    if _wait_done(driver, 7):
        return
    for i in range(3):
        _step(f"{label}: clicking Turnstile checkbox ({i + 1}/3)")
        _click_turnstile(driver)
        if _wait_done(driver, 5):
            return
    _step(f"{label}: trying built-in Cloudflare bypass")
    _bypass(driver)
    if _wait_done(driver, config.TURNSTILE_TIMEOUT):
        return
    log.info("turnstile failed: %s", _diag(driver))
    _screenshot(driver)
    raise Exception(f"Turnstile not solved during {label} (see screenshot)")


# ---------------------------------------------------------------- login
def _open_login_modal(driver: Driver) -> bool:
    sel = config.LOGIN_ICON_SELECTOR or 'a.clicklogin[aria-label="Login"]'
    for attempt in range(3):
        if attempt < 2:
            driver.run_js("const e = document.querySelector(args.s); if (e) e.click();", {"s": sel})
        else:  # heuristic fallback
            r = driver.run_js(LOGIN_ICON_JS, {"click": True})
            if not r:
                raise Exception("Profile icon not found (set LOGIN_ICON_SELECTOR in .env)")
            log.info("profile icon candidates: %s", r["cands"])
        end = time.time() + 6
        while time.time() < end:
            if driver.run_js(LOGIN_FORM_VISIBLE_JS):
                return True
            time.sleep(0.25)
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


def _find_links(driver: Driver) -> list:
    """All visible download links in the modal, best quality first."""
    links = driver.run_js(FIND_LINKS_JS, {"s": config.DOWNLOAD_LINK_SELECTOR}) or []

    def q(l):
        m = re.match(r"(\d+)", l["label"])
        return int(m.group(1)) if m else 0

    return sorted(links, key=q, reverse=True)


def _open_modal(driver: Driver) -> bool:
    """Click Download until the Turnstile widget (or the links themselves) show up."""
    for attempt in range(3):
        if attempt == 0:
            _click_download(driver)
        else:  # plain DOM click as a fallback
            driver.run_js("const e = document.querySelector(args.s); if (e) e.click();",
                          {"s": config.DOWNLOAD_BTN_SELECTOR or "a.dt-dl-open"})
        end = time.time() + 5
        while time.time() < end:
            if driver.run_js(WIDGET_RECT_JS) or _find_links(driver):
                return True
            time.sleep(0.25)
        log.info("modal not visible after click %d: %s", attempt + 1, _diag(driver))
    return False


def _wait_link(driver: Driver, seconds: float = 20):
    end = time.time() + seconds
    while time.time() < end:
        if _find_links(driver):
            time.sleep(0.5)  # let all quality rows render
            return _find_links(driver)
        time.sleep(0.25)
    return None


AD_HOSTS = ["*smartfloralab.com*", "*juicyads.com*", "*jads.co*", "*nettrck.store*",
            "*nelvoroq.com*", "*velvetloom.net*", "*trialhd.com*", "*banhq.com*"]


def _block_ads(driver: Driver):
    if not config.BLOCK_ADS or getattr(driver, "_ads_blocked", False):
        return
    try:
        from botasaurus.browser import cdp
        driver.run_cdp_command(cdp.network.enable())
        fn = getattr(cdp.network, "set_blocked_ur_ls", None) or getattr(cdp.network, "set_blocked_urls")
        driver.run_cdp_command(fn(urls=AD_HOSTS))
        driver._ads_blocked = True
        log.info("ad hosts blocked")
    except Exception as e:
        log.warning("ad blocking skipped: %s", e)


# ---------------------------------------------------------------- page info
PAGE_META_JS = """
const m = n => { const e = document.querySelector('meta[property="' + n + '"], meta[name="' + n + '"]');
                 return e ? (e.content || '').trim() : ''; };
const txt = sel => { const e = document.querySelector(sel); return e ? (e.innerText || '').trim().replace(/\\s+/g, ' ') : ''; };
let g = [...document.querySelectorAll('.ep-genres a, .dsc-genres a')];
if (!g.length) g = [...document.querySelectorAll('a[href*="/genre/"]')];
const genres = [...new Set(g.map(a => a.innerText.trim()).filter(Boolean))];
const info = [];
document.querySelectorAll('.ep-meta-field').forEach(f => {
  const k = (f.querySelector('.ep-meta-label') || {}).innerText, v = (f.querySelector('.ep-meta-value') || {}).innerText;
  if (k && v && !/^(views|added)$/i.test(k.trim())) info.push(k.trim() + ': ' + v.trim().replace(/\\s+/g, ' '));
});
document.querySelectorAll('.custom_fields').forEach(f => {
  const k = (f.querySelector('.variante') || {}).innerText, v = (f.querySelector('.valor') || {}).innerText;
  if (k && v) info.push(k.trim() + ': ' + v.trim().replace(/\\s+/g, ' '));
});
return {title: m('og:title') || document.title, h1: txt('h1.epih1') || txt('h1'),
        image: m('og:image'), desc: txt('.episode-desc span') || m('og:description') || m('description'),
        genres: genres, info: info};
"""

SERIES_META_JS = """
const t = sel => { const e = document.querySelector(sel); return e ? (e.innerText || '').trim().replace(/\\s+/g, ' ') : ''; };
const og = n => { const e = document.querySelector('meta[property="' + n + '"]'); return e ? (e.content || '').trim() : ''; };
let poster = '';
const img = document.querySelector('.dsc-poster img');
if (img) {
  poster = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '';
  if (poster.startsWith('data:')) poster = '';
}
const info = [];
const chips = [...document.querySelectorAll('.dsc-chip')].map(e => e.innerText.trim()).filter(Boolean);
if (chips.length) info.push(chips.join(' · '));
document.querySelectorAll('.dsc-stat').forEach(e => {
  const kk = (e.querySelector('span') || {}).innerText, v = (e.querySelector('b') || {}).innerText;
  if (kk && v && !/views/i.test(kk)) info.push(kk.trim() + ': ' + v.trim().replace(/\\s+/g, ' '));
});
const alt = t('.dsc-alt');
return {title: t('.dsc-title'), image: poster || og('og:image'),
        desc: t('.dsc-desc') || og('og:description'),
        genres: [...document.querySelectorAll('.dsc-genres a')].map(a => a.innerText.trim()).filter(Boolean),
        info: info, alt: alt};
"""

CARD_LIB = """
const txt = e => e ? (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ') : '';
const ok = u => u && !u.startsWith('data:');
const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
const isTarget = h => /\\/(tvshows|episodes)\\/[^/?#]+/.test(h || '');
const pick = img => {
  if (!img) return '';
  let u = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
  if (ok(u)) return abs(u);
  const ss = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';
  if (ss) {
    const c = ss.split(',').map(x => x.trim().split(/\\s+/)).filter(p => p[0])
      .map(p => ({u: p[0], w: parseInt(p[1] || '0', 10) || 0})).sort((a, b) => a.w - b.w);
    const best = c.find(x => x.w >= 300) || c[c.length - 1];
    if (best && ok(best.u)) return abs(best.u);
  }
  for (const v of [img.currentSrc, img.getAttribute('src')]) if (ok(v)) return abs(v);
  return '';
};
// one anime/episode card -> item. Works for series-cards, DooPlay search results and homepage cards.
const card = el => {
  const link = el.querySelector('.sc-title a, .title a, h3 a, h2 a') ||
               el.querySelector('a[href*="/tvshows/"], a[href*="/episodes/"]');
  if (!link || !link.href || !isTarget(link.href)) return null;
  const img = el.querySelector('.sc-poster img, .poster img, .thumbnail img, .image img, img');
  return {
    url: link.href,
    title: txt(link) || txt(el.querySelector('h3, .title')) || (img ? img.alt : '') || '',
    poster: pick(img),
    rating: txt(el.querySelector('.sc-btn-rating, .rating')).replace(/[^0-9.]/g, ''),
    badge: txt(el.querySelector('.dt-ep-badge')),
    alt: txt(el.querySelector('.sc-alt')),
    studios: [...el.querySelectorAll('.sc-tag-studio')].map(txt),
    tags: [...el.querySelectorAll('.sc-meta:not(.sc-meta-studios) .sc-tag, .card-tags .tag, .meta .year')].map(txt),
    genres: [...el.querySelectorAll('.sc-genres a')].map(txt).slice(0, 6),
    desc: txt(el.querySelector('.sc-desc, .contenido p'))
  };
};
const uniq = arr => { const seen = new Set(); return arr.filter(i => i && !seen.has(i.url) && seen.add(i.url)); };
const pagination = () => {
  const pg = document.querySelector('.dt-pg');
  if (pg) return {page: parseInt(pg.dataset.page || '1', 10), pages: parseInt(pg.dataset.pages || '1', 10),
                  tpl: pg.dataset.tpl || '', next: '', prev: ''};
  let page = 1, pages = 1;
  const ps = document.querySelector('.pagination span');   // the "Page 1 of 3" label only, not the page-number links
  const m = ps ? (ps.textContent || '').match(/Page\\s+(\\d+)\\s+of\\s+(\\d+)/i) : null;
  if (m) { page = parseInt(m[1], 10); pages = parseInt(m[2], 10); }
  const near = (sel, id) => { const i = document.querySelector(id); const a = i ? i.closest('a') : document.querySelector(sel);
                              return a && a.href ? a.href : ''; };
  return {page: page, pages: pages, tpl: '',
          next: near('a.next, a.nextpostslink, a[rel="next"]', '#nextpagination'),
          prev: near('a.prev, a.previouspostslink, a[rel="prev"]', '#previouspagination')};
};
"""

LISTING_JS = CARD_LIB + """
const pg = pagination();
const q = args && args.q;
let items = uniq([...document.querySelectorAll('article.series-card, .result-item article')].map(card));
let flat = false;
if (!items.length) {
  items = uniq([...document.querySelectorAll('article.item')].filter(a => !a.closest('.swiper-wrapper')).map(card));
}
if (!items.length) {   // flat index (e.g. /hentai-list/) or an unknown results layout: take the links
  flat = true;
  const scope = q ? (document.querySelector('.search-page, #archive-content, .content.csearch, #contenedor .content')
                     || document) : document;
  items = uniq([...scope.querySelectorAll('a[href*="/tvshows/"], a[href*="/episodes/"]')].map(a => {
    const t = txt(a) || ((a.querySelector('img') || {}).alt || '');
    if (!t || !isTarget(a.href) || /\\/(tvshows|episodes)\\/?$/.test(a.href)) return null;
    return {url: a.href, title: t, poster: '', rating: '', badge: '', alt: '', studios: [], tags: [], genres: [], desc: ''};
  }));
}
const h1 = document.querySelector('h1');
const ogt = document.querySelector('meta[property="og:title"]');
const heading = (h1 ? txt(h1) : '') || (ogt ? ogt.content : '') || document.title || '';
const body = document.body.innerText || document.body.textContent || '';
const none = !items.length && /nothing found|no results?|no matches|couldn.t find|not found/i.test(body);
return {heading: heading, flat: flat && !document.querySelector('article.series-card'), items: items,
        page: pg.page, pages: pg.pages, tpl: pg.tpl, next: pg.next, prev: pg.prev, none: none};
"""

HOME_JS = CARD_LIB + """
const latest = uniq([...document.querySelectorAll('article.item')]
  .filter(a => !a.closest('.swiper-wrapper, .swiper') && /\\/episodes\\//.test((a.querySelector('a[href]') || {}).href || ''))
  .map(card));
const featured = uniq([...document.querySelectorAll('article.item.swiper-slide')].map(card));
return {latest: latest, featured: featured};
"""

LIVE_TYPE_JS = """
const inputs = [...document.querySelectorAll('input[name="s"]')];
const el = inputs.find(i => i.offsetParent !== null) || inputs[0];
if (!el) return false;
el.focus();
Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, args.q);
['input', 'keydown', 'keyup'].forEach(t => el.dispatchEvent(new Event(t, {bubbles: true})));
el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'a'}));
return true;
"""

LIVE_LINKS_JS = CARD_LIB + """
const out = [];
document.querySelectorAll('a[href*="/tvshows/"], a[href*="/episodes/"]').forEach(a => {
  if (a.offsetParent === null || !isTarget(a.href)) return;
  const img = a.querySelector('img') || (a.parentElement && a.parentElement.querySelector('img'));
  const t = txt(a) || (img ? img.alt : '');
  if (t) out.push({url: a.href, title: t, poster: pick(img)});
});
return out;
"""

EPISODES_JS = """
const items = [...document.querySelectorAll('a.dt-se-item')];
if (items.length) return items.map(a => {
  const num = ((a.querySelector('.dt-se-num') || {}).innerText || '').trim();
  const mm = num.match(/(\\d+)/);
  return {url: a.href, label: ((a.querySelector('.dt-se-title') || {}).innerText || num).trim(),
          n: mm ? parseInt(mm[1], 10) : null};
});

const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };
const re = new RegExp(args.pat, 'i');
const byUrl = new Map();
for (const a of document.querySelectorAll('a[href]')) {
  const h = abs(a.getAttribute('href'));
  if (!h || !re.test(h) || h.split('#')[0] === location.href.split('#')[0]) continue;
  const t = (a.innerText || a.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
  const mm = h.match(/episode-(\\d+)/i);
  const cur = byUrl.get(h);
  if (!cur) byUrl.set(h, {url: h, label: t, n: mm ? parseInt(mm[1], 10) : null});
  else if (!cur.label && t) cur.label = t;
}
const all = [...byUrl.values()];
const slug = (args.slug || '').toLowerCase();
const mine = slug ? all.filter(e => e.url.toLowerCase().includes(slug)) : all;
return mine.length ? mine : all;
"""


def _clean_title(t: str) -> str:
    t = re.split(r"\s+[–—|-]\s+(?:Watch|Hentaimama)", t or "")[0]
    t = re.sub(r"\b(Episode\s+\d+)\s+\1\b", r"\1", t, flags=re.I)  # "Episode 1 Episode 1" -> "Episode 1"
    return t.strip()


def _meta(driver: Driver, prefer_h1: bool = False) -> dict:
    try:
        d = driver.run_js(PAGE_META_JS) or {}
    except Exception:
        d = {}
    title = (d.get("h1") if prefer_h1 else "") or d.get("title") or ""
    return {
        "title": _clean_title(title),
        "image": d.get("image") or "",
        "desc": d.get("desc") or "",
        "genres": d.get("genres") or [],
        "info": d.get("info") or [],
    }


def _session(driver: Driver) -> dict:
    return {
        "cookies": driver.get_cookies_dict(),
        "ua": driver.run_js("return navigator.userAgent"),
        "referer": driver.current_url,
    }


def _open_page(driver: Driver, url: str, ready_sel: str):
    """Open url and return as soon as ready_sel exists and the page's scripts have started (max ~15s)."""
    driver.google_get(url)
    t0 = time.time()
    challenged = False
    while time.time() - t0 < 15:
        try:
            if driver.run_js("return !!document.querySelector(args.s) && document.readyState !== 'loading'",
                             {"s": ready_sel}):
                time.sleep(0.6)  # let the site bind its click handlers
                return
        except Exception:
            pass
        if not challenged and time.time() - t0 > 4 and "just a moment" in _state(driver).lower():
            challenged = True
            _step("Cloudflare page challenge, bypassing")
            driver.google_get(url, bypass_cloudflare=True)
        time.sleep(0.25)
    log.info("page never showed %r, continuing anyway", ready_sel)


# ---------------------------------------------------------------- jobs
def _series_job(driver: Driver, url: str):
    _job()["shot"] = None
    _step("series 1/2: opening page")
    _open_page(driver, url, "a.dt-se-item")
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    eps = driver.run_js(EPISODES_JS, {"pat": config.EPISODE_URL_PATTERN, "slug": slug}) or []
    _step(f"series 2/2: found {len(eps)} episodes")
    if not eps:
        _screenshot(driver)
        raise Exception("No episodes found on that page. Send the series page that lists the episodes.")
    eps.sort(key=lambda e: (e["n"] is None, e["n"] or 0))  # stable: unnumbered keep page order
    try:
        meta = driver.run_js(SERIES_META_JS) or {}
    except Exception:
        meta = {}
    if not meta.get("title"):
        meta = _meta(driver, prefer_h1=True)
    meta["title"] = _clean_title(meta.get("title", ""))
    return {**meta, "episodes": eps, **_session(driver)}


def _episode_job(driver: Driver, url: str):
    _job()["shot"] = None

    _step("1/5 opening page")
    _open_page(driver, url, "a.dt-dl-open")
    _step("2/5 page opened: " + _state(driver))

    if not _is_logged_in(driver):
        _login(driver)
        _step("2/5 logged in, reopening page")
        _open_page(driver, url, "a.dt-dl-open")
    else:
        log.info("already logged in (saved session)")

    meta = _meta(driver, prefer_h1=True)  # title / thumbnail / description of this episode

    _step("3/5 clicking Download")
    if not _open_modal(driver):
        _screenshot(driver)
        raise Exception("Download modal did not appear (see screenshot)")

    links = _find_links(driver)
    if not links:
        _step("4/5 Turnstile")
        _solve_turnstile(driver, "download")
        links = _wait_link(driver)
    else:
        links = _wait_link(driver, 3) or links
    if not links:
        log.info("no links: %s", _diag(driver))
        _screenshot(driver)
        raise Exception("Download links not found after verification (see screenshot)")
    _step("5/5 got links: " + ", ".join(l["label"] for l in links))

    return {"links": links, "meta": meta, **_session(driver)}


def _warmup(driver: Driver):
    """Start Chrome and log in before the first user request, so the first job is already fast."""
    _step("warmup: opening site")
    _open_page(driver, config.SITE_URL, "a.clicklogin")
    if not _is_logged_in(driver):
        _login(driver)
    _step("warmup done")
    return True



def _blank_item(url, title, poster=""):
    return {"url": url, "title": title, "poster": poster, "rating": "", "badge": "", "alt": "",
            "studios": [], "tags": [], "genres": [], "desc": ""}


def _live_search(driver: Driver, q: str) -> list:
    """Fallback: type into the site's own search box and read the dropdown (links that were not on the page before)."""
    _open_page(driver, config.SITE_URL, "input[name='s']")
    base = {i["url"] for i in driver.run_js(LIVE_LINKS_JS) or []}
    if not driver.run_js(LIVE_TYPE_JS, {"q": q}):
        return []
    end = time.time() + 6
    while time.time() < end:
        time.sleep(0.4)
        new = [i for i in driver.run_js(LIVE_LINKS_JS) or [] if i["url"] not in base]
        if new:
            time.sleep(0.6)  # let the rest of the dropdown render
            new = [i for i in driver.run_js(LIVE_LINKS_JS) or [] if i["url"] not in base]
            return [_blank_item(i["url"], i["title"], i.get("poster", "")) for i in new]
    return []


def _scroll_once(driver: Driver):
    try:  # lazy-loaded posters only pick their real source once scrolled near
        driver.run_js("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.4)
        driver.run_js("window.scrollTo(0, 0);")
    except Exception:
        pass


def _listing_job(driver: Driver, url: str):
    """Any list of anime: /hentai-series/, /genre/<x>/, /studio/<x>/, a flat index, or a search (?s=...)."""
    _job()["shot"] = None
    q = (parse_qs(urlparse(url).query).get("s") or [""])[0].strip()
    _step("listing 1/2: opening " + ("search results" if q else "page"))
    _open_page(driver, url, "article.series-card, .result-item, article.item, a[href*='/tvshows/'], a[href*='/episodes/']")
    _scroll_once(driver)
    data = driver.run_js(LISTING_JS, {"q": q}) or {}
    items = data.get("items") or []
    if not items and q:
        _step("listing: no results on the page, trying the site's live search")
        items = _live_search(driver, q)
        data = {**data, "items": items, "flat": False, "page": 1, "pages": 1, "tpl": "", "next": "", "prev": ""}
    _step(f"listing 2/2: {len(items)} items, page {data.get('page')}/{data.get('pages')}")
    if not items:
        if q and data.get("none"):
            raise Exception(f"No results for \u201c{q}\u201d.")
        _screenshot(driver)
        if q:
            raise Exception(f"Couldn't read the search results for \u201c{q}\u201d (see screenshot).")
        raise Exception("Couldn't read any anime from that page (see screenshot).")
    page, pages, tpl, nxt = data.get("page") or 1, data.get("pages") or 1, data.get("tpl") or "", data.get("next") or ""
    if q and pages > 1 and not tpl:
        tpl = f"{config.SITE_URL.rstrip('/')}/page/987654321/?s={quote_plus(q)}"
    if nxt and pages <= page:
        pages = page + 1
    return {
        "url": url,
        "heading": f"Search: {q}" if q else (_clean_title(data.get("heading", "")) or "List"),
        "items": items,
        "flat": bool(data.get("flat")),
        "page": page, "pages": pages, "tpl": tpl,
        "next": nxt, "prev": data.get("prev") or "",
        **_session(driver),
    }


def _home_job(driver: Driver, url: str):
    """Homepage: latest episodes + featured series, each shaped like a list page."""
    _job()["shot"] = None
    _step("home 1/2: opening page")
    _open_page(driver, url, "article.item")
    _scroll_once(driver)
    data = driver.run_js(HOME_JS) or {}
    latest, featured = data.get("latest") or [], data.get("featured") or []
    _step(f"home 2/2: {len(latest)} latest, {len(featured)} featured")
    if not latest and not featured:
        _screenshot(driver)
        raise Exception("Couldn't read the home page (see screenshot).")
    base = {"url": url, "flat": False, "page": 1, "pages": 1, "tpl": "", "next": "", "prev": "", **_session(driver)}
    return {"latest": {**base, "heading": "Latest episodes", "items": latest},
            "featured": {**base, "heading": "Featured series", "items": featured}}


def _run(driver: Driver, data: dict):
    CTX.job = data.get("job") or {}
    CTX.job.setdefault("t0", time.time())
    _block_ads(driver)
    kind = data["kind"]
    if kind == "warmup":
        return _warmup(driver)
    if kind == "series":
        return _series_job(driver, data["url"])
    if kind == "listing":
        return _listing_job(driver, data["url"])
    if kind == "home":
        return _home_job(driver, data["url"])
    return _episode_job(driver, data["url"])


def _make_scraper(idx: int):
    """One decorated function per slot: each owns its own Chrome and login profile, so slots never collide."""
    def fn(driver: Driver, data: dict):
        return _run(driver, data)

    fn.__name__ = fn.__qualname__ = f"scrape_slot_{idx}"
    return browser(
        profile=f"{config.PROFILE_NAME}_{idx}",   # keeps the login cookies between runs
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
    )(fn)


SCRAPERS = [_make_scraper(i) for i in range(config.BROWSER_WORKERS)]