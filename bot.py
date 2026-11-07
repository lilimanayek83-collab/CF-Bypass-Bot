import asyncio
import html
import io
import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlparse

from pyrogram import Client, enums, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

import catalog
import config
from downloader import fetch, fetch_bytes, fetch_image
from scraper import SCRAPERS

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:  # poster grids are optional: without Pillow the list is text-only
    PIL_OK = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
for noisy in ("pyrogram", "websocket"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "cfdl",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

HTML = enums.ParseMode.HTML
esc = html.escape
EPISODE_RE = re.compile(config.EPISODE_URL_PATTERN, re.I)
SERIES_RE = re.compile(config.SERIES_URL_PATTERN, re.I)
SITE_HOST = urlparse(config.SITE_URL).netloc.lower().removeprefix("www.")
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov")
LINK_TTL = 20 * 60  # download links are only trusted for 20 minutes

# ---- concurrency state (created in main(), needs the running loop)
SLOTS: asyncio.Queue = None            # free browser slot ids
EXECUTORS: list = []                   # one single-thread executor per browser slot
TRANSFERS: asyncio.Semaphore = None    # simultaneous download+upload jobs
ACTIVE = defaultdict(int)              # user id -> running jobs
TASKS: set = set()                     # keep references to running job tasks
CACHE: dict = {}                       # (kind, url) -> (time, scrape result)
INFLIGHT: dict = {}                    # (kind, url) -> future shared by identical requests

SERIES: "OrderedDict[str, dict]" = OrderedDict()    # token -> series scrape result
EPISODES: "OrderedDict[str, dict]" = OrderedDict()  # token -> episode scrape result (links)
LISTINGS: "OrderedDict[str, dict]" = OrderedDict()  # token -> {res, posters, screen} for list pages
POSTER_CACHE: "OrderedDict[str, bytes]" = OrderedDict()  # poster url -> image bytes


def remember(store, key, val, limit=200):
    store[key] = val
    while len(store) > limit:
        store.popitem(last=False)


def site_url(path=""):
    return config.SITE_URL.rstrip("/") + "/" + path.lstrip("/")


def search_url(q):
    return site_url() + "?s=" + quote_plus(q)


MENU_ROWS = [
    [("🔍 Search", "m|search"), ("🆕 Latest", "m|latest")],
    [("📚 Series", "m|series"), ("📃 A–Z list", "m|list")],
    [("🔓 Uncensored", "m|unc"), ("🧊 3D", "m|3d")],
    [("🏷 Genres", "m|genres"), ("🏢 Studios", "m|studios")],
    [("🔥 Featured", "m|featured")],
]
MENU_PATHS = {"series": "hentai-series/", "list": "hentai-list/", "unc": "genre/uncensored/", "3d": "genre/3d/"}
MENU_TEXT = "🍥 <b>Hentaimama</b>\nPick a section, or just type a title to search."


def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in MENU_ROWS])


def nav_row():
    return [InlineKeyboardButton("🏠 Menu", callback_data="m|home"),
            InlineKeyboardButton("🔍 Search", callback_data="m|search")]


def hum(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


async def safe_edit(msg, text, **kw):
    try:
        await msg.edit_text(text, **kw)
    except Exception:
        pass


def _allowed(user_id) -> bool:
    return not config.AUTH_USERS or user_id in config.AUTH_USERS


is_auth = filters.create(lambda _, __, m: bool(m.from_user) and _allowed(m.from_user.id))


# ------------------------------------------------------------------ captions / keyboards
def build_caption(title, genres, info, desc, tail="", series=None):
    head = [f"<b>{esc(clip(title, 200))}</b>"]
    if series:
        head.append(f"📺 {esc(clip(series, 120))}")
    if genres:
        head.append("🏷 " + esc(", ".join(genres[:8])))
    head += [esc(clip(i, 80)) for i in (info or [])[:6]]
    base = "\n".join(head)
    tail_s = f"\n\n{tail}" if tail else ""
    room = 900 - len(base) - len(tail_s)
    body = f"\n\n{esc(clip(desc, room))}" if desc and room > 60 else ""
    cap = base + body + tail_s
    return cap if len(cap) <= 1024 else base + tail_s


def ep_label(e, idx):
    return f"EP {e['n']}" if e.get("n") is not None else clip(e.get("label") or str(idx + 1), 14)


def episode_keyboard(token, eps, page=0):
    size = config.PAGE_SIZE
    start = page * size
    btns = [InlineKeyboardButton(ep_label(e, start + i), callback_data=f"e|{token}|{start + i}")
            for i, e in enumerate(eps[start:start + size])]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"p|{token}|{page - 1}"))
    if start + size < len(eps):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"p|{token}|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(nav_row())
    return InlineKeyboardMarkup(rows)


