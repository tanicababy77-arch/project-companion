---
name: file-card
description: "给文件写可检索名片。AI agent 新建或改动项目文件后，调用本 skill 在文件所在项目写下 .pm_cards/<路径哈希>.json 记录（status/note/agent，一文件一卡），供「项目伴侣」看板识别过程文件与最终版。当 agent 创建或改动文件、或需要记录文件产出时使用。"
---

# 文件名片 file-card

## 这个 skill 是干什么的

多 Agent 干活会留下一堆过程文件（草稿、中间产物、被取代的旧版），事后很难分清哪个是最终版。与其事后猜，不如在**写文件的那一刻**主动留一张「名片」，声明这个文件是什么。

本 skill 往 `<项目根>/.pm_cards/` 写一个独立的 `.json`（一文件一卡，同一路径反复写 = 覆盖成最新）。看板读这些记录，把「猜」变成「声明」。

## 何时调用（必须）

每**新建或改动一个文件**后，调用本 skill 写一张名片：

- 新建一个文件（尤其 Word/PPT/Excel、代码、报告）
- 覆盖/更新一个已有文件，产生了新版本
- 生成了一份中间产物、草稿、临时文件

**不需要写名片**：删除文件、纯格式化改动、改动 `.pm_cards/` 本身。

## 怎么写（运行脚本）

优先用项目里自带的脚本（随项目走、跨机器一致）：

```bash
python3 <项目根>/scripts/write_card.py \
  --root <项目绝对路径> \
  --path <文件相对项目的路径> \
  --agent <Trae/WorkBuddy/DSH/豆包> \
  --status <final|intermediate|input|reference> \
  --note "<一句话，人话说明这是什么>" \
  [--replaces <被这份取代的旧文件相对路径>]
```

如果项目里没有 `scripts/write_card.py`，用本技能自带的副本：`python3 <本技能所在目录>/scripts/write_card.py ...`（参数同上）。

- `--root` 是文件所在项目的根目录，通常是本次任务的工作目录 / 工作区根。
- `--path` 相对 `--root`，路径统一用 `/`。

## status 四个值（务必诚实）

| status | 含义 | 什么时候用 |
|---|---|---|
| `final` | 最终版 | 这是交付物、要保留的成品 |
| `intermediate` | 过程文件 | 中间产物、草稿、会被后面版本取代、可清理 |
| `input` | 参考资料 | 从外部拿来的输入，不是产出 |
| `reference` | 参考 | 留给以后看的参考，不是交付物 |

## 例子

```bash
# 生成一份最终报告
python3 <项目根>/scripts/write_card.py \
  --root "<项目根>" \
  --path "报告/季度总结.docx" \
  --agent Trae --status final --note "Q3 季度总结，最终版"

# 定稿取代了草稿：给定稿写卡时声明它取代了草稿
python3 <项目根>/scripts/write_card.py \
  --root "<项目根>" \
  --path "报告/季度总结.docx" \
  --agent Trae --status final --note "Q3 季度总结，最终版" \
  --replaces "报告/季度总结_草稿.docx"

# 那个草稿本身
python3 <项目根>/scripts/write_card.py \
  --root "<项目根>" \
  --path "报告/季度总结_草稿.docx" \
  --agent Trae --status intermediate --note "初稿，已被定稿取代"
```

## 铁律

- 名片是**自述**，写真实状态。过程文件就标 `intermediate`，别为了省事都标 `final`。
- 一文件一卡：同一路径反复写 = 覆盖成最新；不同路径互不冲突。
- 路径统一用 `/`，相对项目根。
