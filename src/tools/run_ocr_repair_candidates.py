"""Run configured OCR providers on candidate crops and store review-only results.

Providers are opt-in.  Missing MinerU or PP-FormulaNet is recorded as
``unavailable`` rather than substituted with guessed mathematics.  The VLM
provider calls the existing private service.  This script never changes
``problems`` or any published answer.
"""
from __future__ import annotations

import argparse, base64, json, os, shutil, sqlite3, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench8014.ocr_repair_consensus import decide
from workbench8014.source_evidence import ensure_source_evidence_schema


def now() -> str: return datetime.now(timezone.utc).isoformat()

def command_provider(name: str, command: str, image: Path) -> dict:
    if not command:
        return {"provider": name, "status": "unavailable", "reason": f"{name}_command_not_configured"}
    executable = command.split()[0]
    if not Path(executable).is_file() and not shutil.which(executable):
        return {"provider": name, "status": "unavailable", "reason": f"{name}_command_not_found"}
    try:
        completed = subprocess.run([*command.split(), str(image)], capture_output=True, text=True, timeout=180, check=True)
        result = json.loads(completed.stdout)
        return {"provider": name, "status": "completed", "latex_text": str(result.get("latex_text") or result.get("text") or ""),
                "confidence": float(result.get("confidence") or 0), "risks": result.get("risks") or [], "raw": result}
    except Exception as exc:
        return {"provider": name, "status": "failed", "reason": str(exc)[:500]}

def vlm_provider(url: str, image: Path, section: str, number: str) -> dict:
    if not url:
        return {"provider": "vlm", "status": "unavailable", "reason": "MATH_VLM_URL_not_configured"}
    try:
        payload = {"image_base64": base64.b64encode(image.read_bytes()).decode(), "section_no": section, "problem_no": number}
        request = Request(url.rstrip('/') + '/solve-from-image', data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method='POST')
        with urlopen(request, timeout=240) as response: result = json.loads(response.read().decode())
        return {"provider":"vlm", "status":"completed", "latex_text":str(result.get("problem_text") or ""),
                "confidence":float(result.get("confidence") or 0), "risks":result.get("risks") or [], "raw":result}
    except Exception as exc:
        return {"provider":"vlm", "status":"failed", "reason":str(exc)[:500]}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True); parser.add_argument('--image-root', type=Path, required=True)
    parser.add_argument('--vlm-url', default=os.environ.get('MATH_VLM_URL',''))
    parser.add_argument('--mineru-command', default=os.environ.get('MINERU_OCR_COMMAND',''))
    parser.add_argument('--formula-command', default=os.environ.get('PP_FORMULANET_COMMAND',''))
    parser.add_argument('--limit', type=int, default=100); args=parser.parse_args()
    conn=sqlite3.connect(args.db); conn.row_factory=sqlite3.Row; ensure_source_evidence_schema(conn)
    rows=conn.execute("""SELECT a.id anchor_id,a.problem_id,a.crop_path,s.section_no,p.problem_no FROM problem_source_anchors a
      JOIN problems p ON p.id=a.problem_id JOIN sections s ON s.id=p.section_id
      WHERE a.status='candidate' AND a.crop_path<>'' ORDER BY a.id LIMIT ?""",(args.limit,)).fetchall()
    outcome=[]
    for row in rows:
        image=(args.image_root / row['crop_path']).resolve()
        if not image.is_file(): outcome.append({'anchor_id':row['anchor_id'],'status':'blocked','reason':'candidate_crop_missing'}); continue
        results=[command_provider('mineru',args.mineru_command,image),command_provider('pp_formulanet',args.formula_command,image),vlm_provider(args.vlm_url,image,row['section_no'],str(row['problem_no']))]
        usable=[]
        for result in results:
            if result['status']=='completed':
                usable.append(result)
                conn.execute("""INSERT INTO ocr_repair_candidates(id,problem_id,anchor_id,provider,crop_path,latex_text,confidence,risks_json,result_json,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(problem_id,anchor_id,provider,crop_path) DO UPDATE SET latex_text=excluded.latex_text,confidence=excluded.confidence,risks_json=excluded.risks_json,result_json=excluded.result_json,status='pending_teacher',updated_at=excluded.updated_at""",
                   (uuid.uuid4().hex,row['problem_id'],row['anchor_id'],result['provider'],row['crop_path'],result['latex_text'],result['confidence'],json.dumps(result['risks'],ensure_ascii=False),json.dumps(result,ensure_ascii=False),'pending_teacher',now(),now()))
        recommendation=decide(usable)
        conn.execute("""INSERT INTO ocr_repair_decisions(problem_id,decision,decision_json,teacher_status,created_at,updated_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(problem_id) DO UPDATE SET decision=excluded.decision,decision_json=excluded.decision_json,teacher_status='pending',updated_at=excluded.updated_at""",
          (row['problem_id'],recommendation['decision'],json.dumps(recommendation,ensure_ascii=False),'pending',now(),now()))
        outcome.append({'anchor_id':row['anchor_id'],'providers':[{k:v for k,v in r.items() if k!='raw'} for r in results],'recommendation':recommendation})
    conn.commit(); conn.close(); print(json.dumps({'processed':len(outcome),'outcome':outcome},ensure_ascii=False))
if __name__=='__main__': main()
