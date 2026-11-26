#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_panel.py —— Agent 生产控制面板（本地 Web 面板，127.0.0.1:8899）

回答三个问题：
  1. 什么时候、在哪个项目干过活        → 时间轴（按天分组）
  2. 每个文件是哪个工具的产出           → 归因标记（DSH / WorkBuddy / Trae(git) / 未知）
  3. 哪些旧文件可以删                   → 三档清理（点击进废纸篓，可一键还原）

归因原理（通用「活动窗口 × 文件时间」相关法）：
  · dsh:       ~/.dsh/sessions/--编码项目路径--/session-* 目录的时间范围
  · workbuddy: ~/.workbuddy/projects/<项目编码>/*.jsonl 的时间范围
  · trae:      项目 git 提交（path→提交时间 精确到文件）
  · 豆包:      暂无日志 → 归因「未知来源」（接入后即可标记）
  文件的 创建/修改时间 落在某个窗口内 → 标记为该工具。

用法：
  python3 project_panel.py [--port 8899] [--open]
"""

import argparse
import contextlib
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_map as pm

HOME = Path.home()
PANEL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PANEL_DIR / "panel_config.json"
MANIFEST_DIR = PANEL_DIR / "manifests"
PORT = 8899
SLACK = 600                      # 归因容差：会话结束后 10 分钟内的写入也算
SCAN_TTL = 60                    # 索引缓存秒数
BOARD_TTL = 180                  # 晾衣绳看板缓存秒数

def _load_panel_token():
    """令牌落盘复用：面板重启后，已打开的页面不会因为换 token 而全变 403"""
    f = PANEL_DIR / ".panel_token"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    try:
        PANEL_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
        os.chmod(f, 0o600)
    except OSError:
        pass
    return t

PANEL_TOKEN = _load_panel_token()   # 防本机其他网页/进程伪造请求

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
THUMB_PREF = ("封面", "cover", "预览", "preview", "截图", "screenshot",
              "头像", "avatar", "poster", "主视觉", "抠图", "koutu")
THUMB_NO = ("stickers", "表情", "library", "icon", "logo", "sprite")
CLUSTER_MAX = 40
BOARD_HASH_MAX = 8000        # 看板：小于这个规模才算重复副本哈希
INDEX_DEEP_MAX = 15000       # 详情：超过这个规模只做轻量索引
AGENTS = {
    "dsh": {"label": "DSH", "color": "#a78bfa"},
    "workbuddy": {"label": "WorkBuddy", "color": "#fb923c"},
    "trae": {"label": "Trae(git)", "color": "#38bdf8"},
    "doubao": {"label": "豆包", "color": "#34d399"},
    "unknown": {"label": "未知来源", "color": "#71717a"},
}


# ---------------- 归因数据源 ----------------

def decode_agent_path(name: str) -> str:
    """--Users-<用户名>-77~0020~7684~9879~76EE-deepbai-- → /Users/<用户名>/77 的项目/deepbai
    Users-<用户名>-77 的项目-白白 → /Users/<用户名>/77 的项目/白白"""
    s = name.strip("-")
    s = re.sub(r"~([0-9A-Fa-f]{4})~", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("-", "/")
    return s if s.startswith("/") else "/" + s


def dir_time_range(d: Path):
    """目录里所有文件（含子目录一层）的最小/最大 mtime"""
    ts = []
    try:
        for p in d.rglob("*"):
            if p.is_file():
                ts.append(p.stat().st_mtime)
        if not ts:
            ts = [d.stat().st_mtime]
    except OSError:
        ts = [d.stat().st_mtime if d.exists() else 0]
    return min(ts), max(ts)


def collect_dsh_windows():
    """DSH 会话目录 → (项目路径, 开始, 结束)"""
    wins = []
    root = HOME / ".dsh" / "sessions"
    if not root.is_dir():
        return wins
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir() or not proj_dir.name.startswith("-"):
            continue
        proj = Path(decode_agent_path(proj_dir.name))
        for s in proj_dir.iterdir():
            if s.is_dir() and s.name.startswith("session-"):
                t0, t1 = dir_time_range(s)
                if t1 > 0:
                    wins.append({"agent": "dsh", "project": str(proj),
                                 "start": t0, "end": t1})
    return wins


def collect_workbuddy_windows():
    """WorkBuddy 项目目录 → (项目路径, 开始, 结束)"""
    wins = []
    root = HOME / ".workbuddy" / "projects"
    if not root.is_dir():
        return wins
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        proj = Path(decode_agent_path(proj_dir.name))
        t0, t1 = dir_time_range(proj_dir)
        # 优先用 jsonl 里声明的真实工作区
        ws = None
        for f in list(proj_dir.glob("*.jsonl"))[:3]:
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:8000]
                m = re.search(r"Workspace Folder:\s*([^\n<]+)", head)
                if m:
                    ws = m.group(1).strip()
                    break
            except OSError:
                pass
        if ws and Path(ws).is_dir():
            proj = Path(ws)
        if t1 > 0:
            wins.append({"agent": "workbuddy", "project": str(proj),
                         "start": t0, "end": t1})
    return wins


def collect_trae_paths(project: Path):
    """git 提交 → {文件绝对路径: 提交时间}（Trae 用 git 干活，提交者常是用户身份）"""
    out = {}
    try:
        r = subprocess.run(
            ["git", "-C", str(project), "log", "--format=@@%at", "--name-only"],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return out
    if r.returncode != 0:
        return out
    ts = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("@@"):
            ts = float(line[2:])
        elif line and ts:
            out[str((project / line).resolve())] = ts
    return out


def attribute_files(files, windows, trae_paths, project_root):
    """给每个文件打归因标记：新建自/修改自 哪个工具"""
    for f in files:
        ap = str((project_root / f["rel"]).resolve())
        tag = conf = None
        if ap in trae_paths:
            tag, conf = "trae", "确定"
        else:
            for w in windows:
                if w["start"] - SLACK <= f["btime"] <= w["end"] + SLACK:
                    tag, conf = w["agent"], "推测"
                    break
        f["attribution"] = {"agent": tag or "unknown", "confidence": conf or "-"}


# ---------------- 项目索引 ----------------

def load_config():
    cfg = {"projects": []}
    if CONFIG_PATH.is_file():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    projs = set(cfg.get("projects", []))
    # 默认：DSH/WorkBuddy 日志里出现过的项目 + 77 的项目 的子目录
    for w in collect_dsh_windows() + collect_workbuddy_windows():
        p = Path(w["project"])
        if p.is_dir() and p.name and not p.name.startswith("."):
            if not any(seg in ("Applications", "System", "Library", "private", "opt")
                       for seg in p.parts[:3]):
                projs.add(w["project"])
    base_env = os.environ.get("PANEL_PROJECTS_DIR", "").strip()
    if base_env:
        base = Path(base_env).expanduser()
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    projs.add(str(d))
    # 跨机器：用环境变量指定项目根（Windows 用 ; 分隔）
    for p in (os.environ.get("PANEL_PROJECTS") or "").split(os.pathsep):
        p = p.strip()
        if p and Path(p).expanduser().is_dir():
            projs.add(str(Path(p).expanduser().resolve()))
    cfg["projects"] = sorted(projs)
    return cfg


_cache = {"projects": {}, "built": 0}
_wins_cache = {"built": 0, "by_proj": {}, "trae": {}}
WINS_TTL = 300


def gather_windows(projects):
    """把所有工具的窗口按项目归拢；trae 精确到文件路径（5 分钟缓存）"""
    now = time.time()
    if now - _wins_cache["built"] < WINS_TTL and _wins_cache["by_proj"]:
        return _wins_cache["by_proj"], _wins_cache["trae"]
    wins = collect_dsh_windows() + collect_workbuddy_windows()
    by_proj = defaultdict(list)
    for w in wins:
        by_proj[w["project"]].append(w)
    trae_paths = {}
    for p in projects:
        trae_paths.update(collect_trae_paths(Path(p)))
    _wins_cache.update({"built": now, "by_proj": by_proj, "trae": trae_paths})
    return by_proj, trae_paths


def index_project(path_str):
    """完整索引一个项目（扫描+版本链+三档+归因+堆叠），带 TTL 缓存。
    超大项目只做轻量索引，避免把面板拖死。"""
    now = time.time()
    c = _cache["projects"].get(path_str)
    if c and now - c["built"] < SCAN_TTL:
        return c
    root = Path(path_str)
    quick, quick_runtime = _walk_stat(root)
    light = len(quick) > INDEX_DEEP_MAX

    if light:
        files = [{**f, "text": False, "data": None, "head_h": None, "tail_h": None}
                 for f in quick]
        runtime = quick_runtime
        tiers = {"1": [], "2": [], "3": []}
        clusters = []
    else:
        files, runtime = pm.scan(root)
        chains, orphans = pm.build_chains(files)
        tiers = pm.compute_tiers(files, chains, orphans)
        ref = pm.build_reference_text(files)
        clusters = build_clusters(files, ref)

    by_proj, trae_paths = gather_windows([path_str])
    attribute_files(files, by_proj.get(str(root.resolve()), []),
                    trae_paths, root)
    last = max((f["mtime"] for f in files), default=0)
    c = {"built": now, "root": str(root), "files": files,
         "runtime": runtime, "tiers": tiers, "clusters": clusters,
         "light": light, "last_activity": last,
         "stats": {"total": len(files),
                   "runtime_total": sum(runtime.values()),
                   "tier1": len(tiers["1"]), "tier2": len(tiers["2"]),
                   "tier3": len(tiers["3"]),
                   "clusters": len(clusters),
                   "isolatable": sum(len(x["isolatable"]) for x in clusters),
                   "light": light}}
    _cache["projects"][path_str] = c
    return c


# ---------------- 版本堆叠 ----------------

def _referenced(rel: str, ref: str) -> bool:
    return Path(rel).name in ref


def _select_keep(group, ref: str):
    """从一堆副本里挑「正主」：被代码引用 > 最新修改 > 路径最短"""
    def score(f):
        return (1 if _referenced(f["rel"], ref) else 0, f["mtime"],
                -len(f["rel"]))
    return max(group, key=score)


def build_clusters(files, ref: str):
    """把「同一份文件堆了好几遍」摊开：完全一致组 + 同目录同名系列"""
    out = []

    # A. 字节级完全一致
    by_hash = defaultdict(list)
    for f in files:
        if f["size"] > 0 and not f.get("sampled"):
            by_hash[f["hash"]].append(f)
    for _h, g in by_hash.items():
        if len(g) < 2:
            continue
        if Path(g[0]["rel"]).name in ("__init__.py", ".gitkeep"):
            continue
        keep = _select_keep(g, ref)
        iso = [x["rel"] for x in g
               if x["rel"] != keep["rel"] and not _referenced(x["rel"], ref)]
        out.append({
            "kind": "identical",
            "title": Path(keep["rel"]).name,
            "dirs": sorted({str(Path(x["rel"]).parent) for x in g})[:6],
            "keep": keep["rel"],
            "isolatable": iso,
            "files": [{"rel": x["rel"], "size": x["size"], "mtime": x["mtime"],
                       "keep": x["rel"] == keep["rel"],
                       "referenced": _referenced(x["rel"], ref)} for x in g],
        })

    # B. 同目录、归一化后同名、但内容有差异（真正的「版本」）
    by_key = defaultdict(list)
    for f in files:
        by_key[(str(Path(f["rel"]).parent), f["key"])].append(f)
    for (_d, _k), g in by_key.items():
        if len(g) < 2:
            continue
        if len({x["hash"] for x in g}) == 1:
            continue                      # 完全一致已在上面的 A 组里
        keep = _select_keep(g, ref)
        out.append({
            "kind": "same_name",
            "title": Path(keep["rel"]).name,
            "dirs": [str(Path(keep["rel"]).parent)],
            "keep": keep["rel"],
            "isolatable": [],
            "files": [{"rel": x["rel"], "size": x["size"], "mtime": x["mtime"],
                       "keep": x["rel"] == keep["rel"],
                       "referenced": _referenced(x["rel"], ref)} for x in g],
        })

    out.sort(key=lambda c: (0 if c["kind"] == "identical" else 1,
                            -len(c["files"]),
                            -max(f["mtime"] for f in c["files"])))
    return out[:CLUSTER_MAX]


def archive_versions(root_str, rels, reason="旧版本归档"):
    """把旧副本挪进 <项目>/_versions/<时间戳>/，保留相对路径，写可还原清单。
    不动最新那份，不删除任何东西。"""
    root = Path(root_str).resolve()
    if str(root) not in registered_roots():
        return {"ok": False, "error": "项目未登记"}
    batch = root / "_versions" / datetime.now().strftime("%Y%m%d_%H%M%S")
    actions = []
    for rel in list(rels)[:200]:
        try:
            src = (root / str(rel)).resolve()
            src.relative_to(root)
        except (OSError, ValueError):
            continue
        if not src.is_file() or "_versions" in src.parts:
            continue
        try:
            sha = pm.file_sha256(src)
            size = src.stat().st_size
        except OSError:
            continue
        dst = batch / src.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except OSError as e:
            return {"ok": False, "error": f"移动失败：{e}"}
        actions.append({
            "original": str(root / str(rel)), "moved_to": str(dst),
            "sha256": sha, "size": size, "reason": reason,
            "tier": "archive", "moved_at": datetime.now().isoformat(timespec="seconds"),
        })
    if not actions:
        return {"ok": False, "error": "没有可归档的文件"}
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    mf = MANIFEST_DIR / f"archive_{batch.name}.json"
    mf.write_text(json.dumps({
        "version": 2, "mode": "archive", "project_root": str(root),
        "batch_dir": str(batch),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "actions": actions,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _board["built"] = 0
    return {"ok": True, "moved": len(actions), "batch": str(batch),
            "manifest": str(mf)}


def _thumb_rank(f) -> tuple:
    rel = f["rel"].lower()
    pref = 2 if any(x in rel for x in THUMB_PREF) else 1
    ext = Path(rel).suffix
    fmt = 3 if ext in (".png", ".webp") else 2 if ext in (".jpg", ".jpeg") else 1
    return (pref, fmt, f["mtime"])


def _image_ok(p: Path, ext: str) -> bool:
    """确认是真图：校验文件头，挡住 12 字节的假 gif 和空文件"""
    try:
        if p.stat().st_size < 2048:
            return False
        head = p.open("rb").read(16)
    except OSError:
        return False
    if ext == ".png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if ext == ".gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return True


def pick_thumb(files):
    best = None
    for f in files:
        rel = f["rel"].lower()
        ext = Path(rel).suffix
        if ext not in IMG_EXT or f["size"] > 8 * 1024 * 1024:
            continue
        if any(x in rel for x in THUMB_NO):
            continue
        if not _image_ok(f["path"], ext):
            continue
        if best is None or _thumb_rank(f) > _thumb_rank(best):
            best = f
    return best


def _walk_stat(root):
    """只看 stat、不读内容：大项目也能秒回（看板/大项目判定都用它）"""
    files, runtime = [], defaultdict(int)
    for p, rel in pm._iter_files(root, runtime):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append({
            "path": p, "rel": rel, "size": st.st_size,
            "mtime": st.st_mtime,
            "btime": pm.file_birth(st),
            "key": pm.normalize_stem(p.stem) + "|" + p.suffix.lower(),
            "hash": None,
        })
    return files, runtime


def _hash_files(files, cap=8 * 1024 * 1024):
    for f in files:
        if f["size"] > cap:
            f["hash"] = None          # 大文件不参与重复副本统计
            continue
        h = hashlib.md5()
        try:
            with open(f["path"], "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
            f["hash"] = h.hexdigest()
        except OSError:
            f["hash"] = None


def board_scan(path_str):
    """轻量扫描：stat 走一遍，小项目再哈希；大项目不算重复副本"""
    root = Path(path_str)
    files, runtime = _walk_stat(root)
    big = len(files) > BOARD_HASH_MAX
    if not big:
        _hash_files(files)

    today = datetime.now().date()
    days = defaultdict(int)
    created_today = modified_today = 0
    for f in files:
        md = datetime.fromtimestamp(f["mtime"]).date()
        days[md] += 1
        if md == today:
            modified_today += 1
        if datetime.fromtimestamp(f["btime"]).date() == today:
            created_today += 1

    series = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        series.append({"d": d.strftime("%m-%d"), "n": days.get(d, 0)})

    by_key = defaultdict(int)
    for f in files:
        by_key[f["key"]] += 1
    chains = sum(1 for n in by_key.values() if n > 1)

    dup_files = None
    dup_groups = None
    stack_by_rel = {}
    if not big:
        by_hash = defaultdict(list)
        for f in files:
            if f["size"] > 0 and f["hash"]:
                by_hash[f["hash"]].append(f)
        groups = [g for g in by_hash.values()
                  if len(g) > 1 and Path(g[0]["rel"]).name not in
                  ("__init__.py", ".gitkeep")]
        dup_groups = len(groups)
        dup_files = sum(len(g) - 1 for g in groups)
        for g in groups:
            for f in g:
                stack_by_rel[f["rel"]] = len(g) - 1

    # 变化清单：只要「最近动过的文件」，不要全量清单
    cutoff7 = time.time() - 7 * 86400
    changed = []
    for f in files:
        if f["mtime"] < cutoff7:
            continue
        md = datetime.fromtimestamp(f["mtime"])
        new_file = datetime.fromtimestamp(f["btime"]).date() == md.date()
        changed.append({
            "rel": f["rel"], "ts": f["mtime"],
            "date": md.strftime("%Y-%m-%d"), "time": md.strftime("%H:%M"),
            "kind": "create" if new_file else "modify",
            "size": f["size"],
            "stack": stack_by_rel.get(f["rel"], 0),
            "today": md.date() == today,
        })
    changed.sort(key=lambda x: -x["ts"])
    chg_today_total = sum(1 for c in changed if c["today"])
    chg_week_total = len(changed)
    changed = changed[:60]

    last = max((f["mtime"] for f in files), default=0)
    thumb = pick_thumb(files)
    return {
        "name": root.name, "root": str(root),
        "files": len(files), "runtime": sum(runtime.values()), "big": big,
        "last_activity": last,
        "days_since": (today - datetime.fromtimestamp(last).date()).days if last else 999,
        "today": {"created": created_today, "modified": modified_today,
                  "total": modified_today},
        "series": series,
        "chains": chains,
        "dups": dup_groups,
        "dup_files": dup_files,
        "changed": changed,
        "changed_today_total": chg_today_total,
        "changed_week_total": chg_week_total,
        "tiers": None,
        "thumb": ({"rel": thumb["rel"], "mtime": thumb["mtime"]}
                  if thumb else None),
    }


_board = {"built": 0, "data": None}


def board_payload():
    now = time.time()
    if _board["data"] is not None and now - _board["built"] < BOARD_TTL:
        return _board["data"]
    cfg = load_config()
    roots = [p for p in cfg["projects"] if Path(p).is_dir()]
    projects = []
    if roots:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for item in pool.map(lambda p: _safe_board_scan(p), roots):
                if item:
                    projects.append(item)
    # 有今天动静的排前面，其次按最近活动
    projects.sort(key=lambda p: (-(p["today"]["total"] > 0),
                                 -p["last_activity"]))
    total_today = sum(1 for p in projects if p["today"]["total"] > 0)
    data = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "today_label": datetime.now().strftime("%m月%d日"),
            "projects": projects,
            "active_today": total_today}
    _board.update({"built": now, "data": data})
    return data


def _safe_board_scan(p):
    try:
        item = board_scan(p)
        cached = _cache["projects"].get(p)
        if cached:
            item["tiers"] = {"1": cached["stats"]["tier1"],
                             "2": cached["stats"]["tier2"],
                             "3": cached["stats"]["tier3"]}
        return item
    except Exception as e:
        print(f"⚠️ 看板扫描失败 {p}: {e}", file=sys.stderr, flush=True)
        return None


def build_timeline(c):
    days = defaultdict(lambda: {"created": [], "modified": []})
    for f in c["files"]:
        a = f["attribution"]
        for key, t in (("created", f["btime"]), ("modified", f["mtime"])):
            dt = datetime.fromtimestamp(t)
            day = dt.strftime("%m-%d")
            days[day][key].append({
                "path": f["rel"], "time": dt.strftime("%H:%M"),
                "agent": a["agent"], "conf": a["confidence"],
                "size": f["size"],
            })
    for d in days.values():
        d["created"].sort(key=lambda x: x["time"], reverse=True)
        d["modified"].sort(key=lambda x: x["time"], reverse=True)
        d["created"] = d["created"][:80]
        d["modified"] = d["modified"][:80]
    # 同一天内 新建 与 修改 去重：新建优先
    created_paths = defaultdict(set)
    for day, d in days.items():
        for e in d["created"]:
            created_paths[day].add(e["path"])
        d["modified"] = [e for e in d["modified"] if e["path"] not in created_paths[day]]
    return [{"day": k, **v} for k, v in
            sorted(days.items(), reverse=True)]


def read_events(root: str, limit=500):
    """watcher 事件流（返回最新的在前）"""
    key = hashlib.md5(root.encode("utf-8")).hexdigest()[:10]
    f = PANEL_DIR / "events" / f"events_{key}.jsonl"
    if not f.is_file():
        return [], 0, None
    events = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    total = len(events)
    since = events[0]["ts"] if events else None
    return events[-limit:][::-1], total, since


# ---------------- HTTP 服务 ----------------

def state_payload():
    """轻量项目列表：不深度扫描，点击项目时才加载详情"""
    cfg = load_config()
    projects = []
    for p in cfg["projects"]:
        root = Path(p)
        if not root.is_dir():
            continue
        try:
            last = root.stat().st_mtime
        except OSError:
            last = 0
        cached = _cache["projects"].get(p)
        projects.append({
            "name": root.name, "root": p,
            "last_activity": cached["last_activity"] if cached else last,
            "stats": cached["stats"] if cached else None,
        })
    projects.sort(key=lambda x: -x["last_activity"])
    return {"projects": projects, "agents": AGENTS}


def registered_roots():
    """已登记项目的绝对路径（清理/还原只允许发生在这些目录里）"""
    try:
        return {str(Path(p).resolve()) for p in load_config()["projects"]
                if Path(p).is_dir()}
    except Exception:
        return set()


def _host_ok(h) -> bool:
    """挡 DNS rebinding：只接受本机 Host"""
    host = (h.headers.get("Host") or "").strip()
    hostname = host.split(":")[0].strip("[]").lower()
    return hostname in ("127.0.0.1", "localhost", "::1")


def _token_ok(h) -> bool:
    got = h.headers.get("X-Panel-Token") or ""
    return hmac.compare_digest(got, PANEL_TOKEN)


def _json_ok(h) -> bool:
    ct = (h.headers.get("Content-Type") or "").lower()
    return ct.startswith("application/json")


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not _host_ok(self):
            return self._send({"error": "host not allowed"}, 403)
        u = urlparse(self.path)
        if u.path == "/":
            html = (PANEL_DIR / "panel.html").read_text(encoding="utf-8")
            data = html.replace("__PANEL_TOKEN__", PANEL_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/thumb":
            # <img> 带不了请求头，所以缩略图用 URL 里的 k 校验
            q = parse_qs(u.query)
            if not hmac.compare_digest((q.get("k") or [""])[0], PANEL_TOKEN):
                return self._send({"error": "token required"}, 403)
            root = (q.get("root") or [""])[0]
            rel = (q.get("rel") or [""])[0]
            try:
                root_r = Path(root).resolve()
            except (OSError, RuntimeError):
                return self._send({"error": "bad root"}, 400)
            if str(root_r) not in registered_roots():
                return self._send({"error": "not registered"}, 403)
            p = (root_r / rel).resolve()
            if root_r not in p.parents or not p.is_file():
                return self._send({"error": "not found"}, 404)
            if p.suffix.lower() not in IMG_EXT or p.stat().st_size > 12 * 1024 * 1024:
                return self._send({"error": "not an image"}, 404)
            data = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=600")
            self.end_headers()
            self.wfile.write(data)
            return
        if not _token_ok(self):
            return self._send({"error": "token required"}, 403)
        if u.path == "/api/state":
            self._send(state_payload())
        elif u.path == "/api/project":
            q = parse_qs(u.query)
            root = (q.get("root") or [""])[0]
            if not root:
                return self._send({"error": "缺少 root"}, 400)
            try:
                c = index_project(root)
            except OSError as e:
                return self._send({"error": str(e)}, 500)
            raw = {}
            if CONFIG_PATH.is_file():
                try:
                    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = {}
            listed = list(raw.get("projects") or [])
            if root not in listed:
                listed.append(root)
                raw["projects"] = listed
                CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            all_files = [{
                "path": f["rel"], "size": f["size"], "btime": f["btime"],
                "mtime": f["mtime"], "attribution": f["attribution"],
            } for f in c["files"]]
            files_total = len(all_files)
            # 大项目别把几十 MB 文件清单塞给浏览器：按 mtime 取最新 800 条
            files = sorted(all_files, key=lambda x: -x["mtime"])[:800]
            # 7 天火花：按全量算，不受上面截断影响
            from collections import Counter
            day_counts = Counter(datetime.fromtimestamp(f["mtime"]).strftime("%m-%d")
                                 for f in all_files
                                 if f["mtime"] >= time.time() - 7 * 86400)
            series7 = []
            for i in range(6, -1, -1):
                d0 = datetime.now() - timedelta(days=i)
                series7.append({"d": d0.strftime("%m-%d"),
                                "n": day_counts.get(d0.strftime("%m-%d"), 0)})
            # 合并 watcher 实时事件（📡 确定级记录）
            events, ev_total, ev_since = read_events(root)
            timeline = build_timeline(c)
            live_days = defaultdict(list)
            for ev in events:
                dt = datetime.fromtimestamp(ev["ts"])
                live_days[dt.strftime("%m-%d")].append({
                    "path": ev["path"], "time": dt.strftime("%H:%M"),
                    "agent": ev.get("agent", "unknown"),
                    "conf": ev.get("conf", "-"),
                    "kind": ev["type"], "live": True, "from": ev.get("from"),
                })
            tmap = {d["day"]: d for d in timeline}
            for day, rows in live_days.items():
                if day not in tmap:
                    tmap[day] = {"day": day, "created": [], "modified": []}
                tmap[day].setdefault("live", []).extend(rows)
            timeline = sorted(tmap.values(), key=lambda d: d["day"], reverse=True)[:40]
            for d0 in timeline:
                d0["created"] = d0["created"][:80]
                d0["modified"] = d0["modified"][:80]
                if "live" in d0:
                    d0["live"] = d0["live"][:80]
            self._send({
                "root": c["root"], "stats": c["stats"],
                "runtime": c["runtime"], "tiers": c["tiers"],
                "clusters": c["clusters"], "light": c.get("light", False),
                "timeline": timeline,
                "files": files, "files_total": files_total, "series7": series7,
                "events": {"total": ev_total, "since": ev_since},
            })
        elif u.path == "/api/board":
            self._send(board_payload())
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not (_host_ok(self) and _json_ok(self) and _token_ok(self)):
            return self._send({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        if u.path == "/api/trash":
            req = self._body()
            files = req.get("files") or []
            try:
                root_r = Path(req.get("root") or "").resolve()
            except (OSError, RuntimeError):
                return self._send({"error": "root 无效"}, 400)
            if not root_r.is_dir():
                return self._send({"error": "root 不存在"}, 400)
            # 白名单：只允许动已登记项目里的文件
            if str(root_r) not in registered_roots():
                return self._send({"error": "该项目未登记，拒绝清理"}, 403)
            batch = pm.QUARANTINE_ROOT / f"{root_r.name}_{time.strftime('%Y%m%d_%H%M%S')}"
            moved, skipped = [], []
            for f in files:
                p = (root_r / f).resolve()
                if not p.is_file() or root_r not in p.parents:
                    skipped.append({"path": f, "why": "不存在或不在项目内"})
                    continue
                try:
                    size = p.stat().st_size
                    digest = pm.file_sha256(p)
                    dst = pm.move_to_quarantine(p, root_r, batch)
                except OSError as e:
                    skipped.append({"path": f, "why": f"移动失败：{e}"})
                    continue
                moved.append({
                    "original": str(p), "moved_to": str(dst),
                    "sha256": digest, "size": size,
                    "reason": req.get("reason", "面板清理"),
                    "tier": req.get("tier", 1),
                    "moved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            mf = MANIFEST_DIR / f"panel_{time.strftime('%Y%m%d_%H%M%S')}.json"
            payload = {
                "version": 2, "mode": "quarantine",
                "project_root": str(root_r), "batch_dir": str(batch),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "actions": moved,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            mf.write_text(text, encoding="utf-8")
            if moved:
                batch.mkdir(parents=True, exist_ok=True)
                (batch / "manifest.json").write_text(text, encoding="utf-8")
            self._send({"moved": len(moved), "skipped": skipped,
                        "manifest": str(mf), "mode": "quarantine"})
        elif u.path == "/api/restore":
            req = self._body()
            mf = Path(req.get("manifest") or "")
            try:
                mf_r = mf.resolve()
            except (OSError, RuntimeError):
                return self._send({"error": "manifest 无效"}, 400)
            # 只允许还原自己写出的清单
            if not mf_r.is_file() or PANEL_DIR.resolve() not in mf_r.parents:
                return self._send({"error": "manifest 不在面板目录内，拒绝"}, 403)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                stats = pm.restore_manifest(mf_r, dry_run=bool(req.get("dry_run")))
            lines = [l for l in buf.getvalue().splitlines() if l.strip()]
            self._send({**stats, "log": lines[-12:]})
        elif u.path == "/api/archive":
            req = self._body()
            res = archive_versions(req.get("root") or "", req.get("files") or [])
            self._send(res, 200 if res.get("ok") else 400)
        elif u.path == "/api/rescan":
            _cache["projects"].clear()
            _board.update({"built": 0, "data": None})
            self._send({"ok": True})
        elif u.path == "/api/reveal":
            req = self._body()
            try:
                root_r = Path(req.get("root") or "").resolve()
            except (OSError, RuntimeError):
                return self._send({"error": "root 无效"}, 400)
            if str(root_r) not in registered_roots():
                return self._send({"error": "该项目未登记"}, 403)
            rel = req.get("rel")
            target = (root_r / rel).resolve() if rel else root_r
            if root_r not in target.parents and target != root_r:
                return self._send({"error": "路径越界"}, 403)
            if not target.exists():
                return self._send({"error": "不存在"}, 404)
            try:
                if os.name == "nt":
                    # Windows：资源管理器定位（explorer 的退出码恒为 1，忽略）
                    subprocess.run(["explorer", "/select,", str(target)] if rel
                                   else ["explorer", str(target)], timeout=5)
                else:
                    subprocess.run(["open", "-R", str(target)] if rel else ["open", str(target)],
                                   capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError) as e:
                return self._send({"error": str(e)}, 500)
            self._send({"ok": True})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


# ---------------- 前端页面 ----------------

def main():
    ap = argparse.ArgumentParser(description="项目伴侣 · Project Companion")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--open", action="store_true", help="自动打开浏览器")
    ap.add_argument("--add", action="append", default=None,
                    help="登记一个项目目录（可重复），写进 panel_config.json")
    args = ap.parse_args()

    if args.add:
        cfg = {"projects": []}
        if CONFIG_PATH.is_file():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {"projects": []}
        cfg.setdefault("projects", [])
        added = []
        for raw in args.add:
            r = str(Path(raw).expanduser().resolve())
            if Path(r).is_dir() and r not in cfg["projects"]:
                cfg["projects"].append(r)
                added.append(r)
            elif not Path(r).is_dir():
                print(f"⚠️ 跳过（目录不存在）：{r}")
        if added:
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            print(f"✅ 已登记 {len(added)} 个项目：{CONFIG_PATH}")
            print("   下次启动看板即可看到它们。")
        return

    if not (PANEL_DIR / "panel.html").is_file():
        sys.exit("❌ 缺少 panel.html（看板页面）")
    # 端口自动避让：被别的进程（如其他 agent 的面板副本）占了就顺延
    port = args.port
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"❌ 端口 {args.port}~{args.port + 19} 全部被占用")
    url = f"http://127.0.0.1:{port}"
    print(f"项目伴侣：{url}")
    print(f"   项目配置：{CONFIG_PATH}")
    print("   Ctrl+C 退出")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
