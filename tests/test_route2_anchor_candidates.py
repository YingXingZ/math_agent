from __future__ import annotations

from types import SimpleNamespace

from src.tools.propose_route2_answer_anchors import numbered_blocks, page_sections, similarity


class FakePage:
    rect = SimpleNamespace(width=500)

    def get_text(self, mode: str):
        if mode == "text":
            return "习题5.1\n1. 设向量 a = b，求解。\n2. 计算内积。"
        return [(72, 72, 300, 90, "1. 设向量 a = b，求解。", 0, 0),
                (72, 100, 300, 118, "2. 计算内积。", 1, 0)]


class TotalExercisePage:
    def get_text(self, mode: str):
        return "总习题五\n1. 这不是习题5.6。" if mode == "text" else []


class Section56Page:
    def get_text(self, mode: str):
        return "习题5.6\n1. 合法题目。" if mode == "text" else []


def test_native_header_and_numbered_prompts_are_located() -> None:
    doc = [FakePage()]
    assert page_sections(doc) == {"5.1": (0, 0)}
    prompts = numbered_blocks(doc, 0, 0)
    assert set(prompts) == {1, 2}
    assert prompts[1][0] == 0
    assert prompts[1][1] == [72.0, 72.0, 482.0, 97.0]


def test_final_section_stops_at_total_exercises() -> None:
    assert page_sections([Section56Page(), TotalExercisePage(), FakePage()])["5.6"] == (0, 1)


def test_similarity_has_conservative_empty_gate() -> None:
    assert similarity("", "题目") == 0.0
    assert similarity("计算内积", "计算内积") == 1.0
