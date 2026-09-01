# 教师标注回归评测集

teacher_labeled_cases.jsonl 是教育 Agent 的**受控回归基准**。每条记录均由既有的“脱敏、教师已确认”真实案例迁移而来；本次迁移只补充标签契约与版本信息，不把模型输出当作金标准。

## 数据安全

- 只保存必要的数学表达式和已匿名化题干，不得包含姓名、学号、联系方式、原始手写图片或可还原身份的信息。
- 只有 teacher_verified: true、label_status: verified、data_governance.approved_for_regression: true 的记录才能进入 CI。
- 新案例必须先由教师确认，再写入本文件；失败案例不得删除，应以新标签版本修正并保留变更原因。

## 标签契约

每一条都必须具有稳定 id、label_version、question_type、teacher_label 和数据治理字段。teacher_label 中：

- correct：教师认可的对错；
- expected_route：安全路由预期；
- expected_diagnosis：有可靠错因标注时填写，否则为 null；
- requires_teacher_review：该样本是否应转教师复核。

当前 v1 的 30 条样本以计算题为主，尚不足以评估复杂证明题、整页多问手写识别或步骤图定位。这些覆盖缺口会在报告中明确呈现，不能被“总体准确率”掩盖。

## 运行和门槛

    cd ~/math-agent/src/workbench8014
    ../agent8000/math_agent/bin/python evals/run_teacher_labeled_eval.py --report /tmp/teacher_eval_report.json

teacher_label_thresholds.json 规定最小样本量、正确性准确率、路由准确率、假阳性率及治理错误上限。任意门槛未通过时脚本返回非零退出码，CI 会失败。