def quality_keyboard(token, links):
    rows = [[InlineKeyboardButton(l["label"], callback_data=f"d|{token}|{i}") for i, l in enumerate(links)],
            [InlineKeyboardButton("All", callback_data=f"d|{token}|all")],
            nav_row()]
    return InlineKeyboardMarkup(rows)


async def get_image(info, url):
    """Download a thumbnail with the browser session; returns (path, dir) or (None, None)."""
    if not url:
        return None, None
    d = os.path.join(config.DOWNLOAD_DIR, "img_" + uuid.uuid4().hex[:8])
    try:
        path = await asyncio.get_running_loop().run_in_executor(None, fetch_image, info, url, d)
        return path, d
    except Exception as e:
        logging.warning("thumbnail failed: %s", e)
        shutil.rmtree(d, ignore_errors=True)
        return None, None


async def send_card(m, caption, img, kb=None):
    if img:
        try:
            return await m.reply_photo(img, caption=caption, parse_mode=HTML, reply_markup=kb)
        except Exception:
            logging.exception("photo send failed, falling back to text")
    return await m.reply_text(caption, parse_mode=HTML, reply_markup=kb)


# ------------------------------------------------------------------ video helpers
def video_meta(path: str, workdir: str):
    thumb, dur = None, 0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        dur = int(float(out)) if out else 0
        thumb = os.path.join(workdir, "thumb.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(min(5, max(dur // 2, 0))), "-i", path,
             "-vframes", "1", "-vf", "scale=320:-1", thumb],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        if not os.path.exists(thumb):
            thumb = None
    except Exception:
        pass
    return thumb, dur


async def up_progress(cur, total, st, box):
    now = time.time()
    if now - box[0] < 4:
        return
    box[0] = now
    await safe_edit(st, f"⬆️ Uploading {box[1]} {hum(cur)} / {hum(total)} ({cur * 100 // max(total, 1)}%)")


def pick(links, q):
    if q == "all":
        return links
    for l in links:
        if l["label"].lower() == q:
            return [l]
    return [links[0]]  # best (links are sorted best first)


async def download_upload(m, st, info, link):
    loop = asyncio.get_running_loop()
    label = link["label"]
    if TRANSFERS.locked():
        await safe_edit(st, f"⏳ {label}: waiting for a free download slot...")
    async with TRANSFERS:
        job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex[:8])
        try:
            state = {"done": 0, "total": 0, "name": ""}
            fut = loop.run_in_executor(None, fetch, dict(info, url=link["url"]), job_dir, state)
            while not fut.done():
                await asyncio.sleep(4)
                t = state["total"]
                pct = f" ({state['done'] * 100 // t}%)" if t else ""
                await safe_edit(st, f"⬇️ Downloading {label} {state['name']}\n{hum(state['done'])}"
                                    f"{' / ' + hum(t) if t else ''}{pct}")
            path = await fut

            if os.path.getsize(path) > config.MAX_SIZE:
                raise Exception(f"{label}: file is larger than 2 GB (Telegram bot limit)")

            box = [0.0, label]
            caption = f"{os.path.basename(path)}  [{label}]"
            if path.lower().endswith(VIDEO_EXT):
                thumb, dur = await loop.run_in_executor(None, video_meta, path, job_dir)
                await m.reply_video(
                    path, caption=caption, duration=dur, thumb=thumb,
                    supports_streaming=True, progress=up_progress, progress_args=(st, box),
                )
            else:
                await m.reply_document(
                    path, caption=caption, progress=up_progress, progress_args=(st, box),
                )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


# ------------------------------------------------------------------ browser slots
def new_job():
    return {"t0": time.time(), "tag": uuid.uuid4().hex[:4], "step": "starting"}


