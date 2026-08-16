"""
混合策略答案填充：
1. 对习题1.3（有种子答案的），恢复手动种子答案
2. 对其他章节，从 IMA OCR 提取简洁最终答案
"""
import json, re, uuid
import sqlite3
import requests

API_BASE = "http://127.0.0.1:8011/api"
OCR_FILE = r"C:\Users\YXZ\.workbuddy\projects\d-workbuddy-2026-08-06-15-31-48\d7ed0532-5a8f-471b-a92a-5ce05bfb0178\tool-results\mcp-connector-proxy-ima-mcp_fetch_media_content-1786105447622-fdd493.txt"
DB_FILE = "D:/workbuddy/2026-08-06-15-31-48/api.db"


def load_ocr():
    with open(OCR_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)['content']


def update_answer(pid, std_answer, grading_steps=None, ptype=None, answer_weight=None):
    payload = {"std_answer": std_answer}
    if grading_steps:
        payload["grading_steps"] = grading_steps
    if ptype:
        payload["ptype"] = ptype
    if answer_weight:
        payload["answer_weight"] = answer_weight
    try:
        resp = requests.put(f"{API_BASE}/problems/{pid}/answer", json=payload, timeout=10)
        return resp.status_code == 200
    except:
        return False


def restore_seed_answers():
    """恢复习题1.3的种子答案"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    # Get 1.3 problems
    cur.execute('''
        SELECT p.id, p.problem_no, p.sub_no, p.ptype
        FROM problems p JOIN sections s ON s.id=p.section_id
        WHERE s.section_no='1.3'
        ORDER BY CAST(p.problem_no AS INTEGER), p.sub_no
    ''')
    rows = cur.fetchall()
    
    # 第1题：证明题
    proof_steps = json.dumps([
        {"key": "def_eps", "patterns": ["任[给意].*ε", "epsilon", "∀.*ε"], "weight": 2.0, "desc": "由极限定义引入 ε"},
        {"key": "choose_eps", "patterns": ["取.*ε\\s*=", "令.*ε\\s*="], "weight": 2.0, "desc": "取特定 ε（如 |a|/2）"},
        {"key": "exist_N", "patterns": ["存在.*N", "∃.*N", "当.*n\\s*>\\s*N"], "weight": 2.0, "desc": "给出 N 的存在性"},
        {"key": "triangle", "patterns": ["三角不等式"], "weight": 1.5, "desc": "使用三角不等式放缩"},
        {"key": "conclude", "patterns": ["故|因此|从而|得证|证毕"], "weight": 1.0, "desc": "给出结论"},
    ], ensure_ascii=False)
    
    # Build seed answer map: (problem_no, sub_no) -> (answer, ptype, steps, weight)
    seed_map = {
        ('1', None): ('证明收敛数列的有界性/保号性等性质', 'proof', proof_steps, 0.4),
        ('2', None): ('3', 'calc', json.dumps([{"key":"substitute","patterns":["代入","直接代入"],"weight":1.0,"desc":"代入求值"}], ensure_ascii=False), 0.6),
        ('3', None): ('0', 'calc', json.dumps([{"key":"factor","patterns":["因式分解","提公因子"],"weight":1.0,"desc":"因式分解消去零因子"}], ensure_ascii=False), 0.6),
        ('4', None): ('1', 'calc', json.dumps([{"key":"rationalize","patterns":["有理化","共轭"],"weight":1.0,"desc":"有理化"}], ensure_ascii=False), 0.6),
        ('5', None): ('1/2', 'calc', json.dumps([{"key":"simplify","patterns":["化简","同除","提取"],"weight":1.0,"desc":"提取最高次幂"}], ensure_ascii=False), 0.6),
        ('6', '1'): ('-2', 'calc', json.dumps([{"key":"substitute","patterns":["代入","x=1","x→1"],"weight":1.0,"desc":"直接代入 x=1（分母不为零）"}], ensure_ascii=False), 0.6),
        ('6', '4'): ('2/3', 'calc', json.dumps([{"key":"rationalize","patterns":["有理化","共轭","同乘"],"weight":1.5,"desc":"分子分母有理化"},{"key":"simplify","patterns":["约分","化简"],"weight":1.0,"desc":"约去零因子"}], ensure_ascii=False), 0.6),
        ('6', '7'): ('cos(a)', 'calc', json.dumps([{"key":"sum2prod","patterns":["和差化积","2cos.*sin"],"weight":1.5,"desc":"和差化积"},{"key":"limit1","patterns":["重要极限","sin.*/.*→1","lim.*sin"],"weight":1.0,"desc":"使用第一个重要极限"}], ensure_ascii=False), 0.6),
    }
    
    count = 0
    for r in rows:
        key = (r[1], r[2])
        if key in seed_map:
            ans, ptype, steps, weight = seed_map[key]
            ok = update_answer(r[0], ans, steps, ptype, weight)
            if ok:
                count += 1
    
    conn.close()
    print(f"  [种子] 恢复习题1.3: {count}/{len(rows)} 题")
    return count


def extract_final_answer(section_text, problem_no, ptype):
    """
    从OCR解答中提取最终答案。
    对于计算题，尝试找数值/表达式结果。
    对于证明题，提取关键结论。
    """
    # 解析该题的解答部分
    prob_pattern = re.compile(r'(?:^|\n)\s*(\d+)\s*(?:[.．。ο]\s*)?(?=[\u4e00-\u9fffA-Za-z])')
    problems = list(prob_pattern.finditer(section_text))
    
    # 构建题号到文本的映射
    prob_texts = {}
    for i, pm in enumerate(problems):
        pno = pm.group(1)
        start = pm.end()
        end = problems[i+1].start() if i+1 < len(problems) else len(section_text)
        prob_texts[pno] = section_text[start:end].strip()
    
    full_text = prob_texts.get(problem_no, section_text)
    
    # 提取"解"后面的部分
    sol_match = re.search(r'(?:解|证明)\s*(.*?)$', full_text, re.DOTALL)
    if sol_match:
        sol = sol_match.group(1).strip()
    else:
        sol = full_text
    
    # 清理OCR乱码
    sol = sol.replace('  ', ' ')
    
    if ptype == 'proof':
        keywords = []
        for kw in ['有界', '无界', '奇函数', '偶函数', '收敛', '发散', '连续', '可导',
                   '极限', '单调', '上界', '下界', '充要条件', '充要', '必要', '充分',
                   '线性无关', '线性相关', '通解', '特解', '一致连续']:
            if kw in sol[:600]:
                keywords.append(kw)
        if keywords:
            return '证明结论: ' + '; '.join(keywords[:5])
        return sol[:200]
    
    # 计算题：多种策略提取最终答案
    
    # 策略1: 找包含"故"所以"因此"于是"即"的最后一句完整句子
    conclusion_patterns = [
        r'(?:故|所以|因此|于是|从而|得|即|有)\s*[，,]?\s*(.+?)[。.](?:\s|$)',
    ]
    for pat in conclusion_patterns:
        matches = re.findall(pat, sol)
        if matches:
            last = matches[-1].strip()
            last = re.sub(r'\s+', ' ', last)
            # 过滤掉太短或纯文本的
            if len(last) >= 3 and len(last) <= 200:
                return last
    
    # 策略2: 找最后一个数学表达式
    math_exprs = re.findall(r'\$([^$]{2,80})\$', sol)
    if math_exprs:
        # 优先找包含数字的
        numeric = [e for e in math_exprs if re.search(r'[0-9=]', e)]
        if numeric:
            return numeric[-1].strip()
        return math_exprs[-1].strip()
    
    # 策略3: 找最后一句话
    sentences = re.split(r'[。.\n]', sol)
    for s in reversed(sentences):
        s = s.strip()
        if len(s) >= 5 and len(s) <= 200 and any(c in s for c in '0123456789'):
            return s
    
    # 策略4: 返回前200字符
    return sol[:200]


def populate_from_ocr():
    """从OCR提取其他章节答案"""
    content = load_ocr()
    
    # 提取各章节
    sec_pattern = re.compile(r'习题\s*(\d+\.\d+)\s*\n')
    matches = list(sec_pattern.finditer(content))
    ocr_sections = {}
    for i, m in enumerate(matches):
        sno = m.group(1)
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(content)
        ocr_sections[sno] = content[start:end].strip()
    
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, section_no, title FROM sections ORDER BY section_no")
    db_sections = {r[1]: {'id': r[0], 'title': r[2]} for r in cur.fetchall()}
    
    total = 0
    for sno, sec_info in db_sections.items():
        if sno == '1.3':
            continue  # 已用种子答案
        
        if sno not in ocr_sections:
            continue
        
        section_id = sec_info['id']
        cur.execute(
            "SELECT id, problem_no, sub_no, ptype FROM problems WHERE section_id=? ORDER BY CAST(problem_no AS INTEGER), sub_no",
            (section_id,)
        )
        problems = cur.fetchall()
        
        for pid, pno, sub_no, ptype in problems:
            ans = extract_final_answer(ocr_sections[sno], pno, ptype)
            if ans:
                ok = update_answer(pid, ans, ptype=ptype)
                if ok:
                    total += 1
    
    conn.close()
    return total


if __name__ == "__main__":
    print("=" * 60)
    print("混合策略：种子答案 + OCR 提取")
    print("=" * 60)
    
    seed_count = restore_seed_answers()
    ocr_count = populate_from_ocr()
    
    # 验证
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM problems WHERE std_answer IS NOT NULL AND std_answer != ''")
    answered = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems")
    total = cur.fetchone()[0]
    conn.close()
    
    print(f"\n结果: {answered}/{total} 题有答案")
    print(f"  种子答案: {seed_count} 题 (习题1.3)")
    print(f"  OCR提取: {ocr_count} 题 (其他章节)")
