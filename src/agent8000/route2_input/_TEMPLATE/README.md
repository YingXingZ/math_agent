# Route 2 输入模板

复制本目录为 `route2_input/<章节>/`，放置该章节每题的裁切图到 `crops/`，
并按需编辑 `manifest.json`（键名 = 裁切图文件名，含扩展名）。

完成后运行（详见 `高数作业助手/ROUTE2_IMPORT_GUIDE.md`）：

    VENV=C:/Users/YXZ/.workbuddy/binaries/python/envs/default/Scripts/python.exe
    $VENV route2_chapter_importer.py --chapter <章节> --input route2_input/<章节> --dry-run
    $VENV route2_chapter_importer.py --chapter <章节> --input route2_input/<章节> --push

注意：
- 每图 = 一题；含小问用 `<章节>-<题号>-<小问>.png`。
- `std_answer` 留空 → 该题导入后标记 unverified，待教师补答案，不会以猜测值发布。
- 脚本不伪造任何数学内容，原题与答案须由教师/助教提供。
