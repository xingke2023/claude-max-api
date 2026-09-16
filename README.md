# claude-max-proxy

将 Claude Max 订阅转换为标准 Anthropic API 接口的本地代理网关。

让你的第三方工具（如 Cursor、OpenClaw 等）通过 Claude Max 订阅额度调用 Claude API，无需额外付费购买 API credits。

> **请低调使用，不要大范围宣传。**

## 原理

```
第三方客户端 → localhost:8899 → [请求处理] → api.anthropic.com
                                    ↓
                              - OAuth 认证注入
                              - 工具名称映射 (双向)
                              - System Prompt 迁移
                              - CCH 签名计算
```

代理读取 Claude Code CLI 本地保存的 OAuth token，将请求伪装为 Claude Code CLI 发出，从而使用订阅额度而非 API credits。

## 前置条件

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录（`claude` 命令可用）
- Claude Max / Pro 有效订阅

## 快速开始

```bash
# 克隆项目
git clone https://github.com/zhangbinhui/claude-max-proxy.git
cd claude-max-proxy

# 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动代理
python3 proxy.py
```

启动后代理监听 `http://localhost:8899`，兼容标准 Anthropic Messages API。

## 配置

通过环境变量配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8899` | 直接运行 Python 时的监听端口 |
| `HOST` | `127.0.0.1` | 监听地址；非回环地址必须配置 API Key |
| `PROXY_API_KEY` | 空 | 代理入口密钥；公网部署必须设置 |
| `SYSTEM_PROMPT_MODE` | `legacy` | `legacy` 迁移 system；`preserve` 原样保留 system |
| `REFRESH_TIMEOUT` | `60` | 凭证刷新超时（秒） |
| `UPSTREAM_TIMEOUT` | `300` | 上游读取超时（秒） |
| `CC_VERSION` / `CC_BUILD` | 自动检测 / 缓存或 `190` | 可显式覆盖客户端版本标识 |
| `DEBUG` | 空 | 设为 `1` 开启调试模式（请求 dump 到 `/tmp/`） |

示例：

```bash
PORT=8080 python3 proxy.py
DEBUG=1 python3 proxy.py
```

## 在第三方工具中使用

将 API Base URL 设置为 `http://localhost:8899`，本地未配置 `PROXY_API_KEY` 时 API Key 可随意填写；配置后必须填写该密钥，支持 `x-api-key` 或 `Authorization: Bearer`。上游仍使用本地 OAuth token。

## 关于 Extra Usage

本项目的主要目的是让第三方工具用上 Claude Max/Pro 的订阅额度，因此建议在 [claude.ai/settings/usage](https://claude.ai/settings/usage) 中关闭 Extra Usage，避免产生额外费用。

如果遇到 `You're out of extra usage` 报错，说明该请求被 Anthropic 判定为第三方客户端，强制走 Extra Usage 计费。

## 工具名称映射

代理通过 `tool_name_mapping.json` 对工具名进行双向映射：

- **请求方向**：第三方客户端的工具名 → Claude Code 原生工具名（如 `exec` → `Bash`，`read` → `Read`）
- **响应方向**：Claude Code 返回的工具名 → 第三方客户端工具名（自动反向映射）
- 历史消息中的 `tool_use.name` 和 `tool_choice.name` 同步转换。
- 响应按本次请求的映射还原，原生工具名保持不变；名称冲突返回 400。
- `direct` 和 `borrowed` 只修改名称，保留原始 schema。
- `_remove`、`_padding` 不再执行：保留客户端声明的工具，不注入客户端无法执行的工具。

## System Prompt 处理

默认 `SYSTEM_PROMPT_MODE=legacy` 保留历史兼容行为：将原始 system prompt 移入第一条 user message，system 使用客户端标识。这会改变指令优先级并丢失原 system 块的缓存属性。

设为 `SYSTEM_PROMPT_MODE=preserve` 可原样保留 system 内容及缓存属性。上游是否接受此模式需要实际验证，代理不保证订阅计费或上游兼容性。

## 参考文件

| 文件 | 说明 |
|------|------|
| `cc_tools_baseline.json` | Claude Code 原生工具定义快照，用于对照映射 |
| `cc_system_baseline.json` | Claude Code 原生 system prompt 快照，用于对照格式 |
| `tool_name_mapping.json` | 工具名称映射配置 |

## 注意事项

- 本项目仅供学习和研究用途
- Token 依赖 Claude Code CLI 的本地凭证，请勿泄露 `~/.claude/.credentials.json`
- Token 过期时代理会自动通过 `claude --print` 刷新
- CC 版本号从本地 `claude --version` 自动检测，build 号可通过 `.cc_build` 文件缓存
- 本项目由 Claude Opus 4.6 编写，遇到问题请咨询 AI

## License

[MIT](LICENSE)

## HTTPS 部署与测试

本机部署入口：`https://tokens.xingke888.com`，Messages 路径为 `/v1/messages`。

- Nginx 配置：`deploy/tokens.xingke888.com.conf`；关闭响应缓冲，支持 SSE。
- systemd 配置：`claude-max-proxy.service`；Gunicorn 单 worker、8 线程，监听 `127.0.0.1:8899`。
- 密钥存于 `.env`（权限 600，已排除 Git），由 systemd 的 `EnvironmentFile` 加载；直接运行 Python 不会自动读取 `.env`。
- `/health` 同样要求密钥；过期凭证返回 503。此接口仅检查本地凭证，不验证上游可用性。
- 凭证刷新在单进程内加锁并设有超时；增加 worker 前需改为跨进程锁。
- DEBUG 文件包含完整请求，仅用于排查，使用独立临时文件及权限 600，使用后手动清理。

运行不调用上游的回归测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```
