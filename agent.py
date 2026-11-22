#!/usr/bin/env python3
"""Multi-model agent: one conversation, switchable models, built-in token accounting."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AGENT_CONFIG") or (SCRIPT_DIR / "models.json"))
AGENT_HOME = Path(os.environ.get("AGENT_HOME") or (Path.home() / ".multi-model-agent"))
DOWNLOAD_DIR = Path(os.environ.get("AGENT_DOWNLOAD_DIR") or (AGENT_HOME / "downloads"))

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_TOOL_OUTPUT = 20000

DEFAULT_SYSTEM_PROMPT = (
    "你是一个可以直接操作本机环境的通用智能体。"
    "可用工具：run_shell（执行 shell 命令）、read_file（读文件）、write_file（写文件）、list_dir（列目录）、"
    "publish_download（把文件复制到 WebUI 下载目录）、get_token_usage（查询本次会话和历史 Token 用量）。"
    "用户要求把某个文件加入下载路径、提供下载或分享文件时，调用 publish_download，不要只给出路径。"
    "需要事实时先用工具核实，不要凭空猜测文件内容或命令结果。"
    "用户询问 Token 消耗时，调用 get_token_usage 获取真实数据再回答。"
    "执行破坏性、不可逆或影响面较大的操作前，先说明原因。"
    "当用户只是在询问你是否具备某种能力（例如“能否生成小说”“可以写文章吗”“会不会写代码”）时，"
    "这属于确认性提问：先用一两句话回答可以或不可以，并询问主题、篇幅、风格、用途等必要信息，"
    "在用户明确确认前不要输出正文、示例、提纲或代码。"
    "对于可能产生较长内容的请求（小说、文章、报告、代码等），如果缺少主题、篇幅、风格等关键信息，"
    "先简短确认再生成，不要自行编造参数后直接输出。"
    "最终回答使用用户使用的语言，简洁、准确，给出关键结论。"
)

TOOL_DEFS = [
    {
        "name": "run_shell",
        "description": "在用户机器上执行 shell 命令，返回合并后的 stdout/stderr 与退出码。命令在当前工作目录执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 1200"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取一个文本文件的内容。默认最多读取 200000 字节。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，支持 ~"},
                "max_bytes": {"type": "integer", "description": "最多读取的字节数"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "把文本内容写入文件（覆盖或新建），自动创建父目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径，支持 ~"},
                "content": {"type": "string", "description": "要写入的完整内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "列出目录下的文件和子目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认为当前目录"},
            },
        },
    },
    {
        "name": "publish_download",
        "description": (
            "把本机上的一个文件复制到 Agent 的 WebUI 下载目录，用户可在页面顶部的“文件下载”中点击下载。"
            "当用户要求把文件加入下载路径、提供下载或分享文件时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要提供下载的源文件路径，支持 ~"},
                "name": {"type": "string", "description": "下载目录中显示的文件名，默认为源文件名"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_token_usage",
        "description": "查询当前会话与历史累计的 Token 用量，包括输入、输出、缓存命中和按模型细分。",
        "parameters": {"type": "object", "properties": {}},
    },
]

APPROVAL_TOOLS = {"run_shell", "write_file"}
FILE_TOOLS = {"run_shell", "write_file", "read_file", "list_dir", "publish_download"}
DESTRUCTIVE_RE = re.compile(
    r"(^|[;&|()\s])(rm|rmdir|unlink|shred)\s|find\b[^\n]*\s-delete\b|truncate\b",
    re.IGNORECASE,
)

LONG_CONTENT_RE = re.compile(
    r"(小说|故事|文章|文案|剧本|诗歌|诗|作文|论文|报告|方案|计划|邮件|演讲稿|歌词|段子|长文|代码|程序)"
)
GENERATION_VERB_RE = re.compile(r"(生成|写|创作|编写|撰写|做|画)")
INQUIRY_HINT_RE = re.compile(r"吗|？|\?|能否|能不能|可不可以|是否可以|是否|会不会|行不行|可以吗")
LONG_CONTENT_EN_RE = re.compile(
    r"\b(novel|story|article|essay|report|poem|code|screenplay|script|lyrics)\b", re.IGNORECASE
)
INQUIRY_EN_RE = re.compile(
    r"\b(can|could|are you able to|do you know how to)\b.{0,40}\b(write|generate|create|produce|make)\b",
    re.IGNORECASE,
)
CLARIFY_CONSTRAINT = (
    "用户当前只是在询问能力或可行性，并没有确认开始。"
    "请只做简短确认（不超过 60 字），并询问必要参数（主题或功能、篇幅或规模、风格或用途、是否保存为文件）。"
    "禁止输出任何正文、示例、提纲、代码或长篇内容。"
)
CLARIFY_REPLY = (
    "可以，不过为了避免浪费 Token，开始前请先确认几点：\n"
    "1. 主题或功能\n"
    "2. 篇幅或规模\n"
    "3. 风格或用途\n"
    "4. 是否需要保存为文件\n\n"
    "你回复具体要求或「开始」后，我再生成内容。"
)


def is_generation_inquiry(text: str) -> bool:
    text = (text or "").strip()
    if not text or len(text) > 300:
        return False
    if not LONG_CONTENT_RE.search(text) and not LONG_CONTENT_EN_RE.search(text):
        return False
    if LONG_CONTENT_EN_RE.search(text) and INQUIRY_EN_RE.search(text):
        return True
    if not GENERATION_VERB_RE.search(text):
        return False
    return bool(INQUIRY_HINT_RE.search(text))


class AgentError(Exception):
    pass


class HTTPStatusError(AgentError):
    def __init__(self, code: int, body: str):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:500]}")


def default_config() -> dict:
    return {
        "default_model": "deepseek",
        "auto_approve": False,
        "max_tool_steps": 20,
        "shell_timeout": 120,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "usage_log": str(AGENT_HOME / "usage.jsonl"),
        "providers": {
            "kimi": {
                "protocol": "anthropic",
                "base_url": "https://api.kimi.com/coding/v1",
                "auth_style": "both",
                "api_key_env": "KIMI_API_KEY",
                "api_key_from": [
                    {
                        "file": "~/.kimi-code/credentials/kimi-code.json",
                        "json_paths": [["access_token"], ["tokens", "access_token"]],
                    },
                    {
                        "file": "~/.kimi-code/config.toml",
                        "toml_path": ["providers", "managed:kimi-code", "api_key"],
                    },
                ],
            },
            "deepseek": {
                "protocol": "openai",
                "base_url": "http://localhost:8030/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
                "api_key_from": {
                    "file": "~/.kimi-code/config.toml",
                    "toml_path": ["providers", "local-deepseek", "api_key"],
                },
            },
        },
        "models": {
            "deepseek": {
                "provider": "deepseek",
                "model": "DeepSeek-V4.1-Flash-1017",
                "display": "DeepSeek V4.1 Flash (local)",
                "max_context": 262144,
                "max_tokens": 8192,
            },
            "kimi": {
                "provider": "kimi",
                "model": "kimi-for-coding",
                "display": "Kimi K2.8 Preview",
                "max_context": 1048576,
                "max_tokens": 8192,
            },
            "kimi-fast": {
                "provider": "kimi",
                "model": "kimi-for-coding-highspeed",
                "display": "Kimi K2.7 Code Highspeed",
                "max_context": 262144,
                "max_tokens": 8192,
            },
            "k3": {
                "provider": "kimi",
                "model": "k3",
                "display": "Kimi K3",
                "max_context": 262144,
                "max_tokens": 8192,
            },
            "k3-256k": {
                "provider": "kimi",
                "model": "k3-256k",
                "display": "Kimi K3 256k",
                "max_context": 262144,
                "max_tokens": 8192,
            },
            "k3.1-pro": {
                "provider": "kimi",
                "model": "k3.1-pro-highspeed",
                "display": "Kimi K3.1 Pro Highspeed",
                "max_context": 1048576,
                "max_tokens": 8192,
            },
        },
    }


def merge_config(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_config(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(default_config(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"已生成配置文件：{CONFIG_PATH}", file=sys.stderr)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentError(f"读取配置文件失败 {CONFIG_PATH}: {exc}") from None
    local_path = Path(
        os.environ.get("AGENT_LOCAL_CONFIG") or (CONFIG_PATH.parent / "models.local.json")
    )
    if local_path.exists():
        try:
            local_cfg = json.loads(local_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AgentError(f"读取本地覆盖配置失败 {local_path}: {exc}") from None
        if not isinstance(local_cfg, dict):
            raise AgentError(f"本地覆盖配置必须是 JSON 对象：{local_path}")
        cfg = merge_config(cfg, local_cfg)
    if not cfg.get("models"):
        raise AgentError(f"配置文件 {CONFIG_PATH} 缺少 models 段")
    if not cfg.get("providers"):
        raise AgentError(f"配置文件 {CONFIG_PATH} 缺少 providers 段")
    return cfg


def expand(path: str) -> Path:
    return Path(os.path.expanduser(str(path)))


def dig(data, keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def resolve_api_key(provider: dict, provider_name: str) -> tuple[str, str]:
    env_name = provider.get("api_key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip(), f"env:{env_name}"

    literal = provider.get("api_key")
    if isinstance(literal, str) and literal.strip():
        return literal.strip(), "config"

    raw_source = provider.get("api_key_from") or {}
    sources = raw_source if isinstance(raw_source, list) else [raw_source]
    for src in sources:
        file_value = src.get("file")
        if not file_value:
            continue
        path = expand(file_value)
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            suffix = path.suffix.lower()
            if suffix == ".toml":
                if tomllib is None:
                    raise AgentError("需要 Python 3.11+ 才能读取 TOML 密钥")
                data = tomllib.loads(text)
            elif suffix == ".json":
                data = json.loads(text)
            else:
                value = text.strip()
                if value:
                    return value, f"file:{path}"
                continue
            paths = src.get("toml_path") or src.get("json_paths") or src.get("json_path") or []
            if paths and isinstance(paths[0], str):
                paths = [paths]
            for keys in paths:
                value = dig(data, keys)
                if isinstance(value, str) and value.strip():
                    return value.strip(), f"{suffix.lstrip('.')}:{path}"
        except AgentError:
            raise
        except Exception:
            continue

    hint = f"环境变量 {env_name}" if env_name else "api_key 字段"
    raise AgentError(
        f"模型提供方 '{provider_name}' 没有可用的 API Key。"
        f"请设置{hint}，或在 {CONFIG_PATH} 的 providers.{provider_name} 里填写 api_key。"
    )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]", text))
    other = max(0, len(text) - cjk)
    return max(1, int(cjk * 1.1 + other / 3.6))


def compact(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp


def aggregate_usage(log_path: Path, since: datetime | None = None, bucket: str = "day") -> dict:
    totals = {"requests": 0, "prompt": 0, "completion": 0, "cached": 0}
    by_model = {}
    series = {}
    if not log_path.exists():
        return {"totals": totals, "by_model": by_model, "series": series}
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = parse_timestamp(record.get("ts"))
                if since is not None and (timestamp is None or timestamp < since):
                    continue
                prompt = int(record.get("prompt_tokens") or 0)
                completion = int(record.get("completion_tokens") or 0)
                cached = int(record.get("cached_tokens") or 0)
                totals["requests"] += 1
                totals["prompt"] += prompt
                totals["completion"] += completion
                totals["cached"] += cached

                alias = record.get("alias") or record.get("model") or "?"
                item = by_model.setdefault(
                    alias, {"requests": 0, "prompt": 0, "completion": 0, "cached": 0}
                )
                item["requests"] += 1
                item["prompt"] += prompt
                item["completion"] += completion
                item["cached"] += cached

                if timestamp is None:
                    continue
                if bucket == "hour":
                    key = timestamp.strftime("%Y-%m-%d %H:00")
                elif bucket == "week":
                    key = timestamp.strftime("%G-W%V")
                elif bucket == "month":
                    key = timestamp.strftime("%Y-%m")
                else:
                    key = timestamp.strftime("%Y-%m-%d")
                entry = series.setdefault(key, {"prompt": 0, "completion": 0})
                entry["prompt"] += prompt
                entry["completion"] += completion
    except OSError:
        pass
    return {"totals": totals, "by_model": by_model, "series": series}


def post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise AgentError(f"接口返回的不是 JSON：{raw[:300]}") from None


def call_endpoint(url: str, headers: dict, payload: dict, timeout: float, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        try:
            return post_json(url, headers, payload, timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in RETRY_STATUS and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise HTTPStatusError(exc.code, body) from None
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise AgentError(f"网络错误（{url}）：{exc.reason}") from None
        except (socket.timeout, TimeoutError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise AgentError(f"请求超时（>{timeout:g}s）：{url}") from None
    raise AgentError("请求失败")


def to_openai_messages(messages: list, system_prompt: str) -> list:
    result = [{"role": "system", "content": system_prompt}]
    for message in messages:
        role = message["role"]
        if role == "assistant":
            item = {"role": "assistant", "content": message.get("content") or None}
            calls = message.get("tool_calls") or []
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            result.append(item)
        elif role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": message["tool_call_id"],
                    "content": message.get("content") or "",
                }
            )
        else:
            result.append({"role": "user", "content": message.get("content") or ""})
    return result


def to_anthropic_messages(messages: list, provider_name: str) -> list:
    result = []
    pending_results = []

    def flush_results():
        if pending_results:
            result.append({"role": "user", "content": pending_results.copy()})
            pending_results.clear()

    for message in messages:
        role = message["role"]
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message.get("content") or "",
                }
            )
            continue
        flush_results()
        if role == "assistant":
            blocks = message.get("_anthropic_blocks")
            if blocks and message.get("_provider") == provider_name:
                result.append({"role": "assistant", "content": blocks})
                continue
            content_blocks = []
            if message.get("content"):
                content_blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments") or {},
                    }
                )
            if content_blocks:
                result.append({"role": "assistant", "content": content_blocks})
        else:
            result.append({"role": "user", "content": [{"type": "text", "text": message.get("content") or ""}]})
    flush_results()
    return result


def anthropic_headers(provider: dict, key: str) -> dict:
    style = provider.get("auth_style") or "both"
    headers = {"anthropic-version": provider.get("anthropic_version") or "2023-06-01"}
    if style in ("x-api-key", "both"):
        headers["x-api-key"] = key
    if style in ("bearer", "both"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def normalize_anthropic_usage(usage: dict) -> dict:
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    prompt = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    completion = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cache_read,
        "estimated": False,
    }


def call_anthropic(
    provider: dict,
    provider_name: str,
    model_cfg: dict,
    messages: list,
    system_prompt: str,
    tools: list,
    max_tokens: int | None,
) -> dict:
    key, key_source = resolve_api_key(provider, provider_name)
    url = provider["base_url"].rstrip("/") + "/messages"
    payload = {
        "model": model_cfg["model"],
        "max_tokens": max_tokens or model_cfg.get("max_tokens", 8192),
        "system": system_prompt,
        "messages": to_anthropic_messages(messages, provider_name),
    }
    if tools:
        payload["tools"] = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            }
            for tool in tools
        ]
    payload.update(model_cfg.get("extra_body") or {})

    styles = []
    for style in [provider.get("auth_style") or "both", "bearer", "x-api-key", "both"]:
        if style not in styles:
            styles.append(style)
    data = None
    last_error = None
    for style in styles:
        attempt_provider = dict(provider)
        attempt_provider["auth_style"] = style
        try:
            data = call_endpoint(url, anthropic_headers(attempt_provider, key), payload, provider.get("timeout", 300))
            break
        except HTTPStatusError as exc:
            last_error = exc
            if exc.code != 401:
                break
    if data is None:
        if last_error and last_error.code == 401:
            hint = provider.get("api_key_env") or "对应的环境变量"
            raise AgentError(
                f"Kimi 认证失败（401）。密钥来源：{key_source}。"
                f"如果它已过期，请重新登录 Kimi Code 或设置环境变量 {hint}。"
            )
        detail = f"HTTP {last_error.code}: {last_error.body[:600]}" if last_error else "未知错误"
        raise AgentError(f"{provider_name} API 错误：{detail}")

    text_parts = []
    reasoning_parts = []
    tool_calls = []
    for block in data.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            text_parts.append(block.get("text") or "")
        elif kind == "thinking":
            reasoning_parts.append(block.get("thinking") or "")
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                    "name": block.get("name") or "",
                    "arguments": block.get("input") or {},
                }
            )
    usage = normalize_anthropic_usage(data.get("usage") or {})
    return {
        "text": "".join(text_parts).strip(),
        "reasoning": "".join(reasoning_parts).strip(),
        "tool_calls": tool_calls,
        "usage": usage,
        "blocks": data.get("content") or [],
        "finish_reason": data.get("stop_reason"),
    }


def call_openai(
    provider: dict,
    provider_name: str,
    model_cfg: dict,
    messages: list,
    system_prompt: str,
    tools: list,
    max_tokens: int | None,
    extra_body: dict | None = None,
) -> dict:
    key, _ = resolve_api_key(provider, provider_name)
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": model_cfg["model"],
        "messages": to_openai_messages(messages, system_prompt),
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    elif model_cfg.get("max_tokens"):
        payload["max_tokens"] = model_cfg["max_tokens"]
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]
    payload.update(model_cfg.get("extra_body") or {})
    if extra_body:
        payload.update(extra_body)

    try:
        data = call_endpoint(url, headers, payload, provider.get("timeout", 300))
    except HTTPStatusError as exc:
        if exc.code == 401:
            hint = provider.get("api_key_env") or "对应的环境变量"
            raise AgentError(f"{provider_name} 认证失败（401）。请检查 {hint} 或配置文件中的密钥。") from None
        raise AgentError(f"{provider_name} API 错误 HTTP {exc.code}: {exc.body[:600]}") from None

    choices = data.get("choices") or []
    if not choices:
        raise AgentError(f"{provider_name} 返回了空 choices：{compact(data, 300)}")
    message = choices[0].get("message") or {}
    tool_calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            arguments = {"__raw_arguments__": raw_args}
        tool_calls.append(
            {
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "name": function.get("name") or "",
                "arguments": arguments,
            }
        )

    usage = data.get("usage") or {}
    if usage:
        details = usage.get("prompt_tokens_details") or {}
        normalized = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(details.get("cached_tokens") or 0),
            "estimated": False,
        }
    else:
        normalized = None
    return {
        "text": (message.get("content") or "").strip(),
        "reasoning": (message.get("reasoning_content") or message.get("reasoning") or "").strip(),
        "tool_calls": tool_calls,
        "usage": normalized,
        "blocks": None,
        "finish_reason": choices[0].get("finish_reason"),
    }


class UsageTracker:
    def __init__(self, log_path: Path, session_id: str):
        self.log_path = log_path
        self.session_id = session_id
        self.session = {}
        self.last_prompt = 0
        self.requests = 0

    def record(self, alias: str, model_id: str, usage: dict) -> dict:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cached = int(usage.get("cached_tokens") or 0)
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session": self.session_id,
            "alias": alias,
            "model": model_id,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": cached,
            "total_tokens": prompt + completion,
            "estimated": bool(usage.get("estimated")),
        }
        item = self.session.setdefault(
            alias,
            {"model": model_id, "requests": 0, "prompt": 0, "completion": 0, "cached": 0},
        )
        item["requests"] += 1
        item["prompt"] += prompt
        item["completion"] += completion
        item["cached"] += cached
        self.requests += 1
        self.last_prompt = prompt
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"提示：Token 日志写入失败（{exc}），本次统计仅在内存中有效。", file=sys.stderr)
        return record

    def session_totals(self) -> dict:
        return {
            "requests": sum(item["requests"] for item in self.session.values()),
            "prompt": sum(item["prompt"] for item in self.session.values()),
            "completion": sum(item["completion"] for item in self.session.values()),
            "cached": sum(item["cached"] for item in self.session.values()),
        }

    def all_time(self) -> dict:
        totals = {"requests": 0, "prompt": 0, "completion": 0, "cached": 0}
        by_model = {}
        days = {}
        if not self.log_path.exists():
            return {"totals": totals, "by_model": by_model, "days": days}
        try:
            with self.log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    prompt = int(record.get("prompt_tokens") or 0)
                    completion = int(record.get("completion_tokens") or 0)
                    cached = int(record.get("cached_tokens") or 0)
                    totals["requests"] += 1
                    totals["prompt"] += prompt
                    totals["completion"] += completion
                    totals["cached"] += cached
                    alias = record.get("alias") or record.get("model") or "?"
                    item = by_model.setdefault(alias, {"requests": 0, "prompt": 0, "completion": 0, "cached": 0})
                    item["requests"] += 1
                    item["prompt"] += prompt
                    item["completion"] += completion
                    item["cached"] += cached
                    day = str(record.get("ts") or "")[:10]
                    if day:
                        day_item = days.setdefault(day, {"requests": 0, "prompt": 0, "completion": 0})
                        day_item["requests"] += 1
                        day_item["prompt"] += prompt
                        day_item["completion"] += completion
        except OSError:
            pass
        return {"totals": totals, "by_model": by_model, "days": days}


def print_usage_table(tracker: UsageTracker) -> None:
    session = tracker.session
    totals = tracker.session_totals()
    print("\n本次会话 Token 用量")
    if not session:
        print("  （还没有调用过模型）")
    else:
        print(f"  {'模型':<14}{'请求':>6}{'输入':>12}{'输出':>12}{'缓存命中':>12}")
        for alias, item in session.items():
            print(
                f"  {alias:<14}{item['requests']:>6}{item['prompt']:>12,}{item['completion']:>12,}{item['cached']:>12,}"
            )
        print(
            f"  {'合计':<14}{totals['requests']:>6}{totals['prompt']:>12,}{totals['completion']:>12,}{totals['cached']:>12,}"
        )
        print(f"  合计 token：{totals['prompt'] + totals['completion']:,}（输入 {totals['prompt']:,} + 输出 {totals['completion']:,}）")

    all_time = tracker.all_time()
    grand = all_time["totals"]
    print("\n历史累计（跨会话）")
    if grand["requests"]:
        print(f"  请求 {grand['requests']:,} 次 · 输入 {grand['prompt']:,} · 输出 {grand['completion']:,} · 缓存命中 {grand['cached']:,}")
        print(f"  总 token：{grand['prompt'] + grand['completion']:,}")
        print("  按模型：")
        for alias, item in sorted(all_time["by_model"].items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"])):
            print(
                f"    {alias:<14}{item['requests']:>6} 次  {item['prompt'] + item['completion']:>12,} token"
            )
    else:
        print("  （暂无记录）")
    print(f"  明细日志：{tracker.log_path}")
    print()


class Agent:
    def __init__(
        self,
        cfg: dict,
        alias: str,
        no_tools: bool = False,
        auto: bool = False,
        verbose: bool = False,
        event_sink=None,
        session_id: str | None = None,
    ):
        self.cfg = cfg
        self.models = cfg["models"]
        self.providers = cfg["providers"]
        if alias not in self.models:
            raise AgentError(f"未知模型别名 '{alias}'。可用：{', '.join(self.models)}")
        self.alias = alias
        self.messages = []
        self.tools_enabled = not no_tools
        self.auto_approve = bool(cfg.get("auto_approve")) or auto
        self.approval_mode = "auto" if self.auto_approve else "standard"
        self.approval_handler = None
        self.thinking = None
        self.verbose = verbose
        self.event_sink = event_sink
        self.system_prompt = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        self.turn_constraint = ""
        log_path = expand(cfg.get("usage_log") or (AGENT_HOME / "usage.jsonl"))
        session_id = session_id or (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        self.usage = UsageTracker(log_path, session_id)
        self._key_cache = {}
        self.model = self._model()

    def _model(self) -> dict:
        return self.models[self.alias]

    def _provider(self) -> tuple[str, dict]:
        model_cfg = self._model()
        name = model_cfg["provider"]
        if name not in self.providers:
            raise AgentError(f"模型 {self.alias} 引用了未定义的 provider '{name}'")
        return name, self.providers[name]

    def key_status(self, alias: str) -> str:
        model_cfg = self.models.get(alias) or {}
        name = model_cfg.get("provider")
        provider = self.providers.get(name)
        if not provider:
            return "provider 未定义"
        try:
            _, source = resolve_api_key(provider, name)
            return f"✓ {source}"
        except AgentError as exc:
            return f"✗ {exc}"

    def switch(self, alias: str) -> None:
        if alias not in self.models:
            print(f"未知模型 '{alias}'。可用：{', '.join(self.models)}")
            return
        self.alias = alias
        self.model = self._model()
        print(f"已切换到 {alias} · {self.model.get('display') or self.model['model']}")

    def _call_model(
        self,
        messages: list,
        with_tools: bool,
        max_tokens: int | None = None,
        alias: str | None = None,
        force_no_thinking: bool = False,
        thinking: bool | None = None,
    ) -> dict:
        target_alias = alias or self.alias
        model_cfg = self.models[target_alias]
        provider_name, provider = (
            model_cfg["provider"],
            self.providers[model_cfg["provider"]],
        )
        protocol = (provider.get("protocol") or "openai").lower()
        tools = TOOL_DEFS if (with_tools and self.tools_enabled) else []
        system_prompt = self.system_prompt
        if self.turn_constraint:
            system_prompt = system_prompt + "\n\n【本轮约束】" + self.turn_constraint
        thinking_enabled = bool(model_cfg.get("thinking_default")) if thinking is None else bool(thinking)
        extra_body = None
        if force_no_thinking:
            extra_body = provider.get("disable_thinking_body")
        elif provider.get("enable_thinking_body") or provider.get("disable_thinking_body"):
            key = "enable_thinking_body" if thinking_enabled else "disable_thinking_body"
            extra_body = provider.get(key)

        configured_max = int(max_tokens or model_cfg.get("max_tokens") or 8192)
        max_context = int(model_cfg.get("max_context") or 0)
        if model_cfg.get("max_tokens_from_context") and max_context:
            prompt_parts = [system_prompt]
            for message in messages:
                prompt_parts.append(str(message.get("content") or ""))
                if message.get("tool_calls"):
                    prompt_parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
            if tools:
                prompt_parts.append(json.dumps(tools, ensure_ascii=False))
            prompt_estimate = estimate_tokens("\n".join(prompt_parts))
            configured_max = min(configured_max, max(1024, max_context - prompt_estimate - 8192))
        if thinking_enabled and not force_no_thinking:
            configured_max = min(configured_max, int(model_cfg.get("thinking_max_tokens") or configured_max))

        if protocol == "anthropic":
            reply = call_anthropic(
                provider, provider_name, model_cfg, messages, system_prompt, tools, configured_max
            )
        elif protocol == "openai":
            reply = call_openai(
                provider,
                provider_name,
                model_cfg,
                messages,
                system_prompt,
                tools,
                configured_max,
                extra_body=extra_body,
            )
        else:
            raise AgentError(f"未知 protocol '{protocol}'")

        usage = reply.get("usage")
        estimated = False
        if not usage:
            estimated = True
            prompt_text = [system_prompt]
            for message in messages:
                prompt_text.append(str(message.get("content") or ""))
                if message.get("tool_calls"):
                    prompt_text.append(json.dumps(message["tool_calls"], ensure_ascii=False))
            prompt_estimate = estimate_tokens("\n".join(prompt_text))
            usage = {
                "prompt_tokens": prompt_estimate,
                "completion_tokens": estimate_tokens(reply.get("text") or ""),
                "cached_tokens": 0,
                "estimated": True,
            }
        reply["usage"] = usage
        record = self.usage.record(target_alias, model_cfg["model"], usage)
        record["requests"] = 1
        reply["record"] = record

        can_retry = (
            not force_no_thinking
            and not (reply.get("text") or "").strip()
            and not reply["tool_calls"]
            and bool(provider.get("disable_thinking_body"))
        )
        if not can_retry:
            return reply

        print("[提示] 模型返回空内容，关闭思考模式后重试一次…", file=sys.stderr)
        retry = self._call_model(
            messages,
            with_tools=with_tools,
            max_tokens=max_tokens,
            alias=target_alias,
            force_no_thinking=True,
        )
        retry_record = retry["record"]
        combined = {
            "requests": int(record.get("requests") or 1) + int(retry_record.get("requests") or 1),
            "prompt_tokens": int(record.get("prompt_tokens") or 0) + int(retry_record.get("prompt_tokens") or 0),
            "completion_tokens": int(record.get("completion_tokens") or 0)
            + int(retry_record.get("completion_tokens") or 0),
            "cached_tokens": int(record.get("cached_tokens") or 0) + int(retry_record.get("cached_tokens") or 0),
            "estimated": bool(record.get("estimated")) or bool(retry_record.get("estimated")),
        }
        combined["total_tokens"] = combined["prompt_tokens"] + combined["completion_tokens"]
        retry["record"] = combined
        return retry

    def _needs_approval(self, name: str, arguments: dict) -> bool:
        mode = self.approval_mode
        if mode == "auto" or self.auto_approve:
            return False
        if mode == "necessary":
            if name != "run_shell":
                return False
            command = str(arguments.get("command") or "")
            return bool(DESTRUCTIVE_RE.search(command))
        if mode == "standard":
            return name in APPROVAL_TOOLS
        return name in FILE_TOOLS

    def _approve(self, name: str, arguments: dict) -> bool:
        if name not in FILE_TOOLS:
            return True
        if not self._needs_approval(name, arguments):
            return True
        if self.approval_handler:
            try:
                return bool(self.approval_handler(name, arguments))
            except Exception:
                return False
        if not sys.stdin.isatty():
            return False
        preview = compact(json.dumps(arguments, ensure_ascii=False), 500)
        try:
            answer = input(f"\n[确认] {name} {preview}\n允许执行？[y] 是 / [n] 否 / [a] 本会话全部允许： ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "a":
            self.auto_approve = True
            self.approval_mode = "auto"
            return True
        return answer in ("y", "yes")

    def _run_shell(self, arguments: dict) -> str:
        command = str(arguments.get("command") or "").strip()
        if not command:
            return "错误：command 不能为空"
        try:
            timeout = int(arguments.get("timeout") or self.cfg.get("shell_timeout") or 120)
        except (TypeError, ValueError):
            timeout = 120
        timeout = max(1, min(timeout, 1200))
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"命令超时（>{timeout}s）：{command}"
        except OSError as exc:
            return f"命令执行失败：{exc}"
        output = ""
        if completed.stdout:
            output += completed.stdout
        if completed.stderr:
            output += ("\n" if output and not output.endswith("\n") else "") + completed.stderr
        output = output or "（无输出）"
        output += f"\n[exit code: {completed.returncode}]"
        if len(output) > MAX_TOOL_OUTPUT:
            output = output[:MAX_TOOL_OUTPUT] + f"\n...[输出已截断，共 {len(output)} 字符]"
        return output

    def _read_file(self, arguments: dict) -> str:
        path_value = arguments.get("path")
        if not path_value:
            return "错误：path 不能为空"
        path = expand(str(path_value))
        try:
            max_bytes = int(arguments.get("max_bytes") or 200000)
        except (TypeError, ValueError):
            max_bytes = 200000
        max_bytes = max(1, min(max_bytes, 2000000))
        try:
            data = path.read_bytes()
        except OSError as exc:
            return f"读取失败：{exc}"
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", "replace")
        if truncated:
            text += f"\n...[文件已截断，仅读取前 {max_bytes} 字节，总大小 {len(data)} 字节]"
        return text

    def _write_file(self, arguments: dict) -> str:
        path_value = arguments.get("path")
        if not path_value:
            return "错误：path 不能为空"
        path = expand(str(path_value))
        content = arguments.get("content")
        if content is None:
            return "错误：content 不能为空"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
        except OSError as exc:
            return f"写入失败：{exc}"
        return f"已写入 {path}（{len(str(content))} 字符）"

    def _list_dir(self, arguments: dict) -> str:
        path = expand(str(arguments.get("path") or "."))
        try:
            entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            return f"列目录失败：{exc}"
        lines = []
        for entry in entries[:500]:
            suffix = "/" if entry.is_dir() else ""
            lines.append(entry.name + suffix)
        if len(entries) > 500:
            lines.append(f"...[共 {len(entries)} 项，已截断]")
        return "\n".join(lines) or "（空目录）"

    def _publish_download(self, arguments: dict) -> str:
        path_value = arguments.get("path")
        if not path_value:
            return "错误：path 不能为空"
        source = expand(str(path_value))
        if not source.exists():
            return f"文件不存在：{source}"
        if not source.is_file():
            return f"不是普通文件：{source}"
        name = str(arguments.get("name") or source.name).strip()
        if (
            not name
            or name in (".", "..")
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or any(ord(ch) < 32 for ch in name)
        ):
            return "错误：文件名不合法，只能使用单个文件名"
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            target = DOWNLOAD_DIR / name
            shutil.copy2(source, target)
        except OSError as exc:
            return f"复制到下载目录失败：{exc}"
        size = target.stat().st_size
        return (
            f"已加入下载目录：{name}（{size} 字节）。"
            "用户可在页面顶部的“文件下载”中点击下载。"
        )

    def _token_usage(self) -> str:
        totals = self.usage.session_totals()
        all_time = self.usage.all_time()
        grand = all_time["totals"]
        data = {
            "session": {
                "requests": totals["requests"],
                "prompt_tokens": totals["prompt"],
                "completion_tokens": totals["completion"],
                "cached_tokens": totals["cached"],
                "total_tokens": totals["prompt"] + totals["completion"],
                "by_model": {
                    alias: {
                        "requests": item["requests"],
                        "prompt_tokens": item["prompt"],
                        "completion_tokens": item["completion"],
                        "cached_tokens": item["cached"],
                    }
                    for alias, item in self.usage.session.items()
                },
            },
            "all_time": {
                "requests": grand["requests"],
                "prompt_tokens": grand["prompt"],
                "completion_tokens": grand["completion"],
                "cached_tokens": grand["cached"],
                "total_tokens": grand["prompt"] + grand["completion"],
            },
            "current_model": self.alias,
            "last_request_prompt_tokens": self.usage.last_prompt,
            "usage_log": str(self.usage.log_path),
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def execute_tool(self, name: str, arguments: dict) -> str:
        if not self._approve(name, arguments):
            return "用户拒绝了该操作，未执行。请不要重试同一操作，改为询问用户。"
        if name == "run_shell":
            return self._run_shell(arguments)
        if name == "read_file":
            return self._read_file(arguments)
        if name == "write_file":
            return self._write_file(arguments)
        if name == "list_dir":
            return self._list_dir(arguments)
        if name == "publish_download":
            return self._publish_download(arguments)
        if name == "get_token_usage":
            return self._token_usage()
        return f"未知工具：{name}"

    def _emit(self, kind: str, **data) -> bool:
        if not self.event_sink:
            return False
        try:
            self.event_sink({"type": kind, **data})
        except Exception:
            pass
        return True

    def turn(self, text: str, thinking: bool | None = None) -> dict:
        self.messages.append({"role": "user", "content": text, "ts": now_iso()})
        clarify_only = is_generation_inquiry(text)
        self.turn_constraint = CLARIFY_CONSTRAINT if clarify_only else ""
        max_steps = int(self.cfg.get("max_tool_steps") or 20)
        turn_prompt = 0
        turn_completion = 0
        turn_cached = 0
        turn_requests = 0
        turn_tools = 0
        turn_estimated = False
        answer_text = ""
        for step in range(max_steps + 1):
            with_tools = self.tools_enabled and step < max_steps and not clarify_only
            reply = self._call_model(
                self.messages,
                with_tools=with_tools,
                max_tokens=900 if clarify_only else None,
                thinking=thinking,
            )
            record = reply["record"]
            turn_requests += int(record.get("requests") or 1)
            turn_estimated = turn_estimated or bool(record["estimated"])
            turn_prompt += record["prompt_tokens"]
            turn_completion += record["completion_tokens"]
            turn_cached += int(record.get("cached_tokens") or 0)
            step_ts = now_iso()

            if reply.get("reasoning"):
                emitted = self._emit("reasoning", text=reply["reasoning"], ts=step_ts)
                if not emitted and self.verbose:
                    print(f"\n[思考] {compact(reply['reasoning'], 1000)}", file=sys.stderr)

            if reply["tool_calls"] and with_tools:
                assistant_message = {
                    "role": "assistant",
                    "content": reply["text"] or None,
                    "tool_calls": reply["tool_calls"],
                    "ts": step_ts,
                    "_provider": self._provider()[0],
                }
                if reply.get("reasoning"):
                    assistant_message["reasoning"] = reply["reasoning"]
                if reply.get("blocks"):
                    assistant_message["_anthropic_blocks"] = reply["blocks"]
                self.messages.append(assistant_message)
                for call in reply["tool_calls"]:
                    arguments = call.get("arguments") or {}
                    turn_tools += 1
                    if not self._emit("tool_call", name=call["name"], arguments=arguments, ts=step_ts):
                        print(f"\n→ {call['name']}({compact(arguments, 300)})", flush=True)
                    result = self.execute_tool(call["name"], arguments)
                    summary = compact(result, 200)
                    result_ts = now_iso()
                    if not self._emit(
                        "tool_result", name=call["name"], summary=summary, output=result, ts=result_ts
                    ):
                        print(f"← {summary}", flush=True)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": result,
                            "ts": result_ts,
                        }
                    )
                continue

            answer_text = reply["text"] or "模型本次没有返回可见内容，已自动重试仍未成功。请重试，或切换到其他模型。"
            if clarify_only and (not reply["text"] or len(answer_text) > 500):
                answer_text = CLARIFY_REPLY
            final_ts = now_iso()
            final_message = {
                "role": "assistant",
                "content": answer_text,
                "ts": final_ts,
                "_provider": self._provider()[0],
                "_anthropic_blocks": reply.get("blocks"),
            }
            if reply.get("reasoning"):
                final_message["reasoning"] = reply["reasoning"]
            self.messages.append(final_message)
            if not self._emit("answer", text=answer_text, ts=final_ts):
                print("\n" + answer_text)
            break

        self.turn_constraint = ""
        model_cfg = self._model()
        max_context = int(model_cfg.get("max_context") or 0)
        totals = self.usage.session_totals()
        summary = {
            "answer": answer_text,
            "model": self.alias,
            "turn": {
                "prompt_tokens": turn_prompt,
                "completion_tokens": turn_completion,
                "cached_tokens": turn_cached,
                "total_tokens": turn_prompt + turn_completion,
                "requests": turn_requests,
                "tool_calls": turn_tools,
                "estimated": turn_estimated,
            },
            "session": {
                "requests": totals["requests"],
                "prompt_tokens": totals["prompt"],
                "completion_tokens": totals["completion"],
                "cached_tokens": totals["cached"],
                "total_tokens": totals["prompt"] + totals["completion"],
            },
            "context_tokens": self.usage.last_prompt,
            "max_context": max_context,
        }
        if not self._emit("usage", **summary):
            context_part = (
                f" · 上下文 ≈ {self.usage.last_prompt:,}/{max_context:,}"
                if max_context
                else f" · 上下文 ≈ {self.usage.last_prompt:,}"
            )
            print(
                f"\n[tokens] 本轮 {turn_prompt:,} in / {turn_completion:,} out"
                f" · 会话 {totals['prompt'] + totals['completion']:,}"
                f" · 本轮请求 {turn_requests} 次{context_part}"
            )
        return summary

    def ping(self) -> None:
        print(f"逐个测试 {len(self.models)} 个模型的连通性与鉴权（每次约 256 token 上限）\n")
        for alias in list(self.models):
            model_cfg = self.models[alias]
            try:
                reply = self._call_model(
                    [{"role": "user", "content": "请只回复 OK"}],
                    with_tools=False,
                    max_tokens=256,
                    alias=alias,
                )
                text = compact(reply.get("text") or "(空回复)", 40)
                usage = reply["usage"]
                print(
                    f"✓ {alias:<12} {model_cfg['model']:<32} "
                    f"{usage['prompt_tokens']} in / {usage['completion_tokens']} out · {text}"
                )
            except Exception as exc:
                print(f"✗ {alias:<12} {model_cfg['model']:<32} {exc}")


def print_models(agent: Agent) -> None:
    print(f"\n可用模型（配置文件：{CONFIG_PATH}）\n")
    print(f"  {'别名':<12}{'模型 ID':<34}{'协议':<11}{'上下文':>10}  密钥")
    for alias, model_cfg in agent.models.items():
        provider = agent.providers.get(model_cfg.get("provider"), {})
        protocol = provider.get("protocol") or "openai"
        current = "*" if alias == agent.alias else " "
        context = model_cfg.get("max_context")
        context_text = f"{int(context):,}" if context else "-"
        print(
            f"{current} {alias:<12}{model_cfg.get('model',''):<34}{protocol:<11}{context_text:>10}  {agent.key_status(alias)}"
        )
    print("\n  用 /model <别名> 切换，或在启动时加 -m <别名>。\n")


HELP_TEXT = """
命令：
  /models            列出所有模型及密钥状态
  /model <别名>      切换模型（对话历史保留）
  /usage             查看本次会话与历史累计 Token
  /context           查看当前上下文占用
  /new               清空对话历史（Token 统计继续累计）
  /auto [on|off]     开关工具执行自动确认
  /tools [on|off]    开关工具调用
  /help              显示本帮助
  /exit              退出

