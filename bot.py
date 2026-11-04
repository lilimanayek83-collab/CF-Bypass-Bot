import asyncio
import logging
import os
import shutil
import subprocess
import time
import uuid

from pyrogram import Client, filters, idle

import config
from downloader import fetch
from scraper import DEBUG, STEP, get_download

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

browser_pool = None  # single thread: one browser at a time
queue: asyncio.Queue = asyncio.Queue()
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov")


def hum(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


def auth(_, __, m):
    return not config.AUTH_USERS or (m.from_user and m.from_user.id in config.AUTH_USERS)


is_auth = filters.create(auth)


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
    await safe_edit(st, f"⬆️ Uploading {hum(cur)} / {hum(total)} ({cur * 100 // max(total, 1)}%)")


async def process(m, url):
    loop = asyncio.get_running_loop()
    st = await m.reply_text("🔎 Opening page & passing Cloudflare...", quote=True)
    job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex[:8])
    try:
        fut = loop.run_in_executor(browser_pool, get_download, url)
        t0 = time.time()
        while True:
            try:
                info = await asyncio.wait_for(asyncio.shield(fut), timeout=5)
                break
            except asyncio.TimeoutError:
                el = int(time.time() - t0)
                if el > 240:
                    get_download.close()  # kill stuck Chrome so next job can run
                    raise Exception(f"Timed out at step: {STEP['name']}")
                await safe_edit(st, f"🔎 {STEP['name']} ({el}s)")

        state = {"done": 0, "total": 0, "name": ""}
        fut = loop.run_in_executor(None, fetch, info, job_dir, state)
        while not fut.done():
            await asyncio.sleep(4)
            t = state["total"]
            pct = f" ({state['done'] * 100 // t}%)" if t else ""
            await safe_edit(st, f"⬇️ Downloading {state['name']}\n{hum(state['done'])}"
                                f"{' / ' + hum(t) if t else ''}{pct}")
        path = await fut

        size = os.path.getsize(path)
        if size > config.MAX_SIZE:
            raise Exception("File is larger than 2 GB (Telegram bot limit)")

        box = [0.0]
        caption = os.path.basename(path)
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
        await st.delete()
    except Exception as e:
        logging.exception("job failed")
        await safe_edit(st, f"❌ Failed: {e}")
        shot = DEBUG.get("shot")
        if shot and os.path.exists(str(shot)):
            try:
                await m.reply_photo(str(shot), caption="What the browser saw")
            except Exception:
                pass
            DEBUG["shot"] = None
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def worker():
    while True:
        m, url = await queue.get()
        try:
            await process(m, url)
        finally:
            queue.task_done()


@app.on_message(filters.command("start") & filters.private & is_auth)
async def start(_, m):
    await m.reply_text("Send me a page link, I'll grab the download and upload it here.")


@app.on_message(filters.private & filters.regex(r"^https?://\S+") & is_auth)
async def on_link(_, m):
    url = m.text.strip().split()[0]
    pos = queue.qsize() + 1
    await queue.put((m, url))
    if pos > 1:
        await m.reply_text(f"Queued (#{pos})", quote=True)


async def main():
    global browser_pool
    from concurrent.futures import ThreadPoolExecutor
    browser_pool = ThreadPoolExecutor(max_workers=1)
    await app.start()
    asyncio.create_task(worker())
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
