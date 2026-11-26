#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project_map.py —— 项目文件地图 + 三档安全清理（只读扫描；清理只进废纸篓）

解决多 Agent 混用后的文件混乱：
  · 扫描项目，按「命名归一化 + mtime + 内容重合度」判定每个文件的最新版本
  · 找出跨目录、跨命名习惯的完全重复 / 高度相似文件
  · 输出 PROJECT_MAP.md（给人和下一个 Agent 看）+ project_map.json

三档清理（面向小白用户的安全设计）：
  🟢 第一档「可证明安全」—— 字节级完全一致 + 无引用 / 文件名自标旧版，
     自动移入隔离区（保留目录结构 + 记录哈希，绝不永久删除），可一键还原
  🟡 第二档「建议清理」—— 新旧共存但引用关系无法确定，只列清单不代删
  🔴 第三档「需人工确认」—— 同名不同内容等，只展示对比，绝不代删

用法：
  python3 project_map.py --root /path/to/project                 # 生成地图
  python3 project_map.py --root /path/to/project --clean         # 查看三档清理计划
  python3 project_map.py --root /path/to/project --apply-tier 1  # 只执行第一档
  python3 project_map.py --restore /path/to/cleanup_manifest_*.json   # 一键还原
  python3 project_map.py --restore ... --dry-run                 # 还原演练（不改动）

铁律：只读扫描；清理 = 移入隔离区；每次清理留 manifest（含 sha256）。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# ---------------- 配置 ----------------

EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "venv", ".venv", "env",
    "__pycache__", ".idea", ".vscode", ".DS_Store", "dist", "build",
    ".next", ".cache", "target", ".trae", ".workbuddy", ".trae-cn",
    ".agent-browser", "feedback", "quarantine", "manifests", "_versions",
    ".pm_cache", ".pm_cards",
}

# 隔离区：清理的文件先挪到这里（保留目录结构），还原 100% 可靠；
# 相比直接写 ~/.Trash，不受 macOS 全盘访问权限限制，也不会同名覆盖。
QUARANTINE_ROOT = Path(__file__).resolve().parent / "quarantine"

# 运行时/数据目录：不参与版本链判定，单独汇总计数（清理时也绝不自动动它们）
RUNTIME_DIRS = {
    ".screenshots", "snapshots", "memory", "memory_mock", "memory_guests",
    "tts_cache", "__pycache__",
}

TEXT_EXTS = {
    ".py", ".md", ".txt", ".js", ".ts", ".jsx", ".tsx", ".json", ".html",
    ".css", ".scss", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
    ".log", ".sql", ".sh", ".rb", ".go", ".java", ".c", ".cpp", ".h",
    ".rs", ".vue", ".xml", ".tex", ".ipynb", ".mjs", ".cjs", ".svg",
}

# 英文版本词：用 lookbehind/lookahead 代替 \b（下划线是词字符）
RE_VERSION_WORDS_EN = re.compile(
    r"(?<![a-z0-9])(?:v\d+(?:\.\d+)*|final|draft|temp|tmp|old|new|"
    r"copy(?:\s*of)?\s*\d*|backup|bak|wip|rev\s*\d*|version\s*\d*)"
    r"(?![a-z0-9])", re.IGNORECASE
)
# 长的排前面：否则「最终版」会先被「最终」匹配掉，剩下个「版」字，和原名对不上
RE_VERSION_WORDS_CN = re.compile(
    "(" + "|".join(sorted(
        ["最终", "最终版", "最新", "最新版", "新版", "旧版", "副本", "备份", "草稿",
         "临时", "初稿", "终稿", "定稿", "修改版", "完成版", "完整版"],
        key=len, reverse=True)) + ")"
)
# 日期/时间戳：只有文件名自己带版本词（v2 / 最终版 / 备份 …）时才去掉。
# 前后都不能紧挨数字——否则 bb01-003 里的「01-00」会被当成日期吃掉。
RE_DATE_STAMP = re.compile(
    r"(?<!\d)\d{4}[-_/.]\d{1,2}[-_/.]\d{1,2}(?!\d)"
    r"|(?<![\d-])\d{1,2}[-_/.]\d{1,2}(?![\d-])"
)
RE_TIME_STAMP = re.compile(r"(?<!\d)\d{8}(?:[-_ ]?\d{4,6})?(?!\d)")
# 浏览器/系统下载的「报告 (1)」：一定是某份文件的副本，任何时候都去掉
RE_COPY_MARK = re.compile(r"\(\d+\)")
# 明确表示「废弃/过时」的词（第一档自动清理的依据之一）
RE_OBSOLETE = re.compile(
    r"(?<![a-z0-9])(?:backup|bak|old|copy(?:\s*of)?\s*\d*|draft|temp|tmp|wip)"
    r"(?![a-z0-9])|(旧版|副本|备份|草稿|临时)", re.IGNORECASE
)
# 明确表示「正在使用」的词（出现则绝不自动删）
RE_KEEP = re.compile(r"(现用|当前|current|active)", re.IGNORECASE)

TEXT_SIZE_CAP = 1_000_000        # >1MB 的文本不做内容比较
CONTENT_SAMPLE = 60_000
STALE_MIN_COV = 0.65
SUSPICIOUS_SIM = 0.85
LEN_DROP_RATIO = 0.70
CHAIN_COMPARE_MAX = 40
ORPHAN_LIST_MAX = 120
SMALL_FILE_B = 8 * 1024
SNAPSHOT_GAP_DAYS = 7            # 目录中位 mtime 差超过 7 天才视为「旧快照目录」
REF_CAP = 512 * 1024             # 引用文本里单个文件的大小上限
LARGE_FILE_B = 64 * 1024 * 1024  # 超过 64MB 只做抽样哈希（防止 80GB 视频拖死扫描）


