# 高数学习 Agent 阅读导图

建议按下面顺序阅读，不要一开始就从 API 文件读起。

1. skills/schemas.py：每个 Skill 的共同输入输出约定。
2. skills/symbolic_verification.py：SymPy 如何确定性地判断数学等价。
3. skills/independent_solving.py：Qwen 如何只拿题目独立求解，不接触标准答案。
4. skills/misconception_diagnosis.py：规则和符号证据如何输出错误标签。
5. math_agent_graph.py：LangGraph 如何按置信度把上述 Skill 串起来。
6. evals/math_agent_eval_cases.jsonl 与 evals/run_eval.py：怎样验证改动没有退化。
7. api_app.py：最后看安全 API 如何从题库读答案、只返回安全结果。

## Skill 与 LangGraph @tool 的区别

当前 Skill 是普通 Python 函数，使用项目自己的 registry.register 装饰器登记。

- LangGraph 节点：流程控制者，决定下一步走哪个节点。
- Skill：专业能力单元，例如符号判等、Qwen 求解、错因诊断。
- LangChain/LangGraph @tool：通常是给 LLM 自主选择调用的工具描述。

当前项目没有让 LLM 决定“是否判对”或“是否调用 SymPy”，而是由图中的确定性路由规则决定。这是为了保证数学正确性与安全性。以后若做开放式问答或检索，可把部分低风险 Skill 适配成 @tool；但 SymPy 判等与发布权限检查不应交给 LLM 自主选择。
