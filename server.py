#!/usr/bin/env python3
"""Web/HTTP front-end for the multi-model agent."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import agent as core


UI_PATH = Path(__file__).with_name("ui.html")


def render_ui() -> bytes:
    return UI_PATH.read_bytes()


def load_token(explicit: str | None) -> tuple[str, str]:
    if explicit:
        return explicit, "命令行参数"
    path = core.AGENT_HOME / "server.token"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value, str(path)
    value = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value, str(path)


EMPTY_USAGE = {
    "requests": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cached_tokens": 0,
    "total_tokens": 0,
}


def list_downloads() -> list[dict]:
    directory = core.DOWNLOAD_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        entries = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and not path.name.startswith(".")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    files = []
    for path in entries[:1000]:
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return files


def make_title(text: str) -> str:
    line = " ".join((text or "").split())
    if not line:
        return "新对话"
    return line[:40] + ("…" if len(line) > 40 else "")


def conversation_usage(record: dict) -> dict:
    usage = record.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return {
        "requests": int(usage.get("requests") or 0),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": int(usage.get("cached_tokens") or 0),
        "total_tokens": prompt + completion,
    }


def sanitize_messages(messages: list) -> list:
    cleaned = []
    for message in messages:
        role = message.get("role")
        ts = message.get("ts") if isinstance(message.get("ts"), str) else ""
        if role == "user":
            item = {"role": "user", "content": str(message.get("content") or "")[:100000]}
            if ts:
                item["ts"] = ts
            cleaned.append(item)
        elif role == "assistant":
            item = {"role": "assistant", "content": str(message.get("content") or "")[:100000]}
            if ts:
                item["ts"] = ts
            reasoning = str(message.get("reasoning") or "").strip()
            if reasoning:
                item["reasoning"] = reasoning[:100000]
            calls = []
            for call in message.get("tool_calls") or []:
                calls.append(
                    {
                        "id": call.get("id"),
                        "name": call.get("name"),
                        "arguments": call.get("arguments") or {},
                    }
                )
            if calls:
                item["tool_calls"] = calls
            cleaned.append(item)
        elif role == "tool":
            text = str(message.get("content") or "")
            if len(text) > 8000:
                text = text[:8000] + "\n...[历史记录已截断]"
            item = {
                "role": "tool",
                "tool_call_id": message.get("tool_call_id"),
                "name": message.get("name"),
                "content": text,
            }
            if ts:
                item["ts"] = ts
            cleaned.append(item)
    return cleaned


def conversation_summary(record: dict) -> dict:
    messages = record.get("messages") or []
    preview = ""
    for message in reversed(messages):
        if message.get("role") in ("assistant", "user") and message.get("content"):
            preview = core.compact(message["content"], 80)
            break
    return {
        "id": record.get("id"),
        "title": record.get("title") or "未命名对话",
        "model": record.get("model"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "pinned": bool(record.get("pinned")),
        "message_count": sum(1 for message in messages if message.get("role") == "user"),
        "usage": conversation_usage(record),
        "preview": preview,
    }


def conversation_events(messages: list) -> list:
    events = []
    for message in messages:
        role = message.get("role")
        ts = message.get("ts") if isinstance(message.get("ts"), str) else ""
        if role == "user":
            event = {"role": "user", "text": message.get("content") or ""}
            if ts:
                event["ts"] = ts
            events.append(event)
        elif role == "assistant":
            reasoning = (message.get("reasoning") or "").strip()
            if reasoning:
                event = {"role": "assistant", "type": "reasoning", "text": reasoning}
                if ts:
                    event["ts"] = ts
                events.append(event)
            text = (message.get("content") or "").strip()
            if text:
                event = {"role": "assistant", "text": text}
                if ts:
                    event["ts"] = ts
                events.append(event)
            for call in message.get("tool_calls") or []:
                event = {
                    "role": "tool",
                    "type": "tool_call",
                    "id": call.get("id") or "",
                    "name": call.get("name") or "?",
                    "arguments": call.get("arguments") or {},
                }
                if ts:
                    event["ts"] = ts
                events.append(event)
        elif role == "tool":
            content = str(message.get("content") or "")
            event = {
                "role": "tool",
                "type": "tool_result",
                "id": message.get("tool_call_id") or "",
                "name": message.get("name") or "?",
                "summary": core.compact(content, 200),
                "output": content[:4000] + ("\n...[历史记录已截断]" if len(content) > 4000 else ""),
            }
            if ts:
                event["ts"] = ts
            events.append(event)
    return events


def display_event(event: dict) -> dict:
    """Trim oversized tool output before it goes over the wire."""
    item = dict(event)
    output = item.get("output")
    if isinstance(output, str) and len(output) > 4000:
        item["output"] = output[:4000] + "\n...[已截断]"
    return item


class ConversationStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    @staticmethod
    def valid_id(cid: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{8,32}", cid or ""))

    def path(self, cid: str) -> Path:
        return self.directory / f"{cid}.json"

    def load(self, cid: str) -> dict | None:
        if not self.valid_id(cid):
            return None
        path = self.path(cid)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or record.get("id") != cid:
            return None
        return record

    def save(self, record: dict) -> None:
        cid = record.get("id")
        if not self.valid_id(cid):
            return
        path = self.path(cid)
        temp_path = path.with_suffix(".tmp")
        with self.lock:
            temp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)

    def delete(self, cid: str) -> bool:
        if not self.valid_id(cid):
            return False
        try:
            self.path(cid).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def list(self) -> list[dict]:
        records = []
        try:
            paths = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return []
        for path in paths[:200]:
            record = self.load(path.stem)
            if not record or not record.get("messages"):
                continue
            records.append(record)
        records.sort(
            key=lambda record: (
                1 if record.get("pinned") else 0,
                str(record.get("updated_at") or record.get("created_at") or ""),
            ),
            reverse=True,
        )
        return [conversation_summary(record) for record in records]

    def set_pinned(self, cid: str, pinned: bool) -> dict | None:
        record = self.load(cid)
        if not record:
            return None
        record["pinned"] = bool(pinned)
        record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.save(record)
        return record


class SessionEntry:
    def __init__(self, agent, record: dict):
        self.agent = agent
        self.record = record
        self.lock = threading.Lock()
        self.last_used = time.time()


class SessionStore:
    def __init__(self, cfg: dict, default_model: str, auto: bool, no_tools: bool, conversations: ConversationStore):
        self.cfg = cfg
        self.default_model = default_model
        self.auto = auto
        self.no_tools = no_tools
        self.conversations = conversations
        self.entries: dict[str, SessionEntry] = {}
        self.lock = threading.Lock()

    def _new_entry(self, session_id: str, record: dict | None = None) -> SessionEntry:
        model = (record or {}).get("model")
        if model not in self.cfg["models"]:
            model = self.default_model
        agent = core.Agent(
            self.cfg,
            model,
            no_tools=self.no_tools,
            auto=self.auto,
            session_id="web-" + session_id,
        )
        if record:
            agent.messages = record.get("messages") or []
            stored = record.get("usage") or {}
            if stored:
                agent.usage.session[agent.alias] = {
                    "model": agent.model.get("model"),
                    "requests": int(stored.get("requests") or 0),
                    "prompt": int(stored.get("prompt_tokens") or 0),
                    "completion": int(stored.get("completion_tokens") or 0),
                    "cached": int(stored.get("cached_tokens") or 0),
                }
                agent.usage.requests = int(stored.get("requests") or 0)
        else:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            record = {
                "id": session_id,
                "title": "新对话",
                "created_at": now,
                "updated_at": now,
                "model": agent.alias,
                "messages": [],
                "usage": dict(EMPTY_USAGE),
            }
        return SessionEntry(agent, record)

    def get(self, session_id: str | None) -> tuple[str, SessionEntry]:
        with self.lock:
            if session_id and session_id in self.entries:
                entry = self.entries[session_id]
                entry.last_used = time.time()
                return session_id, entry

            record = self.conversations.load(session_id) if session_id else None
            if record:
                entry = self._new_entry(session_id, record)
                self.entries[session_id] = entry
                return session_id, entry

            if len(self.entries) >= 64:
                oldest = min(self.entries, key=lambda key: self.entries[key].last_used)
                self.entries.pop(oldest, None)

            new_id = uuid.uuid4().hex[:16]
            while self.conversations.load(new_id):
                new_id = uuid.uuid4().hex[:16]
            entry = self._new_entry(new_id)
            self.entries[new_id] = entry
            return new_id, entry

    def persist(self, entry: SessionEntry, user_text: str, result: dict) -> dict:
        record = entry.record
        if record.get("title") in (None, "", "新对话"):
            record["title"] = make_title(user_text)
        record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        record["model"] = entry.agent.alias
        record["messages"] = sanitize_messages(entry.agent.messages)
        usage = record.setdefault("usage", dict(EMPTY_USAGE))
        turn = result.get("turn") or {}
        usage["requests"] = int(usage.get("requests") or 0) + int(turn.get("requests") or 0)
        usage["prompt_tokens"] = int(usage.get("prompt_tokens") or 0) + int(turn.get("prompt_tokens") or 0)
        usage["completion_tokens"] = int(usage.get("completion_tokens") or 0) + int(turn.get("completion_tokens") or 0)
        usage["cached_tokens"] = int(usage.get("cached_tokens") or 0) + int(turn.get("cached_tokens") or 0)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        self.conversations.save(record)
        return record

    def reset(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self.lock:
            entry = self.entries.get(session_id)
        if not entry:
            return False
        with entry.lock:
            entry.agent.messages.clear()
            entry.record["messages"] = []
            entry.record["title"] = "新对话"
            entry.record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            entry.record["usage"] = dict(EMPTY_USAGE)
            self.conversations.save(entry.record)
        return True

    def delete(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self.lock:
            self.entries.pop(session_id, None)
        return self.conversations.delete(session_id)

    def set_pinned(self, session_id: str | None, pinned: bool) -> dict | None:
        if not session_id:
            return None
        with self.lock:
            entry = self.entries.get(session_id)
        if entry:
            with entry.lock:
                entry.record["pinned"] = bool(pinned)
                entry.record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self.conversations.save(entry.record)
                return entry.record
        return self.conversations.set_pinned(session_id, pinned)


class ApprovalBroker:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[str, dict] = {}

    def request(self, turn_id: str, name: str, arguments: dict, timeout: int = 600) -> bool:
        approval_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        item = {
            "id": approval_id,
            "turn_id": turn_id,
            "name": name,
            "arguments": arguments,
            "created_at": time.time(),
            "event": event,
            "approved": False,
        }
        now = time.time()
        with self.lock:
            for key in [key for key, value in self.pending.items() if now - value["created_at"] > timeout + 60]:
                self.pending.pop(key, None)
            self.pending[approval_id] = item
        event.wait(timeout)
        with self.lock:
            self.pending.pop(approval_id, None)
        return bool(item["approved"])

    def get(self, turn_id: str) -> dict | None:
        now = time.time()
        with self.lock:
            for item in self.pending.values():
                if item["turn_id"] == turn_id and now - item["created_at"] <= 660:
                    return {
                        "id": item["id"],
                        "name": item["name"],
                        "arguments": item["arguments"],
                        "created_at": item["created_at"],
                    }
        return None

    def decide(self, approval_id: str, approved: bool) -> bool:
        with self.lock:
            item = self.pending.get(approval_id)
            if not item:
                return False
            item["approved"] = bool(approved)
        item["event"].set()
        return True


RANGES = {
    "24h": ("近24小时", timedelta(hours=24), "hour"),
    "7d": ("近7天", timedelta(days=7), "day"),
    "30d": ("近1个月", timedelta(days=30), "day"),
    "90d": ("近3个月", timedelta(days=90), "week"),
    "all": ("全部", None, "month"),
}


def _series_entry(label: str, item: dict | None) -> dict:
    prompt = int((item or {}).get("prompt") or 0)
    completion = int((item or {}).get("completion") or 0)
    return {"label": label, "prompt": prompt, "completion": completion, "total": prompt + completion}


def build_series(range_key: str, raw: dict, now: datetime) -> list[dict]:
    if range_key == "24h":
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        series = []
        for step in range(24):
            moment = start + timedelta(hours=step)
            key = moment.strftime("%Y-%m-%d %H:00")
            series.append(_series_entry(moment.strftime("%H:00"), raw.get(key)))
        return series
    if range_key in ("7d", "30d"):
        days = 7 if range_key == "7d" else 30
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        series = []
        for step in range(days):
            moment = start + timedelta(days=step)
            key = moment.strftime("%Y-%m-%d")
            series.append(_series_entry(moment.strftime("%m-%d"), raw.get(key)))
        return series
    items = sorted(raw.items())
    if range_key == "90d":
        return [_series_entry(key[2:], value) for key, value in items]
    return [_series_entry(key, value) for key, value in items[-18:]]


def stats_payload(log_path: Path, range_key: str) -> dict:
    label, delta, bucket = RANGES.get(range_key, RANGES["24h"])
    now = datetime.now().astimezone()
    since = now - delta if delta else None
    current = core.aggregate_usage(log_path, since=since, bucket=bucket)
    all_time = core.aggregate_usage(log_path)

    totals = current["totals"]
    by_model = []
    for alias, item in current["by_model"].items():
        total = item["prompt"] + item["completion"]
        by_model.append(
            {
                "alias": alias,
                "requests": item["requests"],
                "prompt_tokens": item["prompt"],
                "completion_tokens": item["completion"],
                "cached_tokens": item["cached"],
                "total_tokens": total,
            }
        )
    by_model.sort(key=lambda item: item["total_tokens"], reverse=True)

    all_totals = all_time["totals"]
    return {
        "range": range_key,
        "range_label": label,
        "since": since.isoformat(timespec="seconds") if since else None,
        "until": now.isoformat(timespec="seconds"),
        "totals": {
            "requests": totals["requests"],
            "prompt_tokens": totals["prompt"],
            "completion_tokens": totals["completion"],
            "cached_tokens": totals["cached"],
            "total_tokens": totals["prompt"] + totals["completion"],
        },
        "all_time": {
            "requests": all_totals["requests"],
            "prompt_tokens": all_totals["prompt"],
            "completion_tokens": all_totals["completion"],
            "cached_tokens": all_totals["cached"],
            "total_tokens": all_totals["prompt"] + all_totals["completion"],
        },
        "series": build_series(range_key, current["series"], now),
        "by_model": by_model,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MultiModelAgent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, data: dict):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _stream_send(self, event: dict) -> bool:
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        try:
            self.wfile.write(b"data: " + payload + b"\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _authorized(self) -> bool:
        token = self.server.token
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:].strip(), token)
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"error": "未授权：缺少或错误的访问令牌"})
        return False

    def _send_download(self, raw_name: str):
        name = unquote(raw_name)
        if (
            not name
            or name in (".", "..")
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or any(ord(ch) < 32 for ch in name)
        ):
            self._json(404, {"error": "文件不存在"})
            return
        target = core.DOWNLOAD_DIR / name
        try:
            handle = target.open("rb")
        except OSError:
            self._json(404, {"error": "文件不存在"})
            return
        with handle:
            size = os.fstat(handle.fileno()).st_size
            ascii_name = name.encode("ascii", "ignore").decode("ascii").replace('"', "_").strip()
            ascii_base = ascii_name.rsplit(".", 1)[0] if "." in ascii_name else ascii_name
            if not re.search(r"[A-Za-z0-9]", ascii_base):
                ascii_name = "download" + target.suffix
            disposition = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(name, safe='')}"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                shutil.copyfileobj(handle, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, render_ui(), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        if not self._require_auth():
            return
        if path == "/api/models":
            self._json(200, self.server.models_payload)
            return
        if path == "/api/stats":
            query = parse_qs(urlparse(self.path).query)
            range_key = (query.get("range") or ["24h"])[0]
            if range_key not in RANGES:
                self._json(400, {"error": f"未知统计范围 '{range_key}'"})
                return
            self._json(200, stats_payload(self.server.usage_log, range_key))
            return
        if path == "/api/usage":
            query = parse_qs(urlparse(self.path).query)
            session_id = (query.get("session_id") or [""])[0]
            entry = self.server.store.entries.get(session_id) if session_id else None
            if not entry:
                record = (
                    self.server.store.conversations.load(session_id) if session_id else None
                )
                if not record:
                    self._json(404, {"error": "会话不存在"})
                    return
                payload = {"session_id": session_id, "model": record.get("model")}
                payload.update(conversation_usage(record))
                self._json(200, payload)
                return
            with entry.lock:
                totals = entry.agent.usage.session_totals()
                model = entry.agent.alias
            payload = {
                "session_id": session_id,
                "model": model,
                "requests": totals["requests"],
                "prompt_tokens": totals["prompt"],
                "completion_tokens": totals["completion"],
                "cached_tokens": totals["cached"],
                "total_tokens": totals["prompt"] + totals["completion"],
            }
            self._json(200, payload)
            return
        if path == "/api/conversations":
            self._json(200, {"conversations": self.server.store.conversations.list()})
            return
        if path.startswith("/api/conversations/"):
            cid = path[len("/api/conversations/"):].strip()
            if not ConversationStore.valid_id(cid):
                self._json(404, {"error": "对话不存在"})
                return
            entry = self.server.store.entries.get(cid)
            record = entry.record if entry else self.server.store.conversations.load(cid)
            if not record:
                self._json(404, {"error": "对话不存在"})
                return
            payload = conversation_summary(record)
            payload["events"] = conversation_events(record.get("messages") or [])
            self._json(200, payload)
            return
        if path == "/api/approvals":
            query = parse_qs(urlparse(self.path).query)
            turn_id = (query.get("turn_id") or [""])[0].strip()
            approval = self.server.approvals.get(turn_id) if turn_id else None
            self._json(200, {"approval": approval})
            return
        if path == "/api/downloads":
            self._json(
                200,
                {"dir": str(core.DOWNLOAD_DIR), "files": list_downloads()},
            )
            return
        if path.startswith("/downloads/"):
            self._send_download(path[len("/downloads/"):])
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        if path.startswith("/api/conversations/"):
            cid = path[len("/api/conversations/"):].strip()
            if not ConversationStore.valid_id(cid):
                self._json(404, {"error": "对话不存在"})
                return
            ok = self.server.store.delete(cid)
            if ok:
                self._json(200, {"ok": True, "id": cid})
            else:
                self._json(404, {"error": "对话不存在"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._require_auth():
            return
        pin_id = None
        if path.startswith("/api/conversations/") and path.endswith("/pin"):
            pin_id = path[len("/api/conversations/"):-len("/pin")].strip("/")
        approval_id = None
        if path.startswith("/api/approvals/"):
            approval_id = path[len("/api/approvals/"):].strip("/")
        if path not in ("/api/chat", "/api/reset") and not pin_id and not approval_id:
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2_000_000:
            self._json(400, {"error": "请求体为空或过大"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "请求体不是合法 JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "请求体必须是 JSON 对象"})
            return

        if approval_id:
            if not re.fullmatch(r"[0-9a-f]{8,32}", approval_id):
                self._json(404, {"error": "审批请求不存在或已过期"})
                return
            ok = self.server.approvals.decide(approval_id, bool(body.get("approved")))
            if not ok:
                self._json(404, {"error": "审批请求不存在或已过期"})
                return
            self._json(200, {"ok": True, "approved": bool(body.get("approved"))})
            return

        if path == "/api/reset":
            ok = self.server.store.reset((body.get("session_id") or "").strip() or None)
            self._json(200, {"ok": ok})
            return

        if pin_id:
            if not ConversationStore.valid_id(pin_id):
                self._json(404, {"error": "对话不存在"})
                return
            record = self.server.store.set_pinned(pin_id, bool(body.get("pinned", True)))
            if not record:
                self._json(404, {"error": "对话不存在"})
                return
            self._json(200, {"ok": True, "id": pin_id, "pinned": bool(record.get("pinned"))})
            return

        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json(400, {"error": "message 不能为空"})
            return
        if len(message) > 200_000:
            self._json(400, {"error": "message 过长"})
            return

        requested_model = (body.get("model") or "").strip()
        session_id, entry = self.server.store.get((body.get("session_id") or "").strip() or None)
        if requested_model and requested_model not in entry.agent.models:
            self._json(400, {"error": f"未知模型 '{requested_model}'"})
            return

        approval_mode = (body.get("approval_mode") or "necessary").strip()
        if approval_mode not in ("always", "necessary", "auto"):
            approval_mode = "necessary"
        thinking_value = body.get("thinking")
        thinking = bool(thinking_value) if isinstance(thinking_value, bool) else None
        turn_id = str(body.get("turn_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", turn_id):
            turn_id = uuid.uuid4().hex[:16]

        wants_stream = "text/event-stream" in (self.headers.get("Accept") or "").lower()
        events: list[dict] = []
        stream_state = {"open": wants_stream}
        if wants_stream:
            self._stream_start()

        def sink(event: dict) -> None:
            if wants_stream:
                if stream_state["open"]:
                    stream_state["open"] = self._stream_send(display_event(event))
            else:
                events.append(display_event(event))

        with entry.lock:
            if requested_model:
                entry.agent.alias = requested_model
                entry.agent.model = entry.agent._model()
            entry.agent.approval_mode = approval_mode
            entry.agent.auto_approve = approval_mode == "auto"
            entry.agent.approval_handler = lambda name, arguments: self.server.approvals.request(
                turn_id, name, arguments
            )
            entry.agent.event_sink = sink
            try:
                result = entry.agent.turn(message.strip(), thinking=thinking)
            except core.AgentError as exc:
                if wants_stream:
                    if stream_state["open"]:
                        self._stream_send({"type": "error", "error": str(exc), "session_id": session_id})
                else:
                    self._json(502, {"error": str(exc), "session_id": session_id})
                return
            except Exception as exc:
                traceback.print_exc()
                if wants_stream:
                    if stream_state["open"]:
                        self._stream_send(
                            {"type": "error", "error": f"内部错误：{exc}", "session_id": session_id}
                        )
                else:
                    self._json(500, {"error": f"内部错误：{exc}", "session_id": session_id})
                return
            finally:
                entry.agent.event_sink = None
                entry.agent.approval_handler = None
            try:
                record = self.server.store.persist(entry, message.strip(), result)
            except OSError:
                traceback.print_exc()
                record = entry.record

        payload = {
            "session_id": session_id,
            "model": result["model"],
            "answer": result["answer"],
            "tools": [] if wants_stream else events,  # already trimmed by sink
            "usage": {
                "turn": result["turn"],
                "session": result["session"],
                "context_tokens": result["context_tokens"],
                "max_context": result["max_context"],
            },
            "conversation": {
                "id": session_id,
                "title": record.get("title"),
                "usage": conversation_usage(record),
            },
        }
        if wants_stream:
            if stream_state["open"]:
                self._stream_send({"type": "done", "payload": payload})
        else:
            self._json(200, payload)


def build_models_payload(cfg: dict) -> dict:
    models = []
    for alias, model_cfg in cfg["models"].items():
        provider = cfg["providers"].get(model_cfg.get("provider"), {})
        try:
            core.resolve_api_key(provider, model_cfg.get("provider") or "?")
            available = True
            detail = ""
        except core.AgentError as exc:
            available = False
            detail = str(exc)
        models.append(
            {
                "alias": alias,
                "display": model_cfg.get("display") or model_cfg.get("model"),
                "model": model_cfg.get("model"),
                "protocol": provider.get("protocol") or "openai",
                "max_context": model_cfg.get("max_context"),
                "thinking_configurable": bool(
                    provider.get("enable_thinking_body") and provider.get("disable_thinking_body")
                ),
                "thinking_default": bool(model_cfg.get("thinking_default")),
                "available": available,
                "detail": detail,
            }
        )
    return {"default_model": cfg.get("default_model"), "models": models}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多模型 Agent 的 Web/HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("-m", "--model", help="默认模型别名")
    parser.add_argument("--auto", action="store_true", help="自动确认 shell/写文件（Web 模式通常需要）")
    parser.add_argument("--no-tools", action="store_true", help="禁用工具调用")
    parser.add_argument("--token", help="指定访问令牌；省略则读取或生成 ~/.multi-model-agent/server.token")
    parser.add_argument("--config", help="指定 models.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.config:
        core.CONFIG_PATH = core.expand(args.config)
    try:
        cfg = core.load_config()
    except core.AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    default_model = args.model or cfg.get("default_model")
    if default_model not in cfg["models"]:
        print(f"错误：未知模型 '{default_model}'。可用：{', '.join(cfg['models'])}", file=sys.stderr)
        return 2

    token, token_source = load_token(args.token)
    core.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conversations = ConversationStore(core.AGENT_HOME / "conversations")
    store = SessionStore(cfg, default_model, args.auto, args.no_tools, conversations)
    models_payload = build_models_payload(cfg)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.store = store
    httpd.approvals = ApprovalBroker()
    httpd.token = token
    httpd.models_payload = models_payload
    httpd.usage_log = core.expand(cfg.get("usage_log") or (core.AGENT_HOME / "usage.jsonl"))

    print(f"多模型 Agent 服务已启动 · 默认模型 {default_model}", flush=True)
    print(f"令牌来源：{token_source}", flush=True)
    print(f"本机访问：http://localhost:{args.port}/?token={token}", flush=True)
    print(f"局域网访问：请使用本机地址的 {args.port} 端口（不在此处显示具体 IP）", flush=True)
    if not args.auto:
        print("提示：Web 端是否确认由页面「操作权限」决定；--auto 主要影响命令行模式。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
