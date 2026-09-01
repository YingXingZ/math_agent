"""可注册、可描述、可在测试或实验中替换的 Skill Registry。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    implementation: Callable
    version: str = "1.0.0"
    config: dict = field(default_factory=dict)


class SkillRegistry:
    """A small dependency-injection container for graph skills.

    A graph receives a registry instance when it is compiled. Experiments can
    clone that registry and replace one skill without mutating production.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, name: str, *, version: str = "1.0.0", config: dict | None = None) -> Callable:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("Skill 名称只能包含字母、数字与下划线")

        def decorator(skill: Callable) -> Callable:
            if name in self._skills:
                raise ValueError(f"Skill 已注册：{name}")
            self._skills[name] = SkillDefinition(name, skill, version, dict(config or {}))
            return skill

        return decorator

    def get(self, name: str) -> Callable:
        try:
            return self._skills[name].implementation
        except KeyError as exc:
            raise KeyError(f"未注册的 Skill：{name}") from exc

    def describe(self, name: str) -> dict:
        try:
            item = self._skills[name]
        except KeyError as exc:
            raise KeyError(f"未注册的 Skill：{name}") from exc
        return {"name": item.name, "version": item.version, "config": dict(item.config)}

    def manifest(self, names: tuple[str, ...] | list[str] | None = None) -> list[dict]:
        selected = sorted(names or self._skills)
        return [self.describe(name) for name in selected]

    def clone(self) -> "SkillRegistry":
        cloned = SkillRegistry()
        cloned._skills = dict(self._skills)
        return cloned

    def replace(self, name: str, implementation: Callable, *, version: str, config: dict | None = None) -> None:
        if name not in self._skills:
            raise KeyError(f"未注册的 Skill：{name}")
        self._skills[name] = SkillDefinition(name, implementation, version, dict(config or {}))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))


registry = SkillRegistry()
