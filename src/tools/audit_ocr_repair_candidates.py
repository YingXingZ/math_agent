"""Report OCR repair provider coverage, confidence and risks without mutations."""
from __future__ import annotations
import argparse,json,sqlite3
from pathlib import Path
def main():
 p=argparse.ArgumentParser();p.add_argument('--db',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
 c=sqlite3.connect(a.db);c.row_factory=sqlite3.Row
 try:
  providers=[dict(r) for r in c.execute("select provider,count(*) candidates,round(avg(confidence),4) avg_confidence,round(min(confidence),4) min_confidence,round(max(confidence),4) max_confidence,sum(case when risks_json!='[]' then 1 else 0 end) risk_marked from ocr_repair_candidates group by provider")]
  decisions=[dict(r) for r in c.execute('select decision,count(*) count from ocr_repair_decisions group by decision')]
  risks=[dict(r) for r in c.execute("select problem_id,provider,confidence,risks_json from ocr_repair_candidates where risks_json!='[]' order by confidence,provider")]
  payload={'schema_version':'ocr-repair-audit/v1','providers':providers,'decisions':decisions,'risk_marked_candidates':risks,'question_bank_written':False}
 finally:c.close()
 a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps(payload,ensure_ascii=False))
if __name__=='__main__':main()
