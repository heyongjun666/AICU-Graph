"""假厂商服务的一致性测试。

这些用例的作用是**把官方报文形状固化成可回归的断言**：mock 是拿来当"真厂商"用的，
它一旦被改得不忠实，端到端跑出来的绿就是假的。所以这里逐条钉住：

* 流式帧的**顺序与字段**（Anthropic 的 `event:`+`data:`、OpenAI Responses 的事件名与
  `sequence_number`、Chat 的 `data: [DONE]`、Google 的 data-only `\\r\\n` 帧）
* 各家**错误体的形状与状态码**（Anthropic 的 `request_id`、OpenAI 的四个字段、
  Google 的 `status` 字符串）
* **必填字段校验**真的会拦（缺 `max_tokens` / `input` / `contents` 都要报错）——
  宽容的假服务会把 AICU 的接线错误掩盖掉，这正是这个 mock 要避免的
* 请求账本记下了「key 落在哪个头」，这是回答 claude CLI 跨厂商认证头问题的依据

来源见 `tests/mock_providers.py` 的模块 docstring：官方文档 + 本机对真实接口的实测。
"""

import json
import urllib.error
import urllib.request

import pytest

from tests.mock_providers import MockProviders, flavor_of, reply_text, running_mock


# ============================================================
# 工具
# ============================================================


def post(base_url, path, payload, headers=None, *, raw=None):
    """发一条请求，返回 (status, 文本, 响应头)。4xx 不抛异常，方便断言错误体。"""
    body = raw if raw is not None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base_url + path, data=body, method="POST",
        headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def get(base_url, path, headers=None):
    request = urllib.request.Request(base_url + path, headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read()), dict(response.headers)


def sse_frames(text):
    """把 SSE 文本拆成 [(事件名或 None, 解析后的 data)]。

    ``[DONE]`` 这类非 JSON 载荷原样作为字符串返回 —— 它本身就是协议的一部分。
    """
    frames, event = [], None
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        frames.append((event, payload))
    return frames


@pytest.fixture()
def mock():
    with running_mock() as server:
        yield server


# ============================================================
# 厂商分辨
# ============================================================


class TestFlavorOf:
    @pytest.mark.parametrize("model,expected", [
        ("gpt-5.6", "openai"),
        ("claude-opus-5", "anthropic"),
        ("gemini-3.6-flash", "google"),
        ("grok-4.6", "xai"),
        ("deepseek-v4-flash", "openai"),   # 其余厂商都是 OpenAI 兼容的，落到 openai 形状
        ("", "openai"),
    ])
    def test_flavor_from_model_name(self, model, expected):
        assert flavor_of(model) == expected


# ============================================================
# Anthropic Messages
# ============================================================


