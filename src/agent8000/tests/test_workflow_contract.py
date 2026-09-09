from pathlib import Path


def test_critical_workflow_routes_remain_available():
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    required = {
        '/api/assignments',
        '/api/assignments/{assignment_id}/submissions',
        '/api/submissions/{submission_id}/grading',
        '/api/submissions/{submission_id}/regrade',
        '/api/submissions/{submission_id}/question-regions',
        '/api/submissions/{submission_id}/review',
        '/api/submissions/{submission_id}/release',
        '/api/student/released-submissions/{submission_id}/mistakes',
    }
    missing = sorted(route for route in required if f'"{route}"' not in source)
    assert not missing, f"critical grading workflow routes missing: {missing}"


def test_release_gate_requires_private_pdf_manifest_in_production():
    source = (Path(__file__).parents[1] / "scripts" / "release_quality_gate.py").read_text(encoding="utf-8")
    assert "REQUIRE_REAL_PDF_GOLD" in source
    assert "PDF_GOLD_MANIFEST is required" in source
