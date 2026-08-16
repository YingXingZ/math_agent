#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性迁移：把 submissions.file_path 中的相对路径改写为绝对路径。

根因：早期提交（submission #1）在 upload_dir 还相对时落库，导致批改必须依赖
服务器进程 cwd；新的提交（#2 起）已存绝对路径。本脚本只转换仍相对的行，
绝对路径行原样保留。安全：先备份再 UPDATE。
"""
import shutil
import sqlite3
from pathlib import Path

AGENT_DIR = Path(r"D:\My File\大四\高数教材答案\高数作业助手")
DB = AGENT_DIR / "data" / "homework.db"
BACKUP = AGENT_DIR / "data" / f"homework.db.bak_filepath_{Path(__file__).stat().st_mtime:.0f}"

# 相对路径以项目根（高数作业助手）为基准解析
ROOT = AGENT_DIR.resolve()


def main() -> None:
    shutil.copyfile(DB, BACKUP)
    print(f"备份 -> {BACKUP}")

    conn = sqlite3.connect(DB)
    updated = 0
    for sid, fp in conn.execute("SELECT id, file_path FROM submissions"):
        p = Path(fp)
        if p.is_absolute():
            print(f"  #{sid}: 已是绝对路径，跳过")
            continue
        abs_p = (ROOT / fp).resolve()
        conn.execute(
            "UPDATE submissions SET file_path=? WHERE id=?", (str(abs_p), sid)
        )
        updated += 1
        print(f"  #{sid}: {fp} -> {abs_p}")
    conn.commit()
    conn.close()
    print(f"迁移完成，更新 {updated} 行（备份：{BACKUP}）")


if __name__ == "__main__":
    main()
