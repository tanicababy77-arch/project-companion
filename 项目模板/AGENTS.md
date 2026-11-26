# 给在本项目干活的 AI Agent

## 铁律：写文件必须留名片

你每**新建或改动一个文件**后，都要给这个文件写一张「名片」，把「这是过程文件还是最终版」
在写的那一刻就声明出来，而不是让事后的人去猜。

写卡脚本**随项目走**，就在本项目的 `scripts/write_card.py`（网盘/U盘同步时会一起带走，
任何机器都能用，不用装全局 skill）：

```bash
python3 scripts/write_card.py \
  --root "<项目绝对路径>" \
  --path "<文件相对项目的路径>" \
  --agent <Trae/WorkBuddy/DSH/豆包> \
  --status <final|intermediate|input|reference> \
  --note "<一句话人话说明>"
```

如果项目里没有 `scripts/write_card.py`，就用全局 skill 的脚本：

```bash
python3 ~/.trae-cn/skills/file-card/scripts/write_card.py --root ... --path ... --agent ... --status ... --note ...
```

## status 怎么选（务必诚实）

- `final` —— 最终交付物，要保留的成品
- `intermediate` —— 过程文件 / 草稿 / 中间产物，可清理
- `input` —— 从外部拿来的输入资料
- `reference` —— 留给以后看的参考

## 要点

- 别把过程文件标成 `final`，否则看板会误导用户。
- 名片一文件一卡，同一路径反复写 = 覆盖成最新；不同路径互不冲突。
- 路径相对项目根，用 `/`。

（名片写在 `<项目根>/.pm_cards/<路径哈希>.json`，由「项目伴侣」看板读取。）