async def run_scrape(st, job, data, icon="🔎"):
    """Take a free browser slot, run the scrape in that browser's own thread, give the slot back."""
    loop = asyncio.get_running_loop()
    job["step"] = "waiting for a free browser"
    getter = asyncio.ensure_future(SLOTS.get())
    try:
        while True:
            done, _ = await asyncio.wait({getter}, timeout=5)
            if done:
                break
            await safe_edit(st, f"⏳ All {config.BROWSER_WORKERS} browsers are busy, you're in line...")
    except BaseException:
        getter.cancel()
        raise
    slot = getter.result()
    job["t0"] = t0 = time.time()
    try:
        fut = loop.run_in_executor(EXECUTORS[slot], SCRAPERS[slot], dict(data, job=job))
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=5)
            except asyncio.TimeoutError:
                el = int(time.time() - t0)
                if el > 240:
                    SCRAPERS[slot].close()  # kill the stuck Chrome; it restarts on the next job
                    EXECUTORS[slot] = ThreadPoolExecutor(max_workers=1)  # abandon the hung thread
                    raise Exception(f"Timed out at step: {job.get('step')}")
                await safe_edit(st, f"{icon} {job.get('step')} ({el}s)")
    finally:
        SLOTS.put_nowait(slot)


async def scrape_cached(kind, url, st, job, icon="🔎"):
    """Reuse a recent result for the same page; identical simultaneous requests share one browser run."""
    key = (kind, url)
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < config.CACHE_TTL:
        return hit[1]
    waiter = INFLIGHT.get(key)
    if waiter:
        await safe_edit(st, "⏳ Same page is already being opened for another request...")
        return await asyncio.shield(waiter)
    fut = asyncio.get_running_loop().create_future()
    INFLIGHT[key] = fut
    try:
        res = await run_scrape(st, job, {"kind": kind, "url": url}, icon)
        CACHE[key] = (time.time(), res)
        if len(CACHE) > 200:
            CACHE.pop(min(CACHE, key=lambda k: CACHE[k][0]), None)
        fut.set_result(res)
        return res
    except BaseException as e:
        fut.set_exception(e if isinstance(e, Exception) else Exception("cancelled"))
        fut.exception()  # mark as retrieved even when nobody else is waiting
        raise
    finally:
        INFLIGHT.pop(key, None)


async def report_error(m, st, e, job=None):
    logging.exception("job failed")
    text = f"🔎 {e}" if str(e).startswith("No results") else f"❌ Failed: {e}"
    if st:
        await safe_edit(st, text)
    else:
        await m.reply_text(text)
    shot = (job or {}).get("shot")
    if shot and os.path.exists(str(shot)):
        try:
            await m.reply_photo(str(shot), caption="What the browser saw")
        except Exception:
            pass
        job["shot"] = None


# ------------------------------------------------------------------ jobs
async def process_series(job_in):
    m, url = job_in["m"], job_in["url"]
    st = await m.reply_text("📚 Opening series page...", quote=True)
    job, img_dir = new_job(), None
    try:
        res = await scrape_cached("series", url, st, job, "📚")
        eps = res["episodes"]
        token = uuid.uuid4().hex[:8]
        remember(SERIES, token, res)
        img, img_dir = await get_image(res, res.get("image"))
        cap = build_caption(res["title"], res["genres"], res["info"], res["desc"],
                            tail=f"📚 {len(eps)} episodes — pick one:")
        await st.delete()
        await send_card(m, cap, img, episode_keyboard(token, eps, 0))
    except Exception as e:
        await report_error(m, st, e, job)
    finally:
        if img_dir:
            shutil.rmtree(img_dir, ignore_errors=True)


