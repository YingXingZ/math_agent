# -*- coding: utf-8 -*-
"""
答案提取 v2：从 IMA OCR 答案册中准确提取每道题的标准答案。

核心改进：
1. 正确识别大题边界（题号 N. 到下一题号）
2. 从"解"/"证明"开始提取，忽略题干
3. 对大题含多个子题 (1)(2)(3)... 的，分别提取每个子题的最终答案
4. 计算题保存最终表达式，证明题保存关键结论和步骤规则
5. 多个子题答案用 " ||| " 分隔，grading_engine 会逐一匹配
"""
import json, re, uuid, os
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
    if grading_steps is not None:
        payload["grading_steps"] = grading_steps
    if ptype is not None:
        payload["ptype"] = ptype
    if answer_weight is not None:
        payload["answer_weight"] = answer_weight
    try:
        resp = requests.put(f"{API_BASE}/problems/{pid}/answer", json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"    API error for {pid}: {e}")
        return False


def restore_seed_answers():
    """恢复习题1.3的种子答案"""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        SELECT p.id, p.problem_no, p.sub_no, p.ptype
        FROM problems p JOIN sections s ON s.id=p.section_id
        WHERE s.section_no='1.3'
        ORDER BY CAST(p.problem_no AS INTEGER), p.sub_no
    ''')
    rows = cur.fetchall()

    proof_steps = json.dumps([
        {"key": "def_eps", "patterns": ["任[给意].*ε", "epsilon", "∀.*ε"], "weight": 2.0, "desc": "由极限定义引入 ε"},
        {"key": "choose_eps", "patterns": ["取.*ε\s*=*", "令.*ε\s*=*"], "weight": 2.0, "desc": "取特定 ε（如 |a|/2）"},
        {"key": "exist_N", "patterns": ["存在.*N", "∃.*N", "当.*n\s*>\s*N"], "weight": 2.0, "desc": "给出 N 的存在性"},
        {"key": "triangle", "patterns": ["三角不等式"], "weight": 1.5, "desc": "使用三角不等式放缩"},
        {"key": "conclude", "patterns": ["故|因此|从而|得证|证毕"], "weight": 1.0, "desc": "给出结论"},
    ], ensure_ascii=False)

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
    seeded_nos = set(k[0] for k in seed_map.keys())
    for r in rows:
        key = (r[1], r[2])
        if key in seed_map:
            ans, ptype, steps, weight = seed_map[key]
            ok = update_answer(r[0], ans, steps, ptype, weight)
            if ok:
                count += 1
        elif r[2] is None and r[1] in seeded_nos:
            # 该大题已拆分为子题，清空大题答案避免误导
            ok = update_answer(r[0], "", ptype='calc')
            if ok:
                count += 1
    conn.close()
    print(f"  [种子] 恢复习题1.3: {count}/{len(rows)} 题")
    return count


# ---------- OCR 提取核心 ----------

PAGE_HEADER_RE = re.compile(r'\n\s*五、习题解答\s*\n|\n\s*第[一二三四五六七八九十]+章[^\n]*\n|\n\s*\d{3}\s*\n')


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


def extract_problem_block(section_text, problem_no):
    """从章节文本中提取某一个大题号的内容块"""
    # 匹配 " N. " 或 " N." 开头，后面跟非数字的内容
    escaped = re.escape(str(problem_no))
    start_pat = re.compile(r'(?:^|\n)\s*' + escaped + r'\s*[\.．。]\s+(?=[^\d])')
    matches = list(start_pat.finditer(section_text))
    if not matches:
        return None
    start = matches[0].end()

    # 找到下一个大题号（任意数字+点+空格+非数字）
    next_pat = re.compile(r'(?:^|\n)\s*(\d+)\s*[\.．。]\s+(?=[^\d])')
    nxt = None
    for m in next_pat.finditer(section_text, start):
        if m.group(1) != str(problem_no):
            nxt = m
            break
    end = nxt.start() if nxt else len(section_text)
    return section_text[start:end]


def clean_solution_text(text):
    """删除页码、章节标题等多余内容"""
    text = PAGE_HEADER_RE.sub('\n', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def split_sub_solutions(solution_text):
    """把一道大题的解答按子题号 (1)(2)(3)... 拆分（只在"解"/"证明"之后拆分）"""
    # 先找到"解"或"证明"的位置，忽略题干中的子题号
    sol_start = re.search(r'(?:^|\n)\s*(解|证明)\s*', solution_text)
    if sol_start:
        sol_text = solution_text[sol_start.start():]
    else:
        sol_text = solution_text

    sol_text = clean_solution_text(sol_text)

    # 去掉开头的"解""证明"
    sol_text = re.sub(r'^[\s解证明]*', '', sol_text).strip()

    # 按 (1) (2) ... 拆分（只看解答部分）
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


def clean_latex_trailing(text):
    """去掉答案末尾的 LaTeX 环境结束符、标点等无关内容"""
    if not text:
        return text
    text = text.strip()
    # 去掉末尾的 \end{aligned}, \end{align*}, \end{cases} 等
    text = re.sub(r'\s*\\end\{[^}]+\}\s*$', '', text)
    text = re.sub(r'\s*\\blacksquare\s*$', '', text)
    # 去掉末尾的句末标点
    text = re.sub(r'[;,.。、]+\s*$', '', text)
    return text.strip()


def extract_final_expression(sub_text):
    """从单个子题的解答文本中提取最终表达式"""
    if not sub_text:
        return ""

    # 去掉开头的"解""证明"等
    sub_text = re.sub(r'^[\s解证明]*', '', sub_text).strip()

    # 1. 优先从最后一个 LaTeX 块中提取
    # 支持 $$...$$ 和 $...$
    latex_blocks = re.findall(r'\$\$([^$]+)\$\$|\$([^$]+)\$', sub_text)
    latex_blocks = [a if a else b for a, b in latex_blocks]

    if latex_blocks:
        last = latex_blocks[-1].strip()
        # 若包含等号，取最后一个等号后的内容（通常是最终结果）
        if '=' in last:
            parts = last.split('=')
            cand = parts[-1].strip()
            if 2 <= len(cand) <= 200:
                return cand
        # 若长度合适，直接返回
        if 2 <= len(last) <= 200:
            return last

    # 2. 找结论词后的简短内容
    conclusion_pat = re.compile(r'(?:故|所以|因此|于是|从而|得|即|为)\s*[，,]?\s*(.+?)[。\.\n]')
    matches = conclusion_pat.findall(sub_text)
    for m in reversed(matches):
        cand = m.strip()
        # 至少包含一点数学特征
        if 2 <= len(cand) <= 200 and (re.search(r'[0-9=\+\-\*/\^\(\)a-zA-Z]', cand) or '$' in cand):
            return cand

    # 3. fallback：取解答文本的前 180 个字符（去除中文说明）
    return clean_latex_trailing(sub_text[:180].strip())


PROOF_KEYWORDS = ['有界', '无界', '奇函数', '偶函数', '收敛', '发散', '连续', '可导',
                  '可微', '单调', '上界', '下界', '充要条件', '线性无关', '线性相关',
                  '一致连续', '介值定理', '零点定理', '罗尔定理', '拉格朗日', '柯西', '泰勒']


CONCEPT_TYPES = {
    'odd_even': {
        'trigger': r'奇偶性|判断.*奇.*偶|是奇函数还是偶函数',
        'keywords': ['偶函数', '奇函数', '非奇非偶', '非偶非奇', '既不是奇函数也不是偶函数', '既不是偶函数也不是奇函数'],
        'steps': [
            {"key": "def", "patterns": ["f\\(-x\\)", "用.*-x.*代替", "代入 -x"], "weight": 2.0, "desc": "使用 f(-x) 定义判断"},
            {"key": "compare", "patterns": ["f\\(-x\\)=f\\(x\\)", "f\\(-x\\)=-f\\(x\\)", "相等", "相反"], "weight": 2.0, "desc": "比较 f(-x) 与 f(x) 或 -f(x)"},
            {"key": "conclude", "patterns": ["偶函数", "奇函数", "非奇非偶"], "weight": 1.5, "desc": "给出奇偶性结论"},
        ]
    },
    'same_diff': {
        'trigger': r'是否相同|相同.*不同|f\\(x\\)\\s*与\\s*g\\(x\\)',
        'keywords': ['相同', '不同', '对应法则不同', '定义域不同', '值域不同'],
        'steps': [
            {"key": "domain", "patterns": ["定义域"], "weight": 1.5, "desc": "比较定义域"},
            {"key": "rule", "patterns": ["对应法则", "f\\(x\\)=g\\(x\\)", "相等"], "weight": 1.5, "desc": "比较对应法则"},
            {"key": "conclude", "patterns": ["相同", "不同"], "weight": 1.0, "desc": "给出结论"},
        ]
    },
}


def detect_conceptual_type(block_text):
    """判断是否为概念判断题（奇偶性、是否相同等），返回类型key或None"""
    if not block_text:
        return None
    text = block_text[:350]
    for ctype, cfg in CONCEPT_TYPES.items():
        if re.search(cfg['trigger'], text):
            return ctype
    return None


def extract_conceptual_answer(sub_text, ctype):
    """从子题解答中提取概念判断题的结论关键词"""
    if not sub_text or ctype not in CONCEPT_TYPES:
        return ""
    cfg = CONCEPT_TYPES[ctype]

    if ctype == 'odd_even':
        # 优先匹配“既不是...也不是...”
        if re.search(r'既不是偶函数也不是奇函数|既不是奇函数也不是偶函数|非奇非偶|非偶非奇', sub_text):
            return '非奇非偶'
        if re.search(r'是偶函数', sub_text):
            return '偶函数'
        if re.search(r'是奇函数', sub_text):
            return '奇函数'
        # 兜底：只看关键词出现位置
        for kw in cfg['keywords']:
            if kw in sub_text:
                return kw
        return ""

    if ctype == 'same_diff':
        # 取结论词附近的内容（注意：必须用(相同|不同)，不能写[相同不同]，否则按单字匹配）
        m = re.search(r'(相同|不同)[，,。\s]*因为(.+?)[。.]', sub_text)
        if m:
            conclusion = m.group(1)
            reason = m.group(2).strip()[:60]
            if reason:
                return f'{conclusion}，因为{reason}'
            return conclusion
        if '不同' in sub_text and '相同' not in sub_text[:100]:
            return '不同'
        if '相同' in sub_text and '不同' not in sub_text[:100]:
            return '相同'
        return ""

    return ""


def detect_proof(problem_text):
    """根据题干内容判断是否为证明题"""
    if not problem_text:
        return False
    text = problem_text[:300]
    if re.search(r'证明|求证', text):
        return True
    # 题目里出现"证明"相关关键词且没有"求""计算"等计算词
    if any(kw in text for kw in ['证', '求证']):
        return True
    return False


def build_proof_answer(solution_text):
    """为证明题构建标准答案（结论关键词），避免保存冗长解题过程"""
    if not solution_text:
        return "证明题"
    # 只取"解"之前的部分作为题干扫描（"解"可能出现在 \text{解} 中）
    sol_pos = re.search(r'解', solution_text)
    if sol_pos:
        scan = solution_text[:sol_pos.start()]
    else:
        scan = solution_text[:400]
    keywords = []
    for kw in PROOF_KEYWORDS:
        if kw in scan and kw not in keywords:
            keywords.append(kw)
    if keywords:
        return '证明: ' + '; '.join(keywords[:6])
    # fallback：尝试提取题干中的待证命题（LaTeX 块）
    latex_blocks = re.findall(r'\$\$([^$]+)\$\$|\$([^$]+)\$', scan)
    latex_blocks = [a if a else b for a, b in latex_blocks]
    # 过滤掉包含"解"的块和过长的块
    latex_blocks = [b.strip() for b in latex_blocks
                    if '解' not in b and '证明' not in b and len(b.strip()) <= 120]
    if latex_blocks:
        return '证明: ' + ' ; '.join(latex_blocks[:3])
    return "证明题"


def build_proof_steps(problem_text, solution_text):
    """为证明题构建步骤规则"""
    steps = []
    combined = (problem_text or "") + " " + (solution_text or "")
    combined_flat = re.sub(r"\s+", "", combined)

    # 通用证明步骤
    if re.search(r'极限|收敛|数列', combined):
        steps.append({"key": "def", "patterns": ["任.*ε", "∀.*ε", "lim", "极限定义"], "weight": 2.0, "desc": "使用定义（如 ε-N）"})
    if re.search(r'导数|微分|可导', combined):
        steps.append({"key": "derivative_def", "patterns": ["导数定义", "f'", "极限.*差商"], "weight": 2.0, "desc": "使用导数定义"})
    if '连续' in combined:
        steps.append({"key": "continuous", "patterns": ["连续", "lim.*f\\(x\\)", "极限值=函数值"], "weight": 2.0, "desc": "使用连续性定义/性质"})
    if any(k in combined for k in ['中值定理', '罗尔', '拉格朗日', '柯西']):
        steps.append({"key": "mvt", "patterns": ["中值定理", "Rolle", "Lagrange", "Cauchy"], "weight": 2.5, "desc": "应用中值定理"})
    if '泰勒' in combined:
        steps.append({"key": "taylor", "patterns": ["Taylor", "泰勒"], "weight": 2.5, "desc": "使用泰勒展开"})

    # 通用结论步骤
    steps.append({"key": "derive", "patterns": ["\\because", "\\therefore", "故|因此|从而|于是", "=>", "⇒"], "weight": 1.0, "desc": "逻辑推导"})
    steps.append({"key": "conclude", "patterns": ["得证|证毕|所以.*成立|因此.*成立"], "weight": 1.0, "desc": "得出结论"})

    return json.dumps(steps, ensure_ascii=False)


def populate_from_ocr():
    content = load_ocr()
    ocr_sections = split_sections(content)

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, section_no, title FROM sections ORDER BY section_no")
    db_sections = {r[1]: {'id': r[0], 'title': r[2]} for r in cur.fetchall()}

    total = 0
    for sno, sec_info in db_sections.items():
        if sno == '1.3':
            continue  # 种子答案已处理
        if sno not in ocr_sections:
            print(f"  [{sno}] OCR 中无对应章节，跳过")
            continue

        section_id = sec_info['id']
        cur.execute(
            "SELECT id, problem_no, sub_no, ptype, content_text FROM problems WHERE section_id=? ORDER BY CAST(problem_no AS INTEGER), sub_no",
            (section_id,)
        )
        problems = cur.fetchall()

        section_text = ocr_sections[sno]
        for pid, pno, sub_no, ptype, content_text in problems:
            block = extract_problem_block(section_text, pno)
            if not block:
                print(f"  [{sno} #{pno}] 未在OCR中找到题块，清空答案待手动配置")
                update_answer(pid, "", ptype='calc')
                continue

            is_proof = detect_proof(block[:500])
            concept_type = detect_conceptual_type(block[:500]) if not is_proof else None
            sub_solutions = split_sub_solutions(block)

            if is_proof:
                # 证明题：只取题干部分扫描，避免保存冗长解题过程
                scan_text = block[:350]
                std_answer = build_proof_answer(scan_text)
                steps = build_proof_steps(content_text, scan_text)
                ok = update_answer(pid, std_answer, steps, 'proof', 0.4)
            elif concept_type:
                # 概念判断题（奇偶性、是否相同等）：提取结论关键词，按 proof 类型走 AI 复核
                cfg = CONCEPT_TYPES[concept_type]
                if sub_no:
                    selected = None
                    for sn, st in sub_solutions:
                        if sn == sub_no:
                            selected = st
                            break
                    if selected is None and sub_solutions:
                        selected = sub_solutions[0][1]
                    std_answer = extract_conceptual_answer(selected, concept_type) if selected else ""
                else:
                    finals = []
                    for sn, st in sub_solutions:
                        final = extract_conceptual_answer(st, concept_type)
                        if final:
                            finals.append(final)
                    if not finals and sub_solutions:
                        finals = [extract_conceptual_answer(sub_solutions[0][1], concept_type)]
                    std_answer = " ||| ".join(finals) if finals else ""
                steps = json.dumps(cfg['steps'], ensure_ascii=False)
                ok = update_answer(pid, std_answer, steps, 'proof', 0.4)
            else:
                # 计算题
                if sub_no:
                    # DB 中的子题：找对应子题号
                    selected = None
                    for sn, st in sub_solutions:
                        if sn == sub_no:
                            selected = st
                            break
                    if selected is None and sub_solutions:
                        selected = sub_solutions[0][1]
                    std_answer = clean_latex_trailing(extract_final_expression(selected)) if selected else ""
                    ok = update_answer(pid, std_answer, ptype='calc')
                else:
                    # DB 中的大题：收集所有子题答案，用 ||| 连接
                    finals = []
                    for sn, st in sub_solutions:
                        final = clean_latex_trailing(extract_final_expression(st))
                        if final:
                            finals.append(final)
                    if not finals and sub_solutions:
                        finals = [clean_latex_trailing(extract_final_expression(sub_solutions[0][1]))]
                    std_answer = " ||| ".join(finals) if finals else ""
                    ok = update_answer(pid, std_answer, ptype='calc')

            if ok:
                total += 1
            else:
                print(f"  [{sno} #{pno}] 更新失败")

    conn.close()
    print(f"  [OCR] 成功更新 {total} 题")
    return total


if __name__ == "__main__":
    print("=" * 60)
    print("答案提取 v2：修复子题识别与最终答案提取")
    print("=" * 60)

    seed_count = restore_seed_answers()
    ocr_count = populate_from_ocr()

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM problems WHERE std_answer IS NOT NULL AND std_answer != ''")
    answered = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems WHERE ptype='proof'")
    proof_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM problems WHERE ptype='calc'")
    calc_count = cur.fetchone()[0]
    conn.close()

    print(f"\n结果: {answered}/{total} 题有答案")
    print(f"  种子答案: {seed_count} 题 (习题1.3)")
    print(f"  OCR提取: {ocr_count} 题 (其他章节)")
    print(f"  证明题: {proof_count} 题, 计算题: {calc_count} 题")
