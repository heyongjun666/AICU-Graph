"""本地假厂商服务 —— 按四家厂商官方文档与真实接口的报文形状复刻其模型 API。

为什么需要它
------------
AICU 的跨厂商能力，本质是把 CLI 的 base_url 改写到「模型厂商的原生端点」
（见 ``cli_adapters.py`` 的 ``protocol_endpoints``）。要验证这条链路，最硬的办法是
真调厂商接口，但本机没有 gpt / gemini / claude / grok 的额度与 key，链路就一直没法
端到端跑。这个模块在本地把四家厂商的接口复刻出来，让 CLI 把请求打到本机 —— 于是
「AICU 的接线对不对」就能在本机验证，不用等真有额度。

假得真不真，决定这个测试有没有意义
----------------------------------
所以下面每一条都按官方来源逐字复刻，不是「能跑通就行」：

* **Anthropic Messages**：响应体与非流式示例来自 docs.anthropic.com 的 messages 页；
  SSE 事件序列来自 messages-streaming 页（``message_start → content_block_start →
  ping → content_block_delta → content_block_stop → message_delta → message_stop``，
  帧形如 ``event: <type>\\n`` + ``data: <json>\\n\\n``）；错误体来自 errors 页
  （``{"type":"error","error":{"type","message"},"request_id"}``，含 ``request-id`` 头）。
  鉴权头取自 getting-started 页：``Authorization: Bearer`` 为主，``x-api-key`` 是仍受
  支持的兼容写法，``anthropic-version: 2023-06-01`` 必填。
* **OpenAI Chat / Responses**：字段取自官方 OpenAPI 规范
  （``raw.githubusercontent.com/openai/openai-openapi``），含 Responses 的完整流式事件
  序列与 Chat 的 ``data: [DONE]`` 终止符。错误体与 401 文案是本机对 ``api.openai.com``
  的实测（见 ``_openai_error`` 的注释），不是照抄规范。
* **Google Gemini 原生 generateContent**：schema 取自官方 Discovery 文档
  （``generativelanguage.googleapis.com/$discovery/rest?version=v1beta``）。错误体、
  鉴权（``?key=`` 与 ``x-goog-api-key`` 都收）、SSE 帧形态（只有
  ``data: {json}\\r\\n\\r\\n``，**没有事件名也没有 [DONE]**）都是本机对真实接口实测的。
* **xAI Grok**：Responses 报文取自 docs.x.ai。它与 OpenAI 有一处真实差异 ——
  流式是 **data-only 的 SSE 且以 ``data: [DONE]`` 收尾**（官方 stream 参数原文），
  而 OpenAI 的 Responses 带 ``event:`` 名且没有 [DONE]；响应 id 也是裸 UUID、
  ``usage`` 多出 ``num_sources_used`` / ``cost_in_usd_ticks`` 等字段。

除了照着答，它还做两件让测试真正有信息量的事：

1. **按厂商要求校验请求**：缺必填字段、鉴权头不对，就按该厂商的真实错误体报错，而不是
   宽容放过 —— 宽容的假服务会把 AICU 的接线错误掩盖掉（本项目就吃过这个亏：kimi 把
   上下文窗口当 max_tokens 发出去，直到真厂商回 400 才暴露）。
2. **记录每条请求**：路径、协议、模型、key 落在哪个头（``x-api-key`` 还是
   ``Authorization``）、关键参数。跑完可以直接回答「CLI 到底发了什么」。

用法
----
::

    # 单独起一个假厂商（四家共用这一个端口，按路径 + 模型名分辨）
    python -m tests.mock_providers --port 8799
    # 把请求账本写到文件，跑完直接看
    python -m tests.mock_providers --port 8799 --log /tmp/mock.jsonl

在测试或脚本里用 ``running_mock()`` 上下文管理器，自动挑空闲端口、自动收尾::

    with running_mock() as mock:
        ...  # mock.base_url 形如 http://127.0.0.1:52341
        assert mock.provider.requests_for(protocol="messages")

把 CLI 指过来（``base_url`` 各 CLI 拼接规则不同，这里是它们各自要的取值）::

    claude  ANTHROPIC_BASE_URL=<base_url>              # CLI 自己拼 /v1/messages
    codex   ~/.codex/config.toml 里 base_url=<base_url>/v1   # 拼 /responses
    kimi    KIMI_MODEL_BASE_URL=<base_url>/v1          # 拼 /chat/completions
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse


# ============================================================
# 厂商分辨与应答文本
# ============================================================

# 这个 mock 实现了四家的「口音」，其余厂商（deepseek / moonshot / aliyun …）都是
# OpenAI 兼容的，落到 openai 这套形状即可 —— 它们的差异在错误文案，不在协议结构。
_FLAVOR_BY_PREFIX = (
    ("gpt-", "openai"),
    ("chatgpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
    ("gemma-", "google"),
    ("grok-", "xai"),
)

_PROTOCOLS = ("messages", "chat", "responses", "google")


def flavor_of(model: str) -> str:
    """按模型名判「口音」（用于错误文案、id 前缀、流式帧风格）。判不出就当 openai。"""
    name = (model or "").lower()
    for prefix, flavor in _FLAVOR_BY_PREFIX:
        if name.startswith(prefix):
            return flavor
    return "openai"


def reply_text(model: str, protocol: str, flavor: str) -> str:
    """假模型的回答。

    内容固定且自证身份：跑完日志里一眼能看出「这个回答是哪家、哪条协议、哪个模型产出的」，
    于是「CLI 把请求发到了这里」和「CLI 正确解析了响应」两件事同时被证明。
    """
    return f"可用（mock:{flavor}/{protocol}/{model}）"


def _usage_counts(raw_body: bytes, text: str) -> tuple[int, int]:
    """粗略但确定的 token 计数：按字节数估，不引入 tokenizer 依赖。"""
    prompt = max(1, len(raw_body) // 4)
    completion = max(1, len(text.encode("utf-8")) // 4)
    return prompt, completion


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


# ============================================================
# 响应载体
# ============================================================


@dataclass
class _Response:
    """一条响应。流式用 ``frames``（逐帧写出去），非流式用 ``body``。"""

    status: int = 200
    headers: dict = field(default_factory=dict)
    body: Optional[bytes] = None
    frames: list = field(default_factory=list)

    @property
    def is_stream(self) -> bool:
        return bool(self.frames)


def _json_response(payload: dict, status: int = 200, headers: Optional[dict] = None) -> _Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return _Response(status=status, headers=merged, body=body)


def _sse(event: Optional[str], payload: Any, *, crlf: bool = False, data_only: bool = False) -> bytes:
    """拼一帧 SSE。

    ``data_only`` 用于 xAI Responses 与 Google：这两家的流式只有 ``data:`` 行，
    没有 ``event:`` 名（都是实测/官方原文确认的）。``crlf`` 是 Google 用的
    ``\\r\\n`` 行尾（实测如此）。
    """
    eol = "\r\n" if crlf else "\n"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    lines = [] if (data_only or not event) else [f"event: {event}"]
    lines.append(f"data: {text}")
    return (eol.join(lines) + eol + eol).encode("utf-8")


# ============================================================
# 错误体（各厂商的真实形状）
# ============================================================


def _openai_error(message: str, *, status: int, type_: str = "invalid_request_error",
                  param: Optional[str] = None, code: Optional[str] = None) -> _Response:
    """OpenAI / xAI 的错误体。

    400 与 401 两条文案是本机对 api.openai.com 的实测原文：缺 model 时
    ``{"message":"Missing required parameter: 'model'.","type":"invalid_request_error",
    "param":"model","code":"missing_required_parameter"}``；不带 key 时
    ``{"message":"Missing bearer or basic authentication in header","type":"invalid_request_error",
    "param":null,"code":null}``。规范里 ``Error`` 也确实是 ``{code,message,param,type}`` 四个字段。
    """
    return _json_response(
        {"error": {"message": message, "type": type_, "param": param, "code": code}},
        status=status,
    )


def _anthropic_error(message: str, *, status: int, type_: str) -> _Response:
    """Anthropic 错误体（errors 页逐字：顶层 type=error，error 里 type+message，
    响应体里还带 request_id，响应头带 request-id）。"""
    request_id = _new_id("req_")
    return _json_response(
        {"type": "error", "error": {"type": type_, "message": message}, "request_id": request_id},
        status=status,
        headers={"request-id": request_id},
    )


def _google_error(message: str, *, status: int, status_str: str) -> _Response:
    """Google 错误体（实测原文）：``{"error":{"code":400,"message":"…","status":"INVALID_ARGUMENT"}}``。"""
    return _json_response({"error": {"code": status, "message": message, "status": status_str}},
                          status=status)


# ============================================================
# 假厂商本体
# ============================================================


class MockProviders:
    """假厂商的行为与账本。HTTP 层只做解析与回写，协议实现都在这里。

    :param flavor: 强制指定口音（默认按模型名自动判）。测试某家的错误文案时用得上。
    :param fail_status: 非空时，接下来 ``fail_times`` 条请求都直接返回这个状态码，
        用来验证调用方对厂商报错的处理（例如 429）。``None`` 表示不注入。
    :param log_path: 请求账本落盘路径（JSONL），便于脚本跑完再翻。
    """

    def __init__(self, *, flavor: Optional[str] = None, fail_status: Optional[int] = None,
                 fail_times: int = 1, log_path: Optional[str] = None, delay: float = 0.0):
        self.forced_flavor = flavor
        self.fail_status = fail_status
        self.fail_times = fail_times
        self.log_path = Path(log_path) if log_path else None
        self.delay = delay
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    # ---------------- 账本 ----------------

    def record(self, entry: dict) -> None:
        with self._lock:
            self.requests.append(entry)
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def requests_for(self, *, protocol: Optional[str] = None, model: Optional[str] = None) -> list:
        """按协议 / 模型过滤请求账本，供断言使用。"""
        out = []
        for entry in self.requests:
            if protocol and entry.get("protocol") != protocol:
                continue
            if model and entry.get("model") != model:
                continue
            out.append(entry)
        return out

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()

    # ---------------- 分发 ----------------

    def handle(self, *, method: str, path: str, query: dict, headers: dict, raw: bytes) -> _Response:
        """按路径分发。未知路径返回 404 空体 —— 实测 api.openai.com 对伪造路径就是空体 404。"""
        if path == "/__mock__/config" and method == "POST":
            return self._configure(raw)
        if path == "/v1/models" and method == "GET":
            return self._models(headers)

        # Google 的 OpenAI 兼容层挂在 /v1beta/openai 下（AICU 表里就是这个 base_url），
        # 把它折成 /v1，后面按 OpenAI 那套处理。
        if path.startswith("/v1beta/openai/"):
            path = "/v1" + path[len("/v1beta/openai"):]

        if path == "/v1/messages" and method == "POST":
            return self._messages(headers, raw)
        if path == "/v1/chat/completions" and method == "POST":
            return self._chat(headers, raw)
        if path == "/v1/responses" and method == "POST":
            return self._responses(headers, raw)

        matched = re.match(r"^/v1beta/models/(?P<model>[^:]+):(?P<action>generateContent|streamGenerateContent)$",
                           path)
        if matched and method == "POST":
            return self._google(headers, matched.group("model"), matched.group("action"), raw)

        return self._unknown(method, path, headers, raw)

    def _unknown(self, method: str, path: str, headers: dict, raw: bytes) -> _Response:
        """未知路径：404 空体（实测 api.openai.com 对伪造路径就是空体 404）。

        照样记一条账 —— 排查「CLI 把请求打到了哪个路径」时，这条往往就是答案
        （比如某个 CLI 不接受本机端点、仍然往官方域名发，账本里会一条都没有）。
        """
        self.record({"protocol": "unknown", "model": "", "path": path, "method": method,
                     "key_header": self._key_location(headers), "body_bytes": len(raw),
                     "user_agent": headers.get("user-agent")})
        return _Response(status=404)

    def _configure(self, raw: bytes) -> _Response:
        """测试夹具用：``{"fail_status":429,"fail_times":2}`` 注入厂商报错。"""
        try:
            cfg = json.loads(raw or b"{}")
        except ValueError:
            return _json_response({"error": "bad json"}, status=400)
        if "fail_status" in cfg:
            self.fail_status = cfg["fail_status"]
        if "fail_times" in cfg:
            self.fail_times = int(cfg["fail_times"])
        return _json_response({"ok": True, "fail_status": self.fail_status,
                               "fail_times": self.fail_times})

    def _injected_failure(self) -> Optional[_Response]:
        """注入的厂商故障（429 等）。带 Retry-After，与厂商一致。"""
        if not self.fail_status or self.fail_times <= 0:
            return None
        self.fail_times -= 1
        status = self.fail_status
        if status == 429:
            return _json_response(
                {"error": {"message": "Rate limit reached", "type": "rate_limit_error",
                           "param": None, "code": "rate_limit_exceeded"}},
                status=429, headers={"retry-after": "1"})
        if status == 401:
            return _openai_error("Missing bearer or basic authentication in header", status=401)
        return _json_response(
            {"error": {"message": f"mock injected status {status}", "type": "server_error",
                       "param": None, "code": None}}, status=status)

    # ---------------- 公共 ----------------

    def _models(self, headers: dict) -> _Response:
        """GET /v1/models。

        Anthropic 与 OpenAI 都在这个路径上，返回形状却不同：Anthropic 是
        ``{data:[{type,id,display_name,created_at}],has_more,first_id,last_id}``，
        OpenAI 是 ``{object:"list",data:[{id,object,created,owned_by}]}``。
        用请求头区分 —— 带 anthropic-version 的就是 Anthropic 客户端。
        """
        ids = ["gpt-5.6", "claude-opus-5", "gemini-3.6-flash", "grok-4.6"]
        if "anthropic-version" in headers:
            return _json_response({
                "data": [{"type": "model", "id": m, "display_name": m,
                          "created_at": "2026-01-01T00:00:00Z"} for m in ids],
                "has_more": False, "first_id": ids[0], "last_id": ids[-1],
            })
        return _json_response({
            "object": "list",
            "data": [{"id": m, "object": "model", "created": 1767225600, "owned_by": "mock"}
                     for m in ids],
        })

    def _read_json(self, raw: bytes) -> Optional[dict]:
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _flavor(self, model: str) -> str:
        return self.forced_flavor or flavor_of(model)

    @staticmethod
    def _key_location(headers: dict) -> str:
        """key 落在哪个头 —— 这个字段是给「claude CLI 跨厂商时到底发什么认证头」这类
        悬而未决的问题用的：跑一次就能从账本里读出来，不用猜。"""
        if headers.get("x-api-key"):
            return "x-api-key"
        if headers.get("authorization"):
            return "authorization"
        if headers.get("x-goog-api-key"):
            return "x-goog-api-key"
        return "none"

    def _log(self, *, protocol: str, model: str, path: str, headers: dict, body: dict,
             raw: bytes, stream: bool, stream_style: str = "") -> None:
        self.record({
            "protocol": protocol,
            "model": model,
            "path": path,
            "stream": stream,
            "stream_style": stream_style,
            "key_header": self._key_location(headers),
            "anthropic_version": headers.get("anthropic-version"),
            "user_agent": headers.get("user-agent"),
            "max_tokens": body.get("max_tokens") or (body.get("generationConfig") or {}).get("maxOutputTokens"),
            "body_keys": sorted(body.keys()),
            "n_messages": len(body.get("messages") or body.get("input") or body.get("contents") or []),
            "body_bytes": len(raw),
        })

    # ---------------- Anthropic Messages ----------------

    def _messages(self, headers: dict, raw: bytes) -> _Response:
        injected = self._injected_failure()
        body = self._read_json(raw)
        if body is None:
            return _anthropic_error("Invalid JSON body", status=400, type_="invalid_request_error")

        model = body.get("model") or ""
        flavor = self._flavor(model)
        self._log(protocol="messages", model=model, path="/v1/messages", headers=headers,
                  body=body, raw=raw, stream=bool(body.get("stream")),
                  stream_style="event+data")

        # 鉴权：官方 getting-started 页写的是 Authorization: Bearer 为主、x-api-key 为
        # 兼容写法。两种都收，都不带则 401。
        if not (headers.get("x-api-key") or headers.get("authorization")):
            return _anthropic_error("x-api-key header is required", status=401,
                                    type_="authentication_error")
        if not headers.get("anthropic-version"):
            return _anthropic_error("anthropic-version: header is required", status=400,
                                    type_="invalid_request_error")
        if not model:
            return _anthropic_error("model: Field required", status=400,
                                    type_="invalid_request_error")
        if not body.get("messages"):
            return _anthropic_error("messages: Field required", status=400,
                                    type_="invalid_request_error")
        # max_tokens 是必填：官方的非流式示例与参数表都是这么写的，真实接口缺它会报错。
        # 这条校验对 AICU 有价值 —— 它正是「请求关不完整」这类接线错误的哨兵。
        if body.get("max_tokens") is None:
            return _anthropic_error("max_tokens: Field required", status=400,
                                    type_="invalid_request_error")
        if injected:
            return injected

        text = reply_text(model, "messages", flavor)
        prompt_tokens, completion_tokens = _usage_counts(raw, text)
        msg_id = _new_id("msg_")

        if not body.get("stream"):
            return _json_response({
                "id": msg_id, "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": model, "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
            })

        # 事件序列逐字照 messages-streaming 页：message_start → content_block_start →
        # ping → content_block_delta… → content_block_stop → message_delta → message_stop。
        frames = [
            _sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant", "content": [],
                "model": model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": prompt_tokens, "output_tokens": 1}}}),
            _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
            _sse("ping", {"type": "ping"}),
        ]
        for piece in _split_chunks(text):
            frames.append(_sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": piece}}))
        frames += [
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": completion_tokens}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        return _Response(headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                         frames=frames)

    # ---------------- OpenAI Chat Completions ----------------

    def _chat(self, headers: dict, raw: bytes) -> _Response:
        injected = self._injected_failure()
        body = self._read_json(raw)
        if body is None:
            return _openai_error("Invalid JSON body", status=400)

        model = body.get("model") or ""
        flavor = self._flavor(model)
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        self._log(protocol="chat", model=model, path="/v1/chat/completions", headers=headers,
                  body=body, raw=raw, stream=stream, stream_style="data-only")
        if not headers.get("authorization"):
            return _openai_error("Missing bearer or basic authentication in header", status=401)
        if not model:
            return _openai_error("Missing required parameter: 'model'.", status=400,
                                 param="model", code="missing_required_parameter")
        if not body.get("messages"):
            return _openai_error("Missing required parameter: 'messages'.", status=400,
                                 param="messages", code="missing_required_parameter")
        if injected:
            return injected

        text = reply_text(model, "chat", flavor)
        prompt_tokens, completion_tokens = _usage_counts(raw, text)
        created = int(time.time())
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}

        if not stream:
            return _json_response({
                "id": _new_id("chatcmpl-"), "object": "chat.completion", "created": created,
                "model": model, "system_fingerprint": "fp_mock",
                "choices": [{"index": 0, "logprobs": None, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text, "refusal": None}}],
                "usage": usage,
            })

        chunk_id = _new_id("chatcmpl-")
        def chunk(delta: dict, finish: Optional[str], usage_payload: Optional[dict] = None,
                  choices: Optional[list] = None) -> bytes:
            payload = {"id": chunk_id, "object": "chat.completion.chunk", "created": created,
                       "model": model, "system_fingerprint": "fp_mock",
                       "choices": choices if choices is not None else
                       [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
                       "usage": usage_payload}
            return _sse(None, payload)

        frames = [chunk({"role": "assistant", "content": ""}, None)]
        for piece in _split_chunks(text):
            frames.append(chunk({"content": piece}, None))
        frames.append(chunk({}, "stop"))
        # include_usage 时，usage 单独一帧、choices 恒为空数组（规范原文），且它在 [DONE] 之前。
        if include_usage:
            frames.append(chunk({}, None, usage_payload=usage, choices=[]))
        frames.append(_sse(None, "[DONE]"))
        return _Response(headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                         frames=frames)

    # ---------------- OpenAI Responses ----------------

    def _responses(self, headers: dict, raw: bytes) -> _Response:
        injected = self._injected_failure()
        body = self._read_json(raw)
        if body is None:
            return _openai_error("Invalid JSON body", status=400)

        model = body.get("model") or ""
        flavor = self._flavor(model)
        stream = bool(body.get("stream"))
        # 真实厂商的流式风格不同：OpenAI 带 event: 名、没有 [DONE]；xAI 是 data-only
        # 且以 data: [DONE] 收尾（docs.x.ai 的 stream 参数原文）。
        data_only = flavor == "xai"
        style = "data-only+[DONE]" if data_only else "event+data"
        self._log(protocol="responses", model=model, path="/v1/responses", headers=headers,
                  body=body, raw=raw, stream=stream, stream_style=style)

        if not headers.get("authorization"):
            return _openai_error("Missing bearer or basic authentication in header", status=401)
        # 缺 input 的形态两家不同：OpenAI 是 400 + missing_required_parameter；
        # 实测 xAI 返回 422（Rust 风格反序列化报错）。
        if not model:
            if flavor == "xai":
                return _json_response({"error": "missing field `model` at line 1 column 1"},
                                      status=422)
            return _openai_error("Missing required parameter: 'model'.", status=400,
                                 param="model", code="missing_required_parameter")
        if not body.get("input"):
            if flavor == "xai":
                return _json_response({"error": "missing field `input` at line 1 column 1"},
                                      status=422)
            return _openai_error("Missing required parameter: 'input'.", status=400,
                                 param="input", code="missing_required_parameter")
        if injected:
            return injected

        text = reply_text(model, "responses", flavor)
        prompt_tokens, completion_tokens = _usage_counts(raw, text)
        response_id = uuid.uuid4().hex if flavor == "xai" else _new_id("resp_")
        item_id = _new_id("msg_")
        usage = {
            "input_tokens": prompt_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": completion_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if flavor == "xai":
            # xAI 的 usage 多这三个字段（docs.x.ai 示例逐字）。
            usage.update({"num_sources_used": 0, "num_server_side_tools_used": 0,
                          "cost_in_usd_ticks": 0})

        message_item = {
            "id": item_id, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
        }

        def response_object(status: str) -> dict:
            return {
                "id": response_id, "object": "response", "created_at": int(time.time()),
                "status": status, "model": model, "output": [message_item],
                "error": None, "incomplete_details": None, "instructions": None,
                "parallel_tool_calls": True, "previous_response_id": None,
                "reasoning": {"effort": None, "summary": None},
                "store": True, "temperature": 1, "top_p": 1, "truncation": "disabled",
                "text": {"format": {"type": "text"}}, "tool_choice": "auto", "tools": [],
                "user": None, "metadata": {}, "usage": usage,
            }

        if not stream:
            return _json_response(response_object("completed"))

        # 事件序列按 OpenAI 规范：created → in_progress → output_item.added →
        # content_part.added → output_text.delta… → output_text.done → content_part.done →
        # output_item.done → completed。
        seq = iter(range(1, 64))
        frames = [
            _sse("response.created", {"type": "response.created", "response": response_object("in_progress"),
                                      "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.in_progress", {"type": "response.in_progress",
                                          "response": response_object("in_progress"),
                                          "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.output_item.added", {"type": "response.output_item.added",
                                                "output_index": 0,
                                                "item": {"id": item_id, "type": "message",
                                                         "role": "assistant", "status": "in_progress",
                                                         "content": []},
                                                "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.content_part.added", {"type": "response.content_part.added",
                                                 "item_id": item_id, "output_index": 0,
                                                 "content_index": 0,
                                                 "part": {"type": "output_text", "text": "",
                                                          "annotations": []},
                                                 "sequence_number": next(seq)}, data_only=data_only),
        ]
        for piece in _split_chunks(text):
            frames.append(_sse("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": item_id, "output_index": 0,
                "content_index": 0, "delta": piece, "logprobs": [],
                "sequence_number": next(seq)}, data_only=data_only))
        frames += [
            _sse("response.output_text.done", {"type": "response.output_text.done",
                                               "item_id": item_id, "output_index": 0,
                                               "content_index": 0, "text": text, "logprobs": [],
                                               "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.content_part.done", {"type": "response.content_part.done",
                                                "item_id": item_id, "output_index": 0,
                                                "content_index": 0, "part": message_item["content"][0],
                                                "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.output_item.done", {"type": "response.output_item.done",
                                               "output_index": 0, "item": message_item,
                                               "sequence_number": next(seq)}, data_only=data_only),
            _sse("response.completed", {"type": "response.completed",
                                        "response": response_object("completed"),
                                        "sequence_number": next(seq)}, data_only=data_only),
        ]
        if data_only:
            frames.append(_sse(None, "[DONE]"))
        return _Response(headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
                         frames=frames)

    # ---------------- Google 原生 generateContent ----------------

    def _google(self, headers: dict, model: str, action: str, raw: bytes) -> _Response:
        injected = self._injected_failure()
        body = self._read_json(raw)
        if body is None:
            return _google_error("Invalid JSON payload received.", status=400,
                                 status_str="INVALID_ARGUMENT")

        stream = action == "streamGenerateContent"
        flavor = self._flavor(model)
        self._log(protocol="google", model=model, path=f"/v1beta/models/{model}:{action}",
                  headers=headers, body=body, raw=raw, stream=stream,
                  stream_style="data-only(crlf)")
        # 实测无凭据时的原文：403 PERMISSION_DENIED（不是 401）。
        if self._key_location(headers) == "none":
            return _google_error(
                "Method doesn't allow unregistered callers (callers without established identity). "
                "Please use API Key or other form of API consumer identity to call this API.",
                status=403, status_str="PERMISSION_DENIED")
        if body.get("contents") is None:
            return _google_error(
                "Invalid value at 'contents' (type.googleapis.com/google.ai.generativelanguage.v1beta.Content)",
                status=400, status_str="INVALID_ARGUMENT")
        if injected:
            return injected

        text = reply_text(model, "google", flavor)
        prompt_tokens, completion_tokens = _usage_counts(raw, text)
        payload = {
            "candidates": [{
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": "STOP", "index": 0,
                "safetyRatings": [{"category": "HARM_CATEGORY_HATE_SPEECH",
                                   "probability": "NEGLIGIBLE", "blocked": False}],
            }],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": completion_tokens,
                "totalTokenCount": prompt_tokens + completion_tokens,
                "cachedContentTokenCount": 0, "thoughtsTokenCount": 0,
                "serviceTier": "standard",
            },
            "modelVersion": model, "responseId": uuid.uuid4().hex[:20],
        }
        if not stream:
            return _json_response(payload)
        # 实测：Google 的 SSE 帧只有 data: 行、行尾是 \r\n、没有 [DONE]。
        frames = [_sse(None, {**payload, "candidates": [{**payload["candidates"][0],
                                                        "content": {"parts": [{"text": piece}],
                                                                    "role": "model"}}]},
                       crlf=True)
                  for piece in _split_chunks(text)]
        return _Response(headers={"content-type": "text/event-stream"}, frames=frames)


def _split_chunks(text: str, pieces: int = 4) -> list:
    """把回答切成几段流式发出去 —— 第一次就整段发，等于没测到流式拼接。"""
    if len(text) <= 1:
        return [text]
    size = max(1, len(text) // pieces)
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


# ============================================================
# HTTP 层
# ============================================================


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音：测试输出里不需要访问日志
        pass

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        provider: MockProviders = self.server.provider  # type: ignore[attr-defined]
        # Google 的 key 可以走 ?key=，把它的落点补进账本（header 优先，没头才记 query）。
        if query.get("key") and not (headers.get("x-goog-api-key") or headers.get("authorization")):
            headers["x-goog-api-key"] = query["key"]

        response = provider.handle(method=method, path=parsed.path, query=query,
                                   headers=headers, raw=raw)
        if response.is_stream:
            self._write_stream(response)
        else:
            self._write_plain(response)

    def _write_plain(self, response: "_Response") -> None:
        body = response.body or b""
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _write_stream(self, response: "_Response") -> None:
        """chunked 分帧写出去 —— 真厂商就是 chunked 的，一次性塞完就不算流式了。"""
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        provider: MockProviders = self.server.provider  # type: ignore[attr-defined]
        try:
            for frame in response.frames:
                self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
                self.wfile.flush()
                if provider.delay:
                    time.sleep(provider.delay)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 调用方提前断开（例如超时 kill 子进程），不是 mock 的问题。
            pass

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_GET(self) -> None:
        self._dispatch("GET")


class MockServer(ThreadingHTTPServer):
    """把 ``MockProviders`` 挂到 HTTP 上的server。``port=0`` 时自动挑空闲端口。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address=("127.0.0.1", 0), provider: Optional[MockProviders] = None):
        super().__init__(address, _Handler)
        self.provider = provider or MockProviders()
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}"


