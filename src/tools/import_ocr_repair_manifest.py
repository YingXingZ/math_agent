"""Import an existing evidence-backed OCR manifest as review-only candidates."""
from __future__ import annotations
import argparse,json,sqlite3,sys,uuid
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workbench8014.ocr_repair_consensus import decide
from workbench8014.source_evidence import ensure_source_evidence_schema

def now(): return datetime.now(timezone.utc).isoformat()
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);a=p.parse_args()
 m=json.loads(a.manifest.read_text(encoding='utf-8'));c=sqlite3.connect(a.db);ensure_source_evidence_schema(c); added=0
 try:
  for x in m.get('candidates',[]):
   text=str(x.get('latex_candidate') or '')
   if not text: continue
   raw=x.get('model_result') or {}; risks=raw.get('risks') or []; confidence=float(raw.get('confidence') or 0)
   c.execute("""INSERT INTO ocr_repair_candidates(id,problem_id,anchor_id,provider,crop_path,latex_text,confidence,risks_json,result_json,status,created_at,updated_at)
     VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(problem_id,anchor_id,provider,crop_path) DO UPDATE SET latex_text=excluded.latex_text,confidence=excluded.confidence,risks_json=excluded.risks_json,result_json=excluded.result_json,status='pending_teacher',updated_at=excluded.updated_at""",
    (uuid.uuid4().hex,x['problem_id'],x['anchor_id'],'vlm',str(x.get('evidence_crop') or ''),text,confidence,json.dumps(risks,ensure_ascii=False),json.dumps(raw,ensure_ascii=False),'pending_teacher',now(),now()))
   recommendation=decide([{'provider':'vlm','latex_text':text,'confidence':confidence,'risks':risks}])
   c.execute("""INSERT INTO ocr_repair_decisions(problem_id,decision,decision_json,teacher_status,created_at,updated_at) VALUES(?,?,?,?,?,?)
    ON CONFLICT(problem_id) DO UPDATE SET decision=excluded.decision,decision_json=excluded.decision_json,teacher_status='pending',updated_at=excluded.updated_at""",(x['problem_id'],recommendation['decision'],json.dumps(recommendation,ensure_ascii=False),'pending',now(),now()))
   added+=1
  c.commit();print(json.dumps({'imported':added,'question_bank_written':False},ensure_ascii=False))
 finally:c.close()
if __name__=='__main__':main()
