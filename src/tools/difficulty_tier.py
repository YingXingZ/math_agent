# -*- coding: utf-8 -*-
"""
难度分层引擎 —— 将题库题目自动分为三层

三层定义（对应构想文档 2(3)）：
  basic      基础训练 —— 直接公式代入、概念辨析
  medium     综合提高 —— 需组合多个知识点、中等变换
  advanced   拓展挑战 —— 证明题、多步推导、灵活应用

分类策略：
  1. 证明题自动归入 advanced（或至少 medium）
  2. 同一节内按题号位置分位（前30%=basic / 中40%=medium / 后30%=advanced）
  3. 知识点映射：涉及 ε-δ / 夹逼 / 单调有界 → advanced
  4. 子题（如 6(1)~6(12)）按序号比例分层
"""
from __future__ import annotations
import sqlite3, os, json, sys
from typing import Dict, List, Optional, Tuple

DB = os.path.join(os.path.dirname(__file__), "api.db")

# 知识点到默认难度层的映射
KP_TIER_HINT: Dict[str, str] = {
    # 基础层知识点
    "limit.sequence": "basic",
    "limit.function": "basic",
    "limit.def": "basic",
    "continuity.def": "basic",
    "continuity.elem": "basic",

    # 提高层知识点
    "limit.four_ops": "medium",
    "limit.comparison": "medium",
    "limit.two_importants": "medium",
    "limit.inf_small": "medium",
    "limit.order": "medium",
    "continuity.ops": "medium",

    # 挑战层知识点
    "limit.squeeze": "advanced",
    "limit.monotone": "advanced",
    "limit.inf_large": "advanced",
    "continuity.closed_interval": "advanced",
    "continuity.zero_point": "advanced",
}

TIER_NAMES = {"basic": "基础训练", "medium": "综合提高", "advanced": "拓展挑战"}
TIER_DIFFICULTY = {"basic": 1, "medium": 3, "advanced": 5}


def ensure_tier_column():
    """给 problems 表添加 tier 列（幂等）"""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE problems ADD COLUMN tier TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    conn.close()


def classify_problems(dry_run: bool = False) -> List[dict]:
    """
    对所有题目执行难度分层，返回每道题的分层结果。
    dry_run=True 时不写库，仅返回分析。
    """
    ensure_tier_column()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 取出所有题目，按章节分组
    rows = cur.execute("""
        SELECT p.id, p.section_id, s.section_no, p.problem_no, p.sub_no,
               p.ptype, p.knowledge_pts, p.content_text
        FROM problems p JOIN sections s ON s.id = p.section_id
        ORDER BY s.section_no, CAST(p.problem_no AS INTEGER),
                 CASE WHEN p.sub_no IS NULL THEN 0 ELSE CAST(REPLACE(REPLACE(p.sub_no,'(',''),')','') AS INTEGER) END
    """).fetchall()

    # 按 section 分组
    sections: Dict[str, list] = {}
    for r in rows:
        sn = r["section_no"]
        sections.setdefault(sn, []).append(dict(r))

    results = []
    for sn, probs in sorted(sections.items()):
        n = len(probs)
        # 按序号排序（带子题的处理）
        ordered = sorted(probs, key=_sort_key)

        for i, p in enumerate(ordered):
            tier = _classify_one(p, i, n)
            results.append({"id": p["id"], "section_no": sn,
                            "problem_no": p["problem_no"],
                            "sub_no": p["sub_no"],
                            "tier": tier, "tier_name": TIER_NAMES[tier],
                            "difficulty": TIER_DIFFICULTY[tier]})

            if not dry_run:
                cur.execute(
                    "UPDATE problems SET tier=?, difficulty=? WHERE id=?",
                    (tier, TIER_DIFFICULTY[tier], p["id"]))

    conn.commit()
    conn.close()

    summary = {}
    for r in results:
        summary.setdefault(r["tier_name"], 0)
        summary[r["tier_name"]] += 1

    print(f"分层完成：共 {len(results)} 题")
    for t, c in summary.items():
        print(f"  {t}: {c} 题")
    return results


def _sort_key(p: dict) -> tuple:
    """题目排序键：主题号 → 子题号"""
    try:
        main = int(p["problem_no"])
    except (ValueError, TypeError):
        main = 0
    sub = 0
    if p.get("sub_no"):
        try:
            sub = int(p["sub_no"].strip("()"))
        except (ValueError, TypeError):
            pass
    return (main, sub)


