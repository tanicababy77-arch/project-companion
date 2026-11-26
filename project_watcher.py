#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_watcher.py —— 活动触发式文件监控（不轮询、不烧 CPU）

原理（按「agent 完工后再对比」设计）：
  1. 空闲时只 stat 各工具的「活动信号」文件（DSH 会话 jsonl / WorkBuddy 会话 jsonl /
     git HEAD·index·refs，共几百个 stat，成本约为全量扫描的万分之一）
  2. 某个 agent 的信号持续刷新 = 它在干活；信号停止 IDLE_SEC 秒 = 收工
  3. 「收工」那一刻才做一次全量快照对比，把期间的新建/修改/删除/移动一次性入账，
     归因 = 刚收工的那个 agent（确定级）
  4. 兜底：最长 FALLBACK_MIN 分钟必做一次对比（覆盖豆包等无信号的产出）；
     每 30 分钟全量对比 74k 文件 ≈ 每 5 秒全量对比的 1/360 成本

事件存储：<面板目录>/events/events_<项目md5>.jsonl（追加写）
用法：
  python3 project_watcher.py                     # 常驻（launchd 托管）
  python3 project_watcher.py --once              # 只做基线
  python3 project_watcher.py --fake-signal FILE  # 测试：FILE 的 mtime 模拟一个 agent 活动
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_map as pm
from project_panel import PANEL_DIR, SLACK, gather_windows, load_config

EVENTS_DIR = PANEL_DIR / "events"
POLL_IDLE = 10          # 空闲时每 10s 检查一次活动信号
IDLE_SEC = 60           # 最近 60s 无任何信号 = agent 收工，触发对比
FALLBACK_MIN = 30       # 兜底：最长 30 分钟必做一次全量对比
MAX_WALK_ENTRIES = 200_000