async def process_episode(job_in):
    m, url = job_in["m"], job_in["url"]
    st = await m.reply_text(f"🔎 Opening {job_in.get('label') or 'episode'}...", quote=True)
    job, img_dir = new_job(), None
    try:
        res = await scrape_cached("episode", url, st, job)
        links, meta = res["links"], res["meta"]
        token = uuid.uuid4().hex[:8]
        remember(EPISODES, token, dict(res, t=time.time()))

        ask = config.QUALITY == "ask" and len(links) > 1
        tail = "Pick a quality:" if ask else "Qualities: " + ", ".join(l["label"] for l in links)
        img, img_dir = await get_image(res, meta.get("image"))
        cap = build_caption(meta["title"] or job_in.get("label") or "Episode", meta["genres"], meta["info"],
                            meta["desc"], tail=tail, series=job_in.get("series_title"))
        await st.delete()
        await send_card(m, cap, img, quality_keyboard(token, links) if ask else None)

        if not ask:
            st2 = await m.reply_text("⬇️ Starting...")
            for l in pick(links, config.QUALITY if config.QUALITY != "ask" else "best"):
                await download_upload(m, st2, res, l)
            await st2.delete()
    except Exception as e:
        await report_error(m, st, e, job)
    finally:
        if img_dir:
            shutil.rmtree(img_dir, ignore_errors=True)


async def process_download(job_in):
    m = job_in["m"]
    entry = EPISODES.get(job_in["token"])
    if not entry or time.time() - entry["t"] > LINK_TTL:
        return await m.reply_text("Links expired. Open the episode again from the list.")
    links = entry["links"]
    chosen = links if job_in["sel"] == "all" else [links[int(job_in["sel"])]]
    st = await m.reply_text("⬇️ Starting...")
    try:
        for l in chosen:
            await download_upload(m, st, entry, l)
        await st.delete()
    except Exception as e:
        await report_error(m, st, e)


# ------------------------------------------------------------------ list pages (poster grid + numbered buttons)
class NullMsg:
    """Stand-in status message when we edit an existing card instead of sending a new one."""
    async def edit_text(self, *a, **k):
        pass


def classify(url):
    """episode | series | listing | None (not our site)."""
    if urlparse(url).netloc.lower().removeprefix("www.") != SITE_HOST:
        return None
    if EPISODE_RE.search(url):
        return "episode"
    if SERIES_RE.search(url):
        return "series"
    return "listing"


def page_url(res, n):
    tpl = res.get("tpl") or ""
    if tpl and 1 <= n <= res["pages"]:
        return re.sub(r"page/987654321/?(?=\?|$)", "", tpl) if n == 1 else tpl.replace("987654321", str(n))
    if n == res["page"] + 1:
        return res.get("next") or None
    if n == res["page"] - 1:
        return res.get("prev") or None
    return None


def per_screen(res):
    return 10 if res["flat"] else config.LISTING_PER_SCREEN


def n_screens(res):
    return max(1, math.ceil(len(res["items"]) / per_screen(res)))


def nice_heading(h):
    return re.sub(r"^Studios?/", "Studio: ", re.sub(r"^Genres?/", "Genre: ", h or ""))


def item_line(n, it):
    bits = []
    if it.get("badge"):
        bits.append(it["badge"])
    if it.get("rating"):
        bits.append(f"⭐{it['rating']}")
    tags = it.get("tags", [])
    eps = next((t for t in tags if "episode" in t.lower()), None)
    if eps:
        bits.append(eps.replace("Episodes", "eps").replace("Episode", "ep"))
    when = next((t for t in tags if re.fullmatch(r"\d{4}|\d{1,2}/\d{4}", t)), None)
    if when:
        bits.append(when)
    lang = next((t for t in tags if t.upper() in ("SUB", "DUB", "RAW", "UNCENSORED")), None)
    if lang:
        bits.append(lang)
    tail = " — " + " · ".join(bits) if bits else ""
    return f"{n}. <b>{esc(clip(it['title'], 40))}</b>{esc(tail)}"


