import os
import re
import subprocess
from urllib.parse import urlparse, unquote

import requests

import config


def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "file"
    return name[:180]


def _filename(resp, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return _safe(unquote(m.group(1)))
    return _safe(unquote(os.path.basename(urlparse(url).path)) or "video.mp4")


def fetch(info: dict, dest_dir: str, state: dict) -> str:
    """Blocking. Updates state['done'], state['total'], state['name']."""
    os.makedirs(dest_dir, exist_ok=True)
    url = info["url"]
    headers = {"User-Agent": info["ua"], "Referer": info["referer"]}

    if ".m3u8" in url:
        path = os.path.join(dest_dir, _safe(os.path.basename(urlparse(url).path) or "video") + ".mp4")
        hdr = f"User-Agent: {info['ua']}\r\nReferer: {info['referer']}\r\n"
        state["name"] = os.path.basename(path)
        subprocess.run(
            ["ffmpeg", "-y", "-headers", hdr, "-i", url, "-c", "copy", path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state["done"] = state["total"] = os.path.getsize(path)
        return path

    with requests.get(
        url, headers=headers, cookies=info["cookies"],
        stream=True, timeout=30, allow_redirects=True,
    ) as r:
        r.raise_for_status()
        if "text/html" in r.headers.get("content-type", ""):
            raise Exception("Got an HTML page instead of a file (link expired or blocked)")
        total = int(r.headers.get("content-length", 0))
        if total > config.MAX_SIZE:
            raise Exception("File is larger than 2 GB (Telegram bot limit)")
        name = _filename(r, url)
        path = os.path.join(dest_dir, name)
        state.update(name=name, total=total, done=0)
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                state["done"] += len(chunk)
    return path
