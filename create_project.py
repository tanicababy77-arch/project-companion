#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新建项目（给小白用）：填「项目位置」和「项目名称」，一键创建文件夹并复制 file-card 模板。

模板 = AGENTS.md + scripts/write_card.py，从本脚本同级的「项目模板」目录复制过去。
"""
import os
import shutil
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SCRIPT_DIR / "项目模板"
LAST_DIR_FILE = Path.home() / ".project_companion_last_dir"


def default_location():
    """默认位置：上次用过的目录 > 环境变量 PANEL_PROJECTS_DIR > 用户主目录。"""
    try:
        if LAST_DIR_FILE.is_file():
            s = LAST_DIR_FILE.read_text(encoding="utf-8").strip()
            if s and Path(s).expanduser().is_dir():
                return s
    except OSError:
        pass
    env = os.environ.get("PANEL_PROJECTS_DIR")
    if env and Path(env).expanduser().is_dir():
        return env
    return str(Path.home())


def create_project(location, name):
    """创建 <location>/<name> 并把模板复制进去。返回 (ok, 信息)。"""
    name = (name or "").strip()
    location = (location or "").strip()
    if not name:
        return False, "项目名称不能为空"
    if not location:
        return False, "项目位置不能为空"
    if any(c in name for c in "/\\\x00"):
        return False, "项目名称里不能包含 / 或 \\"
    if not TEMPLATE_DIR.is_dir():
        return False, f"找不到模板目录：{TEMPLATE_DIR}"
    target = Path(location) / name
    if target.exists():
        return False, f"已存在：{target}"
    try:
        shutil.copytree(TEMPLATE_DIR, target)
    except OSError as e:
        return False, f"创建失败：{e}"
    return True, str(target)


class App:
    def __init__(self, root):
        root.title("新建项目")
        root.geometry("520x230")
        root.resizable(False, False)

        frm = tk.Frame(root)
        frm.pack(fill="both", expand=True, padx=18, pady=14)

        tk.Label(frm, text="项目位置").grid(row=0, column=0, sticky="w", pady=5)
        self.loc = tk.Entry(frm, width=40)
        self.loc.insert(0, default_location())
        self.loc.grid(row=0, column=1, sticky="we", pady=5)
        tk.Button(frm, text="浏览…", command=self.on_browse, width=7).grid(
            row=0, column=2, padx=(6, 0), pady=5)

        tk.Label(frm, text="项目名称").grid(row=1, column=0, sticky="w", pady=5)
        self.name = tk.Entry(frm, width=40)
        self.name.grid(row=1, column=1, columnspan=2, sticky="we", pady=5)
        self.name.focus_set()

        self.status = tk.Label(frm, text="", fg="#2f6b45", anchor="w", wraplength=480)
        self.status.grid(row=2, column=0, columnspan=3, sticky="we", pady=6)

        tk.Button(frm, text="创建", command=self.on_create, width=12).grid(
            row=3, column=0, columnspan=3, pady=8)

        frm.columnconfigure(1, weight=1)
        root.bind("<Return>", lambda e: self.on_create())

    def on_browse(self):
        initial = self.loc.get().strip() or str(Path.home())
        d = filedialog.askdirectory(initialdir=initial, title="选择项目位置")
        if d:
            self.loc.delete(0, tk.END)
            self.loc.insert(0, d)

    def on_create(self):
        loc = self.loc.get().strip()
        ok, msg = create_project(loc, self.name.get())
        if ok:
            try:
                LAST_DIR_FILE.write_text(loc, encoding="utf-8")
            except OSError:
                pass
            self.status.config(text=f"已创建：{msg}", fg="#2f6b45")
        else:
            self.status.config(text=msg, fg="#8a3330")


def main():
    root = tk.Tk()
    icon_file = SCRIPT_DIR / "assets" / "icon_1024.png"
    if icon_file.exists():
        try:
            root.iconphoto(True, tk.PhotoImage(file=icon_file).subsample(16, 16))
        except tk.TclError:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