class TestMessages:
    HEADERS = {"x-api-key": "sk-mock", "anthropic-version": "2023-06-01"}
    BODY = {"model": "claude-opus-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}

    def test_non_stream_body_matches_documented_shape(self, mock):
        status, text, _ = post(mock.base_url, "/v1/messages", self.BODY, self.HEADERS)
        assert status == 200
        body = json.loads(text)
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["id"].startswith("msg_")
        assert body["stop_reason"] == "end_turn"
        assert body["stop_sequence"] is None
        assert body["content"] == [{"type": "text", "text": reply_text("claude-opus-5", "messages", "anthropic")}]
        assert set(body["usage"]) == {"input_tokens", "output_tokens"}

    def test_stream_event_sequence_is_the_documented_one(self, mock):
        status, text, headers = post(mock.base_url, "/v1/messages",
                                     {**self.BODY, "stream": True}, self.HEADERS)
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        events = [name for name, _ in sse_frames(text)]
        assert events[0] == "message_start"
        assert events[1] == "content_block_start"
        assert "content_block_delta" in events
        assert events[-3:] == ["content_block_stop", "message_delta", "message_stop"]

    def test_stream_payloads_carry_the_documented_fields(self, mock):
        _, text, _ = post(mock.base_url, "/v1/messages", {**self.BODY, "stream": True}, self.HEADERS)
        frames = dict(sse_frames(text))
        assert frames["message_start"]["message"]["content"] == []
        assert frames["message_start"]["message"]["stop_reason"] is None
        assert set(frames["message_start"]["message"]["usage"]) == {"input_tokens", "output_tokens"}
        assert frames["content_block_start"]["content_block"] == {"type": "text", "text": ""}
        assert frames["content_block_delta"]["delta"]["type"] == "text_delta"
        assert frames["message_delta"]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
        assert "output_tokens" in frames["message_delta"]["usage"]

    def test_deltas_join_into_the_full_reply(self, mock):
        _, text, _ = post(mock.base_url, "/v1/messages", {**self.BODY, "stream": True}, self.HEADERS)
        joined = "".join(f["delta"]["text"] for name, f in sse_frames(text)
                         if name == "content_block_delta")
        assert joined == reply_text("claude-opus-5", "messages", "anthropic")

    def test_authorization_bearer_is_accepted_too(self, mock):
        """官方 getting-started：Authorization 为主、x-api-key 为兼容写法。"""
        status, _, _ = post(mock.base_url, "/v1/messages", self.BODY,
                            {"authorization": "Bearer sk-mock", "anthropic-version": "2023-06-01"})
        assert status == 200

    def test_missing_key_is_401_with_anthropic_error_shape(self, mock):
        status, text, _ = post(mock.base_url, "/v1/messages", self.BODY,
                               {"anthropic-version": "2023-06-01"})
        assert status == 401
        body = json.loads(text)
        assert body["type"] == "error"
        assert body["error"]["type"] == "authentication_error"
        assert body["request_id"].startswith("req_")

    def test_missing_max_tokens_is_rejected(self, mock):
        """max_tokens 必填 —— 这条校验是「请求关不完整」的哨兵。"""
        status, text, _ = post(mock.base_url, "/v1/messages",
                               {"model": "claude-opus-5",
                                "messages": [{"role": "user", "content": "hi"}]}, self.HEADERS)
        assert status == 400
        assert json.loads(text)["error"]["type"] == "invalid_request_error"

    def test_missing_anthropic_version_is_rejected(self, mock):
        status, _, _ = post(mock.base_url, "/v1/messages", self.BODY, {"x-api-key": "sk-mock"})
        assert status == 400

    def test_request_log_records_which_header_carried_the_key(self, mock):
        post(mock.base_url, "/v1/messages", self.BODY, self.HEADERS)
        entry = mock.provider.requests_for(protocol="messages")[0]
        assert entry["key_header"] == "x-api-key"
        assert entry["anthropic_version"] == "2023-06-01"
        assert entry["model"] == "claude-opus-5"
        assert entry["max_tokens"] == 64
        assert entry["stream"] is False


# ============================================================
# OpenAI Chat Completions
# ============================================================


class TestChat:
    HEADERS = {"authorization": "Bearer sk-mock"}
    BODY = {"model": "gpt-5.6", "messages": [{"role": "user", "content": "hi"}]}

    def test_non_stream_body_matches_spec(self, mock):
        status, text, _ = post(mock.base_url, "/v1/chat/completions", self.BODY, self.HEADERS)
        assert status == 200
        body = json.loads(text)
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}

    def test_stream_ends_with_done_and_chunks_are_completion_chunks(self, mock):
        _, text, _ = post(mock.base_url, "/v1/chat/completions",
                          {**self.BODY, "stream": True}, self.HEADERS)
        frames = sse_frames(text)
        assert all(name is None for name, _ in frames), "Chat 的 SSE 不带 event: 名"
        assert frames[-1][1] == "[DONE]"
        for _, payload in frames[:-1]:
            assert payload["object"] == "chat.completion.chunk"
        assert frames[-2][1]["choices"][0]["finish_reason"] == "stop"

    def test_usage_chunk_only_when_include_usage(self, mock):
        _, plain, _ = post(mock.base_url, "/v1/chat/completions",
                           {**self.BODY, "stream": True}, self.HEADERS)
        assert all(p.get("usage") is None for _, p in sse_frames(plain)[:-1])

        _, with_usage, _ = post(mock.base_url, "/v1/chat/completions",
                                {**self.BODY, "stream": True,
                                 "stream_options": {"include_usage": True}}, self.HEADERS)
        frames = sse_frames(with_usage)
        usage_frames = [p for _, p in frames if isinstance(p, dict)
                        and p.get("usage") and p["choices"] == []]
        assert len(usage_frames) == 1, "规范：usage 单独一帧且 choices 恒为空数组"
        assert "total_tokens" in usage_frames[0]["usage"]

    def test_deltas_join_into_the_full_reply(self, mock):
        _, text, _ = post(mock.base_url, "/v1/chat/completions",
                          {**self.BODY, "stream": True}, self.HEADERS)
        joined = "".join(p["choices"][0]["delta"].get("content", "")
                         for _, p in sse_frames(text) if isinstance(p, dict) and p["choices"])
        assert joined == reply_text("gpt-5.6", "chat", "openai")

    def test_missing_key_is_401_with_measured_message(self, mock):
        status, text, _ = post(mock.base_url, "/v1/chat/completions", self.BODY)
        assert status == 401
        error = json.loads(text)["error"]
        assert error["message"] == "Missing bearer or basic authentication in header"
        assert set(error) == {"message", "type", "param", "code"}

    def test_missing_model_names_the_parameter_like_the_real_api(self, mock):
        status, text, _ = post(mock.base_url, "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "hi"}]}, self.HEADERS)
        assert status == 400
        error = json.loads(text)["error"]
        assert error["param"] == "model"
        assert error["code"] == "missing_required_parameter"

    def test_google_openai_compat_prefix_is_served(self, mock):
        """google 在 AICU 的表里是 https://…/v1beta/openai，CLI 会拼 /chat/completions。"""
        status, text, _ = post(mock.base_url, "/v1beta/openai/chat/completions",
                               {**self.BODY, "model": "gemini-3.6-flash"}, self.HEADERS)
        assert status == 200
        assert json.loads(text)["model"] == "gemini-3.6-flash"


# ============================================================
# OpenAI Responses
# ============================================================


class TestResponses:
    HEADERS = {"authorization": "Bearer sk-mock"}
    BODY = {"model": "gpt-5.6", "input": "hi", "stream": True}

    def test_non_stream_body_matches_spec(self, mock):
        status, text, _ = post(mock.base_url, "/v1/responses",
                               {**self.BODY, "stream": False}, self.HEADERS)
        assert status == 200
        body = json.loads(text)
        assert body["object"] == "response"
        assert body["id"].startswith("resp_")
        assert body["status"] == "completed"
        assert body["output"][0]["type"] == "message"
        assert body["output"][0]["content"][0]["type"] == "output_text"
        assert set(body["usage"]) == {"input_tokens", "input_tokens_details",
                                      "output_tokens", "output_tokens_details", "total_tokens"}

    def test_openai_stream_uses_named_events_in_documented_order(self, mock):
        status, text, headers = post(mock.base_url, "/v1/responses", self.BODY, self.HEADERS)
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        events = [name for name, _ in sse_frames(text)]
        assert events[:4] == ["response.created", "response.in_progress",
                              "response.output_item.added", "response.content_part.added"]
        assert events[-4:] == ["response.output_text.done", "response.content_part.done",
                               "response.output_item.done", "response.completed"]
        assert "response.output_text.delta" in events
        assert "[DONE]" not in text, "OpenAI 的 Responses 规范里没有 [DONE]"

    def test_stream_frames_carry_sequence_numbers_and_indexes(self, mock):
        _, text, _ = post(mock.base_url, "/v1/responses", self.BODY, self.HEADERS)
        frames = sse_frames(text)
        numbers = [payload["sequence_number"] for _, payload in frames]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
        delta_events = [p for name, p in frames if name == "response.output_text.delta"]
        assert delta_events, "应当有文本增量事件"
        for payload in delta_events:
            assert payload["output_index"] == 0 and payload["content_index"] == 0
            assert payload["item_id"].startswith("msg_")

    def test_deltas_join_into_the_full_reply(self, mock):
        _, text, _ = post(mock.base_url, "/v1/responses", self.BODY, self.HEADERS)
        joined = "".join(p["delta"] for name, p in sse_frames(text)
                         if name == "response.output_text.delta")
        assert joined == reply_text("gpt-5.6", "responses", "openai")

    def test_xai_stream_is_data_only_and_ends_with_done(self, mock):
        """xAI 与 OpenAI 的真实差异：data-only 帧 + `data: [DONE]` 收尾（官方原文）。"""
        status, text, _ = post(mock.base_url, "/v1/responses",
                               {**self.BODY, "model": "grok-4.6"}, self.HEADERS)
        assert status == 200
        frames = sse_frames(text)
        assert all(name is None for name, _ in frames)
        assert frames[-1][1] == "[DONE]"

    def test_xai_response_id_is_bare_uuid_and_usage_has_extra_fields(self, mock):
        _, text, _ = post(mock.base_url, "/v1/responses",
                          {**self.BODY, "stream": False, "model": "grok-4.6"}, self.HEADERS)
        body = json.loads(text)
        assert not body["id"].startswith("resp_"), "xAI 的 id 是裸 UUID"
        assert "num_sources_used" in body["usage"]
        assert "cost_in_usd_ticks" in body["usage"]

    def test_missing_input_is_rejected_per_vendor(self, mock):
        openai_status, _, _ = post(mock.base_url, "/v1/responses",
                                   {"model": "gpt-5.6", "stream": True}, self.HEADERS)
        assert openai_status == 400
        xai_status, _, _ = post(mock.base_url, "/v1/responses",
                                {"model": "grok-4.6", "stream": True}, self.HEADERS)
        assert xai_status == 422, "实测 xAI 对缺字段返回 422，不是 400"


# ============================================================
# Google Gemini 原生
# ============================================================


class TestGoogle:
    BODY = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}

    def test_non_stream_body_matches_discovery_schema(self, mock):
        status, text, _ = post(mock.base_url,
                               "/v1beta/models/gemini-3.6-flash:generateContent?key=mk", self.BODY)
        assert status == 200
        body = json.loads(text)
        assert body["candidates"][0]["content"]["role"] == "model"
        assert body["candidates"][0]["finishReason"] == "STOP"
        assert body["modelVersion"] == "gemini-3.6-flash"
        assert "promptTokenCount" in body["usageMetadata"]
        assert "totalTokenCount" in body["usageMetadata"]

    def test_stream_frames_are_data_only_with_crlf(self, mock):
        """实测：Google 的 SSE 只有 data: 行、行尾 \\r\\n、没有 [DONE]。"""
        status, text, _ = post(
            mock.base_url,
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent?alt=sse&key=mk", self.BODY)
        assert status == 200
        assert "\r\n\r\n" in text
        assert "event:" not in text
        assert "[DONE]" not in text
        frames = sse_frames(text)
        assert all(name is None for name, _ in frames)
        joined = "".join(f["candidates"][0]["content"]["parts"][0]["text"] for _, f in frames)
        assert joined == reply_text("gemini-3.6-flash", "google", "google")

    def test_key_can_come_from_header_instead_of_query(self, mock):
        """实测：x-goog-api-key 头与 ?key= 等价。"""
        status, _, _ = post(mock.base_url,
                            "/v1beta/models/gemini-3.6-flash:generateContent",
                            self.BODY, {"x-goog-api-key": "mk"})
        assert status == 200

    def test_missing_key_is_403_with_measured_message(self, mock):
        status, text, _ = post(mock.base_url,
                               "/v1beta/models/gemini-3.6-flash:generateContent", self.BODY)
        assert status == 403
        error = json.loads(text)["error"]
        assert error["status"] == "PERMISSION_DENIED"
        assert "unregistered callers" in error["message"]

    def test_missing_contents_is_400_invalid_argument(self, mock):
        status, text, _ = post(mock.base_url,
                               "/v1beta/models/gemini-3.6-flash:generateContent?key=mk", {})
        assert status == 400
        assert json.loads(text)["error"]["status"] == "INVALID_ARGUMENT"


# ============================================================
# 通用行为
# ============================================================


class TestCommon:
    def test_unknown_path_is_404(self, mock):
        status, text, _ = post(mock.base_url, "/v1/nope", {}, {"authorization": "Bearer k"})
        assert status == 404

    def test_model_list_shape_depends_on_client(self, mock):
        _, openai_list, _ = get(mock.base_url, "/v1/models", {"authorization": "Bearer k"})
        assert openai_list["object"] == "list"
        assert "created" in openai_list["data"][0]

        _, anthropic_list, _ = get(mock.base_url, "/v1/models", {"anthropic-version": "2023-06-01"})
        assert anthropic_list["data"][0]["type"] == "model"
        assert "display_name" in anthropic_list["data"][0]

    def test_injected_failure_returns_the_status_once(self, mock):
        mock.provider.fail_status = 429
        mock.provider.fail_times = 1
        headers = {"authorization": "Bearer k"}
        body = {"model": "gpt-5.6", "messages": [{"role": "user", "content": "hi"}]}
        status, text, resp_headers = post(mock.base_url, "/v1/chat/completions", body, headers)
        assert status == 429
        assert resp_headers.get("retry-after") == "1"
        assert json.loads(text)["error"]["type"] == "rate_limit_error"
        again, _, _ = post(mock.base_url, "/v1/chat/completions", body, headers)
        assert again == 200, "注入只应生效约定的次数"

    def test_config_endpoint_sets_injection(self, mock):
        post(mock.base_url, "/__mock__/config", {"fail_status": 500, "fail_times": 2})
        assert mock.provider.fail_status == 500 and mock.provider.fail_times == 2

    def test_ledger_can_be_written_to_disk(self, tmp_path):
        log = tmp_path / "requests.jsonl"
        with running_mock(log_path=str(log)) as server:
            post(server.base_url, "/v1/messages",
                 {"model": "claude-opus-5", "max_tokens": 8,
                  "messages": [{"role": "user", "content": "hi"}]},
                 {"x-api-key": "k", "anthropic-version": "2023-06-01"})
        lines = [json.loads(line) for line in log.read_text().splitlines()]
        assert lines[0]["protocol"] == "messages"
        assert lines[0]["model"] == "claude-opus-5"

    def test_provider_can_be_constructed_without_http(self):
        """``MockProviders`` 不依赖 HTTP 层，方便单测里直接调 handle。"""
        provider = MockProviders()
        assert provider.requests_for(protocol="chat") == []
        assert provider.fail_status is None