def large_hash(path: Path, size: int) -> str:
    """大文件抽样指纹：size + 头/中/尾各 1MB，前缀 S: 表示「抽样」，
    绝不能据此判定完全一致，也就绝不进第一档自动清理。"""
    h = hashlib.md5(str(size).encode())
    with open(path, "rb") as fh:
        for off in (0, max(0, size // 2 - (1 << 19)), max(0, size - (1 << 20))):
            fh.seek(off)
            h.update(fh.read(1 << 20))
    return "S:" + h.hexdigest()


# ---------------- 工具函数 ----------------

def is_text(path: Path, head: bytes) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    if b"\x00" in head[:4096]:
        return False
    try:
        head[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def text_sample(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8", errors="replace")[:CONTENT_SAMPLE]
    except Exception:
        return ""


def similarity(a: bytes, b: bytes) -> float:
    ta, tb = text_sample(a), text_sample(b)
    if not ta or not tb:
        return 0.0
    m = SequenceMatcher(None, ta, tb)
    if m.real_quick_ratio() < 0.5:
        return 0.0
    return m.ratio()


def containment(a: bytes, b: bytes) -> float:
    ta, tb = text_sample(a), text_sample(b)
    if not ta or not tb:
        return 0.0
    m = SequenceMatcher(None, ta, tb)
    matched = sum(block.size for block in m.get_matching_blocks())
    return matched / max(1, min(len(ta), len(tb)))


def normalize_stem(stem: str) -> str:
    s = stem.lower()
    # 名字里的数字默认是「身份」的一部分：bb01-003 和 bb01-013 是两个文件，第13课和第14课是两份教案。
    # 只有名字自己标了版本（v2 / 最终版 / 备份 …）时，日期和时间戳才当作附注去掉。
    marked = bool(RE_VERSION_WORDS_EN.search(s) or RE_VERSION_WORDS_CN.search(s))
    s = RE_VERSION_WORDS_EN.sub(" ", s)
    s = RE_VERSION_WORDS_CN.sub(" ", s)
    s = RE_COPY_MARK.sub(" ", s)
    if marked:
        s = RE_DATE_STAMP.sub(" ", s)
        s = RE_TIME_STAMP.sub(" ", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)
    return s or stem.lower()


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def has_version_word(name: str) -> bool:
    low = name.lower()
    return bool(RE_VERSION_WORDS_EN.search(low) or RE_VERSION_WORDS_CN.search(name))


def has_temp_word(name: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])(?:temp|tmp|draft|wip)(?![a-z0-9])",
                          name.lower()) or re.search(r"(草稿|临时)", name))


def has_obsolete_word(name: str) -> bool:
    return bool(RE_OBSOLETE.search(name))


def has_keep_word(name: str) -> bool:
    return bool(RE_KEEP.search(name))


def topdir(rel: str) -> str:
    return rel.split("/")[0]


# ---------------- 扫描 ----------------

def _iter_files(root: Path, runtime=None):
    """遍历文件：不跟随软链接、跳过排除目录，运行数据目录只计数不返回。
    用 os.walk 而不是 rglob，避免软链环和大目录把扫描拖死。"""
    if runtime is None:
        runtime = defaultdict(int)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        try:
            rel_dir = d.relative_to(root)
        except ValueError:
            continue
        dirnames[:] = [x for x in dirnames if x not in EXCLUDE_DIRS]
        if any(part in RUNTIME_DIRS for part in rel_dir.parts):
            runtime[rel_dir.parts[0]] += len(filenames)
            continue
        for name in filenames:
            if name in EXCLUDE_DIRS:
                continue
            p = d / name
            if p.is_symlink() or not p.is_file():
                continue
            yield p, str(p.relative_to(root))


def scan(root: Path, cache=None):
    """读一遍项目里每个文件的内容指纹。

    cache 是上一次的索引（build_index 的产物）：修改时间和大小都没变的文件
    直接沿用旧哈希，不再重读——只有新建的、改动过的才真正读内容（增量扫描）。"""
    files = []
    runtime = defaultdict(int)
    for p, rel in _iter_files(root, runtime):
        try:
            st = p.stat()
        except OSError:
            continue

        size = st.st_size
        mtime_ns = st.st_mtime_ns
        old = cache.get(rel) if cache else None
        reuse = old if (old and old.get("hash")
                        and old.get("mtime_ns") == mtime_ns
                        and old.get("size") == size) else None
        sampled = size > LARGE_FILE_B
        data = head_h = tail_h = None
        if reuse is not None and "text" in reuse:
            # 上次读过：哈希、是否文本、内容指纹全部照搬
            digest, text = reuse["hash"], reuse["text"]
            head_h, tail_h = reuse.get("head_h"), reuse.get("tail_h")
        else:
            h = hashlib.md5()
            head = b""
            try:
                if reuse is not None:
                    # 看板只算过哈希：补读文件头判断是不是文本，不必整份重读
                    with open(p, "rb") as fh:
                        head = fh.read(4096)
                    digest = reuse["hash"]
                elif sampled:
                    with open(p, "rb") as fh:
                        head = fh.read(4096)
                    digest = large_hash(p, size)
                else:
                    with open(p, "rb") as fh:
                        while True:
                            chunk = fh.read(1 << 20)
                            if not chunk:
                                break
                            h.update(chunk)
                            if len(head) < 4096:
                                head += chunk[:4096 - len(head)]
                    digest = h.hexdigest()
            except OSError:
                continue
            text = (not sampled) and is_text(p, head)
        if text and 0 < size <= TEXT_SIZE_CAP:
            # 文本内容：比较相似度要用，读一次
            try:
                data = p.read_bytes()
                ts = text_sample(data)
                if ts:
                    head_h = hashlib.md5(ts[:4096].encode("utf-8", "replace")).hexdigest()
                    tail_h = hashlib.md5(ts[-4096:].encode("utf-8", "replace")).hexdigest()
            except OSError:
                data = None

        files.append({
            "path": p, "rel": rel, "size": size, "mtime": st.st_mtime,
            "mtime_ns": mtime_ns,
            "btime": file_birth(st),  # 文件创建时间（归因面板用）
            "hash": digest, "text": text, "data": data,
            "head_h": head_h, "tail_h": tail_h,
            "key": normalize_stem(p.stem) + "|" + p.suffix.lower(),
            "version_word": has_version_word(p.name),
            "sampled": sampled,
        })
    return files, runtime


def build_index(files):
    """扫描结果 → 可落盘的索引，供下次 scan(cache=...) 增量复用"""
    return {f["rel"]: {"mtime_ns": f["mtime_ns"], "size": f["size"],
                       "hash": f["hash"], "text": f["text"],
                       "head_h": f["head_h"], "tail_h": f["tail_h"],
                       "sampled": bool(f.get("sampled"))}
            for f in files if f.get("hash")}


# ---------------- 分析与判定 ----------------

def build_chains(files):
    chains = defaultdict(list)
    for f in files:
        chains[f["key"]].append(f)
    multi = {k: v for k, v in chains.items() if len(v) > 1}
    orphans = [f for f in files if len(chains[f["key"]]) == 1]
    return multi, orphans


def pick_primary(members):
    ordered = sorted(members, key=lambda m: m["mtime"], reverse=True)
    compare = ordered[:CHAIN_COMPARE_MAX]
    newest = compare[0]
    notes = []
    confidence = "高"

    text_members = [m for m in compare if m["text"] and m["data"] is not None]
    if newest["text"] and newest["data"] is not None and text_members:
        longest = max(text_members, key=lambda m: m["size"])
        if newest["size"] < longest["size"] * LEN_DROP_RATIO and newest is not longest:
            confidence = "低"
            notes.append(
                f"⚠️ 最新修改的 {newest['rel']} 内容明显短于最全的 {longest['rel']}"
                f"（可能只是小修补的半成品）")
        if newest["version_word"]:
            notes.append("💡 主版本文件名带版本词（final/最终 等），可能只是中间产物")

    roles = []
    for m in compare:
        if m is newest:
            roles.append((m, "primary"))
        elif m["text"] and newest["text"]:
            if m["hash"] == newest["hash"]:
                roles.append((m, "stale"))
            elif m["data"] is not None and newest["data"] is not None:
                cov = containment(newest["data"], m["data"])
                if cov >= STALE_MIN_COV:
                    if m["size"] > newest["size"] * 1.15:
                        roles.append((m, "diverged"))
                    else:
                        roles.append((m, "stale"))
                else:
                    roles.append((m, "unrelated"))
            else:
                roles.append((m, "uncompared"))
        else:
            roles.append((m, "uncompared"))
    return newest, confidence, notes, roles, max(0, len(ordered) - CHAIN_COMPARE_MAX)


# 路径里的「备份/旧版/归档」字样：比不了内容的文件（图片、模型…）靠它认出版本关系
RE_PATH_MARKER = re.compile(
    r"备份|旧版|归档|历史|存档|副本|_versions"
    r"|(?<![a-z])(?:bak|backup|old|archive|copy)(?![a-z])", re.IGNORECASE
)


def _same_dir(a, b) -> bool:
    return Path(a["rel"]).parent == Path(b["rel"]).parent


def _version_marker(*members) -> bool:
    """文件名带版本词，或所在路径带备份/旧版等字样"""
    for m in members:
        if m.get("version_word"):
            return True
    return bool(RE_PATH_MARKER.search("/".join(str(Path(m["rel"]).parent) for m in members)))


def judge_chains(chains):
    """名字只是线索，内容才是证据。把「同名」分成两堆：
      - 真版本链：内容相同/高度重合，或者比不了内容但同目录（或带备份、版本标记）
      - 同名不同文件：内容无关，或者比不了内容又分属不同目录（每个角色一张 walkL.webp）
    返回 (真版本链, 同名不同文件的清单)。比不了内容的那部分保留在链里，并标出来。"""
    real, same_name = {}, []
    for key, members in chains.items():
        primary, _conf, _notes, roles, _skipped = pick_primary(members)
        judged = set()
        keep = [primary]
        for m, role in roles:
            judged.add(id(m))
            if role == "primary":
                continue
            if role == "uncompared" and not m.get("sampled") \
                    and m["hash"] and m["hash"] == primary["hash"]:
                keep.append(m)          # 字节完全相同的图片/模型：是同一份，不算「比不了」
            elif role == "unrelated":
                # 同目录、名字又标了版本，内容却无关 → 留在链里，由三档提醒人来看
                if _same_dir(primary, m) and (m["version_word"] or primary["version_word"]):
                    keep.append(m)
                else:
                    same_name.append(m)
            elif role == "uncompared":
                if _same_dir(primary, m) or _version_marker(primary, m):
                    m["uncompared"] = True
                    keep.append(m)
                else:
                    same_name.append(m)
            else:
                keep.append(m)
        # 超过比较上限、没轮到裁决的成员：原样留着，不下结论
        keep.extend(m for m in members if id(m) not in judged and m is not primary)
        if len(keep) > 1:
            real[key] = keep
    return real, same_name


def find_suspicious_pairs(files):
    pairs = []
    by_hash = defaultdict(list)
    for f in files:
        if not f.get("sampled"):
            by_hash[f["hash"]].append(f)
    for h, group in by_hash.items():
        if len(group) < 2 or group[0]["size"] == 0:
            continue
        if group[0]["path"].name in ("__init__.py", ".gitkeep"):
            continue
        a = group[0]
        for b in group[1:6]:
            pairs.append((a, b, 1.0, "内容完全一致", len(group)))

    small = [f for f in files if f["data"] is not None and 0 < f["size"] <= SMALL_FILE_B]
    small.sort(key=lambda f: f["size"])
    budget = [0]
    n = len(small)
    for i in range(n):
        a = small[i]
        j = i + 1
        while j < n and small[j]["size"] <= a["size"] * 1.15:
            b = small[j]
            if a["key"] != b["key"] and a["hash"] != b["hash"] and \
                    (a["size"] <= 2 * 1024 or a["head_h"] == b["head_h"]
                     or a["tail_h"] == b["tail_h"]):
                budget[0] += 1
                if budget[0] > 200_000:
                    j = n
                    break
                sim = similarity(a["data"], b["data"])
                if sim >= SUSPICIOUS_SIM:
                    pairs.append((a, b, sim, "内容高度相似", None))
            j += 1
        if budget[0] > 200_000:
            break

    buckets = defaultdict(list)
    for f in files:
        if f["data"] is not None and f["size"] > SMALL_FILE_B:
            buckets[(f["head_h"], f["tail_h"])].append(f)
    for group in buckets.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["key"] == b["key"] or a["hash"] == b["hash"]:
                    continue
                if min(a["size"], b["size"]) < max(a["size"], b["size"]) * 0.85:
                    continue
                sim = similarity(a["data"], b["data"])
                if sim >= SUSPICIOUS_SIM:
                    pairs.append((a, b, sim, "内容高度相似", None))
    return pairs


def identical_groups(files):
    """字节级完全一致的重复组（哈希分组，比 pairs 更完整）
    抽样哈希的大文件不算在内——不能凭抽样就断言完全一致。"""
    groups = defaultdict(list)
    for f in files:
        if f["size"] > 0 and f["path"].name not in ("__init__.py", ".gitkeep") \
                and not f.get("sampled"):
            groups[f["hash"]].append(f)
    return {h: g for h, g in groups.items() if len(g) > 1}


def build_reference_text(files):
    """拼接项目内所有文本内容，用于判断「某个文件名是否还被引用」"""
    text = []
    for f in files:
        if not f["text"] or f["size"] > REF_CAP:
            continue
        if f["data"] is None:
            continue
        text.append(text_sample(f["data"]))
    return "\n".join(text)


# ---------------- 文件名片（agent 写文件时主动声明） ----------------
# 名片 = agent 在「写文件那一刻」留下的自述，而不是事后靠名字/内容猜。
# 存放在每个项目下的 .pm_cards/（一文件一卡，文件名=路径的 md5）。已加入 EXCLUDE_DIRS，
# 不参与扫描、不参与版本判定，也不进三档。agent 声明过的文件以名片为高置信线索，
# 名片没有的文件才走原来的名字/内容启发式。
# 一文件一卡是为了抗「多机器/网盘/U盘」同步：每个卡片独立小文件，互不覆盖、互不冲突；
# 同一路径反复写卡 = 覆盖成最新那条。
CARD_DIR = ".pm_cards"
CARD_STATUS = {
    "final": "最终版",
    "intermediate": "过程文件",
    "input": "参考资料",
    "reference": "参考",
}
CARD_STATUS_KEYS = set(CARD_STATUS)


def card_dir(root: Path) -> Path:
    return Path(root) / CARD_DIR


def _card_rel_key(rel: str) -> str:
    """一文件一卡：文件名用路径的 md5，不同路径互不冲突，同一路径覆盖即最新。"""
    return hashlib.md5(rel.encode("utf-8")).hexdigest() + ".json"


def write_card(root, card: dict) -> dict:
    """写一张名片到 <root>/.pm_cards/<路径哈希>.json（一文件一卡）。返回规范化后的记录。"""
    d = card_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    status = card.get("status", "intermediate")
    if status not in CARD_STATUS_KEYS:
        status = "intermediate"
    rec = {
        "path": str(card.get("path") or "").lstrip("/"),
        "agent": card.get("agent", "unknown"),
        "status": status,
        "note": card.get("note", ""),
        "ts": card.get("ts") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if card.get("replaces"):
        rec["replaces"] = str(card["replaces"]).lstrip("/")
    (d / _card_rel_key(str(rec["path"]))).write_text(
        json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    return rec


def load_cards(root) -> dict:
    """读项目下所有名片（一文件一卡），返回 {相对路径: 卡片}。"""
    d = card_dir(root)
    out = {}
    if not d.is_dir():
        return out
    try:
        for f in sorted(d.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rel = rec.get("path")
            if rel:
                out[str(rel)] = rec
    except OSError:
        pass
    return out


# ---------------- 三档清理 ----------------

def compute_tiers(files, chains, orphans):
    """🟢 可证明安全 / 🟡 建议清理 / 🔴 需人工确认"""
    tier1, tier2, tier3 = [], [], []
    ref = build_reference_text(files)

    id_groups = identical_groups(files)
    in_idgroup = {f["rel"] for g in id_groups.values() for f in g}

    # 目录中位 mtime（判断「旧快照目录」）
    dir_mtimes = defaultdict(list)
    for f in files:
        dir_mtimes[topdir(f["rel"])].append(f["mtime"])
    medians = {d: statistics.median(ts) for d, ts in dir_mtimes.items()}

    for h, group in id_groups.items():
        members = [f["rel"] for f in group]
        names = [Path(r).name for r in members]
        # 出现「现用/当前」等字样 → 整组不自动动
        if any(has_keep_word(n) for n in names):
            tier2.append({"files": members,
                          "reason": "完全相同的副本，但文件名带「现用/当前」，需你确认留哪份"})
            continue
        # R1a：废弃词 vs 干净名 → 删废弃词那份
        obsolete = [r for r in members if has_obsolete_word(Path(r).name)]
        clean = [r for r in members if r not in obsolete]
        if obsolete and clean:
            for r in obsolete:
                tier1.append({"files": [r],
                              "reason": f"文件名自标旧版/副本/备份，且与 {clean[0]} 完全相同"})
            continue
        # R1b：跨目录，其中一个目录是「旧快照」→ 删旧目录里的副本
        dirs = defaultdict(list)
        for r in members:
            dirs[topdir(r)].append(r)
        if len(dirs) >= 2:
            newest_med = max(medians[d] for d in dirs)
            old_dirs = [d for d in dirs
                        if newest_med - medians[d] > SNAPSHOT_GAP_DAYS * 86400]
            new_dirs = [d for d in dirs if d not in old_dirs]
            if old_dirs and new_dirs:
                handled = True
                for d in old_dirs:
                    if f"{d}/" in ref:
                        handled = False  # 旧目录仍被代码引用 → 不自动动
                        continue
                    for r in dirs[d]:
                        tier1.append({"files": [r],
                                      "reason": f"{d}/ 是较早的快照目录，相同内容在 "
                                                f"{new_dirs[0]}/ 中仍有副本"})
                if handled:
                    continue
        # R1c：引用检查——注册表/代码引用了谁，就留谁
        referenced = [r for r in members if Path(r).name in ref]
        unreferenced = [r for r in members if r not in referenced]
        if referenced and unreferenced:
            for r in unreferenced:
                tier1.append({"files": [r],
                              "reason": f"重复副本未被任何代码/配置引用"
                                        f"（被引用的是 {referenced[0]}）"})
        elif referenced:
            tier2.append({"files": members,
                          "reason": "所有副本都有人引用，可能各有用处，请确认能否合并"})
        else:
            tier2.append({"files": members,
                          "reason": "没有任何副本被引用，无法判断哪份是正主，请确认留哪份"})

    # 版本链：旧版 → 🟡；同名不同内容 → 🔴（完全一致组已处理过的跳过）
    # 先用内容裁决：内容无关的「同名」是两个文件，不是两个版本，不进任何一档
    for key, members in judge_chains(chains)[0].items():
        primary, conf, notes, roles, skipped = pick_primary(members)
        for m, role in roles:
            if m["rel"] in in_idgroup:
                continue
            if role == "stale":
                tier2.append({"files": [m["rel"]],
                              "reason": f"被 {primary['rel']} 取代的旧版（内容高度重合）"})
            elif role == "diverged":
                tier2.append({"files": [m["rel"]],
                              "reason": f"与 {primary['rel']} 同源但内容更全，"
                                        f"需确认以哪份为准"})
            elif role == "unrelated":
                tier3.append({"files": [primary["rel"], m["rel"]],
                              "reason": "同一目录、名字标着版本，内容却无关，"
                                        "需确认是不是两份不同的东西"})

    # 孤立文件（完全一致组已处理过的跳过）
    for f in orphans:
        if f["rel"] in in_idgroup:
            continue
        card = f.get("card") or {}
        # 名片优先：agent 自声明的过程文件 / 最终版，直接采信，跳过名字启发式
        if card.get("status") == "intermediate":
            tier2.append({"files": [f["rel"]],
                          "reason": "agent 自声明为过程文件"})
            continue
        if card.get("status") == "final":
            continue   # 明确是最终版，不再按名字猜
        if has_temp_word(f["path"].name):
            tier2.append({"files": [f["rel"]],
                          "reason": "疑似草稿/临时文件，且项目里没有其他版本"})
        elif f["version_word"]:
            tier3.append({"files": [f["rel"]],
                          "reason": "文件名带版本词但找不到对应新版，需确认是不是唯一一份"})

    return {"1": tier1, "2": tier2, "3": tier3}


def tier_total_bytes(tier, root):
    total = 0
    for item in tier:
        for r in item["files"]:
            p = root / r
            if p.is_file():
                total += p.stat().st_size
    return total


def render_tiers(root, tiers, runtime):
    L = []
    labels = {
        "1": "🟢 第一档：可证明安全（可自动清理，移入隔离区）",
        "2": "🟡 第二档：建议清理（只列清单，不代删）",
        "3": "🔴 第三档：需人工确认（只展示，绝不代删）",
    }
    for k in ("1", "2", "3"):
        t = tiers[k]
        if not t:
            continue
        L.append(f"## {labels[k]}（{len(t)} 项，约 {tier_total_bytes(t, root) / 1048576:.1f} MB）")
        L.append("")
        for item in t:
            for r in item["files"]:
                L.append(f"- `{r}`")
            L.append(f"  ↳ {item['reason']}")
        L.append("")
    if runtime:
        L.append("## 📦 运行时/数据目录（绝不自动清理）")
        L.append("")
        for d in sorted(runtime, key=lambda k: -runtime[k]):
            L.append(f"- `{d}/` — {runtime[d]} 个文件（运行数据/记忆/截图，动之前先想清楚）")
        L.append("")
    return "\n".join(L)


# ---------------- 执行与还原 ----------------

def file_birth(st):
    """创建时间：macOS 用 st_birthtime；Windows 的 st_ctime 即创建时间；取不到退回 mtime"""
    b = getattr(st, "st_birthtime", None)
    if b:
        return b
    if os.name == "nt":
        return getattr(st, "st_ctime", None) or st.st_mtime
    return st.st_mtime


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def move_to_trash(src: Path) -> Path:
    """（可选）直接写 ~/.Trash。macOS 上受全盘访问权限限制，且不产生
    「放回原处」所需元数据，所以默认不用它，只作 --dest trash 的兜底。"""
    trash = Path.home() / ".Trash"
    dst = trash / src.name
    try:
        if dst.exists():
            dst = trash / f"{src.stem}__{int(time.time())}{src.suffix}"
    except OSError:
        pass
    shutil.move(str(src), str(dst))
    return dst


def move_to_quarantine(src: Path, root: Path, batch_dir: Path) -> Path:
    """移入隔离区，保留「相对项目根」的目录结构 → 还原时一一对应，不会撞名。"""
    rel = src.relative_to(root)
    dst = batch_dir / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}__{int(time.time())}{dst.suffix}")
    shutil.move(str(src), str(dst))
    return dst


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def execute_tier1(root, tiers, out_dir, dest: str = "quarantine"):
    items = tiers["1"]
    if not items:
        print("✅ 第一档没有可清理项，项目很干净。")
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch_dir = QUARANTINE_ROOT / f"{root.name}_{stamp}"
    actions = []
    for item in items:
        for rel in item["files"]:
            src = (root / rel).resolve()
            if not src.is_file():
                print(f"⚠️ 跳过（已不存在）：{rel}")
                continue
            if root.resolve() not in src.parents:
                print(f"⚠️ 跳过（不在项目内）：{rel}")
                continue
            try:
                size = src.stat().st_size
                digest = file_sha256(src)
                dst = (move_to_quarantine(src, root.resolve(), batch_dir)
                       if dest == "quarantine" else move_to_trash(src))
            except OSError as e:
                print(f"⚠️ 跳过（移动失败：{e}）：{rel}")
                continue
            actions.append({
                "original": str(src),
                "moved_to": str(dst),
                "sha256": digest,
                "size": size,
                "reason": item["reason"],
                "tier": 1,
                "moved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"🧊 {rel}  → 隔离区")
    if not actions:
        print("⚠️ 没有文件被移动，未生成清单。")
        return None
    payload = {
        "version": 2,
        "mode": dest,
        "project_root": str(root.resolve()),
        "batch_dir": str(batch_dir),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "actions": actions,
    }
    manifest = out_dir / f"cleanup_manifest_{stamp}.json"
    _write_manifest(manifest, payload)
    _write_manifest(batch_dir / "manifest.json", payload)
    total = sum(a["size"] for a in actions)
    print(f"\n✅ 共清理 {len(actions)} 个文件"
          f"（{total / 1048576:.1f} MB，全部在隔离区，可完整还原）")
    print(f"   还原清单：{manifest}")
    print(f"   还原命令：python3 {Path(__file__).name} --restore \"{manifest}\"")
    return manifest


def restore_manifest(manifest_path, root_hint=None, dry_run: bool = False,
                     force: bool = False):
    """按 manifest 还原。兼容 v2（含 sha256）与旧版 list 清单。"""
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        actions, root, mode = raw, root_hint, "legacy"
    else:
        actions = raw.get("actions") or []
        root = raw.get("project_root") or root_hint
        mode = raw.get("mode", "quarantine")

    if not actions:
        print("⚠️ 清单里没有可还原项。")
        return {"ok": 0, "skip": 0, "fail": 0, "warn": 0}

    ok = skip = fail = warn = 0
    for act in actions:
        moved = act.get("moved_to")
        orig = act.get("original")
        if not moved or not orig:
            print("⚠️ 跳过（清单缺字段）")
            fail += 1
            continue
        src = Path(moved)
        dst = Path(orig) if os.path.isabs(str(orig)) else (
            Path(root) / str(orig) if root else None)
        if dst is None:
            print(f"⚠️ 跳过（旧清单只有相对路径，请补 --root）：{orig}")
            fail += 1
            continue

        if dst.exists() and not src.exists():
            print(f"✅ 原位已有（视为已还原）：{orig}")
            skip += 1
            continue
        if not src.exists():
            print(f"⚠️ 跳过（隔离区里找不到）：{orig}")
            fail += 1
            continue
        if dst.exists():
            print(f"⚠️ 跳过（原位已有其他文件，不覆盖）：{orig}")
            skip += 1
            continue
        expect = act.get("sha256")
        if expect and not force:
            try:
                actual = file_sha256(src)
            except OSError as e:
                print(f"⚠️ 跳过（读不到隔离区文件：{e}）：{orig}")
                fail += 1
                continue
            if actual != expect:
                print(f"⚠️ 跳过（文件内容已变，哈希不符）：{orig}")
                warn += 1
                continue
        if dry_run:
            print(f"🔎 将还原 {orig}")
            ok += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except OSError as e:
            print(f"⚠️ 还原失败（{e}）：{orig}")
            print("   ↳ 若文件在系统废纸篓，macOS 权限会阻止脚本读取；"
                  "请在 Finder 里手动拖回，或改用隔离区模式。")
            fail += 1
            continue
        print(f"♻️  还原 {orig}")
        ok += 1
    prefix = "（演练，未改动）" if dry_run else ""
    print(f"\n✅ 还原 {ok} 个，跳过 {skip} 个，失败 {fail} 个，哈希告警 {warn} 个{prefix}")
    return {"ok": ok, "skip": skip, "fail": fail, "warn": warn, "dry_run": dry_run}



# ---------------- 报告 ----------------

def render_markdown(root, chains, orphans, pairs, stats, runtime, out_json, tiers=None):
    L = []
    L.append(f"# 项目文件地图 · {root.name}")
    L.append("")
    L.append(f"> 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ "
             f"参与分析 {stats['total']} 个文件 ｜ {len(chains)} 条版本链 ｜ "
             f"{len(orphans)} 个孤立文件")
    if stats.get("wasted"):
        L.append(f"> 🔶 完全相同的重复文件：删除多余副本约可释放 "
                 f"{stats['wasted'] / 1048576:.0f} MB")
    L.append("")
    L.append("> **给新 Agent 的提示**：接手本项目请先读本文件，再按需打开各链的"
             "「🟢 主版本」，跳过标为「🟡 旧版」的文件，可大幅节省 token。")
    L.append("")

    if chains:
        L.append("## 🟢 版本链")
        L.append("")
        for key in sorted(chains, key=lambda k: -max(m["mtime"] for m in chains[k])):
            members = chains[key]
            primary, conf, notes, roles, skipped = pick_primary(members)
            L.append(f"### `{primary['rel']}` 所在链（{len(members)} 个版本，置信度 {conf}"
                     + (f"，另有 {skipped} 个未比较" if skipped else "") + "）")
            L.append("")
            for m, role in sorted(roles, key=lambda r: r[0]["mtime"], reverse=True):
                if role == "primary":
                    mark = "🟢 主版本"
                elif role == "stale":
                    mark = "🟡 旧版"
                elif role == "diverged":
                    mark = "🟠 分支（内容更全，可能被跳过）"
                elif role == "unrelated":
                    mark = "⚪ 同名但内容无关"
                else:
                    mark = "⬜ 同名（未能比较内容）"
                cov = ""
                if role in ("stale", "diverged") and m["data"] is not None \
                        and primary["data"] is not None and m["hash"] != primary["hash"]:
                    cov = f"，与主版本内容重合 {containment(primary['data'], m['data']):.0%}"
                L.append(f"- {mark} `{m['rel']}` — {m['size']:,}B，"
                         f"改于 {fmt_time(m['mtime'])}{cov}")
            for note in notes:
                L.append(f"- {note}")
            L.append("")

    if pairs:
        ident = [(a, b, g) for a, b, sim, why, g in pairs if why == "内容完全一致"]
        near = [(a, b, sim) for a, b, sim, why, g in pairs if why == "内容高度相似"]
        if ident:
            L.append("## 🔶 完全相同的重复文件（按目录对汇总）")
            L.append("")
            agg = defaultdict(lambda: {"groups": 0, "wasted": 0, "example": None})
            seen_hashes = set()
            for a, b, g in ident:
                if a["hash"] in seen_hashes:
                    continue
                seen_hashes.add(a["hash"])
                key = tuple(sorted([topdir(a["rel"]), topdir(b["rel"])]))
                e = agg[key]
                e["groups"] += 1
                e["wasted"] += a["size"] * (g - 1)
                if e["example"] is None:
                    e["example"] = (a["rel"], b["rel"])
            for key in sorted(agg, key=lambda k: -agg[k]["wasted"]):
                e = agg[key]
                L.append(f"- `{key[0]}/` ⟷ `{key[1]}/` — {e['groups']} 组完全相同的文件，"
                         f"删除多余副本可释放约 {e['wasted'] / 1048576:.1f} MB"
                         f"（例：`{e['example'][0]}` ⟷ `{e['example'][1]}`）")
            L.append("")
        if near:
            L.append("## 🔶 内容高度相似（起名不同，可能有细微差异）")
            L.append("")
            seen = set()
            for a, b, sim in near:
                sig = tuple(sorted([a["rel"], b["rel"]]))
                if sig in seen:
                    continue
                seen.add(sig)
                L.append(f"- `{a['rel']}` ⟷ `{b['rel']}` — 相似度 {sim:.0%}，"
                         f"大小 {a['size']:,}B / {b['size']:,}B")
            L.append("")

    if orphans:
        L.append(f"## ⚪ 孤立文件（不属于任何版本链，最多列出最新 {ORPHAN_LIST_MAX} 个）")
        L.append("")
        ordered = sorted(orphans, key=lambda m: m["mtime"], reverse=True)
        for f in ordered[:ORPHAN_LIST_MAX]:
            if has_temp_word(f["path"].name):
                tag = " 🧹疑似草稿/临时文件"
            elif f["version_word"]:
                tag = " 🧾文件名带版本词"
            else:
                tag = ""
            L.append(f"- `{f['rel']}` — {f['size']:,}B，改于 {fmt_time(f['mtime'])}{tag}")
        if len(ordered) > ORPHAN_LIST_MAX:
            L.append(f"- …其余 {len(ordered) - ORPHAN_LIST_MAX} 个见 JSON")
        L.append("")

    if runtime:
        L.append("## 📦 运行时/数据目录（未逐一分析，通常可排除在版本管理外）")
        L.append("")
        for d in sorted(runtime, key=lambda k: -runtime[k]):
            L.append(f"- `{d}/` — {runtime[d]} 个文件")
        L.append("")

    if tiers:
        L.append("")
        L.append(render_tiers(root, tiers, runtime))

    if not chains and not pairs:
        L.append("✅ 未发现版本混乱或重复文件。")
        L.append("")

    L.append("---")
    L.append(f"*机器可读数据：{out_json}*")
    return "\n".join(L)


def build_json(chains, orphans, pairs, root, runtime, tiers=None):
    def file_entry(f):
        return {"path": f["rel"], "size": f["size"],
                "mtime": datetime.fromtimestamp(f["mtime"]).isoformat(),
                "btime": datetime.fromtimestamp(f.get("btime", f["mtime"])).isoformat(),
                "hash": f["hash"], "is_text": f["text"]}

    data = {
        "generated_at": datetime.now().isoformat(),
        "root": str(root),
        "chains": [],
        "orphans": [file_entry(f) for f in orphans],
        "suspicious_pairs": [],
        "runtime_dirs": {k: v for k, v in runtime.items()},
    }
    for key, members in chains.items():
        primary, conf, notes, roles, skipped = pick_primary(members)
        data["chains"].append({
            "key": key, "confidence": conf, "notes": notes,
            "primary": file_entry(primary),
            "members": [{**file_entry(m), "role": role} for m, role in roles],
        })
    for a, b, sim, why, g in pairs:
        data["suspicious_pairs"].append(
            {"a": file_entry(a), "b": file_entry(b),
             "similarity": round(sim, 4), "reason": why})
    if tiers:
        data["tiers"] = tiers
    return data


# ---------------- 主程序 ----------------

def main():
    ap = argparse.ArgumentParser(description="项目文件地图 + 三档安全清理（只读/进隔离区）")
    ap.add_argument("--root", default=None, help="项目根目录")
    ap.add_argument("--out", default=None, help="报告输出目录（默认当前目录下 project_map_report/）")
    ap.add_argument("--clean", action="store_true", help="生成并展示三档清理计划（只预览）")
    ap.add_argument("--apply-tier", default=None, help="执行清理档位（目前只支持 1）")
    ap.add_argument("--dest", choices=["quarantine", "trash"], default="quarantine",
                    help="清理去向：quarantine 隔离区（默认，可完整还原）/ trash 系统废纸篓")
    ap.add_argument("--restore", default=None, help="按 manifest 一键还原")
    ap.add_argument("--dry-run", action="store_true", help="配合 --restore 演练，不改动文件")
    ap.add_argument("--force", action="store_true", help="还原时忽略哈希校验（不推荐）")
    args = ap.parse_args()

    if args.restore:
        restore_manifest(args.restore, root_hint=args.root,
                         dry_run=args.dry_run, force=args.force)
        return

    if not args.root:
        sys.exit("❌ 需要 --root，或使用 --restore")

    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.exit(f"❌ 目录不存在：{root}")

    out_dir = Path(args.out).resolve() if args.out else Path.cwd() / "project_map_report"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"🔍 扫描 {root} ...")
    files, runtime = scan(root)
    print(f"   参与分析 {len(files)} 个文件，运行时目录 {sum(runtime.values())} 个文件")

    chains, orphans = build_chains(files)
    # 名字相同只是线索：用内容裁决后，报告里只列真版本链
    chains, same_name = judge_chains(chains)
    print(f"   同名的 {len(same_name)} 个文件经内容比对是不同文件，不算版本链")
    print("   分析版本链与重复文件 ...")
    pairs = find_suspicious_pairs(files)
    wasted = 0
    seen_h = set()
    for a, b, sim, why, g in pairs:
        if why == "内容完全一致" and a["hash"] not in seen_h:
            seen_h.add(a["hash"])
            wasted += a["size"] * (g - 1)
    stats = {"total": len(files), "wasted": wasted}

    tiers = None
    if args.clean or args.apply_tier:
        print("   计算三档清理计划 ...")
        tiers = compute_tiers(files, chains, orphans)

    md = render_markdown(root, chains, orphans, pairs, stats, runtime,
                         out_dir / "project_map.json", tiers)
    (out_dir / "PROJECT_MAP.md").write_text(md, encoding="utf-8")
    (out_dir / "project_map.json").write_text(
        json.dumps(build_json(chains, orphans, pairs, root, runtime, tiers),
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✅ 完成：{len(chains)} 条版本链，{len(pairs)} 对可疑重复，"
          f"{len(orphans)} 个孤立文件")
    print(f"   报告：{out_dir / 'PROJECT_MAP.md'}")
    print(f"   数据：{out_dir / 'project_map.json'}")

    if tiers:
        t1 = tier_total_bytes(tiers["1"], root)
        t2 = tier_total_bytes(tiers["2"], root)
        t3 = tier_total_bytes(tiers["3"], root)
        print(f"\n🧹 三档清理：🟢 {len(tiers['1'])} 项（{t1 / 1048576:.1f} MB）｜ "
              f"🟡 {len(tiers['2'])} 项（{t2 / 1048576:.1f} MB）｜ "
              f"🔴 {len(tiers['3'])} 项（{t3 / 1048576:.1f} MB）")
        if args.clean:
            print("   （预览完毕。执行第一档：--apply-tier 1）")

    if args.apply_tier:
        if args.apply_tier != "1":
            sys.exit("❌ 目前只支持 --apply-tier 1（可证明安全的那一档）")
        execute_tier1(root, tiers, out_dir, dest=args.dest)


if __name__ == "__main__":
    main()