def listing_view(token, res, screen):
    per, screens = per_screen(res), n_screens(res)
    screen = max(0, min(screen, screens - 1))
    lo = screen * per
    hi = min(len(res["items"]), lo + per)
    sub = [f"Page {res['page']}/{res['pages']}"] if res["pages"] > 1 else []
    sub.append(f"{lo + 1}–{hi} of {len(res['items'])}" + (" on this page" if res["pages"] > 1 else ""))
    lines = [f"📚 <b>{esc(clip(nice_heading(res['heading']), 80))}</b>", " · ".join(sub), ""]
    lines += [item_line(i - lo + 1, res["items"][i]) for i in range(lo, hi)]
    caption = "\n".join(lines) + "\n\nTap a number to open it."
    if len(caption) > 1024:  # very long titles: drop the extras
        caption = "\n".join(l.split(" — ")[0] for l in lines) + "\n\nTap a number to open it."
    btns = [InlineKeyboardButton(str(i - lo + 1), callback_data=f"l|{token}|{i}") for i in range(lo, hi)]
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton("⬅️", callback_data=f"g|{token}|p"),
                 InlineKeyboardButton(f"{screen + 1}/{screens}", callback_data=f"g|{token}|x"),
                 InlineKeyboardButton("➡️", callback_data=f"g|{token}|n")])
    if res["pages"] > 1:
        rows.append([InlineKeyboardButton("⏮ First page", callback_data=f"g|{token}|f"),
                     InlineKeyboardButton(f"Last ({res['pages']}) ⏭", callback_data=f"g|{token}|z")])
    elif screens > 12:  # long flat index: jump 10 screens at a time
        rows.append([InlineKeyboardButton("⏪ -10", callback_data=f"g|{token}|m"),
                     InlineKeyboardButton("+10 ⏩", callback_data=f"g|{token}|a")])
    rows.append(nav_row())
    return caption, InlineKeyboardMarkup(rows), lo, hi, screen


def make_collage(tiles, path):
    """tiles: list of image bytes (or None). Numbered poster grid, 4 per row."""
    W, H, G, cols = 190, 285, 8, 4
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * W + (cols + 1) * G, rows * H + (rows + 1) * G), (20, 26, 36))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:
        font = ImageFont.load_default()
    for i, raw in enumerate(tiles):
        x, y = G + (i % cols) * (W + G), G + (i // cols) * (H + G)
        tile = None
        if raw:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                r = max(W / im.width, H / im.height)
                im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
                l, t = (im.width - W) // 2, (im.height - H) // 2
                tile = im.crop((l, t, l + W, t + H))
            except Exception:
                tile = None
        sheet.paste(tile or Image.new("RGB", (W, H), (45, 55, 72)), (x, y))
        d.ellipse((x + 6, y + 6, x + 46, y + 46), fill=(137, 76, 168))
        try:
            d.text((x + 26, y + 26), str(i + 1), fill="white", font=font, anchor="mm")
        except Exception:
            d.text((x + 20, y + 16), str(i + 1), fill="white", font=font)
    sheet.save(path, "JPEG", quality=85)


async def load_posters(res):
    """All posters of the page, downloaded in parallel (cached by URL)."""
    loop = asyncio.get_running_loop()

    async def one(it):
        u = it.get("poster")
        if not u:
            return None
        if u in POSTER_CACHE:
            return POSTER_CACHE[u]
        try:
            raw = await loop.run_in_executor(None, fetch_bytes, res, u)
        except Exception as e:
            logging.warning("poster failed: %s", e)
            return None
        remember(POSTER_CACHE, u, raw, 600)
        return raw

    return await asyncio.gather(*(one(it) for it in res["items"]))


async def render_listing(token, screen, edit_msg=None, reply_to=None):
    """Show one screen of a list: edit the existing card in place, or send a new one."""
    loop = asyncio.get_running_loop()
    st = LISTINGS[token]
    res = st["res"]
    caption, kb, lo, hi, screen = listing_view(token, res, screen)
    st["screen"] = screen
    img = None
    tiles = st["posters"][lo:hi]
    if PIL_OK and not res["flat"] and any(tiles):
        img = os.path.join(config.DOWNLOAD_DIR, f"lst_{uuid.uuid4().hex[:8]}.jpg")
        try:
            await loop.run_in_executor(None, make_collage, tiles, img)
        except Exception:
            logging.exception("collage failed, sending text only")
            img = None
    try:
        if edit_msg is not None:
            has_photo = bool(getattr(edit_msg, "photo", None))
            try:
                if img and has_photo:
                    await edit_msg.edit_media(InputMediaPhoto(img, caption=caption, parse_mode=HTML), reply_markup=kb)
                    return
                if not img and not has_photo:
                    await edit_msg.edit_text(caption, parse_mode=HTML, reply_markup=kb)
                    return
            except Exception as e:
                if "NOT_MODIFIED" in str(e).upper():
                    return
                logging.warning("edit failed, sending a new card: %s", e)
            reply_to = edit_msg  # type changed or edit failed: send fresh, drop the old card
        if img:
            await reply_to.reply_photo(img, caption=caption, parse_mode=HTML, reply_markup=kb)
        else:
            await reply_to.reply_text(caption, parse_mode=HTML, reply_markup=kb)
        if edit_msg is not None:
            try:
                await edit_msg.delete()
            except Exception:
                pass
    finally:
        if img:
            try:
                os.remove(img)
            except OSError:
                pass


async def present_listing(m, res, screen=0, edit=None):
    """Poster grid for a list result (list page, search results, home sections)."""
    posters = [None] * len(res["items"])
    if PIL_OK and not res["flat"]:
        posters = await load_posters(res)
    token = uuid.uuid4().hex[:8]
    remember(LISTINGS, token, {"res": res, "posters": posters, "screen": 0})
    s = n_screens(res) - 1 if screen == "last" else int(screen or 0)
    await render_listing(token, s, edit_msg=edit, reply_to=m)


async def process_listing(job_in):
    m, url, edit = job_in["m"], job_in["url"], job_in.get("edit")
    st = None if edit else await m.reply_text("📚 Loading list...", quote=True)
    job = new_job()
    try:
        res = await scrape_cached("listing", url, st or NullMsg(), job, "📚")
        await present_listing(m, res, job_in.get("screen", 0), edit)
        if st:
            await st.delete()
    except Exception as e:
        await report_error(m, st, e, job)


async def process_home(job_in):
    m, which = job_in["m"], job_in["which"]
    st = await m.reply_text("🏠 Loading...", quote=True)
    job = new_job()
    try:
        data = await scrape_cached("home", config.SITE_URL, st, job, "🏠")
        res = data[which]
        if not res["items"]:
            raise Exception("Nothing to show there right now.")
        await present_listing(m, res)
        await st.delete()
    except Exception as e:
        await report_error(m, st, e, job)


# ------------------------------------------------------------------ genre / studio pickers (no browser needed)
def catalog_view(kind, page):
    items = catalog.GENRES if kind == "g" else catalog.STUDIOS
    size = 24
    pages = max(1, -(-len(items) // size))
    page = max(0, min(page, pages - 1))
    chunk = items[page * size:(page + 1) * size]
    btns = [InlineKeyboardButton(clip(name, 18), callback_data=f"o|{kind}|{page * size + i}")
            for i, (name, _) in enumerate(chunk)]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton("⬅️", callback_data=f"c|{kind}|{page - 1}"),
                 InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"c|{kind}|{page}"),
                 InlineKeyboardButton("➡️", callback_data=f"c|{kind}|{page + 1}")])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="m|back")])
    title = "🏷 <b>Genres</b>" if kind == "g" else "🏢 <b>Studios</b>"
    return f"{title} — {len(items)} · page {page + 1}/{pages}\nTap one to browse it.", InlineKeyboardMarkup(rows)


