"""
从 IMA 知识库提取的答案册 OCR 内容中，批量解析各章节习题答案，
并通过 API 填充到数据库中。

策略：
1. 读取已 fetch 的答案册 OCR 内容
2. 定位每个"习题 X.Y"段落
3. 提取"解"标记后的答案内容
4. 与 DB 中的 problem 记录匹配
5. 通过 PUT /api/problems/{pid}/answer 写入
"""
import json
import re
import sqlite3
import requests
import sys

API_BASE = "http://127.0.0.1:8011/api"
OCR_FILE = r"C:\Users\YXZ\.workbuddy\projects\d-workbuddy-2026-08-06-15-31-48\d7ed0532-5a8f-471b-a92a-5ce05bfb0178\tool-results\mcp-connector-proxy-ima-mcp_fetch_media_content-1786105447622-fdd493.txt"
DB_FILE = "D:/workbuddy/2026-08-06-15-31-48/api.db"


def load_ocr_content():
    with open(OCR_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['content']


def get_db_sections():
    """返回 {section_no: section_id} 映射"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, section_no, title FROM sections ORDER BY section_no")
    rows = cur.fetchall()
    conn.close()
    return {r[1]: {'id': r[0], 'title': r[2]} for r in rows}


def get_db_problems(section_id):
    """获取某 section 下所有 problems"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, problem_no, sub_no, ptype, content_text FROM problems WHERE section_id=? ORDER BY CAST(problem_no AS INTEGER), sub_no",
        (section_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def extract_sections_from_ocr(content):
    """从 OCR 内容中提取各习题段落 (section_no -> answer_text)"""
    # 匹配"习题 X.Y"后面跟的内容，直到下一个"习题"或章节结束
    pattern = re.compile(r'习题\s*(\d+\.\d+)\s*\n')
    
    matches = list(pattern.finditer(content))
    sections = {}
    
    for i, m in enumerate(matches):
        section_no = m.group(1)
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(content)
        answer_text = content[start:end].strip()
        sections[section_no] = answer_text
    
    return sections


def extract_problem_answers(section_text):
    """
    从习题解答段落中提取每道题的答案。
    返回: [(题号, 答案文本), ...]
    
    解析策略:
    - 找到"解"标记作为答案起点
    - 子题用 (1)(2)... 或 (i)(ii)... 标记
    - 证明题用"证明"标记
    """
    answers = []
    
    # 按 "解 " 或 "\n解 " 分割（每个题目的解答以"解"开头）
    # 但要注意有些题是以"证明"开头的
    # 先清理文本
    text = section_text.strip()
    
    # 策略: 找到所有题目编号模式: 数字后可能跟句号/全角句号/OCR乱码"，然后跟中文或字母
    # OCR中常见的变形: "1.", "1．", "1 ", "1ο", "1 求", "1 计算" 等
    problem_pattern = re.compile(r'(?:^|\n)\s*(\d+)\s*(?:[.．。ο]\s*)?(?=[\u4e00-\u9fffA-Za-z])')
    proof_pattern = re.compile(r'(?:^|\n)\s*证明\s+')
    
    problems = list(problem_pattern.finditer(text))
    
    for i, pm in enumerate(problems):
        pno = pm.group(1)
        start = pm.end()
        end = problems[i+1].start() if i+1 < len(problems) else len(text)
        problem_text = text[start:end].strip()
        
        # 提取答案: 找到"解"或"证明"后面的内容
        # 从"解"开始提取，忽略前面的题目重述
        solution_match = re.search(r'(?:解|证明)\s*(.*?)$', problem_text, re.DOTALL)
        
        if solution_match:
            answer = solution_match.group(1).strip()
        else:
            # 找不到"解"标记，使用全部内容
            answer = problem_text
        
        # 截取合理长度
        if len(answer) > 800:
            answer = answer[:800] + "..."
        
        answers.append((pno, answer))
    
    return answers


def build_concise_answer(problem_text, ptype):
    """
    构建简洁的最终答案（用于自动批改匹配）。
    从冗长的解题过程中提取最终结果。
    """
    # 先提取"解"后面的部分（去掉题目重述）
    solution_match = re.search(r'(?:解|证明)\s*(.*?)$', problem_text, re.DOTALL)
    if solution_match:
        solution_text = solution_match.group(1).strip()
    else:
        solution_text = problem_text
    
    if ptype == 'proof':
        # 证明题: 提取关键结论关键词
        keywords = []
        for kw in ['有界', '无界', '奇函数', '偶函数', '收敛', '发散', '连续', '可导',
                   '极限', '单调', '上界', '下界', '充要条件', '充分', '必要',
                   '线性无关', '线性相关', '通解', '特解', '相等', '相同',
                   '一致连续', '不一致连续']:
            if kw in solution_text[:800]:
                keywords.append(kw)
        if keywords:
            return '证明结论: ' + '; '.join(keywords[:5])
        return solution_text[:200]
    
    # 计算题: 多策略提取最终数值答案，按优先级尝试
    
    # 策略1: 找最后一句包含结论性词语的句子
    conclusion_patterns = [
        r'(?:故|所以|因此|于是|得|即|有|则)\s*[，,]?\s*(.+?)[。.\n]',
        r'(?:定义域是|值域是|定义域为|值域为)\s*(.+?)[。.\n]',
        r'(?:极限为|极限是|极限等于|极限=|极限值为)\s*(.+?)[。.\n]',
        r'(?:通解为|通解是|通解:|通解=)\s*(.+?)[。.\n]',
        r'(?:收敛于|发散|收敛)\s*(.+?)[。.\n]',
        r'(?:极值为|极值是|极大值|极小值)\s*(.+?)[。.\n]',
        r'(?:切线方程为|法线方程为|切线方程是)\s*(.+?)[。.\n]',
    ]
    
    for pat in conclusion_patterns:
        matches = re.findall(pat, solution_text)
        if matches:
            # 取最后一个匹配（通常是最终结论）
            last = matches[-1].strip()
            # 清理数学符号
            last = re.sub(r'\s+', ' ', last)
            if len(last) > 10:  # 合理的答案长度
                return last[:250]
    
    # 策略2: 找$...$中的最后一个数学表达式
    math_exprs = re.findall(r'\$([^$]+)\$', solution_text)
    if math_exprs:
        # 取包含数字的表达式，偏后面的
        numeric_exprs = [e for e in math_exprs if re.search(r'\d', e)]
        if numeric_exprs:
            return numeric_exprs[-1].strip()[:200]
        return math_exprs[-1].strip()[:200]
    
    # 策略3: 找最后一行非空内容
    lines = [l.strip() for l in solution_text.split('\n') if l.strip() and len(l.strip()) > 5]
    if lines:
        return lines[-1][:200]
    
    # Fallback
    return solution_text[:200]


def update_answer_via_api(pid, std_answer, grading_steps=None, ptype=None):
    """通过 API 更新题目答案"""
    payload = {"std_answer": std_answer}
    if grading_steps:
        payload["grading_steps"] = grading_steps
    if ptype:
        payload["ptype"] = ptype
    
    try:
        resp = requests.put(f"{API_BASE}/problems/{pid}/answer", json=payload, timeout=10)
        if resp.status_code == 200:
            return True, resp.json()
        else:
            return False, resp.text
    except Exception as e:
        return False, str(e)


def main():
    print("=" * 60)
    print("从 IMA 答案册 OCR 批量填充标准答案")
    print("=" * 60)
    
    # 1. 加载 OCR 内容
    print("\n[1/5] 加载 OCR 内容...")
    content = load_ocr_content()
    print(f"  内容长度: {len(content)} 字符")
    
    # 2. 提取各章节
    print("\n[2/5] 提取各章节答案...")
    sections = extract_sections_from_ocr(content)
    print(f"  找到 {len(sections)} 个习题章节")
    
    # 3. 获取 DB 信息
    print("\n[3/5] 获取数据库信息...")
    db_sections = get_db_sections()
    print(f"  数据库中有 {len(db_sections)} 个 sections")
    
    # 4. 匹配并填充
    print("\n[4/5] 匹配并填充答案...")
    
    total_updated = 0
    skipped = 0
    failed = 0
    
    for section_no, sec_info in db_sections.items():
        if section_no not in sections:
            print(f"  {section_no}: OCR 中无此章节")
            skipped += 1
            continue
        
        section_id = sec_info['id']
        section_text = sections[section_no]
        problems = get_db_problems(section_id)
        
        if not problems:
            print(f"  {section_no}: DB 中无题目")
            continue
        
        # 解析该章节答案
        parsed_answers = extract_problem_answers(section_text)
        answer_map = {pno: ans for pno, ans in parsed_answers}
        
        updated_count = 0
        for pid, pno, sub_no, ptype, content_text in problems:
            # 已有答案的跳过
            # 先从解析的答案中查找
            full_answer = answer_map.get(pno, None)
            
            if full_answer:
                concise = build_concise_answer(full_answer, ptype)
                success, result = update_answer_via_api(pid, concise, ptype=ptype)
                if success:
                    updated_count += 1
                else:
                    print(f"    [{section_no} #{pno}] 更新失败: {result}")
            else:
                # 没找到答案
                pass
        
        if updated_count > 0:
            print(f"  {section_no} ({sec_info['title']}): 更新 {updated_count}/{len(problems)} 题")
            total_updated += updated_count
        
    # 5. 验证
    print(f"\n[5/5] 验证结果...")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM problems WHERE std_answer IS NOT NULL AND std_answer != ''")
    answered = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems")
    total = cur.fetchone()[0]
    conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"完成! {answered}/{total} 道题已有答案 ({total_updated} 道题本次更新)")
    print(f"跳过: {skipped} 个章节 (OCR中无对应)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
