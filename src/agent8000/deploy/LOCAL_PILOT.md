# Windows 单机试点操作

此模式面向教师在本机或校内受控电脑上试用；8000 与 8014 仅监听 `127.0.0.1`，不会直接暴露到公网。

1. 双击或在命令行运行 `start_windows.bat`。
2. 等待两个服务窗口启动完成，运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\healthcheck_windows.ps1
   ```

3. 教师/学生主入口：`http://127.0.0.1:8000/`；OCR 复核：`http://127.0.0.1:8014/ocr-repair`。
4. 每次做大量题库修订前运行 `backup_databases.ps1`；它只复制两个 SQLite 数据库到仓库 `backups/`。

正式面向学生上线前仍需学校或服务器侧提供：HTTPS 域名、统一认证或受控 VPN、教师管理台访问控制，以及合规的备份位置。不要直接把 8000/8014 端口暴露到公网。
