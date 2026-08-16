# Dify 网页版接入指南

本项目将 Dify 作为 AI 编排层，业务数据、账号权限、作业文件归档和成绩发布仍由本系统管理。请在 Dify 网页版分别创建 3 个 **Workflow** 应用，不要使用 Chatbot 应用代替。

## 0. Dify 中的通用操作

1. 进入 **Studio** → **Create from blank** → **Workflow**。
2. 配置开始节点（Start）的输入变量，按下方变量名创建；变量名必须完全一致。
3. 添加 LLM、Document Extractor、Code/Template 和 End 节点；End 节点输出必须是 JSON 文本。
4. 使用右上角 **Test Run** 测试，再点击 **Publish**。
5. 打开该应用的 **API Access / Access API**，创建 Service API Key。每个 Workflow 都有自己的 Key。

云版 API 基地址通常是 `https://api.dify.ai/v1`；自建版填 `https://你的Dify域名/v1`。API Key 只保存到部署服务器的 `.env`，绝不能放进网页前端或提交到 Git。

## 1. 工作流 A：题目结构化

名称：`高数-题目结构化`

Start 输入：

- `ocr_text`：段落文本，必填；来自 OCR 的页面文字。
- `chapter_hint`：短文本，非必填；例如“第二章 导数与微分”。
- `source_files`：文件列表，非必填；允许 PDF 和图片，用于人工/OCR结果校验。

节点顺序：`Start →（文件时）Document Extractor / 视觉模型 → LLM → End`。

LLM System Prompt：

```text
你是高等数学教材题目结构化助手。仅从给定内容抽取练习题，不得补写题目。
公式必须使用 LaTex。返回合法 JSON：
{"questions":[{"question_no":"","content_latex":"","chapter":"","knowledge_tags":[],"difficulty":"基础|提高|综合","question_type":"","answer_hint":"","confidence":0.0}]}
若页面不是习题页，questions 返回空数组。
```

End 输出变量：`questions_json`，值为 LLM 的 JSON 字符串。

## 2. 工作流 B：智能组卷

名称：`高数-智能组卷`

Start 输入全部为段落文本：

- `chapter`
- `candidates_json`：后端筛选出的候选题，不超过约 30 题。
- `constraints_json`：题量、难度比例、题型覆盖、预计时间、近期去重规则。

节点顺序：`Start → LLM → End`。

LLM System Prompt：

```text
你是大学高等数学教师的组卷助手。只能从 candidates_json 选择题目，不能编造题目或题目 ID。
满足 constraints_json 中的题量、难度和题型要求；优先覆盖不同知识点，并保证题目由易到难。
仅返回 JSON：{"selected_ids":[1,2],"rationale":"","coverage":[""],"warnings":[]}。
```

End 输出变量：`selection_json`。

后端收到结果后仍会校验题目 ID、章节、重复性和题量；模型输出不直接写入作业。

## 3. 工作流 C：手写作业初评

名称：`高数-作业初评`

Start 输入：

- `question_json`：段落文本，含题干、答案、评分细则与分值。
- `student_files`：文件列表，允许图片和 PDF，必填。
- `chapter`：短文本。

节点建议：`Start → 文件分类（图片/PDF）→ Document Extractor / 视觉模型 → LLM → End`。图片交给支持视觉的模型；PDF 先经过 Document Extractor。对于公式识别不可靠的页，输出“不确定”而不是猜测。

LLM System Prompt：

```text
你只能依据题干、评分细则和学生提交内容评分。逐个评分点给出 evidence；内容无法辨认、推导不完整或置信度低时，needs_review 必须为 true。不要推测学生未写出的步骤。
仅返回 JSON：{"score":0,"max_score":0,"items":[{"rubric":"","score":0,"evidence":""}],"feedback":"","confidence":0.0,"needs_review":true}。
```

End 输出变量：`grading_json`。本系统只将其作为初评结果，教师复核后才发布成绩。

## 4. 将 API Key 写入项目

在服务器的 `.env` 中填入至少一把用于当前批改工作流的 Key：

```env
DIFY_API_URL=https://api.dify.ai/v1
DIFY_API_KEY=app-你的作业初评工作流服务密钥
```

本项目调用的是：

```http
POST {DIFY_API_URL}/workflows/run
Authorization: Bearer {DIFY_API_KEY}
Content-Type: application/json

{"inputs":{"chapter":"第二章 导数与微分"},"response_mode":"blocking","user":"student-20260001"}
```

这里不需要手工填 Workflow ID：Service API Key 已绑定到一个发布的 Dify 应用。`DIFY_WORKFLOW_ID` 可保留为空。

## 5. 文件上传注意事项

Dify Cloud 无法访问本项目服务器上的 `submission_file_path`。正式接入时，后端应先用 Dify 的 Files Upload API 上传扫描件，拿到 `upload_file_id`，再把该 ID 作为 `student_files` 输入变量传给 Workflow。切勿把带姓名、学号的公网文件 URL 或服务器绝对路径发送给模型。

首次联调时，先在 Dify 的 Test Run 中手工上传一份匿名 PDF/图片，确认 `grading_json` 是合法 JSON；再把 API Key 放入服务器 `.env`，用匿名测试作业调用 API。
