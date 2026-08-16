# -*- coding: utf-8 -*-
"""
从 IMA 知识库 OCR 答案册中提取每道题的完整推导过程，
写入 problems.full_solution 字段，供学生端展示完整解答。

设计原则：
- 不破坏现有 std_answer（保留给自动批改作简洁匹配用）。
- full_solution 保存从"解"/"证明"开始到该题块结束的原始/清洗文本。
- 大题含多个子题时，子题记录保存对应子题的完整解答段落。
"""
import json
import re
import sqlite3
import sys

DB_FILE = "D:/workbuddy/2026-08-06-15-31-48/api.db"
OCR_FILE = r"C:\Users\YXZ\.workbuddy\projects\d-workbuddy-2026-08-06-15-31-48\d7ed0532-5a8f-471b-a92a-5ce05bfb0178\tool-results\mcp-connector-proxy-ima-mcp_fetch_media_content-1786105447622-fdd493.txt"

PAGE_HEADER_RE = re.compile(r'\n\s*五、习题解答\s*\n|\n\s*第[一二三四五六七八九十]+章[^\n]*\n|\n\s*\d{3}\s*\n')


def load_ocr():
    with open(OCR_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)['content']


def split_sections(content):
    """把整本答案册按"习题 X.Y"切分成章节"""
    pattern = re.compile(r'习题\s*(\d+\.\d+)\s*\n')
    matches = list(pattern.finditer(content))
    sections = {}
    for i, m in enumerate(matches):
        sno = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[sno] = content[start:end]
    return sections


def clean_solution_text(text):
    """删除页码、章节标题等多余内容，压缩连续空行"""
    text = PAGE_HEADER_RE.sub('\n', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def extract_problem_block(section_text, problem_no):
    """从章节文本中提取某一个大题号的内容块（从题号开始到下一题号）"""
    escaped = re.escape(str(problem_no))
    start_pat = re.compile(r'(?:^|\n)\s*' + escaped + r'\s*[\.．。]\s+(?=[^\d])')
    matches = list(start_pat.finditer(section_text))
    if not matches:
        return None
    start = matches[0].end()

    next_pat = re.compile(r'(?:^|\n)\s*(\d+)\s*[\.．。]\s+(?=[^\d])')
    nxt = None
    for m in next_pat.finditer(section_text, start):
        if m.group(1) != str(problem_no):
            nxt = m
            break
    end = nxt.start() if nxt else len(section_text)
    return section_text[start:end]


def split_solution_subs(solution_text):
    """
    把一道大题的完整解答按子题号 (1)(2)... 拆分。
    返回 [(sub_no, sub_solution_text), ...]，sub_no 为 None 表示无子题。
    """
    sol_start = re.search(r'(?:^|\n)\s*(解|证明)\s*', solution_text)
    if sol_start:
        sol_text = solution_text[sol_start.start():]
    else:
        sol_text = solution_text

    sol_text = clean_solution_text(sol_text)
    # 去掉开头的"解""证明"字样，保留后面内容
    sol_text = re.sub(r'^[\s解证明]*', '', sol_text).strip()

    sub_pat = re.compile(r'(?:^|\n)\s*\((\d+)\)\s*')
    subs = list(sub_pat.finditer(sol_text))

    results = []
    if subs:
        for i, m in enumerate(subs):
            sub_no = m.group(1)
            start = m.end()
            end = subs[i + 1].start() if i + 1 < len(subs) else len(sol_text)
            sub_text = sol_text[start:end].strip()
            results.append((sub_no, sub_text))
    else:
        results.append((None, sol_text))
    return results


def build_full_solution(block_text, sub_no=None):
    """从题块中提取完整解答；若指定 sub_no 则返回对应子题完整解答。"""
    if not block_text:
        return ""
    subs = split_solution_subs(block_text)
    if sub_no is None:
        # 大题：把所有子题解答按 (1)... (2)... 格式拼起来
        if len(subs) == 1 and subs[0][0] is None:
            return subs[0][1]
        parts = []
        for sn, st in subs:
            parts.append(f"({sn}) {st}")
        return "\n".join(parts)
    # 子题：找对应子题号
    for sn, st in subs:
        if sn == sub_no:
            return st
    # 没匹配到，返回第一个子题作为兜底
    if subs:
        return subs[0][1]
    return ""


def main():
    content = load_ocr()
    ocr_sections = split_sections(content)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 确认字段存在（兼容旧库）
    try:
        cur.execute("ALTER TABLE problems ADD COLUMN full_solution TEXT")
        conn.commit()
        print("[DB] 已新增 full_solution 字段")
    except Exception:
        pass

    cur.execute("""
        SELECT p.id, p.problem_no, p.sub_no, p.section_id, s.section_no
        FROM problems p JOIN sections s ON s.id=p.section_id
        ORDER BY s.section_no, CAST(p.problem_no AS INTEGER), p.sub_no
    """)
    rows = cur.fetchall()

    updated = 0
    skipped = 0
    for r in rows:
        sno = r["section_no"]
        pno = r["problem_no"]
        sub_no = r["sub_no"]
        pid = r["id"]

        if sno not in ocr_sections:
            skipped += 1
            continue

        block = extract_problem_block(ocr_sections[sno], pno)
        if not block:
            skipped += 1
            continue

        full = build_full_solution(block, sub_no)
        if not full:
            skipped += 1
            continue

        cur.execute("UPDATE problems SET full_solution=? WHERE id=?", (full, pid))
        updated += 1
        if updated % 10 == 0:
            print(f"  已更新 {updated} 题...")

    conn.commit()

    # 统计
    cur.execute("SELECT COUNT(*) FROM problems WHERE full_solution IS NOT NULL AND full_solution != ''")
    with_solution = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems")
    total = cur.fetchone()[0]
    conn.close()

    print(f"\n[完成] 本次更新 {updated} 题，跳过 {skipped} 题")
    print(f"[统计] 共有 {with_solution}/{total} 题已填充完整推导过程")


if __name__ == "__main__":
    main()
