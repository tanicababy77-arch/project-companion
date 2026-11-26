#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file-card 写卡脚本（随项目走）：往 <项目根>/.pm_cards/<路径哈希>.json 写一张文件名片。

一文件一卡，抗「多机器 / 网盘 / U盘」同步：每个卡片独立小文件，互不覆盖、互不冲突；
同一路径反复写卡 = 覆盖成最新那条。把本文件拷进项目（网盘/U盘同步时一起带走），
任何机器上的 agent 都能直接 `python3 scripts/write_card.py`，不用装全局 skill。

纯标准库，Mac / Windows 通用。用法：
  python3 scripts/write_card.py --root <项目绝对路径> --path <文件相对路径> \
      --agent Trae --status intermediate --note "一句话说明" [--replaces <被取代的文件>]
"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

STATUS = {"final", "intermediate", "input", "reference"}


def main():
    ap = argparse.ArgumentParser(description="写一张文件名片")
    ap.add_argument("--root", required=True, help="项目根目录（名片写到它的 .pm_cards/ 下）")
    ap.add_argument("--path", required=True, help="文件相对项目根的路径")
    ap.add_argument("--agent", default="unknown", help="写文件的 agent（Trae/WorkBuddy/DSH/豆包…）")
    ap.add_argument("--status", default="intermediate", choices=sorted(STATUS),
                    help="final=最终版 / intermediate=过程文件 / input=参考资料 / reference=参考")
    ap.add_argument("--note", default="", help="一句话人话说明（给不懂电脑的人看）")
    ap.add_argument("--replaces", default=None, help="可选：这份文件取代了哪个旧文件（相对路径）")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    card_dir = root / ".pm_cards"
    card_dir.mkdir(parents=True, exist_ok=True)

    rel = args.path.lstrip("/\\")
    rec = {
        "path": rel,
        "agent": args.agent,
        "status": args.status,
        "note": args.note,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if args.replaces:
        rec["replaces"] = args.replaces.lstrip("/\\")

    key = hashlib.md5(rel.encode("utf-8")).hexdigest() + ".json"
    (card_dir / key).write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    suffix = f"（{args.note}）" if args.note else ""
    print(f"已写名片：{rel} -> {args.status}{suffix}")


if __name__ == "__main__":
    main()
