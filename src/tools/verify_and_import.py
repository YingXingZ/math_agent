# -*- coding: utf-8 -*-
"""提取完成后：验证JSON -> 清库 -> 导入 -> 难度分层 -> 验证"""
import sys, os, json, sqlite3, requests
sys.stdout.reconfigure(encoding="utf-8")

WORKSPACE = "D:/workbuddy/2026-08-06-15-31-48"
JSON_PATH = os.path.join(WORKSPACE, "extract_img_v2/book_problems.json")
DB_PATH = os.path.join(WORKSPACE, "api.db")
API_BASE = "http://127.0.0.1:8011/api"

# Step 1: 验证 JSON
print("=== Step 1: 验证提取结果 ===")
if not os.path.exists(JSON_PATH):
    print(f"ERROR: JSON not found at {JSON_PATH}")
    sys.exit(1)

with open(JSON_PATH, 'r', encoding='utf-8') as f:
    data = json.load(f)

secs = data.get('sections', [])
total_probs = sum(len(s.get('problems', [])) for s in secs)
total_subs = sum(len(p.get('subproblems', [])) for s in secs for p in s.get('problems', []))
print(f"Sections: {len(secs)}")
print(f"Problems: {total_probs} + Subproblems: {total_subs} = {total_probs + total_subs}")

for s in secs:
    probs = s.get('problems', [])
    subs = sum(len(p.get('subproblems', [])) for p in probs)
    print(f"  {s.get('section_no','?')}: {len(probs)} problems, {subs} subproblems")

if total_probs < 200:
    print(f"WARNING: Only {total_probs} problems, expected 300+. Extraction may still be incomplete.")

# Step 2: 清库
print("\n=== Step 2: 清除旧题库数据 ===")
conn = sqlite3.connect(DB_PATH)
conn.execute("DELETE FROM problems")
conn.execute("DELETE FROM sections")
conn.execute("DELETE FROM textbooks")
conn.execute("DELETE FROM meta WHERE key='demo_seeded'")
conn.commit()
secs_count = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
probs_count = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
print(f"After clear: sections={secs_count}, problems={probs_count}")
conn.close()

# Step 3: 通过 API 导入
print("\n=== Step 3: 导入新数据 ===")
resp = requests.post(f"{API_BASE}/ingest/book", json={"path": JSON_PATH})
if resp.status_code != 200:
    print(f"ERROR: ingest failed: {resp.status_code} {resp.text}")
    sys.exit(1)
result = resp.json()
print(f"Ingested: {result['sections']} sections, {result['problems']} problems")

# Step 4: 难度分层
print("\n=== Step 4: 执行难度分层 ===")
resp = requests.post(f"{API_BASE}/admin/classify-tiers")
if resp.status_code == 200:
    tier_result = resp.json()
    print(f"Tier classification: {tier_result['total']} problems classified")
    print(f"Summary: {tier_result['summary']}")
else:
    print(f"Tier classification failed: {resp.status_code} {resp.text}")

# Step 5: 验证
print("\n=== Step 5: 验证最终状态 ===")
resp = requests.get(f"{API_BASE}/stats")
stats = resp.json()
print(f"Total problems: {stats['total_problems']}")
print(f"Sections: {stats['sections_count']}")
print(f"By section: {stats['by_section']}")

resp = requests.get(f"{API_BASE}/stats/tiers")
if resp.status_code == 200:
    print(f"Tier stats: {resp.json()}")

print("\n=== 完成！===")
