from src.agent8000.app.main import _wrap_latex_for_html


def test_printable_html_preserves_existing_parenthesized_latex() -> None:
    source = r"记 \( \overrightarrow{AB}=a \)，故 \( \frac12 a \)。"
    rendered = _wrap_latex_for_html(source)
    assert r"\( \overrightarrow{AB}=a \)" in rendered
    assert r"\( \frac12 a \)" in rendered
    assert r"\( $\overrightarrow" not in rendered


def test_printable_html_hides_answer_book_page_header() -> None:
    rendered = _wrap_latex_for_html("题干\n024第五章 向量代数与空间解析几何")
    assert "024第五章" not in rendered
    assert "题干" in rendered