async def do_search(m, text):
    q = re.sub(r"\s+", " ", text or "").strip()[:80]
    if len(q) < 2:
        return await m.reply_text("Type at least 2 letters to search.")
    spawn({"type": "listing", "m": m, "url": search_url(q), "uid": m.from_user.id, "screen": 0})


HANDLERS = {"series": process_series, "episode": process_episode, "download": process_download,
            "listing": process_listing, "home": process_home}


async def guarded(job_in):
    """Per-user cap, so one person can't take every browser/transfer slot."""
    uid = job_in["uid"]
    if ACTIVE[uid] >= config.MAX_JOBS_PER_USER:
        try:
            await job_in["m"].reply_text(f"⏳ You already have {ACTIVE[uid]} jobs running. Wait for one to finish.")
        except Exception:
            pass
        return
    ACTIVE[uid] += 1
    try:
        await HANDLERS[job_in["type"]](job_in)
    except Exception:
        logging.exception("job crashed")
    finally:
        ACTIVE[uid] -= 1


def spawn(job_in):
    """Every request runs as its own task, so users never wait behind each other's chat handling."""
    t = asyncio.create_task(guarded(job_in))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


# ------------------------------------------------------------------ telegram handlers
@app.on_message(filters.command(["start", "menu", "home"]) & filters.private & is_auth)
async def start(_, m):
    await m.reply_text(MENU_TEXT + "\n\nYou can also send a link (list, series or episode page).\n/status shows the load.",
                       parse_mode=HTML, reply_markup=menu_keyboard())


