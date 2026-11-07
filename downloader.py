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


def fetch_image(info: dict, url: str, dest_dir: str) -> str:
    """Blocking. Download a poster/thumbnail with the browser's cookies, return a .jpg path."""
    os.makedirs(dest_dir, exist_ok=True)
    r = requests.get(
        url, headers={"User-Agent": info["ua"], "Referer": info["referer"]},
        cookies=info["cookies"], timeout=25,
    )
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "image" not in ct or len(r.content) > 9_000_000:
        raise Exception("not a usable image")
    raw = os.path.join(dest_dir, "img.bin")
    out = os.path.join(dest_dir, "img.jpg")
    with open(raw, "wb") as f:
        f.write(r.content)
    if "jpeg" in ct or "jpg" in ct:
        os.replace(raw, out)
        return out
    subprocess.run(  # png/webp -> jpg so Telegram always accepts it
        ["ffmpeg", "-y", "-i", raw, "-frames:v", "1", "-q:v", "3", out],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
    )
    return out


def fetch_bytes(info: dict, url: str) -> bytes:
    """Blocking. Small image download with the browser's cookies (poster grid tiles)."""
    r = requests.get(
        url, headers={"User-Agent": info["ua"], "Referer": info["referer"]},
        cookies=info["cookies"], timeout=20,
    )
    r.raise_for_status()
    if "image" not in r.headers.get("content-type", "") or len(r.content) > 5_000_000:
        raise Exception("not a usable image")
    return r.content