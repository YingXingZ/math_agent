# -*- coding: utf-8 -*-
"""从 IMA 知识库「高数答案OCR」的答案书 OCR 文本回收 8014 缺失的
std_answer(答案) 与 content_text(题干)。

解析策略：
- 两本答案书(上/下册) OCR 为 JSON 包裹的长文本，结构为
  `习题 X.Y` 块 → 题号 `1.` `2.` ... → 子题 `(1)(2)(3)` → 解答 `解 (k)`/`证明`。
- 每个 习题 X.Y 块内，按 8014 该小节已知的 problem_no 列表定位各题切片
  （避免解答内部的 "1. 2." 枚举造成误切分）。
- 题干 = 切片中首个 `解`/`证明(非冒号)` 之前；解答 = 其后。
- 解答按 `(k)` 拆子题；题干按 `(k)` 拆子题干（仅用于 children-only 小题补题干）。
- 仅回填当前为空的字段；不覆盖已有值。标记 answer_status='recovered'。

默认 DRY-RUN（只统计会改动多少行）；加 --apply 才真正写库。
"""
import os, sys, re, json, sqlite3, shutil
from datetime import datetime

SRC_DB = r"D:\My File\大四\高数教材答案\api.workbench.db"
OCR = {
    "upper": r"C:\Users\YXZ\.workbuddy\projects\d-workbuddy-2026-08-06-15-31-48\b0eda85c-676e-485f-b89d-6d86491cefe5\tool-results\mcp-connector-proxy-ima-mcp_fetch_media_content-1786851511508-b50ce8.txt",
    "lower": r"C:\Users\YXZ\.workbuddy\projects\d-workbuddy-2026-08-06-15-31-48\b0eda85c-676e-485f-b89d-6d86491cefe5\tool-results\mcp-connector-proxy-ima-mcp_fetch_media_content-1786851633415-8d4a4b.txt",
}

# 噪声：页眉/页码/模块标题
NOISE_RE = re.compile(r'^\s*(?:[一二三四五六七八九十]+、习题解答|习题解答|\d{1,4})\s*$')
PAGE_NOISE_RE = re.compile(r'\d{2,4}')  # 页码数字单独成行
BLACKSQ_RE = re.compile(r'[\u25a0\u25a1]')  # ■ □

def load_ocr(path):
    raw = open(path, encoding='utf-8', errors='replace').read()
    try:
        return json.loads(raw).get('content') or raw
    except Exception:
        return raw

def clean_noise(text):
    out = []
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        if NOISE_RE.match(s):
            continue
        if PAGE_NOISE_RE.fullmatch(s):
            continue
        out.append(s)
    t = '\n'.join(out)
    t = BLACKSQ_RE.sub('', t)
    return t.strip()

def split_subparts(text):
    """按 (1)(2)(3) 拆子段，返回 { '1':..., '2':... }（含该子题标记后的文本直到下一子题）。
    仅认真正子题标记：'(k)' 前为边界(行首/空白/中文标点/全角左括)，且 k 属于 1..12（排除 f(0) 等误判）。"""
    res = {}
    for m in re.finditer(r'(?<![A-Za-z0-9\u4e00-\u9fff])\((\d{1,2})\)', text):
        k = m.group(1)
        if k == '0' or int(k) > 12:
            continue
        # 位置边界检查：'(' 前应为 空白/行首/中文标点/全角左括
        pre = text[m.start() - 1] if m.start() > 0 else '\n'
        if pre not in (' ', '\n', '\t', '（', '，', '。', '、', '；', '：', '(', ''):

            continue
        # 找下一子题标记
        nxt = re.search(r'(?<![A-Za-z0-9\u4e00-\u9fff])\((\d{1,2})\)', text[m.end():])
        end = m.end() + nxt.start() if nxt else len(text)
        seg = text[m.end():end].strip()
        if seg:
            res.setdefault(k, seg)
    return res

GARBAGE_THRESH = 0.006
def garbage_score(t):
    """全角拉丁字母/全角波浪号/单引号/反斜杠/私用区 占比；越高越可能是 OCR 乱码。"""
    if not t:
        return 0.0
    g = 0
    for ch in t:
        o = ord(ch)
        if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            g += 1
        elif o in (0xFF5E, 0xFF07, 0xFF3C, 0xFF5C):
            g += 1
        elif 0xE000 <= o <= 0xF8FF:
            g += 1
    return g / len(t)

