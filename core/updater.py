# -*- coding: utf-8 -*-
"""基于 GitHub Releases 的在线更新。

流程：读取 latest release → 与当前版本比较 → 下载 exe 到临时目录 →
由批处理在进程退出后替换旧文件并重启。
"""
import os
import subprocess
import sys
import tempfile

import requests

OWNER = "jupiter-wei-sketch"
REPO = "HDU_Lib_AutoBook"
API_URL = "https://api.github.com/repos/{}/{}/releases/latest".format(OWNER, REPO)
RELEASES_URL = "https://github.com/{}/{}/releases/latest".format(OWNER, REPO)


def parse_version(text):
    out = []
    for item in (text or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def check_latest(current_version, timeout=8):
    """有新版本时返回版本信息，否则返回 None（网络异常也返回 None）。"""
    try:
        resp = requests.get(API_URL, timeout=timeout,
                            headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    tag = data.get("tag_name") or ""
    if not is_newer(tag, current_version):
        return None

    asset = None
    for item in data.get("assets") or []:
        if str(item.get("name") or "").lower().endswith(".exe"):
            asset = item
            break
    return {
        "version": tag.lstrip("vV"),
        "notes": (data.get("body") or "").strip(),
        "url": (asset or {}).get("browser_download_url") or data.get("html_url") or RELEASES_URL,
        "page": data.get("html_url") or RELEASES_URL,
        "size": int((asset or {}).get("size") or 0),
        "has_asset": asset is not None,
    }


def download(url, dest, progress=None, timeout=60):
    """流式下载，progress(已下载字节, 总字节) 可选。"""
    with requests.get(url, stream=True, timeout=timeout,
                      headers={"Accept": "application/octet-stream"}) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(256 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return dest


def apply_update(new_exe, target=None):
    """写一个批处理：等本进程退出 → 覆盖旧 exe → 启动新 exe。"""
    target = target or sys.executable
    exe_name = os.path.basename(target)
    script = os.path.join(tempfile.gettempdir(), "hdu_lib_autobook_update.bat")
    lines = [
        "@echo off",
        "ping -n 3 127.0.0.1 >nul",
        ":wait",
        'tasklist /fi "IMAGENAME eq {}" 2>nul | find /i "{}" >nul'.format(exe_name, exe_name),
        "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)",
        "set /a n=0",
        ":move",
        'move /y "{}" "{}" >nul 2>nul'.format(new_exe, target),
        "if not errorlevel 1 goto done",
        "set /a n+=1",
        "if %n% geq 20 goto done",
        "ping -n 2 127.0.0.1 >nul",
        "goto move",
        ":done",
        'start "" "{}"'.format(target),
        'del "%~f0"',
        "",
    ]
    with open(script, "w", encoding="gbk") as fh:
        fh.write("\r\n".join(lines))
    subprocess.Popen(["cmd", "/c", "start", "", script],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return script
