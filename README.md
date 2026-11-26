# 项目伴侣 · Project Companion

陪不懂技术的人，管好 AI agent 干活留下的文件：分清哪个是过程文件、哪个是最终版，能安全清理、不丢东西。

## 解决什么问题

多个 AI agent（Trae / WorkBuddy / DSH / 豆包工作……）同时在项目里写文件，会留下一堆让人头疼的东西：

- 同一份文件的旧版本（`方案v2`、`最终版`、`副本`……）
- 名字正常、但其实是中间产物的过程文件
- 分不清哪个才是最终交付物

项目伴侣做三件事：

1. **识别版本堆叠** —— 找出「同一份文件的多份副本」，标出最新那份。
2. **识别过程文件** —— 靠 agent 自己写的「名片」+ 文件名启发式。
3. **安全归档** —— 把旧副本挪进 `_versions/` 并留还原清单，不直接删。

## 核心机制：文件名片（file-card）

与其事后猜，不如在写文件那一刻让 agent 自己声明。每个新建/改动的文件，agent 写一张名片（`.pm_cards/<路径哈希>.json`）：

```json
{
  "path": "报告/季度总结.docx",
  "agent": "Trae",
  "status": "final",
  "note": "Q3 总结定稿",
  "replaces": "报告/季度总结_v2.docx"
}
```

- `status`：`final`（最终）/ `intermediate`（过程文件）/ `input`（外部输入）/ `reference`（参考）
- 一文件一卡，同一路径反复写 = 覆盖成最新；名片跟着项目走（网盘 / U 盘同步都会带上）

配套三件套：

- `skills/file-card/` —— file-card 技能本体，装进各 agent（Trae 的 `~/.trae-cn/skills/`、豆包工作的 `.user_skills/` 等）
- `项目模板/` —— 拷进每个项目的 `AGENTS.md` + `scripts/write_card.py`
- `create_project.py` —— 小白用的一键新建项目工具（自动放好上面两件）

## 归因（哪个 agent 改的）

| 来源 | 方式 | 可信度 |
|---|---|---|
| 文件名片 | agent 自声明 | 确定 |
| Trae | git 提交记录 | 确定 |
| DSH / WorkBuddy | 会话日志时间窗匹配 | 推测 |
| 豆包工作 | 它自己的 agent 轨迹（macOS） | 推测 |

没名片、也没命中任何会话日志的文件，显示「未知来源」。

## 目录结构

```
project_panel.py           服务端 + 接口（纯标准库，不装 Flask）
project_map.py             扫描 / 版本链 / 名片读取 / 隔离归档
project_watcher.py         文件监听
panel.html                 看板页面
create_project.py          桌面「新建项目」小工具（Tkinter）
project_companion_bar.swift  菜单栏常驻小应用（macOS）
scripts/make_icon.py       生成图标（需要 Pillow）
skills/file-card/          file-card 技能（SKILL.md + write_card.py）
项目模板/                  新项目模板：AGENTS.md + scripts/write_card.py
deploy/                    部署模板（macOS launchd、示例配置）
win_panel/                 Windows 版
```

## 安装（macOS）

### 1. 面板服务（launchd 常驻）

把整个目录放到任意固定位置，填好 `deploy/macos/` 里的模板占位符，装进 LaunchAgents：

```bash
cd deploy/macos
sed -i '' \
  -e "s#__PYTHON__#/opt/homebrew/bin/python3#" \
  -e "s#__TOOL_DIR__#$HOME/工具/project-companion#" \
  -e "s#__LOG_DIR__#$HOME/Library/Logs#" \
  com.project-companion.panel.plist
sed -i '' \
  -e "s#__PYTHON__#/opt/homebrew/bin/python3#" \
  -e "s#__TOOL_DIR__#$HOME/工具/project-companion#" \
  -e "s#__LOG_DIR__#$HOME/Library/Logs#" \
  com.project-companion.watcher.plist
cp com.project-companion.panel.plist ~/Library/LaunchAgents/
cp com.project-companion.watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.project-companion.panel.plist
launchctl load ~/Library/LaunchAgents/com.project-companion.watcher.plist
```

在 panel 的 plist 里加环境变量，指定你的项目根目录：

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PANEL_PROJECTS_DIR</key>
    <string>/你的/项目/目录</string>
</dict>
```

浏览器打开 `http://127.0.0.1:8900`。

### 2. 菜单栏小应用（可选）

```bash
swiftc -O project_companion_bar.swift -o project_companion_bar
```

同样挂到 LaunchAgents（`RunAtLoad` + `KeepAlive`）即可开机常驻；图标在 `assets/menu_bar_template.png`。

## 配置项目

四种方式（任选）：

- 编辑 `panel_config.json`，把项目路径写进 `projects` 数组；
- 命令行：`python3 project_panel.py --add "路径"`；
- 环境变量 `PANEL_PROJECTS_DIR`（自动收录该目录下的子目录）；
- 环境变量 `PANEL_PROJECTS`（多个用 `:` 分隔；Windows 用 `;`）。

新建项目最省事：用桌面「新建项目」小工具（`create_project.py`），填位置和名字一键创建。

## 安全模型

- **本地 HTTP 服务**：只监听 `127.0.0.1`，带随机 token（`.panel_token`），不对外、不联网、不上传。
- **只读 agent 日志**：为了归因，会读 DSH / WorkBuddy / 豆包工作在本机的会话日志，只读不写。
- **归档不是删除**：旧文件被挪进 `_versions/`，`manifests/` 里留还原清单，可恢复。

## 已知限制

- 没有实时事件流，点「重新扫描」或等缓存过期才更新（agent 是成批写文件的）。
- **读不了 Word/PPT/Excel 的内容**，这类文件的版本关系只能靠文件名 + 名片判断。
- **图片/模型文件**只能靠目录和备份标记判断，分不出两个不同字节的图是不是同一版。
- 名片依赖 agent 自律地写；没名片的文件回退到文件名/内容启发式。

## 构建与依赖

- 核心（面板 / 扫描 / 监听 / 写卡）是**纯标准库**，无需安装第三方包。
- 图标生成 `scripts/make_icon.py` 需要 **Pillow**（`pip install Pillow`）；图标已预生成在 `assets/`，不想装 Pillow 可直接用现成的。

## 许可证

[MIT](LICENSE)
