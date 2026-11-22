# 多模型 Agent

一个单文件、零第三方依赖的本地智能体：在同一个对话里自由切换 Kimi 与 DeepSeek，自动统计每一次调用的 Token 用量。

## 功能

- **多模型自由切换**：内置你当前拥有的 6 个模型别名，对话历史保留，随时用 `/model` 切换。
- **两种 API 协议**：Kimi 使用 Anthropic 协议（`/v1/messages`），DeepSeek 使用 OpenAI 协议（`/v1/chat/completions`）。
- **工具调用**：`run_shell`、`read_file`、`write_file`、`list_dir`，让模型真正操作本机。
- **文件下载**：网页顶栏「文件下载」列出 `~/.multi-model-agent/downloads` 中的文件，点击即可下载；模型可用 `publish_download` 把文件复制到该目录。
- **思考过程展示**：模型返回 `reasoning_content` 或 Anthropic `thinking` 块时，网页以可折叠面板展示完整思考过程（默认折叠，点击「展开 / 收起」切换），历史对话重新打开后也能查看。
- **可自查 Token**：模型可以调用 `get_token_usage` 工具，主动回答「你用了多少 Token」；CLI 也内置 `/usage` 和 `--stats`。
- **Token 统计**：
  - 每次 API 响应优先读取服务端返回的 `usage`；
  - 服务端不返回时自动估算并标注；
  - 每轮结束打印本轮与会话累计；
  - 全部记录持久化到 `~/.multi-model-agent/usage.jsonl`，跨会话可查；
  - 网页端始终显示历史累计，并可按「近24小时 / 近7天 / 近1个月 / 近3个月 / 全部」筛选，查看总量、输入/输出/缓存、趋势图和按模型分布；
  - `/usage` 查看会话与历史累计，`--stats` 直接查历史。
- **安全确认**：执行 shell 命令和写文件前默认询问，可临时用 `a` 或 `--auto` 放开。

## 快速开始

```bash
cd ~/multi-model-agent
python3 agent.py                 # 交互模式
python3 agent.py --list-models   # 查看模型与密钥状态
python3 agent.py --ping          # 用最小请求测试所有模型
python3 agent.py -m kimi "你好"  # 指定模型单次调用
python3 agent.py --stats         # 历史 Token 统计
```

要求 Python 3.11+（读取 TOML 密钥需要 `tomllib`）。Agent 本身只用标准库。

## Web 服务（局域网访问）

WSL 内部服务监听 8010，Windows 侧用 `win_proxy.py` 转发到 8000，这样 `localhost:8000` 和局域网地址都可访问：

```bash
# WSL 内
cd ~/multi-model-agent
python3 server.py --host 0.0.0.0 --port 8010 --auto

# Windows 侧（PowerShell 或 cmd）
copy win_proxy.py %USERPROFILE%\agent_win_proxy.py
D:\anaconda\python.exe %USERPROFILE%\agent_win_proxy.py --listen-host 0.0.0.0 --listen-port 8000 --upstream-port 8010
```

访问地址：`http://localhost:8000/?token=<令牌>` 或 `http://127.0.0.1:8000/?token=<令牌>`。令牌保存在 `~/.multi-model-agent/server.token`。网页为白灰浅色主题，支持模型切换、对话、工具过程展示，右上角「Token 统计」抽屉（历史累计 + 时间范围筛选 + 趋势图 + 按模型分布），以及「文件下载」抽屉（列出下载目录并点击下载）。加 `&stats=1` 可直接打开统计抽屉。

在 WSL 内部直接访问时用 `http://127.0.0.1:8010/?token=<令牌>`（WSL 看不到 Windows 侧监听的 localhost:8000）。

