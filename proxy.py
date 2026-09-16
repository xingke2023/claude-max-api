#!/usr/bin/env python3
"""
Claude Max → Standard API 代理网关 v3
基于 Neo (linux.do) 的逆向方案 + mitmproxy 抓包验证

提供 OAuth 转发、工具映射和 SSE 流式响应。
"""

import json
import os
import sys
import time
import uuid
import hmac
import ipaddress
import subprocess
import threading
import tempfile

import xxhash
import requests
from flask import Flask, request, Response, stream_with_context

app = Flask(__name__)

# ============================================================
# 配置
# ============================================================

DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
PORT = int(os.environ.get("PORT", "8899"))
HOST = os.environ.get("HOST", "127.0.0.1")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
SYSTEM_PROMPT_MODE = os.environ.get("SYSTEM_PROMPT_MODE", "legacy")
REFRESH_TIMEOUT = float(os.environ.get("REFRESH_TIMEOUT", "60"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "300"))
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
if SYSTEM_PROMPT_MODE not in {"legacy", "preserve"}:
    raise ValueError("SYSTEM_PROMPT_MODE must be legacy or preserve")
try:
    _loopback = ipaddress.ip_address(HOST).is_loopback
except ValueError:
    _loopback = HOST == "localhost"
if not _loopback and not PROXY_API_KEY:
    raise ValueError("PROXY_API_KEY is required when HOST is not loopback")
_REFRESH_LOCK = threading.Lock()


def api_error(message, status, error_type="api_error"):
    return {"type": "error", "error": {"type": error_type, "message": message}}, status


@app.before_request
def authenticate():
    if PROXY_API_KEY:
        supplied = request.headers.get("x-api-key", "")
        if not supplied:
            auth = request.headers.get("Authorization", "")
            supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), PROXY_API_KEY.encode()):
            return api_error("Invalid proxy API key", 401, "authentication_error")


@app.errorhandler(413)
def request_too_large(error):
    return api_error("Request body exceeds 32 MiB", 413, "invalid_request_error")


def dump_debug(data):
    # Unique files with owner-only permissions; concurrent requests cannot overwrite them.
    with tempfile.NamedTemporaryFile(prefix="claude-proxy-", suffix=".json", delete=False) as f:
        f.write(data)


CLAUDE_DIR = os.path.expanduser("~/.claude")
CREDENTIALS_FILE = os.path.join(CLAUDE_DIR, ".credentials.json")

UPSTREAM = "https://api.anthropic.com"
CCH_SEED = 0x6E52736AC806831E

# CC 版本自动检测
def detect_cc_version():
    import subprocess
    import re
    try:
        out = subprocess.check_output(["claude", "--version"], timeout=5, text=True).strip()
        m = re.search(r'(\d+\.\d+\.\d+)', out)
        main_ver = m.group(1) if m else "2.1.92"
    except:
        main_ver = "2.1.92"

    build_cache = os.path.join(os.path.dirname(__file__), ".cc_build")
    build_num = "190"
    if os.path.exists(build_cache):
        with open(build_cache) as f:
            cached = f.read().strip()
            if cached:
                build_num = cached

    return main_ver, build_num

CC_VERSION, CC_BUILD = detect_cc_version()
CC_VERSION = os.environ.get("CC_VERSION", CC_VERSION)
CC_BUILD = os.environ.get("CC_BUILD", CC_BUILD)
CC_FULL_VERSION = f"{CC_VERSION}.{CC_BUILD}"

# ============================================================
# Token 管理
# ============================================================

def load_credentials():
    with open(CREDENTIALS_FILE) as f:
        data = json.load(f)
    cred = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if (not isinstance(cred, dict) or not isinstance(cred.get("expiresAt"), (int, float))
            or not isinstance(cred.get("accessToken"), str) or not cred["accessToken"]):
        raise ValueError("Invalid OAuth credential format")
    return cred

class CredentialError(Exception):
    pass