@contextmanager
def running_mock(*, port: int = 0, **kwargs) -> Iterator[MockServer]:
    """起一个假厂商，退出时收掉（含线程）。测试与脚本都走这个入口。"""
    server = MockServer(("127.0.0.1", port), MockProviders(**kwargs))
    # poll_interval 默认 0.5s，每个用例收尾都要白等半秒；调小到 0.05s。
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="本地假厂商服务（OpenAI / Anthropic / Google / xAI）")
    parser.add_argument("--port", type=int, default=8799, help="监听端口（默认 8799）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--flavor", choices=["openai", "anthropic", "google", "xai"],
                        help="强制指定口音（默认按模型名自动判）")
    parser.add_argument("--log", help="把请求账本写成 JSONL 到这个路径")
    parser.add_argument("--fail-status", type=int, help="注入厂商报错状态码（如 429）")
    parser.add_argument("--fail-times", type=int, default=1, help="注入几次后恢复正常")
    parser.add_argument("--delay", type=float, default=0.0, help="每帧之间停顿秒数")
    args = parser.parse_args(argv)

    provider = MockProviders(flavor=args.flavor, fail_status=args.fail_status,
                             fail_times=args.fail_times, log_path=args.log, delay=args.delay)
    server = MockServer((args.host, args.port), provider)
    print(f"假厂商已启动：{server.base_url}")
    print("  claude  ANTHROPIC_BASE_URL=" + server.base_url)
    print("  codex   config.toml base_url=" + server.base_url + "/v1")
    print("  kimi    KIMI_MODEL_BASE_URL=" + server.base_url + "/v1")
    print(f"  请求账本：{args.log if args.log else '仅内存（本进程）'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
