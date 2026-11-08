"""Task-based automation pipeline.

A task = link + technique + quality + three channels:
  FILE channel     the downloaded video files (caption: file template)
  EPISODE channel  one post per episode (thumbnail + episode template), button "Download" -> the file message
  SUB channel      one post per anime (poster + sub template), episode-number buttons -> the episode posts
Everything it learns is saved in MongoDB (store.py), so a task resumes after a restart.

Technique "collect": first collect ALL data (every list page + every anime page), then download + post.
Technique "onebyone": collect -> download -> post for one anime, then the next anime.
"""
import asyncio
import logging
import os
import re
import shutil
import time
import uuid

from pyrogram import enums
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import templating as T

log = logging.getLogger("pipeline")
HTML = enums.ParseMode.HTML
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov")
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
QUALITY_CHOICES = ("all", "1080p", "720p", "480p")
TYPES = {"TV", "OVA", "ONA", "MOVIE", "SPECIAL", "WEB", "TV SPECIAL", "MUSIC"}


class TooBig(Exception):
    pass


# ------------------------------------------------------------------ helpers
def human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def chan_name(c):
    if not c:
        return "not set"
    return f"{c.get('title') or c['id']} ({'@' + c['username'] if c.get('username') else c['id']})"


def msg_link(chan, msg_id):
    if chan.get("username"):
        return f"https://t.me/{chan['username']}/{msg_id}"
    cid = str(chan["id"])
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{msg_id}"


def candidates(links, q):
    """Qualities in the order they are tried: the wanted one, then the closest lower ones, then higher."""
    if q == "all":
        return list(links)
    want = int(re.sub(r"\D", "", q) or 0)

    def key(l):
        n = int(re.sub(r"\D", "", l["label"]) or 0)
        return (0, 0) if n == want else ((1, want - n) if n < want else (2, n - want))

    return sorted(links, key=key)


def series_values(row, total_fallback=0):
    m = row.get("meta") or {}
    f = m.get("fields") or {}
    chips = m.get("chips") or []
    cat = next((c for c in chips if c.upper() in TYPES), None) or (chips[1] if len(chips) > 1 else "")
    return {
        "title": row.get("title") or "", "alt_title": m.get("alt") or "", "catagory": cat or "N/A",
        "rating": f.get("score") or "N/A", "total_episode": f.get("episodes") or str(total_fallback or "?"),
        "aired_on": f.get("aired") or "N/A", "studio": f.get("studio") or "N/A",
        "genres": ", ".join(m.get("genres") or []) or "N/A", "synopsis": m.get("desc") or "",
        "status": chips[0] if chips else "", "duration": f.get("duration") or "",
    }


def orphan_values(emeta):
    """A single episode with no anime page: use what the episode page itself told us."""
    return {"title": emeta.get("title") or "", "alt_title": "", "catagory": "N/A", "rating": "N/A",
            "total_episode": "?", "aired_on": "N/A", "studio": "N/A",
            "genres": ", ".join(emeta.get("genres") or []) or "N/A", "synopsis": emeta.get("desc") or "",
            "status": "", "duration": ""}