def _classify_one(p: dict, idx: int, total: int) -> str:
    """对单道题判定难度层"""
    kp_raw = (p.get("knowledge_pts") or "").strip()
    kps = [k.strip() for k in kp_raw.split(",") if k.strip()]
    ptype = (p.get("ptype") or "calc").strip()
    content = (p.get("content_text") or "").strip()

    # 规则1：证明题一律至少 medium，多数 advanced
    if ptype == "proof":
        # 如果证明题在节内靠前、内容短 → medium，其余 → advanced
        ratio = idx / max(total, 1)
        return "medium" if ratio < 0.3 else "advanced"

    # 规则2：知识点驱动
    # 如果包含任何 advanced 知识点 → advanced
    if any(KP_TIER_HINT.get(kp) == "advanced" for kp in kps):
        return "advanced"
    # 如果所有知识点都是 basic → basic
    if kps and all(KP_TIER_HINT.get(kp) == "basic" for kp in kps):
        return "basic"

    # 规则3：位置分位
    ratio = idx / max(total, 1)
    if ratio < 0.30:
        return "basic"
    elif ratio < 0.70:
        return "medium"
    else:
        return "advanced"


def select_homework_problems(section_no: Optional[str] = None,
                             section_nos: Optional[List[str]] = None,
                             counts: Optional[dict] = None
                             ) -> List[dict]:
    """
    按构想 2(5)：从指定章节选题 → 基础 3 + 提高 2 + 拓展 1（共 6 题）
    counts 可覆盖：{"basic": 3, "medium": 2, "advanced": 1}
    如果某层题目不足，从相邻层补足。
    支持单章节（section_no）或多章节（section_nos）
    """
    if counts is None:
        counts = {"basic": 3, "medium": 2, "advanced": 1}

    sections = []
    if section_nos:
        sections = list(section_nos)
    elif section_no:
        sections = [section_no]
    if not sections:
        return []

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    placeholders = ",".join("?" * len(sections))

    selected = []
    for tier, want in counts.items():
        rows = cur.execute(f"""
            SELECT p.id, p.problem_no, p.sub_no, p.tier, p.difficulty,
                   p.crop_image_path, p.content_text, p.knowledge_pts,
                   s.section_no
            FROM problems p JOIN sections s ON s.id = p.section_id
            WHERE s.section_no IN ({placeholders}) AND p.tier = ?
            ORDER BY s.section_no, CAST(p.problem_no AS INTEGER),
                     CASE WHEN p.sub_no IS NULL THEN 0
                          ELSE CAST(REPLACE(REPLACE(p.sub_no,'(',''),')','') AS INTEGER) END
        """, sections + [tier]).fetchall()

        # 每个主问题只取一道（优先取父问题，无子题的）
        seen_main = set()
        pool = []
        for r in rows:
            key = (r["section_no"], r["problem_no"])
            if key in seen_main:
                continue
            seen_main.add(key)
            pool.append(dict(r))

        taken = pool[:want]
        selected.extend(taken)

        # 不足时从下一层补
        if len(taken) < want and tier != "advanced":
            shortage = want - len(taken)
            next_tier = "medium" if tier == "basic" else "advanced"
            fill_rows = cur.execute(f"""
                SELECT p.id, p.problem_no, p.sub_no, p.tier, p.difficulty,
                       p.crop_image_path, p.content_text, p.knowledge_pts,
                       s.section_no
                FROM problems p JOIN sections s ON s.id = p.section_id
                WHERE s.section_no IN ({placeholders}) AND p.tier = ? AND p.id NOT IN (
                    SELECT id FROM problems WHERE id IN ({{}})
                )
                ORDER BY s.section_no, CAST(p.problem_no AS INTEGER)
                LIMIT ?
            """.format(",".join("?" * len(selected))),
                sections + [next_tier] +
                [s["id"] for s in selected] + [shortage]
            ).fetchall()
            selected.extend([dict(r) for r in fill_rows])

    conn.close()
    return selected


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="仅分析不写库")
    ap.add_argument("--select", type=str, help="选题示例，如 1.3")
    args = ap.parse_args()

    if args.select:
        probs = select_homework_problems(args.select)
        print(f"\n{args.select} 节作业选题 ({len(probs)} 题)：")
        for p in probs:
            no = p["problem_no"] + (f"({p['sub_no']})" if p.get("sub_no") else "")
            print(f"  [{TIER_NAMES.get(p['tier'],p['tier'])}] {no}")
    else:
        classify_problems(dry_run=args.dry)