HTTP 接口（都需要 `Authorization: Bearer <token>`）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/models` | 模型列表与密钥状态 |
| `GET /api/stats?range=24h\|7d\|30d\|90d\|all` | 按时间范围聚合 Token：总量、输入/输出/缓存、趋势、按模型分布，并附带历史累计 |
| `POST /api/chat` | `{session_id?, message, model?}` |
| `GET /api/usage?session_id=...` | 会话 Token 统计 |
| `GET /api/downloads` | 下载目录的文件列表（名称、大小、修改时间） |
| `GET /downloads/<文件名>?token=<token>` | 下载指定文件 |
| `POST /api/reset` | `{session_id}` 清空对话 |
| `GET /healthz` | 健康检查，无需令牌 |

`--auto` 会让 shell 与写文件免确认，只建议在可信局域网使用。其他电脑访问 `<你的局域网IP>:8000` 若超时，用管理员 PowerShell 放行端口：

```powershell
New-NetFirewallRule -DisplayName "MultiModelAgent-8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Any
```

停止服务：

```bash
kill $(cat ~/.multi-model-agent/server.pid)
taskkill.exe /F /PID $(cat ~/.multi-model-agent/win_proxy.pid)
```

## 交互命令

| 命令 | 作用 |
| --- | --- |
| `/models` | 列出模型、协议、上下文长度、密钥来源 |
| `/model <别名>` | 切换模型 |
| `/usage` | 本次会话 + 历史累计 Token |
| `/context` | 当前上下文占用 |
| `/new` | 清空对话历史 |
| `/auto on\|off` | shell/写文件免确认 |
| `/tools on\|off` | 开关工具调用 |
| `/exit` | 退出 |

## 模型配置

配置文件是 `models.json`。仓库中的地址统一脱敏为 `localhost`；本机真实服务地址请放在 `models.local.json`（已在 `.gitignore` 中，不会提交），程序启动时会自动把它深度合并到 `models.json` 之上。

每个模型别名包含：

```json
"kimi": {
  "provider": "kimi",
  "model": "kimi-for-coding",
  "display": "Kimi K2.8 Preview",
  "max_context": 1048576,
  "max_tokens": 8192
}
```

提供商（`providers`）支持两种协议：

- `"protocol": "openai"`：POST `{base_url}/chat/completions`，Bearer 鉴权。
- `"protocol": "anthropic"`：POST `{base_url}/messages`，支持 `x-api-key`、`Authorization: Bearer` 或两者（`auth_style`）。

密钥解析顺序：

1. 环境变量 `api_key_env`（如 `KIMI_API_KEY`、`DEEPSEEK_API_KEY`）；
2. 配置里的 `api_key` 字段；
3. `api_key_from` 指向的外部文件，按顺序尝试：
   - Kimi 默认读取 `~/.kimi-code/credentials/kimi-code.json` 里的 OAuth `access_token`（复用你已登录的 Kimi Code，开箱即用）；
   - DeepSeek 默认读取 `~/.kimi-code/config.toml` 中 `local-deepseek` 的密钥；
   - 支持 TOML 路径和 JSON 路径（`json_paths` 可写多组）。

密钥不会打印到终端，也不写入 Token 日志。

> Kimi 的 OAuth 令牌会随登录状态过期。若 `--ping` 报 401，先运行一次 `kimi`（或 `kimi login`）刷新登录；也可以到 Kimi Code 控制台创建 API Key，然后设置 `KIMI_API_KEY`。
>
> `--ping` 实测中，`kimi-fast` 和 `k3.1-pro` 可能返回 403，这是订阅套餐不含该模型，不是配置错误。

## Token 统计口径

- 输入、输出、缓存命中分别累计；缓存命中来自 `cache_read_input_tokens`（Anthropic）或 `prompt_tokens_details.cached_tokens`（OpenAI）。
- Anthropic 的输入总计 = `input_tokens + cache_read + cache_creation`。
- 服务端未返回 `usage` 时，按「中日韩字符 ≈ 1.1 token、其他字符 ≈ 3.6 字符/token」估算，记录会标记 `"estimated": true`。
- 日志每行一条请求：时间、会话、模型别名、模型 ID、输入/输出/缓存/总 token、是否估算。

## 工具与安全

- `run_shell`：在当前目录执行命令，合并输出，exit code 一并返回，最多回传 20000 字符。
- `read_file`：默认最多读 200000 字节。
- `write_file`：覆盖或新建，自动建父目录。
- `list_dir`：最多列 500 项。
- `publish_download`：把一个本机文件复制到 `~/.multi-model-agent/downloads`，供网页下载；同名文件会被覆盖。
- `get_token_usage`：返回本次会话与历史累计的 Token 明细，无需确认。

`run_shell` 与 `write_file` 默认需要确认；非交互模式（直接传 prompt）下默认拒绝，除非加 `--auto`。WebUI 的「必要时询问」还会拦截 `pip install`、`npm install`、`apt install` 等软件安装以及所有 `sudo` 命令，弹窗等待用户允许或拒绝。

## 扩展新模型

在 `providers` 里加一个提供商，再在 `models` 里加别名即可。例如新增一个 OpenAI 兼容服务：

```json
"providers": {
  "myapi": {
    "protocol": "openai",
    "base_url": "https://example.com/v1",
    "api_key_env": "MYAPI_KEY"
  }
},
"models": {
  "my-model": {
    "provider": "myapi",
    "model": "my-model-name",
    "max_context": 128000
  }
}
```