def snapshot(root: Path):
    """{相对路径: (mtime_ns, size)}，跳过工具目录/运行时目录/面板自身。
    只 stat 不读内容；面板目录按「目录」排除而不是逐个文件 resolve——
    几万个文件走一遍也只要一秒左右（这是发现「哪些文件变了」的最低成本）。"""
    snap = {}
    root = root.resolve()
    panel_dir = PANEL_DIR.resolve()
    if root == panel_dir or panel_dir in root.parents:
        return snap
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        try:
            rel_dir = d.relative_to(root)
        except ValueError:
            continue
        dirnames[:] = [x for x in dirnames
                       if x not in pm.EXCLUDE_DIRS and (d / x) != panel_dir]
        if any(part in pm.RUNTIME_DIRS for part in rel_dir.parts):
            continue
        if len(snap) > MAX_WALK_ENTRIES:
            break
        for name in filenames:
            if name in pm.EXCLUDE_DIRS:
                continue
            p = d / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            snap[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return snap


def agent_signals(roots, fake_signal=None, ignore=frozenset()):
    """各工具的活动信号：(agent, mtime)。只 stat 少量文件，不扫项目。"""
    sigs = []
    home = Path.home()

    if "dsh" not in ignore:
        dsh_root = home / ".dsh" / "sessions"
        if dsh_root.is_dir():
            try:
                for proj in dsh_root.iterdir():
                    if not proj.is_dir():
                        continue
                    for s in proj.iterdir():
                        if s.is_dir() and s.name.startswith("session-"):
                            for f in s.iterdir():
                                try:
                                    sigs.append(("dsh", f.stat().st_mtime))
                                except OSError:
                                    pass
            except OSError:
                pass

    if "workbuddy" not in ignore:
        wb_root = home / ".workbuddy" / "projects"
        if wb_root.is_dir():
            try:
                for proj in wb_root.iterdir():
                    if not proj.is_dir():
                        continue
                    for f in proj.glob("*.jsonl"):
                        try:
                            sigs.append(("workbuddy", f.stat().st_mtime))
                        except OSError:
                            pass
            except OSError:
                pass

    if "trae" not in ignore:
        for pr in roots:
            g = pr / ".git"
            if g.is_dir():
                for name in ("HEAD", "index", "refs", "ORIG_HEAD"):
                    q = g / name
                    if q.exists():
                        try:
                            sigs.append(("trae", q.stat().st_mtime))
                        except OSError:
                            pass

    if "doubao" not in ignore:
        # 豆包沙箱活动：Profile 2 下两层内的最新文件 mtime
        p2 = home / "Library" / "Application Support" / "Doubao" / "Profile 2"
        if p2.is_dir():
            newest = 0
            try:
                for dirpath, dirnames, filenames in os.walk(p2):
                    depth = len(Path(dirpath).parts) - len(p2.parts)
                    if depth > 2:
                        dirnames[:] = []
                        continue
                    for fn in filenames:
                        try:
                            newest = max(newest,
                                         os.path.getmtime(os.path.join(dirpath, fn)))
                        except OSError:
                            pass
            except OSError:
                pass
            if newest:
                sigs.append(("doubao", newest))

    if fake_signal and Path(fake_signal).exists():
        try:
            sigs.append(("测试", Path(fake_signal).stat().st_mtime))
        except OSError:
            pass
    return sigs


def attr_now(root=None):
    """兜底归因：活动窗口相关法（无信号来源时用）"""
    try:
        by_proj, _ = gather_windows([])
    except Exception:
        return "unknown", "-"
    now = time.time()
    if root is not None:
        for w in by_proj.get(str(root.resolve()), []):
            if w["start"] - SLACK <= now <= w["end"] + SLACK:
                return w["agent"], "推测"
    for wins in by_proj.values():
        for w in wins:
            if w["start"] - SLACK <= now <= w["end"] + SLACK:
                return w["agent"], "推测"
    return "unknown", "-"


def event_file(root: Path):
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(str(root.resolve()).encode("utf-8")).hexdigest()[:10]
    return EVENTS_DIR / f"events_{key}.jsonl"


def write_event(root: Path, ev: dict):
    with open(event_file(root), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def diff_and_record(root: Path, old: dict, new: dict, agent=None, conf=None):
    """对比两份快照，把差异写入事件流。agent 给定则 conf 默认「确定」。"""
    if agent is None:
        agent, conf = attr_now(root)
    else:
        conf = conf or "确定"
    now = time.time()
    created = [(r, s) for r, s in new.items() if r not in old]
    deleted = [(r, s) for r, s in old.items() if r not in new]
    modified = [(r, s) for r, s in new.items() if r in old and old[r] != s]

    for r, s in list(deleted):
        for r2, s2 in list(created):
            if s[1] == s2[1] and s[1] > 0:
                write_event(root, {"ts": now, "type": "move",
                                   "path": r2, "from": r,
                                   "agent": agent, "conf": conf})
                created.remove((r2, s2))
                deleted.remove((r, s))
                break
    for r, s in created:
        write_event(root, {"ts": now, "type": "create", "path": r,
                           "agent": agent, "conf": conf})
    for r, s in deleted:
        write_event(root, {"ts": now, "type": "delete", "path": r,
                           "agent": agent, "conf": conf})
    for r, s in modified:
        write_event(root, {"ts": now, "type": "modify", "path": r,
                           "agent": agent, "conf": conf})
    return len(created), len(deleted), len(modified)


def diff_all(roots, snaps, agent=None, conf=None, reason=""):
    total = (0, 0, 0)
    for r in roots:
        try:
            new = snapshot(r)
            c, d, m = diff_and_record(r, snaps[r], new, agent, conf)
            if c or d or m:
                print(f"[{time.strftime('%H:%M:%S')}] {reason} → {r.name}: "
                      f"✚{c} 🗑{d} ✎{m}", flush=True)
            snaps[r] = new
            total = (total[0] + c, total[1] + d, total[2] + m)
        except Exception as e:
            print(f"⚠️ {r}: {e}", file=sys.stderr, flush=True)
    return total


def watch_loop(roots, fake_signal=None, ignore=frozenset()):
    snaps = {}
    print(f"👁️  活动触发式监控 {len(roots)} 个项目", flush=True)
    print(f"   空闲只查信号（{POLL_IDLE}s/次），agent 收工 {IDLE_SEC}s 后对比，"
          f"最长 {FALLBACK_MIN} 分钟兜底", flush=True)
    for r in roots:
        snaps[r] = snapshot(r)
        print(f"   - 基线 {r}：{len(snaps[r])} 个文件", flush=True)
    print("   事件流写入:", EVENTS_DIR, flush=True)

    last_diff = time.time()
    active_agent = None

    while True:
        time.sleep(POLL_IDLE)
        now = time.time()
        sigs = agent_signals(roots, fake_signal, ignore)
        if not sigs:
            continue
        latest = max(sigs, key=lambda x: x[1])
        if now - latest[1] < IDLE_SEC:
            # 最近 IDLE_SEC 内有信号 = 还在干活
            if latest[0] != active_agent:
                print(f"[{time.strftime('%H:%M:%S')}] 检测到 {latest[0]} 开始干活",
                      flush=True)
            active_agent = latest[0]
            continue

        # 已静默 IDLE_SEC = 收工（或本来就没在干活）
        need, reason, attr = False, "", None
        if active_agent:
            need, reason, attr = True, f"{active_agent} 收工", active_agent
            active_agent = None
        elif now - last_diff >= FALLBACK_MIN * 60:
            need, reason, attr = True, "定时兜底", None

        if need:
            c, d, m = diff_all(roots, snaps, agent=attr, reason=reason)
            last_diff = now


def main():
    global POLL_IDLE, IDLE_SEC, FALLBACK_MIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只做基线快照")
    ap.add_argument("--fake-signal", default=None,
                    help="测试用：该文件的 mtime 模拟一个 agent 的活动信号")
    ap.add_argument("--ignore-agents", default="",
                    help="测试用：逗号分隔，忽略这些工具的信号（如 dsh,workbuddy）")
    ap.add_argument("--poll", type=int, default=POLL_IDLE)
    ap.add_argument("--idle-sec", type=int, default=IDLE_SEC)
    ap.add_argument("--fallback-min", type=int, default=FALLBACK_MIN)
    args = ap.parse_args()
    POLL_IDLE, IDLE_SEC, FALLBACK_MIN = args.poll, args.idle_sec, args.fallback_min

    cfg = load_config()
    roots = [Path(p) for p in cfg["projects"] if Path(p).is_dir()]
    if not roots:
        print("❌ 没有项目可监控（先跑一次面板生成配置）")
        return

    if args.once:
        for r in roots:
            print(f"基线 {r}: {len(snapshot(r))} 个文件")
        return
    ignore = frozenset(a.strip() for a in args.ignore_agents.split(",") if a.strip())
    watch_loop(roots, fake_signal=args.fake_signal, ignore=ignore)


if __name__ == "__main__":
    main()
