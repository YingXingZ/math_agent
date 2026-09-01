# 真实案例评测集

这里仅保存**已脱敏、已人工确认**的真实案例。不要自动把学生原始答案、姓名、学号、完整作业图片写入本目录。

## 加入一条案例

复制 schema.example.json 的字段，写入 sanitized_cases.jsonl（一行一个 JSON 对象）。

必填字段：

- id：稳定、不含个人信息的编号；
- source：如 teacher_review / ocr_failure；
- consent_or_anonymization：必须为 anonymized；
- student_answer：仅保留必要数学表达式，移除姓名等文本；
- standard_answer：教师确认后的标准答案；
- expected_correct；
- expected_route；
- expected_diagnosis：无法可靠标注时填 null；
- teacher_verified：true。

## 进入回归基准的规则

1. 题目和答案已匿名化；
2. 教师已确认期望结果；
3. 不能用“模型自己的输出”作为金标准；
4. 失败案例不能删除，应保留并修复后重新回归。

运行：

    ../agent8000/math_agent/bin/python evals/run_real_case_eval.py