def get_access_token():
    # Re-read inside the lock so concurrent requests share one refresh.
    with _REFRESH_LOCK:
        try:
            cred = load_credentials()
            if time.time() * 1000 > cred.get("expiresAt", 0) - 300_000:
                subprocess.run(
                    ["claude", "--print", "ping"], check=True,
                    timeout=REFRESH_TIMEOUT, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cred = load_credentials()
            if cred.get("expiresAt", 0) <= time.time() * 1000 or not cred.get("accessToken"):
                raise CredentialError("OAuth credentials are expired or missing")
            return cred["accessToken"]
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            raise CredentialError("Unable to load or refresh OAuth credentials") from exc

# ============================================================
# cch 签名计算
# ============================================================

def compute_cch(body_bytes: bytes) -> str:
    h = xxhash.xxh64(body_bytes, seed=CCH_SEED).intdigest()
    return f"{h & 0xFFFFF:05x}"

# 工具名称配置；仅映射名称，不删除或注入客户端未声明的工具。
_MAPPING_FILE = os.path.join(os.path.dirname(__file__), "tool_name_mapping.json")
with open(_MAPPING_FILE) as _f:
    _mapping = json.load(_f)
OC_TO_CC = {**_mapping["direct"], **_mapping["borrowed"]}


def replace_tools(body: dict) -> dict:
    """Map definitions, history and tool_choice consistently, per request."""
    reverse = {}

    def register(name):
        if not isinstance(name, str) or not name:
            raise ValueError("Tool names must be non-empty strings")
        mapped = OC_TO_CC.get(name, name)
        if mapped in reverse and reverse[mapped] != name:
            raise ValueError(f"Tool name collision: {name} and {reverse[mapped]}")
        reverse[mapped] = name
        return mapped

    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Each tool must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or name in seen:
            raise ValueError("Tool names must be strings and unique")
        seen.add(name)
        tool["name"] = register(name)

    for msg in body["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("Message content blocks must be objects")
                if block.get("type") == "tool_use":
                    block["name"] = register(block.get("name"))

    choice = body.get("tool_choice")
    if choice is not None:
        if not isinstance(choice, dict):
            raise ValueError("tool_choice must be an object")
        if choice.get("type") == "tool":
            name = choice.get("name")
            if not isinstance(name, str) or name not in seen:
                raise ValueError("tool_choice must reference a declared tool")
            choice["name"] = register(name)
    # Do not remove client tools or inject tools the client cannot execute.
    return reverse


def remap_response(data, reverse):
    """Only modify protocol tool-use blocks, never arbitrary tool input or text."""
    if not isinstance(data, dict):
        return data
    blocks = data.get("content", [])
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["name"] = reverse.get(block.get("name"), block.get("name"))
    block = data.get("content_block")
    if isinstance(block, dict) and block.get("type") == "tool_use":
        block["name"] = reverse.get(block.get("name"), block.get("name"))
    return data


def stream_events(resp, reverse):
    # Buffer bytes through complete SSE lines/events before UTF-8 decoding.
    pending = b""
    lines = []

    def encode_event(event_lines):
        data_lines = [line[5:].lstrip(b" ") for line in event_lines if line.startswith(b"data:")]
        if not data_lines:
            return b"\n".join(event_lines) + b"\n\n"
        try:
            data = json.loads(b"\n".join(data_lines))
        except (ValueError, UnicodeError):
            return b"\n".join(event_lines) + b"\n\n"
        mapped = json.dumps(remap_response(data, reverse), ensure_ascii=False).encode("utf-8")
        other = [line for line in event_lines if not line.startswith(b"data:")]
        return b"\n".join(other + [b"data: " + mapped]) + b"\n\n"

    try:
        for chunk in resp.iter_content(chunk_size=4096):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line:
                    lines.append(line)
                else:
                    yield encode_event(lines)
                    lines = []
        if pending or lines:
            # An incomplete event cannot safely be delivered as a successful response.
            raise requests.ConnectionError("Truncated SSE event")
    except requests.RequestException:
        data = {"type": "error", "error": {"type": "api_error", "message": "Upstream stream interrupted"}}
        yield ("event: error\ndata: " + json.dumps(data) + "\n\n").encode()
    finally:
        resp.close()


def inject_system_and_cch(body: dict) -> bytes:
    """注入 Claude Code 的 system prompts + 计算 cch 签名

    核心策略：把 openclaw 的 system prompt 移到第一条 user message 里，
    system 参数只保留标准 Claude Code 格式，避免被 Anthropic 检测。
    """
    if SYSTEM_PROMPT_MODE == "preserve":
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    # 提取原始 system prompt
    original_system = body.get("system", [])
    if isinstance(original_system, str):
        original_system = [{"type": "text", "text": original_system}]

    # 把原始 system prompt 拼接成文本，移到第一条 user message
    if original_system:
        sys_texts = []
        for block in original_system:
            if isinstance(block, dict) and block.get("text"):
                sys_texts.append(block["text"])
            elif isinstance(block, str):
                sys_texts.append(block)

        if sys_texts:
            combined_sys = "\n\n".join(sys_texts)
            prefix = f"<system_instructions>\n{combined_sys}\n</system_instructions>\n\n"

            # 注入到第一条 user message 的开头
            messages = body.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        msg["content"] = prefix + content
                    elif isinstance(content, list):
                        msg["content"] = [{"type": "text", "text": prefix}] + content
                    break

    # system 只保留标准 Claude Code 格式
    billing = {
        "type": "text",
        "text": f"x-anthropic-billing-header: cc_version={CC_FULL_VERSION}; cc_entrypoint=sdk-cli; cch=00000;",
    }
    identity = {
        "type": "text",
        "text": "You are Claude Code, Anthropic's official CLI for Claude.",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }

    body["system"] = [billing, identity]

    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    body_bytes = body_str.encode("utf-8")

    cch = compute_cch(body_bytes)
    body_bytes = body_bytes.replace(b"cch=00000", f"cch={cch}".encode("utf-8"), 1)

    return body_bytes

# ============================================================
# 构造请求头
# ============================================================

def build_headers(access_token: str) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{CC_VERSION} (external, cli)",
        "X-Claude-Code-Session-Id": str(uuid.uuid4()),
        "x-app": "cli",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,context-1m-2025-08-07,context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24",
        "anthropic-version": "2023-06-01",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.80.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
    }

# ============================================================
# Flask 路由
# ============================================================

@app.route("/v1/messages", methods=["POST"])
def proxy_messages():
    try:
        raw = request.get_data()
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Request must be a JSON object")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty array")
        for index, msg in enumerate(messages):
            # Validate only what this proxy needs to process. The upstream owns
            # message role/content semantics, including new client extensions.
            if not isinstance(msg, dict):
                raise ValueError(f"messages[{index}] must be an object")
            if not isinstance(msg.get("role"), str) or not msg["role"]:
                raise ValueError(f"messages[{index}].role must be a non-empty string")
        if not isinstance(body.get("model"), str) or not body["model"]:
            raise ValueError("model must be a non-empty string")
        if not isinstance(body.get("stream", False), bool):
            raise ValueError("stream must be a boolean")
        system = body.get("system", [])
        if not isinstance(system, (str, list)) or (isinstance(system, list) and any(
            not isinstance(block, dict) or block.get("type") != "text"
            or not isinstance(block.get("text"), str) for block in system
        )):
            raise ValueError("system must be a string or an array of text blocks")
        if SYSTEM_PROMPT_MODE == "legacy" and system and not any(m["role"] == "user" for m in messages):
            raise ValueError("Legacy system migration requires a user message")
        reverse = replace_tools(body)
        body_bytes = inject_system_and_cch(body)
    except (ValueError, UnicodeError) as exc:
        return api_error(str(exc), 400, "invalid_request_error")

    try:
        access_token = get_access_token()
    except CredentialError:
        return api_error("OAuth credentials unavailable; check local Claude login", 503)
    if DEBUG:
        dump_debug(raw)
        dump_debug(body_bytes)
    headers = build_headers(access_token)
    is_stream = body.get("stream", False)
    if is_stream:
        headers["Accept"] = "text/event-stream"
    try:
        resp = requests.post(
            f"{UPSTREAM}/v1/messages?beta=true", data=body_bytes,
            headers=headers, stream=is_stream, timeout=(10, UPSTREAM_TIMEOUT),
        )
    except requests.Timeout:
        return api_error("Upstream request timed out", 504)
    except requests.RequestException:
        return api_error("Unable to connect to upstream", 502)

    if is_stream and resp.status_code < 400 and "text/event-stream" in resp.headers.get("content-type", ""):
        response = Response(
            stream_with_context(stream_events(resp, reverse)), status=resp.status_code,
            content_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        response.call_on_close(resp.close)
        return response
    try:
        content = resp.content
        try:
            content = json.dumps(remap_response(json.loads(content), reverse), ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeError):
            pass
        excluded = {"transfer-encoding", "content-encoding", "content-length", "connection",
                    "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"}
        excluded.update(h.strip().lower() for h in resp.headers.get("connection", "").split(","))
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content, status=resp.status_code, headers=resp_headers)
    except requests.Timeout:
        return api_error("Upstream response timed out", 504)
    except requests.RequestException:
        return api_error("Unable to read upstream response", 502)
    finally:
        resp.close()

@app.route("/health")
def health():
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        return {"status": "ok" if remaining > 0 else "expired", "token_hours": round(remaining, 1), "cc_version": CC_FULL_VERSION}, 200 if remaining > 0 else 503
    except Exception as e:
        return api_error("OAuth credentials unavailable", 503)

if __name__ == "__main__":
    print(f"=== Claude Max → API 代理网关 v3 ===")
    print(f"CC Version: {CC_FULL_VERSION}")
    print()
    try:
        cred = load_credentials()
        remaining = (cred.get("expiresAt", 0) / 1000 - time.time()) / 3600
        print(f"Subscription: {cred.get('subscriptionType')} ({cred.get('rateLimitTier')})")
        print(f"Token valid for: {remaining:.1f} hours")
        if remaining < 0.1:
            get_access_token()
    except FileNotFoundError:
        print("❌ Claude Code credentials not found!")
        sys.exit(1)
    print()
    print(f"🚀 http://{HOST}:{PORT}")
    if DEBUG:
        print(f"🔍 DEBUG mode ON (request dumps → /tmp/)")
    print()
    app.run(host=HOST, port=PORT, debug=False)