@app.on_message(filters.command("search") & filters.private & is_auth)
async def search_cmd(_, m):
    q = " ".join(m.command[1:])
    if not q.strip():
        return await m.reply_text("Send /search followed by a title, or just type the title.")
    await do_search(m, q)


@app.on_message(filters.command("status") & filters.private & is_auth)
async def status(_, m):
    busy = config.BROWSER_WORKERS - SLOTS.qsize()
    await m.reply_text(
        f"Browsers: {busy}/{config.BROWSER_WORKERS} busy\n"
        f"Downloads/uploads: {config.MAX_TRANSFERS - TRANSFERS._value}/{config.MAX_TRANSFERS} running\n"
        f"Your active jobs: {ACTIVE[m.from_user.id]}/{config.MAX_JOBS_PER_USER}"
    )


@app.on_message(filters.command("browse") & filters.private & is_auth)
async def browse(_, m):
    url = config.SITE_URL.rstrip("/") + "/" + config.BROWSE_PATH.lstrip("/")
    spawn({"type": "listing", "m": m, "url": url, "uid": m.from_user.id, "screen": 0})


@app.on_message(filters.private & filters.text & ~filters.regex(r"^https?://") & ~filters.regex(r"^/") & is_auth)
async def on_text(_, m):
    await do_search(m, m.text)  # plain text = search


@app.on_message(filters.private & filters.regex(r"^https?://\S+") & is_auth)
async def on_link(_, m):
    url = m.text.strip().split()[0]
    kind = classify(url)
    if not kind:
        return await m.reply_text(f"I only handle links from {SITE_HOST}.")
    spawn({"type": kind, "m": m, "url": url, "uid": m.from_user.id, "screen": 0})


