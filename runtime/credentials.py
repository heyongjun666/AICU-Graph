"""凭据存储（明文）+ 模型厂商映射。

认证凭据按模型厂商（vendor）共享 API key（sk），落盘在
``{root_dir}/api_keys.json``（明文，0600 权限，原子写），通过模块级单例供运行时
（worker 线程 / CLI 子进程）在单进程内访问——无需穿过 Manager/Worker 的配置链。

用户已明确（2026-08-20）：本机单用户单进程，前端↔后端走 localhost 回环，无跨网络
传输，放弃加密、接受明文存储。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

# 内部模型无需 sk，vendor 归为 None。
_INTERNAL_SUFFIX = "内部"

# 带空格/大写的模型名 → vendor 精确映射。
# 其余带连字符的官方名 / display name（gpt-*/grok-*/deepseek-*/glm-*/minimax-*）
# 由大小写不敏感前缀启发式覆盖，见 _PREFIX_VENDOR。
_EXACT_VENDOR = {
    "Claude Haiku 4.5": "anthropic",
    "Claude Sonnet 4.6": "anthropic",
    "Claude Sonnet 5": "anthropic",
    "Opus 4.8": "anthropic",
    "Opus 5": "anthropic",
    "Fable 5": "anthropic",
    "Kimi K2.6": "moonshot",
    "Kimi K3": "moonshot",
}

# 官方名（小写连字符）前缀 → vendor，按序匹配。
_PREFIX_VENDOR = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("kimi-", "moonshot"),
    ("grok-", "xai"),
    ("gemini-", "google"),
    ("deepseek-", "deepseek"),
    ("glm-", "zhipu"),
    ("minimax", "minimax"),
    ("qwen", "aliyun"),
)


def vendor_of_model(model: Optional[str]) -> Optional[str]:
    """把模型名归到已知原生厂商；网关模型保留 None，内部模型也返回 None。

    OpenRouter 等路由网关通过显式 provider 传入，不参与模型原厂商推断。
    """
    if not model:
        return None
    name = model.strip()
    if not name:
        return None
    if name == "auto" or _INTERNAL_SUFFIX in name:
        return None
    if name in _EXACT_VENDOR:
        return _EXACT_VENDOR[name]
    lowered = name.lower()
    for prefix, vendor in _PREFIX_VENDOR:
        if lowered.startswith(prefix):
            return vendor
    return None


# 多厂商 CLI（opencode / pi 等）按模型厂商注入 sk 时使用的标准环境变量名。
_VENDOR_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "xai": "XAI_API_KEY",
    "google": "GOOGLE_GENERATIVE_AI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "aliyun": "DASHSCOPE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def api_key_env_for_vendor(vendor: Optional[str]) -> Optional[str]:
    """按厂商返回注入 sk 时应使用的认证环境变量名；未知/内部厂商返回 None。"""
    if not vendor:
        return None
    return _VENDOR_API_KEY_ENV.get(vendor)


class CredentialStore:
    """明文凭据存储：``{api_keys: {vendor: sk}}``，原子写 + 0600。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {"api_keys": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {"api_keys": {}}
        if not isinstance(data, dict):
            return {"api_keys": {}}
        api_keys = data.get("api_keys")
        if not isinstance(api_keys, dict):
            api_keys = {}
        return {"api_keys": {str(k): str(v) for k, v in api_keys.items()}}

    def _save(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, self._path)
        os.chmod(self._path, 0o600)

    # ---- vendor sk ----

    def get(self, vendor: str) -> Optional[str]:
        with self._lock:
            return self._data["api_keys"].get(vendor)

    def set(self, vendor: str, sk: str) -> None:
        with self._lock:
            self._data["api_keys"][vendor] = sk
            self._save()

    def delete(self, vendor: str) -> None:
        with self._lock:
            self._data["api_keys"].pop(vendor, None)
            self._save()

    def vendors(self) -> list[str]:
        with self._lock:
            return sorted(self._data["api_keys"].keys())


# ---- 模块级单例（单进程共享，供 worker 线程 / CLI 子进程环境注入访问） ----

_store: Optional[CredentialStore] = None


def set_credential_store(store: Optional[CredentialStore]) -> None:
    """绑定当前 SessionManager 的凭据存储（单进程单例）。"""
    global _store
    _store = store


def resolve_sk_for_model(model: Optional[str]) -> Optional[str]:
    """按模型厂商解析已保存的 sk；store 未初始化或内部模型返回 None（安全降级）。"""
    return resolve_sk_for_provider(None, model)


def resolve_sk_for_provider(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """按显式连接 provider 解析 sk，旧调用仍按模型原厂商解析。"""
    if _store is None:
        return None
    vendor = (provider or "").strip().lower() or vendor_of_model(model)
    if not vendor:
        return None
    return _store.get(vendor)