def is_clean(t):
    """仅用于「严重损坏」门禁：全角拉丁/punct/私用区 密度 > 0.006 判乱码。
    刻意只抓全角拉丁类严重乱码（与 bulk_sync 损坏门禁一致），不碰「含个别中文杂字的长答案」，
    避免误杀主体干净的长解答。中文 mojibake 由 gap_analysis 的 is_severe() 单独处理。"""
    if not t or len(t.strip()) < 2:
        return False
    if '\ufffd' in t:
        return False
    return garbage_score(t) <= GARBAGE_THRESH

def find_solution_marker(span):
    """返回解答起始偏移；优先 `解`，否则行首 `证明`(非冒号)。未找到返回 -1。"""
    m = re.search(r'解\s*\(?\d*\)?', span)
    pos = m.start() if m else -1
    # 证明(非冒号)
    mp = re.search(r'证明\s*(?![:：])', span)
    pos_p = mp.start() if mp else -1
    candidates = [p for p in (pos, pos_p) if p != -1]
    return min(candidates) if candidates else -1

def parse_section_block(block):
    """block: 习题 X.Y 之后的文本（到下一块前）。返回 { problem_no(str): {'stem':, 'solution':, 'subs':{}, 'stem_subs':{}} }"""
    problems = {}
    # 找所有行首题号 N.
    pm = list(re.finditer(r'(?m)^\s*(\d+)\.\s', block))
    if not pm:
        return problems
    for i, m in enumerate(pm):
        num = m.group(1)
        start = m.start()
        end = pm[i + 1].start() if i + 1 < len(pm) else len(block)
        ptext = block[start:end]
        sm = find_solution_marker(ptext)
        if sm != -1:
            stem = ptext[:sm].strip()
            solution = ptext[sm:].strip()
        else:
            stem = ptext.strip()
            solution = ""
        stem = clean_noise(stem)
        solution = clean_noise(solution)
        subs = split_subparts(solution)
        stem_subs = split_subparts(stem)
        problems[num] = {'stem': stem, 'solution': solution, 'subs': subs, 'stem_subs': stem_subs}
    return problems

def build_book_index():
    """返回 { section_no: {problem_no: {...}} }，上册覆盖<=4，下册>=5。"""
    idx = {}
    # 上册
    up = load_ocr(OCR['upper'])
    idx.update(_index_text(up, max_chapter=4))
    # 下册
    lo = load_ocr(OCR['lower'])
    idx.update(_index_text(lo, min_chapter=5))
    return idx

def _index_text(text, max_chapter=None, min_chapter=None):
    # 切成 习题 X.Y 块
    sec_marks = list(re.finditer(r'习题\s*(\d+\.\d+)', text))
    out = {}
    for i, m in enumerate(sec_marks):
        sno = m.group(1)
        ch = int(sno.split('.')[0])
        if max_chapter is not None and ch > max_chapter:
            continue
        if min_chapter is not None and ch < min_chapter:
            continue
        s = m.end()
        e = sec_marks[i + 1].start() if i + 1 < len(sec_marks) else len(text)
        block = text[s:e]
        out[sno] = parse_section_block(block)
    return out

def norm_sub(sub_no):
    if not sub_no:
        return None
    m = re.match(r'\(?(\d{1,2})\)?', str(sub_no).strip())
    return m.group(1) if m else str(sub_no).strip('()')

