"""Read-only bridge from released, teacher-confirmed homework to next lesson.

No model guess or unconfirmed provisional score enters the class aggregate.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict


def _tags(value: str, chapter: str) -> list[str]:
    tags = [part.strip() for part in re.split(r"[,，、;；|\n]+", value or "") if part.strip()]
    return tags[:5] or ([chapter] if chapter else [])


def build_next_lesson(conn, class_id: int) -> dict:
    klass = conn.execute("SELECT id,name,semester FROM classes WHERE id=?", (class_id,)).fetchone()
    if not klass:
        raise ValueError("班级不存在")
    questions = {row["id"]: dict(row) for row in conn.execute(
        "SELECT id,chapter,knowledge_points,difficulty,content,answer,rubric,review_status,solution_status FROM questions"
    )}
    rows = conn.execute("""SELECT s.id AS submission_id,s.student_no,a.id AS assignment_id,
                                a.title,a.chapter,ge.evidence_json
                         FROM submissions s JOIN assignments a ON a.id=s.assignment_id
                         JOIN grading_experiences ge ON ge.id=(
                            SELECT MAX(id) FROM grading_experiences WHERE submission_id=s.id)
                         WHERE a.class_id=? AND COALESCE(a.is_demo,0)=0
                           AND s.status='graded' AND s.released_at IS NOT NULL
                         ORDER BY s.released_at DESC,s.id DESC LIMIT 500""", (class_id,)).fetchall()
    buckets = defaultdict(lambda: {"students": set(), "low_students": set(),
                                   "attempts": 0, "low": 0,
                                   "earned": 0.0, "possible": 0.0, "chapters": set(),
                                   "examples": []})
    used_submissions = set()
    excluded_items = 0
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for item in evidence.get("results") or []:
            question = questions.get(item.get("question_id"))
            decision = item.get("teacher_decision") or {}
            if not question or not (decision.get("confirmed_at") or decision.get("stable_auto_accept")):
                excluded_items += 1
                continue
            try:
                maximum = float(item.get("max_score") or 0)
                earned = float(decision.get("score"))
            except (TypeError, ValueError):
                excluded_items += 1
                continue
            if maximum <= 0 or not 0 <= earned <= maximum:
                excluded_items += 1
                continue
            used_submissions.add(row["submission_id"])
            for tag in _tags(question["knowledge_points"], question["chapter"]):
                bucket = buckets[tag]
                bucket["students"].add(row["student_no"])
                bucket["attempts"] += 1
                is_low = earned / maximum < .7
                bucket["low"] += is_low
                if is_low:
                    bucket["low_students"].add(row["student_no"])
                bucket["earned"] += earned
                bucket["possible"] += maximum
                bucket["chapters"].add(question["chapter"])
                if earned / maximum < .7 and len(bucket["examples"]) < 3:
                    bucket["examples"].append({"submission_id": row["submission_id"],
                                               "assignment_title": row["title"],
                                               "problem_no": item.get("problem_no") or ""})
    points = []
    for name, bucket in buckets.items():
        student_count = len(bucket["students"])
        low_student_count = len(bucket["low_students"])
        rate = bucket["earned"] / bucket["possible"]
        low_attempt_rate = bucket["low"] / bucket["attempts"]
        low_student_rate = low_student_count / student_count
        sample_sufficient = student_count >= 3 and bucket["attempts"] >= 3
        common = (sample_sufficient and rate < .7
                  and low_student_count >= max(3, math.ceil(student_count * .3)))
        if not sample_sufficient:
            diagnosis = "accumulating"
            diagnosis_label = "数据积累中"
            suggestion = (f"当前仅覆盖 {student_count} 名学生、{bucket['attempts']} 次已确认作答；"
                          "可用于个别反馈，但暂不足以判断班级整体情况。")
        elif common:
            diagnosis = "class_weakness"
            diagnosis_label = "全班共性薄弱"
            suggestion = (f"平均得分率低于 70%，且 {low_student_count} 名学生低于 70%"
                          f"（占 {low_student_rate:.0%}）。建议下节课前回顾关键条件，"
                          "讲解一道典型错题，并安排基础→提高→综合巩固。")
        elif rate >= .85 and low_attempt_rate < .15:
            diagnosis = "mastered"
            diagnosis_label = "整体掌握"
            follow_up = (f"仅对这 {low_student_count} 名学生单独反馈。" if low_student_count
                         else "用一道迁移题代替重复练习。")
            suggestion = (f"平均得分率为 {rate:.1%}，低分作答仅占 {low_attempt_rate:.0%}。"
                          f"无需安排全班复习；{follow_up}")
        elif low_student_count >= 2 and low_student_rate >= .15:
            diagnosis = "targeted_support"
            diagnosis_label = "分层关注"
            suggestion = (f"班级整体平均得分率为 {rate:.1%}，但仍有 {low_student_count} 名学生"
                          f"低于 70%（占 {low_student_rate:.0%}）。无需全班重讲；建议针对这些学生"
                          "安排基础补偿练习，其余学生进入提高或综合练习。")
        else:
            diagnosis = "mostly_mastered"
            diagnosis_label = "基本掌握"
            suggestion = (f"平均得分率为 {rate:.1%}，低分作答占 {low_attempt_rate:.0%}。"
                          "建议用一道课前检测题确认易错条件，不需要整段重讲。")
        chapters = sorted(bucket["chapters"])
        candidates = {level: [] for level in ("基础", "提高", "综合")}
        for q in questions.values():
            if (q["chapter"] not in chapters or q["review_status"] != "published"
                    or q["solution_status"] != "confirmed" or not q["answer"] or not q["rubric"]):
                continue
            if name not in _tags(q["knowledge_points"], q["chapter"]):
                continue
            level = q["difficulty"]
            if level in candidates and len(candidates[level]) < 2:
                candidates[level].append({"question_id": q["id"], "chapter": q["chapter"],
                                          "preview": q["content"][:90]})
        points.append({"knowledge_point": name, "chapters": chapters,
                       "student_count": student_count, "attempt_count": bucket["attempts"],
                       "low_count": bucket["low"], "low_student_count": low_student_count,
                       "low_attempt_rate": round(low_attempt_rate, 3),
                       "low_student_rate": round(low_student_rate, 3),
                       "score_rate": round(rate, 3), "sample_sufficient": sample_sufficient,
                       "diagnosis": diagnosis, "diagnosis_label": diagnosis_label,
                       "common_error": common, "evidence": bucket["examples"],
                       "review_suggestion": suggestion,
                       "exercise_candidates": candidates})
    priority = {"class_weakness": 0, "targeted_support": 1, "mostly_mastered": 2,
                "mastered": 3, "accumulating": 4}
    points.sort(key=lambda p: (priority[p["diagnosis"]], p["score_rate"], -p["student_count"]))
    return {"class_id": class_id, "class_name": klass["name"],
            "source": "已发布且有逐题教师确认的非演示作业",
            "released_submission_count": len(rows), "usable_submission_count": len(used_submissions),
            "excluded_unconfirmed_items": excluded_items,
            "threshold": "全班共性薄弱＝样本不少于 3 人、低分学生占比至少 30%，且平均得分率低于 70%；其他情况给出分层建议",
            "points": points[:12], "draft_only": True}
