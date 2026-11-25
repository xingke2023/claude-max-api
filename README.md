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
| `PROXY_API_KEY_SECONDARY` | 空 | 可选的第二把代理入口密钥 |
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

将 API Base URL 设置为 `http://localhost:8899`。可通过 `x-api-key` 或 `Authorization: Bearer` 传入任一已配置的代理入口密钥。两个密钥都留空时，本地代理不校验 API Key。

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

网关默认添加顶层 `cache_control={"type":"ephemeral"}`，启用 5 分钟自动 prompt 缓存，覆盖完整对话并随历史增长移动缓存断点。保留客户端提供的缓存设置；legacy 模式若移除 system 缓存标记且无其他标记，则补上自动缓存。实际命中以响应 `usage.cache_read_input_tokens` 为准，首次写入或未满足模型最小缓存长度时可能为 0。

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

## Claude Code 自动更新部署

`deploy/claude-code-update.timer` 每天 UTC 04:00 唤醒更新服务。
`deploy/update-claude-code.sh` 在上次成功检查后满 72 小时时才查询 npm 最新版，
有新版才安装，并在安装后的 `claude --version` 检查成功后重启运行中的代理。
服务使用 `/var/lib/claude-code-update/last-success` 记录检查时间，失败不推进时间。
定时器开机自动启动，关机错过的任务在开机后补跑。
脚本安装到 `/usr/local/sbin/update-claude-code`，两个 unit 安装到 `/etc/systemd/system/`。
日志：`sudo journalctl -u claude-code-update.service`。
停用：`sudo systemctl disable --now claude-code-update.timer`。

## Opus 速度配置

在 `.env` 设置 `FORCE_FAST_OPUS=true` 和 `FAST_OPUS_MODEL=claude-opus-5-5`，
重启服务后，所有 `/v1/messages` 请求（含流式）会覆盖客户端模型和速度，
使用指定 Opus 模型及 `speed=fast`。此模式仅支持 OAuth 上游，按额外付费额度计费，
不包含在 Max 订阅额度内。上游权限、余额或限流错误会原样返回，不自动降速。
设置 `FORCE_FAST_OPUS=false` 并重启可恢复客户端选择。
如需统一使用一般速度的 Opus，设置 `FORCE_FAST_OPUS=false`、
`FORCE_STANDARD_OPUS=true` 并重启；模型仍由 `FAST_OPUS_MODEL` 指定，
客户端传入的快速模式参数会被移除。两个强制模式不能同时开启。

## Bedrock 配置

设置 `UPSTREAM_PROVIDER=bedrock`，并在 `.env` 中配置 `AWS_REGION` 和
`AWS_BEARER_TOKEN_BEDROCK`（也支持 AWS SDK 默认 IAM 凭证链）。重启服务后生效。
Bedrock 模式保留原始 system 和工具名称，不执行 OAuth 身份注入或工具映射。
支持普通 Messages 响应和 SSE 流式响应；调用按 AWS Bedrock 计费。

客户端可使用 `claude-sonnet-4-6`、`claude-opus-4-6`、
`claude-haiku-4-5-20251001`，或直接填写 Bedrock 模型/推理配置 ID、ARN。
其他模型通过 `BEDROCK_MODEL_MAP` JSON 对象配置映射。
`/health` 的 `configured` 仅表示配置可加载，不代表上游权限或额度已验证。

实现参考：[AWS InvokeModel 流式接口](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/invoke_model_with_response_stream.html)。
