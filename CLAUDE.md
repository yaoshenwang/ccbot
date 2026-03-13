# CLAUDE.md

> 所有问答与回复，必须使用中文。

ccbot — Telegram bot，将 Telegram Forum topic 桥接到 Claude Code tmux 会话。每个 topic 绑定一个 tmux window，运行一个 Claude Code 实例。

技术栈：Python, python-telegram-bot, tmux, uv.

---

## 常用命令

```bash
uv run ruff check src/ tests/         # Lint — 提交前必须通过
uv run ruff format src/ tests/        # Format — 自动修复，再用 --check 验证
uv run pyright src/ccbot/             # 类型检查 — 提交前必须 0 错误
uv run pytest tests/ -x -q            # 测试 — 提交前必须通过
./scripts/restart.sh                  # 重启 ccbot 服务
ccbot hook --install                  # 自动安装 Claude Code SessionStart hook
```

---

## 分支管理

| 分支 | 用途 | Telegram Bot | 群组 | 部署目标 |
|:-----|:-----|:-------------|:-----|:---------|
| **main** | 生产版本，稳定可用 | `@ccmux_bot`（生产） | 生产群组 | Mac Mini LaunchAgent |
| **dev** | 功能开发与测试 | `@ccmux_dev_bot`（测试） | 测试群组 | Mac Mini 单独进程 |

两个 bot 各自绑定独立的 Telegram 群组，互不干扰。dev bot 的 `.env` 使用不同的 `TELEGRAM_BOT_TOKEN`，但 `ALLOWED_USERS` 相同。

**工作流：**
1. 功能开发始终在 `dev` 分支进行
2. 在测试群组中通过 dev bot 验证功能正常
3. 确认无误后 `git merge dev` 到 `main`，推送远程
4. Mac Mini 上更新生产：`uv tool install git+<repo>@main --force` → 重启 LaunchAgent

**核心准则：**

| 规则 | 说明 |
|:-----|:-----|
| 禁止直接在 main 上开发 | 所有改动必须先经 dev 分支验证 |
| 禁止未测试就合并到 main | 必须在测试群组实际操作确认 |
| dev 改坏不影响生产 | 两个 bot 进程完全隔离 |
| 回滚优先 | 生产出问题时，`git revert` 或重装上一个 main commit |

---

## 核心设计约束

- **1 Topic = 1 Window = 1 Session** — 内部路由以 tmux window ID（`@0`, `@12`）为键，非 window name。Window name 仅作显示。同目录可有多个 window。
- **Topic-only** — 不兼容非 topic 模式。无 `active_sessions`、无 `/list`、无 General topic 路由。
- **不在解析层截断消息** — 仅在发送层分割（`split_message`, 4096 字符限制）。
- **MarkdownV2 only** — 使用 `safe_reply`/`safe_edit`/`safe_send` 辅助函数（自动降级为纯文本）。
- **Hook 驱动的会话追踪** — `SessionStart` hook 写入 `session_map.json`；monitor 轮询检测会话变化。
- **每用户消息队列** — FIFO 排序，消息合并（3800 字符限制），tool_use/tool_result 配对。
- **限流** — `AIORateLimiter(max_retries=5)`（30/s 全局）。重启时预填桶避免突发。

---

## 代码规范

- 每个 `.py` 文件以模块级 docstring 开头：10 行内讲清用途，首行一句话总结，然后列核心职责和关键组件。
- Telegram 交互：优先用 inline keyboard；用 `edit_message_text` 就地更新；callback data 不超过 64 字节；用 `answer_callback_query` 即时反馈。
- 新增回调流程需遵循现有模式：在 `callback_data.py` 定义常量 → 在 `directory_browser.py` 构建 UI → 在 `bot.py` 处理回调。

---

## 配置

- 配置目录：`~/.ccbot/`，可通过 `CCBOT_DIR` 环境变量覆盖。
- `.env` 加载优先级：本地 `.env` > 配置目录 `.env`。
- 状态文件：`state.json`（topic 绑定）、`session_map.json`（hook 生成）、`monitor_state.json`（字节偏移）。

---

## Hook 配置

自动安装：`ccbot hook --install`

手动配置 `~/.claude/settings.json`：
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

---

## 部署环境（Mac Mini）

仓库地址：`https://github.com/yaoshenwang/ccbot.git`（fork 自 `six-ddc/ccbot`）

### 生产实例（main 分支）

| 配置项 | 值 |
|:-------|:---|
| 安装方式 | `uv tool install git+https://github.com/yaoshenwang/ccbot.git --force` |
| 配置文件 | `~/.ccbot/.env` |
| LaunchAgent | `~/Library/LaunchAgents/com.ccbot.plist` |
| 日志 | `/tmp/ccbot.log`, `/tmp/ccbot.err` |

### 测试实例（dev 分支）

| 配置项 | 值 |
|:-------|:---|
| 安装方式 | clone 仓库，切 dev 分支，`uv run ccbot run` 前台运行 |
| 配置文件 | 项目目录下 `.env`（使用 dev bot token） |
| 运行方式 | 手动前台运行，不需要 LaunchAgent |

### 常用操作

```bash
# 更新生产版本
ssh mini 'source ~/.zshrc; uv tool install git+https://github.com/yaoshenwang/ccbot.git --force'

# 重启生产服务
ssh mini 'launchctl unload ~/Library/LaunchAgents/com.ccbot.plist && launchctl load ~/Library/LaunchAgents/com.ccbot.plist'

# 查看生产日志
ssh mini 'tail -50 /tmp/ccbot.log'

# 回滚到指定版本
ssh mini 'source ~/.zshrc; uv tool install git+https://github.com/yaoshenwang/ccbot.git@<commit_hash> --force'
```

---

## 架构详情

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/topic-architecture.md for topic→window→session mapping details.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.
