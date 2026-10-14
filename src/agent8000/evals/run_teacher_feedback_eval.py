"""Evaluate stored model candidate scores against teacher-confirmed scores.

Reads only numeric candidate/final scores and non-identifying task metadata.
Never exports names, recognized answers, images, or per-submission rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "runtime-data" / "agent" / "homework.db"


def _float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _holdout_rank(seed: str, assignment_id: int, scorer_model: str, submission_id: int) -> bytes:
    key = f"{seed}:{assignment_id}:{scorer_model}:{submission_id}".encode()
    return hashlib.sha256(key).digest()


def load_rows(db_path: Path):
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        query = """
        SELECT s.id AS submission_id, s.assignment_id,
               g.decision_json, j.result_json
        FROM submissions s
        JOIN grading_experiences g ON g.submission_id = s.id
        JOIN grading_jobs j ON j.submission_id = s.id
        WHERE g.decision_json IS NOT NULL AND j.result_json IS NOT NULL
        ORDER BY s.assignment_id, s.id
        """
        for row in conn.execute(query):
            try:
                decision = json.loads(row["decision_json"] or "{}")
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                continue
            runtime = result.get("model_runtime") or {}
            runtime_model = str(runtime.get("model") or runtime.get("provider") or "unknown")[:80]
            qresults = result.get("results") or []
            qresults_by_id = {str(item.get("question_id")): item for item in qresults
                              if isinstance(item, dict) and item.get("question_id") is not None}
            qdecisions = decision.get("question_decisions") or []
            if isinstance(qdecisions, dict):
                qdecisions = list(qdecisions.values())
            seen = set()
            for item in qdecisions:
                if not isinstance(item, dict):
                    continue
                candidate = _float(item.get("candidate_score"))
                confirmed = _float(item.get("confirmed_score"))
                maximum = _float(item.get("max_score"))
                if candidate is None or confirmed is None or maximum is None or maximum <= 0:
                    continue
                qkey = (str(item.get("question_id") or ""), str(item.get("subpart_no") or ""),
                        str(item.get("sort_order") or ""))
                if qkey in seen:
                    continue
                seen.add(qkey)
                qresult = qresults_by_id.get(str(item.get("question_id"))) or {}
                qwen = qresult.get("qwen") if isinstance(qresult.get("qwen"), dict) else {}
                scorer_model = qresult.get("scoring_model") or qwen.get("scoring_model") or runtime_model
                if qresult.get("scoring_model"):
                    model_source = "question_result.scoring_model"
                elif qwen.get("scoring_model"):
                    model_source = "question_result.qwen.scoring_model"
                elif runtime_model != "unknown":
                    model_source = "result.model_runtime"
                else:
                    model_source = "unknown"
                scorer_model = str(scorer_model)[:80]
                yield {
                    "assignment": int(row["assignment_id"]),
                    "submission_id": int(row["submission_id"]),
                    "model": scorer_model,
                    "runtime_model": runtime_model,
                    "model_source": model_source,
                    "problem": str(item.get("problem_no") or "unknown")[:100],
                    "split": "unassigned",
                    "candidate": candidate,
                    "confirmed": confirmed,
                    "maximum": maximum,
                }
    finally:
        conn.close()


def metrics(rows):
    rows = list(rows)
    if not rows:
        return {"n": 0}
    deltas = [r["candidate"] - r["confirmed"] for r in rows]
    abs_errors = [abs(x) for x in deltas]
    return {
        "n": len(rows),
        "submission_count": len({r["submission_id"] for r in rows}),
        "exact_match_rate": round(sum(x == 0 for x in deltas) / len(rows), 4),
        "within_1_point_rate": round(sum(x <= 1 for x in abs_errors) / len(rows), 4),
        "within_2_points_rate": round(sum(x <= 2 for x in abs_errors) / len(rows), 4),
        "mae_points": round(mean(abs_errors), 4),
        "mae_percent_of_max": round(mean(e / r["maximum"] for e, r in zip(abs_errors, rows)), 4),
        "bias_candidate_minus_teacher": round(mean(deltas), 4),
        "under_scored": sum(x < 0 for x in deltas),
        "over_scored": sum(x > 0 for x in deltas),
        "max_absolute_error": round(max(abs_errors), 4),
    }


def evaluate(db_path: Path, seed: str, holdout_percent: int):
    rows = list(load_rows(db_path))
    # Freeze whole submissions within each scorer/assignment cohort. Guarantee
    # a holdout submission whenever a cohort has at least two submissions.
    cohorts = defaultdict(set)
    for row in rows:
        cohorts[(row["model"], row["assignment"])].add(row["submission_id"])
    holdout_ids = set()
    for (model, assignment), submission_ids in cohorts.items():
        ordered = sorted(submission_ids, key=lambda sid: _holdout_rank(seed, assignment, model, sid))
        if len(ordered) >= 2:
            n_holdout = min(len(ordered) - 1, max(1, math.ceil(len(ordered) * holdout_percent / 100)))
            holdout_ids.update((model, assignment, sid) for sid in ordered[:n_holdout])
    for row in rows:
        row["split"] = "holdout" if (row["model"], row["assignment"], row["submission_id"]) in holdout_ids else "development"
    out = {
        "dataset": "teacher_confirmed_candidate_scores",
        "db_path": str(db_path),
        "split": {"method": "stable_ranked_holdout_by_model_assignment_and_submission", "seed": seed,
                  "holdout_percent": holdout_percent},
        "privacy": "aggregate metrics only; no student identifiers or answer text",
        "overall": metrics(rows),
        "splits": {},
        "by_scorer_model": {},
        "by_scorer_model_split": {},
        "by_scorer_model_assignment": {},
        "by_runtime_model": {},
        "by_model_attribution_source": {},
        "by_assignment": {},
        "by_question": {},
    }
    for split in ("development", "holdout"):
        out["splits"][split] = metrics(r for r in rows if r["split"] == split)
    for key, target in (("model", "by_scorer_model"), ("assignment", "by_assignment"),
                        ("problem", "by_question"), ("runtime_model", "by_runtime_model"),
                        ("model_source", "by_model_attribution_source")):
        groups = defaultdict(list)
        for row in rows:
            groups[row[key]].append(row)
        out[target] = {str(name): metrics(group) for name, group in sorted(groups.items(), key=lambda x: str(x[0]))}
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["model"], row["split"])].append(row)
    out["by_scorer_model_split"] = {
        f"{model} / {split}": metrics(group)
        for (model, split), group in sorted(grouped.items())
    }
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["model"], row["assignment"])].append(row)
    out["by_scorer_model_assignment"] = {
        f"{model} / assignment {assignment}": metrics(group)
        for (model, assignment), group in sorted(grouped.items())
    }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed", default="teacher-feedback-v1")
    parser.add_argument("--holdout-percent", type=int, default=20)
    parser.add_argument("--summary", action="store_true", help="print high-level metrics only")
    args = parser.parse_args()
    if not 1 <= args.holdout_percent <= 50:
        parser.error("--holdout-percent must be between 1 and 50")
    report = evaluate(args.db, args.seed, args.holdout_percent)
    if args.summary:
        keys = ("dataset", "split", "overall", "splits", "by_scorer_model",
                "by_scorer_model_split", "by_scorer_model_assignment",
                "by_runtime_model", "by_model_attribution_source")
        report = {key: report[key] for key in keys}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
