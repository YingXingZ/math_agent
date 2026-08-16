# 高数作业助手

面向高等数学课程的作业闭环 MVP：题库管理、章节组卷、学生提交、到期批改任务和成绩汇总。

## 已实现的第一阶段

- 结构化题库：题目、章节、难度、题型、标准答案、评分细则。
- 教师按章节、题量、难度分布创建作业，并生成适合 A4 打印的 HTML 作业单。
- 学生按学号提交 PDF、图片或 Word 作业；文件与元数据分离归档。
- 截止时间到达后自动创建批改任务；教师可查看汇总和待复核项。
- Dify 工作流适配器：配置 `DIFY_API_URL`、`DIFY_API_KEY` 后即可调用外部工作流；未配置时使用安全的本地占位结果。

## 本地运行

```powershell
cd "D:\My File\大四\高数教材答案\高数作业助手"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/docs` 查看接口，或访问根路径使用教师工作台。

## 下一步接入

1. 配置 OCR/公式识别服务，将教材页面识别结果投递至 `POST /api/questions`。
2. 在 Dify 创建“题目结构化”“智能组卷”“主观题批改”工作流，填入 `.env`。
3. 将 SQLite 换为 PostgreSQL、上传目录换为 MinIO/OSS，再接入校园统一认证。

> 自动批改结果必须保留教师复核入口，尤其是含手写推导的主观题。
