# -*- coding: utf-8 -*-
"""种子标准答案：将 grading_engine demo specs 写入 DB"""
import sqlite3, json, uuid

db = sqlite3.connect('api.db')
db.row_factory = sqlite3.Row
cur = db.cursor()

# Get 1.3 problems
rows = cur.execute('''
    SELECT p.id, p.problem_no, p.sub_no, p.ptype, p.content_text, p.knowledge_pts,
           s.section_no
    FROM problems p JOIN sections s ON s.id=p.section_id
    WHERE s.section_no='1.3'
    ORDER BY p.problem_no, p.sub_no
''').fetchall()

print('习题1.3 problems:')
for r in rows:
    print(f'  id={r["id"][:12]}... | #={r["problem_no"]} | sub={r["sub_no"] or "-"} | type={r["ptype"]}')

# --- 第1题：证明题 ---
proof_steps = json.dumps([
    {"key": "def_eps", "patterns": ["任[给意].*ε", "epsilon", "∀.*ε"], "weight": 2.0, "desc": "由极限定义引入 ε"},
    {"key": "choose_eps", "patterns": ["取.*ε\\s*=", "令.*ε\\s*="], "weight": 2.0, "desc": "取特定 ε（如 |a|/2）"},
    {"key": "exist_N", "patterns": ["存在.*N", "∃.*N", "当.*n\\s*>\\s*N"], "weight": 2.0, "desc": "给出 N 的存在性"},
    {"key": "triangle", "patterns": ["三角不等式"], "weight": 1.5, "desc": "使用三角不等式放缩"},
    {"key": "conclude", "patterns": ["故|因此|从而|得证|证毕"], "weight": 1.0, "desc": "给出结论"},
], ensure_ascii=False)

cur.execute('''UPDATE problems SET ptype=?, std_answer=?, grading_steps=?, answer_weight=? WHERE id=?''',
    ('proof', '证明收敛数列的有界性/保号性等性质', proof_steps, 0.4, rows[0]['id']))
print('Updated #1: proof type with step rules')

# --- #6: 创建 3 个子问题 ---
pid6 = [r for r in rows if r['problem_no'] == '6'][0]
sec_id = cur.execute('SELECT section_id FROM problems WHERE id=?', (pid6['id'],)).fetchone()['section_id']

subs_6 = [
    ('6', '1', 'calc', '-2',
     json.dumps([{"key":"substitute","patterns":["代入","x=1","x→1"],"weight":1.0,"desc":"直接代入 x=1（分母不为零）"}], ensure_ascii=False),
     0.6),
    ('6', '4', 'calc', '2/3',
     json.dumps([
         {"key":"rationalize","patterns":["有理化","共轭","同乘"],"weight":1.5,"desc":"分子分母有理化"},
         {"key":"simplify","patterns":["约分","化简"],"weight":1.0,"desc":"约去零因子"}
     ], ensure_ascii=False),
     0.6),
    ('6', '7', 'calc', 'cos(a)',
     json.dumps([
         {"key":"sum2prod","patterns":["和差化积","2cos.*sin"],"weight":1.5,"desc":"和差化积"},
         {"key":"limit1","patterns":["重要极限","sin.*/.*→1","lim.*sin"],"weight":1.0,"desc":"使用第一个重要极限"}
     ], ensure_ascii=False),
     0.6),
]
for no, sub, ptype, ans, steps, aw in subs_6:
    spid = str(uuid.uuid4())
    cur.execute('''INSERT INTO problems(id,section_id,exercise_set,problem_no,sub_no,ptype,
        difficulty,knowledge_pts,extract_status,std_answer,grading_steps,answer_weight)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (spid, sec_id, '1.3', no, sub, ptype, 3, pid6['knowledge_pts'], 'manual', ans, steps, aw))
    print(f'Created subproblem {no}({sub}): {spid[:12]}... answer={ans}')

# --- #2-5: 简单计算题 ---
simple = [
    ('2', 'calc', '3', json.dumps([{"key":"substitute","patterns":["代入","直接代入"],"weight":1.0,"desc":"代入求值"}], ensure_ascii=False)),
    ('3', 'calc', '0', json.dumps([{"key":"factor","patterns":["因式分解","提公因子"],"weight":1.0,"desc":"因式分解消去零因子"}], ensure_ascii=False)),
    ('4', 'calc', '1', json.dumps([{"key":"rationalize","patterns":["有理化","共轭"],"weight":1.0,"desc":"有理化"}], ensure_ascii=False)),
    ('5', 'calc', '1/2', json.dumps([{"key":"simplify","patterns":["化简","同除","提取"],"weight":1.0,"desc":"提取最高次幂"}], ensure_ascii=False)),
]
for no, ptype, ans, steps in simple:
    row = [r for r in rows if r['problem_no'] == no]
    if row:
        cur.execute('UPDATE problems SET ptype=?, std_answer=?, grading_steps=?, answer_weight=? WHERE id=?',
            (ptype, ans, steps, 0.6, row[0]['id']))
        print(f'Updated #{no}: {ptype} answer={ans}')

db.commit()

# Summary
seeded = cur.execute('''SELECT p.problem_no, p.sub_no, p.ptype, p.std_answer, p.grading_steps 
    FROM problems p JOIN sections s ON s.id=p.section_id 
    WHERE s.section_no="1.3" AND p.std_answer IS NOT NULL''').fetchall()
print(f'\n--- Seeded ({len(seeded)}) ---')
for s in seeded:
    has_steps = 'yes' if s['grading_steps'] else 'no'
    ans_preview = (s['std_answer'] or '')[:40]
    print(f'  #{s["problem_no"]}({s["sub_no"] or "-"}): {s["ptype"]} -> {ans_preview} | steps={has_steps}')

db.close()
print('\nDone!')
