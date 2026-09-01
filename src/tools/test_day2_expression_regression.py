# -*- coding: utf-8 -*-
"""Day 2：高数表达式判等的最小回归测试集。

运行：
    cd ~/math-agent/src
    ./agent8000/math_agent/bin/python -m pytest -q tools/test_day2_expression_regression.py

这些例子来自工作台会遇到的标准答案、学生输入和 OCR/LaTex 转写。
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grading_engine as ge


@pytest.mark.parametrize(
    ("student", "standard", "minimum_confidence"),
    [
        ("(x+1)^2", "x^2+2*x+1", 0.99),
        (r"\frac{3}{6}", "0.5", 0.99),
        ("sin(x)^2+cos(x)^2", "1", 0.99),
        ("ln(x)", "log(x)", 0.99),
        (r"\sqrt{4}", "2", 0.99),
        ("e^(x)", "exp(x)", 0.99),
        ("0.6667", "2/3", 0.80),
    ],
    ids=["binomial", "latex_fraction", "trig_identity", "ln_alias", "latex_sqrt", "exponential", "decimal_approximation"],
)
def test_equivalent_expressions(student, standard, minimum_confidence):
    equal, confidence, _ = ge.expr_equal(student, standard)
    assert equal is True
    assert confidence >= minimum_confidence


@pytest.mark.parametrize(
    ("student", "standard"),
    [
        ("x^2", "x^3"),
        ("sin(x)", "cos(x)"),
    ],
    ids=["different_powers", "different_trig_functions"],
)
def test_non_equivalent_expressions(student, standard):
    equal, _, _ = ge.expr_equal(student, standard)
    assert equal is False



def test_ocr_superscript_should_be_equivalent():
    equal, _, _ = ge.expr_equal("x²+2x+1", "(x+1)^2")
    assert equal is True
