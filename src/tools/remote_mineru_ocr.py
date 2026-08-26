"""Run the configured A100 MinerU worker for one crop and emit a JSON candidate.

This uses the existing deployment SSH helper so secrets stay out of OCR result
records.  The returned Markdown is evidence only; confidence is deliberately
zero because MinerU does not provide a calibrated per-crop confidence score.
"""
from __future__ import annotations
import argparse,json,subprocess,sys,uuid
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
 sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')

ROOT=Path(__file__).resolve().parents[2]; SSH=ROOT/'src'/'tools'/'server_ssh.py'
def call(*args:str,timeout:int=300)->str:
 p=subprocess.run([sys.executable,str(SSH),*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
 if p.returncode: raise RuntimeError((p.stderr or p.stdout)[-1500:])
 return p.stdout
def main():
 p=argparse.ArgumentParser();p.add_argument('image',type=Path);a=p.parse_args(); tag='ocr-repair-'+uuid.uuid4().hex
 remote_image=f'/tmp/{tag}.png'; remote_out=f'/tmp/{tag}-out'
 try:
  call('put',str(a.image),remote_image)
  call('run','180',f"/opt/mineru-env/bin/mineru -p {remote_image} -o {remote_out} -m ocr -b pipeline -l ch >/tmp/{tag}.log 2>&1")
  # MinerU derives the inner directory from the uploaded random filename;
  # retrieve the unique generated Markdown by content, not by a guessed stem.
  text=call('run','45',f"find {remote_out} -type f -name '*.md' -print -quit | xargs -r cat").strip()
  if not text: raise RuntimeError('MinerU returned empty Markdown')
  risks=['MinerU 未提供可校准单题置信度；必须对照原图复核']
  if '\ufffd' in text or '�' in text: risks.append('中文 OCR 存在乱码；不得自动采纳')
  print(json.dumps({'latex_text':text,'confidence':0.0,'risks':risks,'provider':'mineru'},ensure_ascii=False))
 except Exception as exc:
  print(json.dumps({'latex_text':'','confidence':0.0,'risks':[str(exc)[:1000]],'provider':'mineru','error':str(exc)[:1000]},ensure_ascii=False));sys.exit(1)
if __name__=='__main__':main()
