from evals.run_grading_quality_eval import run_eval


def test_grading_quality_golden_set_has_no_false_accepts_or_false_reviews():
    report = run_eval()
    assert report["case_count"] >= 14
    assert report["false_accept_rate"] == 0.0, report["failures"]
    assert report["false_review_rate"] == 0.0, report["failures"]
    assert report["accuracy"] == 1.0, report["failures"]


def test_grading_quality_golden_set_covers_required_categories():
    report = run_eval()
    assert {"correct", "incorrect", "blank", "incomplete", "uncertain", "equivalent", "multi_part"} <= set(report["category_counts"])
