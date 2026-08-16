# -*- coding: utf-8 -*-
"""创建演示作业+提交数据，用于测试自动批改"""
import sqlite3, json, uuid
from datetime import datetime

db = sqlite3.connect('api.db')
db.row_factory = sqlite3.Row
cur = db.cursor()

# Check if demo class exists; if not, create one
cls = cur.execute('SELECT id FROM classes ORDER BY name LIMIT 1').fetchone()
if not cls:
    cid = str(uuid.uuid4())
    cur.execute('INSERT INTO classes(id,teacher_id,name,term) VALUES(?,?,?,?)',
        (cid, 'teacher_demo', u'高等数学A班', '2025-2026-1'))
    cls_id = cid
    for sno, name in [('20230101', u'张三'), ('20230202', u'李四'), ('20230303', u'王五')]:
        cur.execute('INSERT INTO students(id,class_id,student_no,name) VALUES(?,?,?,?)',
            (str(uuid.uuid4()), cls_id, sno, name))
    print('Created demo class + 3 students')
else:
    cls_id = cls['id']
    print(f'Using existing class: {cls_id[:8]}...')

# Get textbook
tid = cur.execute('SELECT id FROM textbooks LIMIT 1').fetchone()
if not tid:
    tid_val = str(uuid.uuid4())
    cur.execute('INSERT INTO textbooks(id,name,volume,edition) VALUES(?,?,?,?)',
        (tid_val, u'高等数学 上册', u'上', u'第2版'))
else:
    tid_val = tid['id']
print(f'Using textbook: {tid_val}')

# Get all 1.3 problems including subproblems
pids = cur.execute("""
    SELECT p.id, p.problem_no, p.sub_no, p.ptype, p.std_answer
    FROM problems p JOIN sections s ON s.id=p.section_id
    WHERE s.section_no='1.3'
    ORDER BY p.problem_no, p.sub_no
""").fetchall()
pid_list = [p['id'] for p in pids]
print(f'Problems for homework: {len(pid_list)}')
for p in pids:
    print(f'  #{p["problem_no"]}({p["sub_no"] or "-"}): {p["ptype"]} ans={p["std_answer"]}')

# Check if demo homework already exists
existing = cur.execute("SELECT id FROM homeworks WHERE title LIKE '%演示%'").fetchone()
if existing:
    print(f'Demo homework already exists: {existing["id"][:8]}...')
    hw_id = existing['id']
else:
    hw_id = str(uuid.uuid4())
    cur.execute("""INSERT INTO homeworks(id,textbook_id,title,class_id,section_no,deadline,status,problem_ids,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (hw_id, tid_val, u'演示作业：第一章 1.3 极限运算法则（含自动批改）',
         cls_id, '1.3',
         datetime.now().isoformat(timespec='seconds'),
         'published',
         json.dumps(pid_list),
         datetime.now().isoformat(timespec='seconds')))
    print(f'Created demo homework: {hw_id[:8]}...')

# Build problem map: key -> (pid, ptype, std_answer)
prob_map = {}
for p in pids:
    key = p['problem_no'] + (p['sub_no'] or '')
    prob_map[key] = (p['id'], p['ptype'], p['std_answer'])

# Create test submissions
submissions_data = [
    ('20230101', u'张三', {
        '1': u'任给epsilon>0，由极限定义...',  # proof - partial
        '2': '3',   # correct
        '3': '0',   # correct
        '4': '2',   # wrong
        '5': '1/2', # correct
        '61': '-2',  # correct
        '64': '0.6667', # approximation
        '67': 'cos(a)',  # correct
    }),
    ('20230202', u'李四', {
        '1': u'因为极限存在所以有界',  # proof - very brief
        '2': '3',
        '3': '1',    # wrong
        '4': '1',
        '5': '1/2',
        '61': '2',   # wrong sign
        '64': '2/3',
        '67': 'sin(a)',  # wrong (cos vs sin)
    }),
    ('20230303', u'王五', {
        '1': u'由极限定义，对任意epsilon>0...使用三角不等式...存在N...故得证。',
        '2': '3',
        '3': '0',
        '4': '1',
        '5': '1/2',
        '61': '-2',
        '64': '2/3',
        '67': 'cos(a)',
    }),
]

for sno, sname, answers_dict in submissions_data:
    existing_sub = cur.execute(
        'SELECT id FROM submissions WHERE homework_id=? AND student_no=?',
        (hw_id, sno)).fetchone()
    if existing_sub:
        print(f'Submission exists for {sname}, skipping')
        continue

    answers = []
    for key, (pid, ptype, std_ans) in prob_map.items():
        if key in answers_dict:
            answers.append({'problem_id': pid, 'text': answers_dict[key]})

    sid = str(uuid.uuid4())
    cur.execute("""INSERT INTO submissions(id,homework_id,student_no,student_name,submitted_at,status,score,answers,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (sid, hw_id, sno, sname,
         datetime.now().isoformat(timespec='seconds'),
         'pending', None,
         json.dumps(answers, ensure_ascii=False),
         datetime.now().isoformat(timespec='seconds')))
    print(f'Created submission: {sname} ({len(answers)} answers)')

db.commit()

# Verify
subs = cur.execute("SELECT student_name, status, score FROM submissions WHERE homework_id=?", (hw_id,)).fetchall()
print(f'\nSubmissions for homework:')
for s in subs:
    print(f'  {s["student_name"]}: status={s["status"]} score={s["score"]}')

db.close()
print('\nDone!')