直接输入内容即可对话。模型可调用 run_shell / read_file / write_file / list_dir / publish_download / get_token_usage。
""".strip()


def repl(agent: Agent) -> None:
    print("多模型 Agent · 当前模型：", end="")
    print(f"{agent.alias} · {agent.model.get('display') or agent.model['model']}")
    print(f"配置：{CONFIG_PATH}")
    print("输入 /help 查看命令，/exit 退出。\n")

    while True:
        try:
            line = input("你 › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if not line.startswith("/"):
            try:
                agent.turn(line)
            except AgentError as exc:
                print(f"\n错误：{exc}")
            except KeyboardInterrupt:
                print("\n已中断本轮。")
            continue

        command, _, argument = line.partition(" ")
        argument = argument.strip()
        command = command.lower()
        if command in ("/exit", "/quit", "/q"):
            break
        if command == "/help":
            print(HELP_TEXT)
        elif command == "/models":
            print_models(agent)
        elif command == "/model":
            if not argument:
                print(f"当前模型：{agent.alias}")
            else:
                agent.switch(argument)
        elif command == "/usage":
            print_usage_table(agent.usage)
        elif command == "/context":
            model_cfg = agent.model
            max_context = int(model_cfg.get("max_context") or 0)
            last = agent.usage.last_prompt
            if max_context:
                print(f"\n上下文：≈ {last:,} / {max_context:,} token（{last / max_context * 100:.1f}%）\n")
            else:
                print(f"\n上下文：≈ {last:,} token\n")
        elif command == "/new":
            agent.messages.clear()
            print("已清空对话历史。")
        elif command == "/auto":
            if argument in ("on", "off"):
                agent.auto_approve = argument == "on"
            print(f"工具自动确认：{'开' if agent.auto_approve else '关'}")
        elif command == "/tools":
            if argument in ("on", "off"):
                agent.tools_enabled = argument == "on"
            print(f"工具调用：{'开' if agent.tools_enabled else '关'}")
        else:
            print(f"未知命令：{command}。输入 /help 查看可用命令。")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="多模型 Agent：自由切换 Kimi / DeepSeek 等模型，并统计 Token 用量。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 agent.py                         交互模式\n"
            "  python3 agent.py -m kimi '解释这个报错'   单次调用并指定模型\n"
            "  python3 agent.py --list-models           查看模型列表\n"
            "  python3 agent.py --ping                  测试所有模型连通性\n"
            "  python3 agent.py --stats                 查看历史 Token 统计\n"
        ),
    )
    parser.add_argument("prompt", nargs="*", help="一次性执行的提示词；省略则进入交互模式")
    parser.add_argument("-m", "--model", help="本次使用的模型别名")
    parser.add_argument("--list-models", action="store_true", help="列出模型后退出")
    parser.add_argument("--ping", action="store_true", help="用最小请求测试所有模型")
    parser.add_argument("--stats", action="store_true", help="打印历史 Token 统计后退出")
    parser.add_argument("--no-tools", action="store_true", help="禁用工具调用")
    parser.add_argument("--auto", action="store_true", help="自动确认 shell/写文件操作")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示模型的思考内容（若返回）")
    parser.add_argument("--config", help="指定 models.json 路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global CONFIG_PATH
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.config:
        CONFIG_PATH = expand(args.config)

    try:
        cfg = load_config()
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.stats:
        log_path = expand(cfg.get("usage_log") or (AGENT_HOME / "usage.jsonl"))
        tracker = UsageTracker(log_path, "stats")
        all_time = tracker.all_time()
        totals = all_time["totals"]
        print(f"\n历史累计 Token（{log_path}）\n")
        if not totals["requests"]:
            print("  暂无记录")
        else:
            print(
                f"  请求 {totals['requests']:,} 次 · 输入 {totals['prompt']:,} · 输出 {totals['completion']:,}"
                f" · 缓存命中 {totals['cached']:,}"
            )
            print(f"  总 token：{totals['prompt'] + totals['completion']:,}\n")
            print(f"  {'模型':<14}{'请求':>6}{'总 token':>14}")
            for alias, item in sorted(
                all_time["by_model"].items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"])
            ):
                print(f"  {alias:<14}{item['requests']:>6}{item['prompt'] + item['completion']:>14,}")
            recent = sorted(all_time["days"].items())[-7:]
            if recent:
                print("\n  最近 7 天：")
                for day, item in recent:
                    print(
                        f"    {day}  {item['requests']:>4} 次  "
                        f"{item['prompt'] + item['completion']:>12,} token"
                    )
        print()
        return 0

    alias = args.model or cfg.get("default_model")
    if alias not in cfg["models"]:
        print(f"错误：未知模型 '{alias}'。可用：{', '.join(cfg['models'])}", file=sys.stderr)
        return 2

    agent = Agent(
        cfg,
        alias,
        no_tools=args.no_tools,
        auto=args.auto,
        verbose=args.verbose,
    )

    if args.list_models:
        print_models(agent)
        return 0

    if args.ping:
        agent.ping()
        return 0

    if args.prompt:
        try:
            agent.turn(" ".join(args.prompt))
        except AgentError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return 130
        return 0

    repl(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
