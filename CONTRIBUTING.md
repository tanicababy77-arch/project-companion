# 参与贡献 · CONTRIBUTING

感谢你愿意为「项目伴侣」贡献！本文说明如何报告问题、提出建议、以及提交代码。

## 一、报告问题（Bug）

如果你发现 bug，请在 GitHub Issues 里用「Bug 报告」模板提交，尽量包含：

- 你的系统（macOS / Windows）和版本
- 复现步骤（越具体越好）
- 实际发生了什么、你期望发生什么

## 二、提出功能建议

用「功能请求」模板，说明：

- 你想解决什么问题
- 期望的行为是什么
- 有没有别的替代方案你考虑过

## 三、提交代码

1. Fork 本仓库，clone 到本地。
2. 新建一个分支：`git checkout -b feat/你的功能` 或 `fix/你的修复`。
3. 修改、本地测试后提交。
4. Push 到你的 fork，然后发 Pull Request。

## 四、提交信息规范（Commit 规范）

本项目遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 风格，提交信息统一用：

```
<类型>(<范围>): <一句话说明>
```

**类型（type）**：

| 类型 | 含义 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修复 bug |
| `docs` | 只改文档 |
| `style` | 代码格式（不影响逻辑） |
| `refactor` | 重构（不改功能、不改 bug） |
| `test` | 加测试 |
| `chore` | 杂项（构建、依赖、配置等） |

示例：

```
feat: 新增豆包工作归因来源
fix(panel): 修复端口 8899/8900 不一致
docs: 补充 README 安装说明
```

要求：

- 首行不超过 72 字符
- 用祈使语气（"新增"而不是"新增了"）
- 一个提交只做一件事

## 五、开发环境

- 核心代码是**纯 Python 标准库**，无需安装第三方包
- 图标生成 `scripts/make_icon.py` 需要 `pip install Pillow`
- 菜单栏 app 需要 `swiftc`（Xcode 命令行工具）

## 六、代码风格

- 保持与现有代码一致
- 注释用中文
- 不引入不必要的第三方依赖（尽量纯标准库）

## 许可证

贡献的代码默认按本仓库的 [MIT 许可证](LICENSE) 发布。