@app.on_callback_query(filters.regex(r"^[epdglmco]\|"))
async def on_button(_, cq):
    if not _allowed(cq.from_user.id):
        return await cq.answer("Not allowed", show_alert=True)
    parts = cq.data.split("|")
    kind, token = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else ""
    uid = cq.from_user.id

    if kind == "m":  # main menu
        if token == "search":
            await cq.answer()
            return await cq.message.reply_text("🔍 Type the title you want to find (at least 2 letters).")
        if token == "home":
            await cq.answer()
            return await cq.message.reply_text(MENU_TEXT, parse_mode=HTML, reply_markup=menu_keyboard())
        if token == "back":
            await safe_edit(cq.message, MENU_TEXT, parse_mode=HTML, reply_markup=menu_keyboard())
            return await cq.answer()
        if token in ("genres", "studios"):
            text, kb = catalog_view("g" if token == "genres" else "s", 0)
            await safe_edit(cq.message, text, parse_mode=HTML, reply_markup=kb)
            return await cq.answer()
        if token in ("latest", "featured"):
            spawn({"type": "home", "m": cq.message, "which": token, "uid": uid})
            return await cq.answer("Loading...")
        if token in MENU_PATHS:
            spawn({"type": "listing", "m": cq.message, "url": site_url(MENU_PATHS[token]), "uid": uid, "screen": 0})
            return await cq.answer("Loading...")
        return await cq.answer()

    if kind == "c":  # genre / studio picker page
        text, kb = catalog_view(token, int(arg))
        await safe_edit(cq.message, text, parse_mode=HTML, reply_markup=kb)
        return await cq.answer()

    if kind == "o":  # genre / studio picked
        items = catalog.GENRES if token == "g" else catalog.STUDIOS
        name, slug = items[int(arg)]
        path = ("genre/" if token == "g" else "studio/") + slug + "/"
        spawn({"type": "listing", "m": cq.message, "url": site_url(path), "uid": uid, "screen": 0})
        return await cq.answer(f"Opening {clip(name, 30)}...")

    if kind == "p":  # episode list page
        res = SERIES.get(token)
        if not res:
            return await cq.answer("Expired, send the link again", show_alert=True)
        try:
            await cq.message.edit_reply_markup(episode_keyboard(token, res["episodes"], int(arg)))
        except Exception:
            pass
        return await cq.answer()

    if kind == "e":  # episode picked
        res = SERIES.get(token)
        if not res:
            return await cq.answer("Expired, send the link again", show_alert=True)
        ep = res["episodes"][int(arg)]
        spawn({"type": "episode", "m": cq.message, "url": ep["url"], "uid": uid,
               "label": ep_label(ep, int(arg)), "series_title": res["title"]})
        return await cq.answer(f"Opening {ep_label(ep, int(arg))}...")

    if kind == "l":  # item picked from a list: a series page or an episode page
        st = LISTINGS.get(token)
        if not st:
            return await cq.answer("Expired, send the link again", show_alert=True)
        it = st["res"]["items"][int(arg)]
        if classify(it["url"]) == "episode":
            spawn({"type": "episode", "m": cq.message, "url": it["url"], "uid": uid, "label": clip(it["title"], 30)})
        else:
            spawn({"type": "series", "m": cq.message, "url": it["url"], "uid": uid})
        return await cq.answer(f"Opening {clip(it['title'], 40)}...")

    if kind == "g":  # list navigation
        st = LISTINGS.get(token)
        if not st:
            return await cq.answer("Expired, send the link again", show_alert=True)
        res, cur = st["res"], st["screen"]
        screens = n_screens(res)

        def load(n, screen=0):  # another site page: scrape it, then edit this card
            url = page_url(res, n)
            if not url:
                return False
            spawn({"type": "listing", "m": cq.message, "url": url, "uid": uid, "screen": screen, "edit": cq.message})
            return True

        if arg == "x":
            return await cq.answer(f"Screen {cur + 1}/{screens}")
        if arg in ("n", "a"):
            step = 10 if arg == "a" else 1
            if cur + step < screens or (arg == "a" and cur < screens - 1):
                await render_listing(token, min(cur + step, screens - 1), edit_msg=cq.message)
                return await cq.answer()
            if arg == "n" and res["page"] < res["pages"] and load(res["page"] + 1):
                return await cq.answer(f"Loading page {res['page'] + 1}...")
            return await cq.answer("That's the end of the list")
        if arg in ("p", "m"):
            step = 10 if arg == "m" else 1
            if cur - step >= 0 or (arg == "m" and cur > 0):
                await render_listing(token, max(cur - step, 0), edit_msg=cq.message)
                return await cq.answer()
            if arg == "p" and res["page"] > 1 and load(res["page"] - 1, "last"):
                return await cq.answer(f"Loading page {res['page'] - 1}...")
            return await cq.answer("That's the start of the list")
        if arg == "f":
            if res["page"] > 1 and load(1):
                return await cq.answer("Loading page 1...")
            await render_listing(token, 0, edit_msg=cq.message)
            return await cq.answer()
        if arg == "z":
            if res["page"] < res["pages"] and load(res["pages"]):
                return await cq.answer(f"Loading page {res['pages']}...")
            await render_listing(token, screens - 1, edit_msg=cq.message)
            return await cq.answer()

    if kind == "d":  # quality picked
        if token not in EPISODES:
            return await cq.answer("Expired, open the episode again", show_alert=True)
        spawn({"type": "download", "m": cq.message, "token": token, "sel": arg, "uid": uid})
        return await cq.answer("Queued")


# ------------------------------------------------------------------ startup
async def init_slot(i):
    """Start Chrome + log in, one slot at a time (staggered), then offer the slot to jobs."""
    await asyncio.sleep(i * 4)
    job = {"t0": time.time(), "tag": f"warm{i}", "step": "warmup"}
    try:
        await asyncio.get_running_loop().run_in_executor(EXECUTORS[i], SCRAPERS[i], {"kind": "warmup", "job": job})
        logging.info("browser %d ready, logged in", i)
    except Exception as e:
        logging.warning("browser %d warmup failed (its first job will retry): %s", i, e)
    SLOTS.put_nowait(i)


async def main():
    global SLOTS, TRANSFERS
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))  # downloads / ffmpeg / thumbnails
    SLOTS = asyncio.Queue()
    TRANSFERS = asyncio.Semaphore(config.MAX_TRANSFERS)
    EXECUTORS.extend(ThreadPoolExecutor(max_workers=1) for _ in range(config.BROWSER_WORKERS))
    await app.start()
    for i in range(config.BROWSER_WORKERS):
        asyncio.create_task(init_slot(i))
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())