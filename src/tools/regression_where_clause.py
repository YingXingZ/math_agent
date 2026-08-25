"""Regression test for parameterised 8014 workbench filters."""
import ast
import sqlite3
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PATCHED_FILES = [REPOSITORY_ROOT / "src" / "workbench8014" / "api_app.py"]


def load_where_clause(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_where_clause":
            namespace: dict = {}
            exec(ast.get_source_segment(source, node), namespace)
            return namespace["_where_clause"]
    raise RuntimeError(f"_where_clause not found in {path}")


db = sqlite3.connect(":memory:")
db.executescript("""
CREATE TABLE sections(id INTEGER PRIMARY KEY, section_no TEXT);
CREATE TABLE problems(id INTEGER PRIMARY KEY, section_id INTEGER, exercise_set TEXT,
    problem_no TEXT, sub_no TEXT, ptype TEXT, difficulty INTEGER, knowledge_pts TEXT);
INSERT INTO sections(id, section_no) VALUES (1,'1.1'),(2,'2.3');
INSERT INTO problems(section_id,exercise_set,problem_no,ptype,difficulty,knowledge_pts)
VALUES (1,'A','1','calc',1,'limit'), (1,'A','2','proof',3,'continuity'), (2,'B','1','calc',2,'derivative');
""")


def count(clause: str, args: list):
    return db.execute(
        "SELECT COUNT(*) FROM problems p JOIN sections s ON s.id=p.section_id WHERE 1=1" + clause,
        args,
    ).fetchone()[0]


malicious = "x') OR ('1'='1') -- "
for file_path in PATCHED_FILES:
    where_clause = load_where_clause(file_path)
    clause, args = where_clause(None, None, None, None, None, malicious)
    assert isinstance(clause, str) and isinstance(args, list) and "?" in clause
    assert count(clause, args) == 0
    clause, args = where_clause("1.1", None, None, None, None, None)
    assert count(clause, args) == 2
    clause, args = where_clause(None, "calc", 1, 2, None, None)
    assert count(clause, args) == 2
    print(f"PASS {file_path.name}")

print("ALL_FILES_PASS")