def main():
    realign = '--realign' in sys.argv
    fix = '--fix' in sys.argv
    apply = '--apply' in sys.argv or realign or fix
    mode = 'fix(覆盖答案+仅补缺题干)' if fix else ('realign(覆盖对齐,含题干)' if realign else 'fill(仅补缺)')
    con = sqlite3.connect(SRC_DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""SELECT s.section_no, t.name AS tname, p.id, p.problem_no, p.sub_no,
                          p.content_text, p.std_answer, p.full_solution, p.answer_status
                   FROM problems p JOIN sections s ON s.id=p.section_id
                   JOIN textbooks t ON t.id=s.textbook_id""")
    rows = cur.fetchall()
    print(f"[src] problems 总行数: {len(rows)}  | 模式: {mode}")

    book = build_book_index()
    print(f"[ocr] 索引到小节数: {len(book)} (上册 {sum(1 for k in book if int(k.split('.')[0])<=4)} / 下册 {sum(1 for k in book if int(k.split('.')[0])>=5)})")

    updates = []
    fill_ans = fill_stem = 0
    skip_ans_clean = skip_stem_clean = 0        # realign: 答案书有但当前字段已ok(无需改)
    skipped_corrupt_ans = skipped_corrupt_stem = 0
    sec_not_in_book = prob_not_found = 0
    final_both = set()
    corrupt_sections = set()
    section_missing_ans_samples = {}

    by_section = {}
    for r in rows:
        by_section.setdefault(r['section_no'], []).append(r)

    for sno, rlist in by_section.items():
        is_route2 = any('Route2' in (r['tname'] or '') for r in rlist)
        book_sec = book.get(sno)
        for r in rlist:
            pno = str(r['problem_no'])
            sub = norm_sub(r['sub_no'])
            prob = book_sec.get(pno) if book_sec else None
            if prob is None:
                if not (r['std_answer'] and r['std_answer'].strip()) or not (r['content_text'] and r['content_text'].strip()):
                    prob_not_found += 1
                    section_missing_ans_samples.setdefault(sno, []).append(pno)
                continue
            has_ans = bool(r['std_answer'] and r['std_answer'].strip())
            has_stem = bool(r['content_text'] and r['content_text'].strip())
            has_parent = any(x['problem_no'] == r['problem_no'] and not x['sub_no'] for x in rlist)

            # 目标 题干/答案（来自答案书）
            if sub is None:
                tgt_stem = prob['stem']
                tgt_ans = prob['solution']
            else:
                if has_parent:
                    tgt_stem = None                       # 子题不覆盖题干，避免 Agent 重复
                    tgt_ans = prob['subs'].get(sub) or prob['solution']
                else:
                    tgt_stem = prob['stem_subs'].get(sub) or prob['stem']
                    tgt_ans = prob['subs'].get(sub) or prob['solution']

            got_ans = got_stem = False
            # ---- 答案：realign/fix 均覆盖(修正错位)；fill 仅补缺 ----
            if (realign or fix) and not is_route2:
                if tgt_ans and len(tgt_ans.strip()) >= 2:
                    if is_clean(tgt_ans):
                        updates.append((r['id'], 'ans', tgt_ans.strip()))
                        fill_ans += 1; got_ans = True
                    else:
                        skipped_corrupt_ans += 1; corrupt_sections.add(sno)
            elif not has_ans and not is_route2:
                if tgt_ans and len(tgt_ans.strip()) >= 2:
                    if is_clean(tgt_ans):
                        updates.append((r['id'], 'ans', tgt_ans.strip()))
                        fill_ans += 1; got_ans = True
                    else:
                        skipped_corrupt_ans += 1; corrupt_sections.add(sno)
            # ---- 题干：仅 realign 覆盖；fix/fill 仅补缺(保留教材干净题干) ----
            if realign and not is_route2:
                if tgt_stem and len(tgt_stem.strip()) >= 4:
                    if is_clean(tgt_stem):
                        updates.append((r['id'], 'stem', tgt_stem.strip()))
                        fill_stem += 1; got_stem = True
                    else:
                        skipped_corrupt_stem += 1; corrupt_sections.add(sno)
            elif not has_stem and not is_route2:
                if tgt_stem and len(tgt_stem.strip()) >= 4:
                    if is_clean(tgt_stem):
                        updates.append((r['id'], 'stem', tgt_stem.strip()))
                        fill_stem += 1; got_stem = True
                    else:
                        skipped_corrupt_stem += 1; corrupt_sections.add(sno)

            if has_ans or got_ans:
                if has_stem or got_stem:
                    final_both.add(r['id'])

    print(f"\n[plan] 待改答案: {fill_ans} 行 (OCR损坏跳过 {skipped_corrupt_ans})")
    print(f"[plan] 待改题干: {fill_stem} 行 (OCR损坏跳过 {skipped_corrupt_stem})")
    print(f"[plan] 合计将改动: {len(updates)} 处")
    print(f"[project] 改后 题干+答案齐全预计: {len(final_both)} / 477")

    if not apply:
        print("\n*** DRY-RUN 完成，未写库。加 --apply / --realign 执行。 ***")
        return

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    tag = "fix" if fix else ("realign" if realign else "recover")
    bak = SRC_DB + f'.bak_{tag}_{ts}'
    shutil.copy(SRC_DB, bak)
    print(f"\n[backup] {bak}")

    ca = cb = 0
    for pid, field, val in updates:
        if field == 'ans':
            cur.execute("UPDATE problems SET std_answer=?, full_solution=?, answer_status='recovered' WHERE id=?",
                        (val, val, pid))
            ca += 1
        else:
            cur.execute("UPDATE problems SET content_text=? WHERE id=?", (val, pid))
            cb += 1
    con.commit()
    print(f"[apply] 写入答案 {ca} 行，题干 {cb} 行。")
    cur.execute("SELECT COUNT(*) FROM problems WHERE content_text<>'' AND std_answer<>''")
    print(f"[after] 题干+答案齐全: {cur.fetchone()[0]} / 477")

if __name__ == '__main__':
    main()
