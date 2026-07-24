import json
from pathlib import Path
from typing import Mapping


class PromptConfigurationError(ValueError):
    pass


class PromptRegistry:
    REQUIRED_PROMPTS = frozenset(
        {"planner", "sql_generator", "pre_execution_reviewer", "result_reviewer"}
    )

    def __init__(self, version: str, prompts: Mapping[str, str]) -> None:
        self.version = version
        self._prompts = dict(prompts)

    @classmethod
    def from_file(cls, path: str | Path) -> "PromptRegistry":
        resolved = Path(path)
        if not resolved.is_file():
            raise PromptConfigurationError(f"提示词配置文件不存在: {resolved}")
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptConfigurationError("提示词配置文件无法解析") from exc
        version = payload.get("version")
        prompts = payload.get("system_prompts")
        global_constraints = payload.get("global_constraints", "")
        constraints = payload.get("stage_constraints", {})
        if not isinstance(version, str) or not version.strip():
            raise PromptConfigurationError("提示词配置缺少 version")
        if not isinstance(prompts, dict):
            raise PromptConfigurationError("提示词配置缺少 system_prompts")
        if not isinstance(constraints, dict):
            raise PromptConfigurationError("stage_constraints 格式错误")
        if not isinstance(global_constraints, str):
            raise PromptConfigurationError("global_constraints 格式错误")
        missing = cls.REQUIRED_PROMPTS - set(prompts)
        if missing:
            raise PromptConfigurationError(
                "提示词配置缺少必需提示词: " + ", ".join(sorted(missing))
            )
        invalid = [
            name
            for name in cls.REQUIRED_PROMPTS
            if not isinstance(prompts[name], str) or not prompts[name].strip()
        ]
        if invalid:
            raise PromptConfigurationError(
                "提示词内容为空或格式错误: " + ", ".join(sorted(invalid))
            )
        merged = {}
        for name in cls.REQUIRED_PROMPTS:
            base = prompts[name].strip()
            constraint = constraints.get(name)
            parts = [base]
            if global_constraints.strip():
                parts.append("全局业务约束：" + global_constraints.strip())
            if isinstance(constraint, str) and constraint.strip():
                parts.append("补充强约束：" + constraint.strip())
            merged[name] = "\n\n".join(parts)
        return cls(version.strip(), merged)

    def get(self, name: str) -> str:
        try:
            return self._prompts[name]
        except KeyError as exc:
            raise PromptConfigurationError(f"未注册提示词: {name}") from exc
