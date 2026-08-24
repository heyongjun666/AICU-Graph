"""runtime/delivery/contracts.py — ArtifactContract（产物契约）与类型规则。

Gate 不认识角色名，只消费产物契约（required_types + validators），
契约声明在角色 yaml（``artifact_contract:``），缺省回退默认契约。

坚持「不做 YAML 测试框架」：契约是最小固定集，不扩展
min_files / max_files / max_size / required_symbols / semantic_rules。
"""

from dataclasses import dataclass, field
from typing import Any

# artifact_type → 期望扩展名（第一版固定小映射；不含即视为任何文件均可）
# V3-2.1 P1 最小改名：source_code → code；test_report 并入 document。
# 已知边界（本轮不解决）：.json 同时出现在 document / dataset / config ——
# TYPE_RULES 只是 source_file validator 的扩展名提示，不参与归属判定。
TYPE_RULES: dict[str, set[str]] = {
    "code": {
        ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp",
        ".h", ".sh", ".rb", ".tsx", ".jsx",
    },
    "document": {".md", ".txt", ".rst", ".docx", ".pdf", ".json", ".log"},
    "config": {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"},
    "dataset": {".jsonl", ".csv", ".tsv", ".parquet", ".json"},
}

# validators 第一版固定集
VALID_VALIDATORS = frozenset({"exists", "non_empty", "source_file", "verdict_block"})

DEFAULT_ARTIFACT_TYPE = "generic"
DEFAULT_VALIDATORS = ("exists", "non_empty")


@dataclass
class ArtifactContract:
    """产物契约：声明产物应有的机械特征（确定性校验的输入）。

    Args:
        artifact_type: 内容类型（code / document / config / dataset）。
        required_types: 显式扩展名白名单；为空则按 artifact_type 查 TYPE_RULES。
        validators: 机械验证方式，第一版固定
            {exists, non_empty, source_file, verdict_block}；未知项被忽略。
    """

    artifact_type: str = DEFAULT_ARTIFACT_TYPE
    required_types: list = field(default_factory=list)
    validators: list = field(default_factory=lambda: list(DEFAULT_VALIDATORS))

    def __post_init__(self) -> None:
        self.artifact_type = str(self.artifact_type or DEFAULT_ARTIFACT_TYPE)
        if isinstance(self.required_types, str):
            self.required_types = [self.required_types]
        self.required_types = [str(t) for t in (self.required_types or [])]
        seen: set[str] = set()
        cleaned: list[str] = []
        for v in self.validators or []:
            v = str(v)
            if v not in VALID_VALIDATORS or v in seen:
                continue
            seen.add(v)
            cleaned.append(v)
        self.validators = cleaned or list(DEFAULT_VALIDATORS)


def parse_contract(value: Any) -> ArtifactContract:
    """把角色 yaml 的 ``artifact_contract:`` 解析为 ArtifactContract。

    支持三种形态：
      - None / 缺失 → 默认契约（{validators: [exists, non_empty]}）
      - str: ``"code"``（简写，等价 artifact_type）
      - dict: ``{"artifact_type": "code", "validators": [...]}``

    未知 validator 被忽略（fail-silent，向后兼容）；validators 为空回退默认。
    """
    if not value:
        return ArtifactContract()
    if isinstance(value, str):
        return ArtifactContract(artifact_type=value)
    if not isinstance(value, dict):
        return ArtifactContract()
    return ArtifactContract(
        artifact_type=str(value.get("artifact_type") or DEFAULT_ARTIFACT_TYPE),
        required_types=list(value.get("required_types") or []),
        validators=list(value.get("validators") or []),
    )


def resolve_required_types(contract: ArtifactContract) -> list[str]:
    """确定扩展名白名单：契约显式声明优先，否则按 artifact_type 查 TYPE_RULES。"""
    if contract.required_types:
        return contract.required_types
    return list(TYPE_RULES.get(contract.artifact_type, []))