# ------------------------------------------------------------------ the pipeline
class Pipeline:
    def __init__(self, app, db, deps):
        self.app, self.db, self.d = app, db, deps
        self.wake = asyncio.Event()
        self.loop_task = None
        self.current = {}   # task id -> what it is doing right now
        self.sess = {}      # (task id, series url) -> browser session for poster downloads

    # ---- control (used by the bot's commands)
    async def start_loop(self):
        if self.loop_task is None:
            self.loop_task = asyncio.create_task(self._loop())
        if await self.db.running_tasks():
            self.wake.set()

    async def add_task(self, spec):
        crawl = None
        if spec["kind"] == "listing":
            crawl = {"base": spec["link"], "tpl": "", "page": 1, "pages": None, "done": False}
        tid = await self.db.create_task({**spec, "crawl": crawl})
        if spec["kind"] == "series":
            await self.db.add_series(tid, spec["link"], "")
        elif spec["kind"] == "episode":
            await self.db.add_episode(tid, spec["link"])
        self.wake.set()
        return tid

    async def pause(self, tid):
        await self.db.set_task(tid, status="paused")

    async def resume(self, tid):
        await self.db.set_task(tid, status="running", errors=0)
        self.wake.set()

    async def delete(self, tid):
        await self.db.set_task(tid, status="paused")
        await self.db.delete_task(tid)
        self.current.pop(tid, None)

    async def retry(self, tid):
        n = await self.db.retry_failed(tid)
        if n:
            await self.resume(tid)
        return n

    async def alert(self, task, text):
        try:
            await self.app.send_message(task["admin_id"], text, parse_mode=HTML)
        except Exception as e:
            log.warning("alert failed: %s", e)

    # ---- main loop: one small step of one running task at a time, round-robin between tasks
    async def _loop(self):
        while True:
            await self.wake.wait()
            running = await self.db.running_tasks()
            if not running:
                self.wake.clear()
                continue
            task = min(running, key=lambda t: t.get("last_step") or 0)
            tid = task["_id"]
            try:
                did = await self._step(task)
                await self.db.set_task(tid, errors=0, last_step=time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("task %s step failed", tid)
                n = (task.get("errors") or 0) + 1
                await self.db.set_task(tid, errors=n, last_step=time.time())
                self.current[tid] = f"error: {e}"
                if n >= 5:
                    await self.db.set_task(tid, status="paused")
                    await self.alert(task, f"⚠️ <b>Task #{tid} paused</b> after 5 errors in a row.\n"
                                           f"Last error: {T.html.escape(str(e)[:300])}\nFix it, then resume it in /tasks.")
                else:
                    await asyncio.sleep(getattr(self.d, "error_wait", 30))
                continue
            if not did:
                await self.db.set_task(tid, status="done")
                self.current.pop(tid, None)
                await self.alert(task, await self._summary(tid, f"✅ <b>Task #{tid} finished</b>"))
                continue
            await asyncio.sleep(self.d.delay)

    async def _step(self, task):
        tid = task["_id"]
        collecting = task["technique"] == "collect" and task.get("phase") == "collect" and task["kind"] != "episode"
        if collecting:
            s = await self.db.next_series(tid, ["pending"])
            if s:
                await self._scan(task, s)
                return True
            if self._crawl_open(task):
                await self._crawl(task)
                return True
            c = await self.db.counts(tid)
            eps = sum(c["episodes"].values())
            await self.db.set_task(tid, phase="process")
            await self.alert(task, f"🗂 <b>Task #{tid}: data collected</b>\n{c['series']['scanned']} anime, {eps} episodes saved. "
                                   "Starting the downloads…")
            return True
        s = await self.db.next_series(tid, ["pending", "scanned"])
        if s:
            await self._series(task, s)
            return True
        o = await self.db.next_orphan(tid)
        if o:
            await self._episode(task, o, None)
            return True
        if self._crawl_open(task):
            await self._crawl(task)
            return True
        return False

    @staticmethod
    def _crawl_open(task):
        c = task.get("crawl")
        return bool(c) and not c.get("done")

    # ---- list pages -> anime queue
    async def _crawl(self, task):
        tid, c = task["_id"], dict(task["crawl"])
        page = c["page"]
        url = c["base"] if page == 1 else (c["tpl"].replace("987654321", str(page)) if c.get("tpl") else None)
        if not url:
            c["done"] = True
            return await self.db.set_task(tid, crawl=c)
        self.current[tid] = f"reading list page {page}/{c.get('pages') or '?'}"
        res = await self.d.scrape("listing", url)
        new = 0
        for it in res["items"]:
            if "/tvshows/" in it["url"] and await self.db.add_series(tid, it["url"], it["title"]) == "new":
                new += 1
        c["pages"] = res.get("pages") or 1
        if res.get("tpl"):
            c["tpl"] = res["tpl"]
        c["page"] = page + 1
        if c["page"] > c["pages"]:
            c["done"] = True
        await self.db.set_task(tid, crawl=c)
        log.info("task %s: list page %s -> %d new anime", tid, page, new)

    # ---- one anime page -> data saved in MongoDB
    async def _scan(self, task, row):
        tid, url = task["_id"], row["url"]
        self.current[tid] = f"{row.get('title') or url} · reading the anime page"
        last = None
        for attempt in range(2):
            try:
                res = await self.d.scrape("series", url)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last = e
                log.exception("scan failed: %s", url)
                if attempt == 0:
                    await asyncio.sleep(getattr(self.d, "retry_wait", 20))
        else:
            await self.db.set_series(tid, url, status="failed", error=str(last)[:300])
            return None
        meta = {k: res.get(k) for k in ("image", "desc", "genres", "alt", "chips", "fields")}
        self.sess[(tid, url)] = {k: res.get(k) for k in ("cookies", "ua", "referer")}
        await self.db.save_scan(tid, url, res["title"], meta, res["episodes"])
        return await self.db.series(tid, url)

    # ---- one anime: sub post, then each episode
    async def _series(self, task, row):
        tid, url = task["_id"], row["url"]
        if row["status"] == "pending":
            row = await self._scan(task, row)
            if not row:
                return
        meta = row.get("meta") or {}
        sub = task["sub_channel"]
        eps_all = await self.db.episodes_of(tid, url)

        if not row.get("sub_msg"):
            vals = {**series_values(row, len(eps_all)), "episode": "0"}
            text = T.render(await self.db.template("sub"), vals)
            img, d = await self._image(meta.get("image"), self.sess.get((tid, url)))
            try:
                msg, is_photo = await self._send_post(sub, text, img)
            finally:
                if d:
                    shutil.rmtree(d, ignore_errors=True)
            await self.db.set_series(tid, url, sub_msg=msg.id, sub_photo=is_photo)
            row = await self.db.series(tid, url)

        for ep in await self.db.pending_episodes(tid, url):
            fresh = await self.db.task(tid)
            if not fresh or fresh["status"] != "running":
                return  # paused or deleted: continue from this episode later
            await self._episode(task, ep, row)
            await asyncio.sleep(self.d.delay)

        if not await self.db.pending_episodes(tid, url):
            await self.db.set_series(tid, url, status="done")
            await self._refresh_sub_post(task, url)

    # ---- one episode: download -> file channel -> episode post -> sub post button
    async def _episode(self, task, ep, srow):
        tid, last = task["_id"], None
        for attempt in range(2):
            try:
                await self.db.set_episode(tid, ep["url"], status="working")
                await self._episode_once(task, ep, srow)
                return
            except asyncio.CancelledError:
                await self.db.set_episode(tid, ep["url"], status="pending")
                raise
            except Exception as e:
                last = e
                log.exception("episode failed: %s", ep["url"])
                if attempt == 0:
                    await asyncio.sleep(getattr(self.d, "retry_wait", 20))
        await self.db.fail_episode(tid, ep["url"], str(last))
        n = await self.db.eps.count_documents({"task": tid, "status": "failed"})
        if n % 5 == 1:
            await self.alert(task, f"❌ <b>Task #{tid}</b>: episode failed\n{T.html.escape(ep['url'])}\n"
                                   f"{T.html.escape(str(last)[:250])}\n(Retry from /tasks)")

    async def _episode_once(self, task, ep, srow):
        tid, url = task["_id"], ep["url"]
        fch, ech = task["file_channel"], task["episode_channel"]
        series_title = (srow or {}).get("title") or ""
        ep_num = str(ep["n"]) if ep.get("n") is not None else (ep.get("label") or "")
        self.current[tid] = f"{series_title or url} · EP {ep_num} · opening"
        res = await self.d.scrape("episode", url)
        links, emeta = res["links"], res["meta"]
        if srow:
            total = len(await self.db.episodes_of(tid, srow["url"]))
            base = series_values(srow, total)
        else:
            base = orphan_values(emeta)

        q = task["quality"]
        files = list(ep.get("files") or [])
        have = {f["label"] for f in files}
        for link in candidates(links, q):
            if q != "all" and files:
                break
            if link["label"] in have:
                continue
            try:
                f = await self._transfer(task, res, link, base, ep_num, fch)
            except TooBig:
                log.warning("%s %s is over the 2 GB limit, trying the next quality", url, link["label"])
                continue
            files.append(f)
            await self.db.set_episode(tid, url, files=files)
            have.add(f["label"])
        if not files:
            raise Exception("no quality could be uploaded (all over 2 GB or failed)")

        if not ep.get("ep_msg"):
            vals = {**base, "episode": ep_num, "quality": " · ".join(f["label"] for f in files),
                    "size": " · ".join(human_size(f["size"]) for f in files), "ep_title": emeta.get("title") or ""}
            text = T.render(await self.db.template("episode"), vals)
            kb = [[InlineKeyboardButton("⬇️ Download" if len(files) == 1 else f"⬇️ Download {f['label']}", url=f["link"])]
                  for f in files]
            img, d = await self._image(emeta.get("image"), res)
            try:
                msg, _ = await self._send_post(ech, text, img, InlineKeyboardMarkup(kb))
            finally:
                if d:
                    shutil.rmtree(d, ignore_errors=True)
            await self.db.set_episode(tid, url, ep_msg=msg.id)
        await self.db.set_episode(tid, url, status="done", error=None)
        if srow:
            await self._refresh_sub_post(task, srow["url"])

    async def _transfer(self, task, res, link, base, ep_num, fch):
        tid, loop = task["_id"], asyncio.get_running_loop()
        async with self.d.transfers:
            job_dir = os.path.join(self.d.download_dir, "pl_" + uuid.uuid4().hex[:8])
            try:
                self.current[tid] = f"{base['title']} · EP {ep_num} · {link['label']} · downloading"
                try:
                    path = await loop.run_in_executor(None, self.d.fetch, dict(res, url=link["url"]), job_dir,
                                                      {"done": 0, "total": 0, "name": ""})
                except Exception as e:
                    if "2 GB" in str(e):
                        raise TooBig(str(e))
                    raise
                size = os.path.getsize(path)
                if size > self.d.max_size:
                    raise TooBig(human_size(size))
                thumb, dur = await loop.run_in_executor(None, self.d.video_meta, path, job_dir)

                async def prog(cur, total):
                    self.current[tid] = (f"{base['title']} · EP {ep_num} · {link['label']} · "
                                         f"uploading {cur * 100 // max(total, 1)}%")

                vals = {**base, "episode": ep_num, "quality": link["label"], "size": human_size(size)}
                cap = T.render(await self.db.template("file"), vals)
                if path.lower().endswith(VIDEO_EXT):
                    msg = await self._tg(self.app.send_video, fch["id"], path, caption=cap, parse_mode=HTML,
                                         duration=dur, thumb=thumb, supports_streaming=True, progress=prog)
                else:
                    msg = await self._tg(self.app.send_document, fch["id"], path, caption=cap, parse_mode=HTML,
                                         progress=prog)
                return {"label": link["label"], "chat": fch["id"], "msg": msg.id, "size": size,
                        "link": msg_link(fch, msg.id)}
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)

    # ---- the anime post in the sub channel: caption + episode-number buttons -> episode posts
    async def _refresh_sub_post(self, task, series_url):
        tid = task["_id"]
        row = await self.db.series(tid, series_url)
        if not row or not row.get("sub_msg"):
            return
        sub, ech = task["sub_channel"], task["episode_channel"]
        eps = await self.db.episodes_of(tid, series_url)
        done = [e for e in eps if e["status"] == "done" and e.get("ep_msg")]
        btns = [InlineKeyboardButton(str(e["n"]) if e.get("n") is not None else str(i),
                                     url=msg_link(ech, e["ep_msg"])) for i, e in enumerate(done, 1)][:100]
        kb = InlineKeyboardMarkup([btns[i:i + 5] for i in range(0, len(btns), 5)]) if btns else None
        vals = {**series_values(row, len(eps)), "episode": str(len(done))}
        text = T.render(await self.db.template("sub"), vals)
        try:
            if row.get("sub_photo"):
                await self._tg(self.app.edit_message_caption, sub["id"], row["sub_msg"], text, parse_mode=HTML, reply_markup=kb)
            else:
                await self._tg(self.app.edit_message_text, sub["id"], row["sub_msg"], text, parse_mode=HTML, reply_markup=kb)
        except Exception as e:
            if "NOT_MODIFIED" not in str(e).upper():
                log.warning("sub post update failed: %s", e)

    # ---- telegram helpers
    async def _tg(self, fn, *a, **kw):
        for _ in range(6):
            try:
                return await fn(*a, **kw)
            except FloodWait as e:
                wait = int(getattr(e, "value", 10)) + 1
                log.warning("FloodWait %ss", wait)
                await asyncio.sleep(wait)
        raise Exception("Telegram flood limit, giving up on this request")

    async def _send_post(self, chan, text, img, kb=None):
        if img:
            try:
                return await self._tg(self.app.send_photo, chan["id"], img, caption=text, parse_mode=HTML,
                                      reply_markup=kb), True
            except Exception as e:
                log.warning("photo post failed, sending text: %s", e)
        return await self._tg(self.app.send_message, chan["id"], text, parse_mode=HTML, reply_markup=kb), False

    async def _image(self, url, sess):
        if not url:
            return None, None
        d = os.path.join(self.d.download_dir, "pl_img_" + uuid.uuid4().hex[:8])
        info = {"cookies": (sess or {}).get("cookies") or {}, "ua": (sess or {}).get("ua") or DEFAULT_UA,
                "referer": (sess or {}).get("referer") or url}
        try:
            path = await asyncio.get_running_loop().run_in_executor(None, self.d.fetch_image, info, url, d)
            return path, d
        except Exception as e:
            log.warning("image failed: %s", e)
            shutil.rmtree(d, ignore_errors=True)
            return None, None

    # ---- reporting
    async def _summary(self, tid, head):
        c = await self.db.counts(tid)
        return (f"{head}\nAnime done: {c['series']['done']} · failed: {c['series']['failed']}\n"
                f"Episodes done: {c['episodes']['done']} · failed: {c['episodes']['failed']}")

    async def status_text(self, tid):
        t = await self.db.task(tid)
        if not t:
            return "That task no longer exists."
        c = await self.db.counts(tid)
        s, e = c["series"], c["episodes"]
        icon = {"running": "▶️ running", "paused": "⏸ paused", "done": "✅ finished"}.get(t["status"], t["status"])
        tech = "Data Collect" if t["technique"] == "collect" else "One by one"
        if t["technique"] == "collect" and t["kind"] != "episode":
            tech += " (collecting)" if t.get("phase") == "collect" else " (downloading)"
        lines = [f"📋 <b>Task #{tid}</b> — {icon}", f"🔗 {T.html.escape(t['link'])}",
                 f"⚙️ {tech} · quality <b>{t['quality']}</b>",
                 f"📁 File: {T.html.escape(chan_name(t['file_channel']))}",
                 f"🎞 Episode: {T.html.escape(chan_name(t['episode_channel']))}",
                 f"📚 Sub: {T.html.escape(chan_name(t['sub_channel']))}",
                 f"Anime: {s['done']} done · {s['pending'] + s['scanned']} waiting · {s['failed']} failed",
                 f"Episodes: {e['done']} done · {e['pending'] + e['working']} waiting · {e['failed']} failed"]
        cr = t.get("crawl")
        if cr and not cr.get("done"):
            lines.append(f"Reading list page {cr['page']}/{cr.get('pages') or '?'}")
        if t["status"] == "running" and self.current.get(tid):
            lines.append(f"Now: {T.html.escape(self.current[tid])}")
        return "\n".join(lines)