# 高数学习 Agent

## 定位
面向教师已发布错题的提交后学习助教。它不在作业提交前给答案；不确定时不会编造结论。

## 运行图
题库题型：
- calc：SymPy 判等 → 低置信度时 Qwen 独立求解 → 符号仲裁 → 教学/人工复核
- proof：关键步骤抽取 → 分问评分点 → 教学/人工复核

## Skills
answer_perception、symbolic_verification、independent_solving、misconception_diagnosis、evidence_retrieval。

Skills 使用 Pydantic 输入/输出契约，并由 Registry 在运行时解析；测试或实验可替换实现并记录版本。

## 三档证明题策略
- 明确缺失：自动指出未写出的步骤；
- 证据充分：自动认可关键评分点；
- 识别不清或评分反馈冲突：转教师确认。

## 验证
运行 pytest workbench8014/tests tools/test_langgraph_math_agent.py -q；
运行 python workbench8014/evals/run_eval.py；
运行 python workbench8014/evals/run_real_case_eval.py。
真实案例仅保存脱敏、教师确认过的记录；GitHub Actions 会强制运行上述检查。
