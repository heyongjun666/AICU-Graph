"""凭据存储（明文）+ 模型厂商映射的单元测试。"""

import os

from runtime.credentials import (
    CredentialStore,
    api_key_env_for_vendor,
    resolve_sk_for_provider,
    resolve_sk_for_model,
    set_credential_store,
    vendor_of_model,
)


class TestVendorOfModel:
    def test_official_names_by_prefix(self):
        assert vendor_of_model("claude-opus-5") == "anthropic"
        assert vendor_of_model("gpt-5.6") == "openai"
        assert vendor_of_model("kimi-k3") == "moonshot"
        assert vendor_of_model("grok-4.6") == "xai"
        assert vendor_of_model("gemini-3") == "google"
        assert vendor_of_model("deepseek-v4-pro") == "deepseek"
        assert vendor_of_model("glm-5.2") == "zhipu"
        assert vendor_of_model("MiniMax-M3") == "minimax"

    def test_case_insensitive_prefix(self):
        assert vendor_of_model("GPT-5.6-luna") == "openai"
        assert vendor_of_model("DeepSeek-V4-Flash") == "deepseek"

    def test_qwen_maps_to_aliyun(self):
        assert vendor_of_model("qwen3.8-max") == "aliyun"
        assert vendor_of_model("qwen3-coder-plus") == "aliyun"
        assert vendor_of_model("qwen3.7-plus") == "aliyun"
        assert vendor_of_model("Qwen3-Coder-Plus") == "aliyun"

    def test_internal_models_return_none(self):
        assert vendor_of_model("auto") is None
        assert vendor_of_model("auto-内部") is None
        assert vendor_of_model("DeepSeek-V4-Flash-内部") is None
        assert vendor_of_model("GLM-5.2-内部") is None

    def test_openrouter_model_keeps_gateway_namespace(self):
        assert vendor_of_model("openai/gpt-4.1") is None
        assert api_key_env_for_vendor("openrouter") == "OPENROUTER_API_KEY"

    def test_empty_or_unknown_returns_none(self):
        assert vendor_of_model(None) is None
        assert vendor_of_model("") is None
        assert vendor_of_model("  ") is None
        assert vendor_of_model("some-random-model") is None


class TestCredentialStore:
    def test_vendor_sk_set_get_delete(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        assert store.get("deepseek") is None
        store.set("deepseek", "sk-test-123")
        assert store.get("deepseek") == "sk-test-123"
        assert store.vendors() == ["deepseek"]
        store.delete("deepseek")
        assert store.get("deepseek") is None
        assert store.vendors() == []

    def test_persistence_across_instances(self, tmp_path):
        path = str(tmp_path / "api_keys.json")
        store = CredentialStore(path)
        store.set("openai", "sk-openai")

        reloaded = CredentialStore(path)
        assert reloaded.get("openai") == "sk-openai"

    def test_file_is_written_with_0600_permissions(self, tmp_path):
        path = tmp_path / "api_keys.json"
        store = CredentialStore(str(path))
        store.set("deepseek", "sk-test-123")
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600

    def test_missing_or_corrupt_file_returns_empty(self, tmp_path):
        missing = CredentialStore(str(tmp_path / "missing.json"))
        assert missing.vendors() == []

        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        corrupt = CredentialStore(str(bad))
        assert corrupt.vendors() == []


class TestResolveSingletons:
    def test_resolve_returns_none_when_store_unset(self):
        set_credential_store(None)
        assert resolve_sk_for_model("deepseek-v4-pro") is None

    def test_resolve_sk_by_explicit_provider(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("openrouter", "sk-or")
        store.set("openai", "sk-openai")
        set_credential_store(store)
        try:
            assert resolve_sk_for_provider("openrouter", "openai/gpt-4.1") == "sk-or"
            assert resolve_sk_for_provider(None, "gpt-5.6") == "sk-openai"
        finally:
            set_credential_store(None)

    def test_resolve_sk_by_model_vendor(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("deepseek", "sk-test-123")
        set_credential_store(store)
        try:
            assert resolve_sk_for_model("deepseek-v4-flash") == "sk-test-123"
            # 未存厂商返回 None
            assert resolve_sk_for_model("gpt-5.6") is None
            # 内部模型返回 None
            assert resolve_sk_for_model("auto-内部") is None
        finally:
            set_credential_store(None)
