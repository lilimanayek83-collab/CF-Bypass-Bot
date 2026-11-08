"""MongoDB (Motor) store for the automation pipeline: tasks, scraped series/episodes, post templates."""
import time

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from templating import DEFAULT_TEMPLATES

SERIES_DONE_STATES = ("pending", "scanned", "done", "failed")
EP_STATES = ("pending", "working", "done", "failed")


def _ep_sort(e):
    return (e.get("n") is None, e.get("n") or 0, e.get("order") or 0)


class Store:
    def __init__(self, db):
        self.db = db
        self.tasks, self.series_c, self.eps = db["tasks"], db["series"], db["episodes"]
        self.tpl, self.counters = db["templates"], db["counters"]

    async def init(self):
        await self.series_c.create_index([("task", 1), ("url", 1)], unique=True)
        await self.series_c.create_index([("task", 1), ("status", 1), ("order", 1)])
        await self.eps.create_index([("task", 1), ("url", 1)], unique=True)
        await self.eps.create_index([("task", 1), ("series", 1), ("status", 1)])
        # anything that was mid-flight when the bot stopped goes back in the queue
        await self.eps.update_many({"status": "working"}, {"$set": {"status": "pending"}})
        for name, text in DEFAULT_TEMPLATES.items():
            await self.tpl.update_one({"_id": name}, {"$setOnInsert": {"text": text}}, upsert=True)

    async def _seq(self, name):
        d = await self.counters.find_one_and_update({"_id": name}, {"$inc": {"n": 1}}, upsert=True,
                                                    return_document=ReturnDocument.AFTER)
        return d["n"]

    # ---- templates
    async def template(self, name):
        d = await self.tpl.find_one({"_id": name})
        return d["text"] if d else DEFAULT_TEMPLATES[name]

    async def set_template(self, name, text):
        await self.tpl.update_one({"_id": name}, {"$set": {"text": text}}, upsert=True)

    async def reset_templates(self):
        for name, text in DEFAULT_TEMPLATES.items():
            await self.set_template(name, text)

    # ---- tasks
    async def create_task(self, spec):
        tid = await self._seq("task")
        now = time.time()
        doc = {**spec, "_id": tid, "status": "running", "created": now, "updated": now, "last_step": 0.0,
               "errors": 0, "phase": "collect" if spec["technique"] == "collect" else "process"}
        await self.tasks.insert_one(doc)
        return tid

    async def task(self, tid):
        return await self.tasks.find_one({"_id": tid})

    async def all_tasks(self):
        return await self.tasks.find({}).sort("_id", 1).to_list(None)

    async def running_tasks(self):
        return await self.tasks.find({"status": "running"}).to_list(None)

    async def set_task(self, tid, **f):
        f["updated"] = time.time()
        await self.tasks.update_one({"_id": tid}, {"$set": f})

    async def delete_task(self, tid):
        await self.eps.delete_many({"task": tid})
        await self.series_c.delete_many({"task": tid})
        await self.tasks.delete_one({"_id": tid})

    # ---- series
    async def add_series(self, task, url, title=""):
        if await self.series_c.find_one({"task": task, "url": url}):
            return "exists"
        try:
            await self.series_c.insert_one({"task": task, "url": url, "title": title, "status": "pending",
                                            "order": await self._seq("series"), "meta": {}, "sub_msg": None,
                                            "sub_photo": False, "tries": 0, "error": None})
        except DuplicateKeyError:
            return "exists"
        return "new"

    async def next_series(self, task, statuses):
        return await self.series_c.find_one({"task": task, "status": {"$in": list(statuses)}}, sort=[("order", 1)])

    async def series(self, task, url):
        return await self.series_c.find_one({"task": task, "url": url})

    async def set_series(self, task, url, **f):
        await self.series_c.update_one({"task": task, "url": url}, {"$set": f})

    async def save_scan(self, task, url, title, meta, episodes):
        await self.set_series(task, url, title=title, meta=meta, status="scanned")
        for e in episodes:
            await self.add_episode(task, e["url"], series=url, n=e.get("n"), label=e.get("label") or "")

    # ---- episodes
    async def add_episode(self, task, url, series=None, n=None, label=""):
        if await self.eps.find_one({"task": task, "url": url}):
            return "exists"
        try:
            await self.eps.insert_one({"task": task, "url": url, "series": series, "n": n, "label": label,
                                       "status": "pending", "files": [], "ep_msg": None, "tries": 0,
                                       "error": None, "order": await self._seq("episode")})
        except DuplicateKeyError:
            return "exists"
        return "new"

    async def episode(self, task, url):
        return await self.eps.find_one({"task": task, "url": url})

    async def pending_episodes(self, task, series):
        docs = await self.eps.find({"task": task, "series": series, "status": "pending"}).to_list(None)
        return sorted(docs, key=_ep_sort)

    async def episodes_of(self, task, series):
        docs = await self.eps.find({"task": task, "series": series}).to_list(None)
        return sorted(docs, key=_ep_sort)

    async def next_orphan(self, task):
        return await self.eps.find_one({"task": task, "series": None, "status": "pending"}, sort=[("order", 1)])

    async def set_episode(self, task, url, **f):
        await self.eps.update_one({"task": task, "url": url}, {"$set": f})

    async def fail_episode(self, task, url, error):
        await self.eps.update_one({"task": task, "url": url},
                                  {"$set": {"status": "failed", "error": error[:300]}, "$inc": {"tries": 1}})

    async def retry_failed(self, task):
        """Failed episodes and failed series go back in the queue. Returns how many episodes."""
        failed = await self.eps.find({"task": task, "status": "failed"}).to_list(None)
        for u in {e["series"] for e in failed if e.get("series")}:
            await self.series_c.update_one({"task": task, "url": u, "status": "done"}, {"$set": {"status": "scanned"}})
        await self.series_c.update_many({"task": task, "status": "failed"}, {"$set": {"status": "pending", "tries": 0, "error": None}})
        if failed:
            await self.eps.update_many({"task": task, "status": "failed"},
                                       {"$set": {"status": "pending", "tries": 0, "error": None}})
        return len(failed)

    async def counts(self, task):
        s = {st: await self.series_c.count_documents({"task": task, "status": st}) for st in SERIES_DONE_STATES}
        e = {st: await self.eps.count_documents({"task": task, "status": st}) for st in EP_STATES}
        return {"series": s, "episodes": e}