# 生产部署（8001 Agent 服务）

## 目标

此 Compose 将 Web API、批改 Worker、截止时间 Scheduler 作为三个独立容器运行，
数据保存在 Docker 卷 `math_agent_data`。8014 工作台和 GPU Qwen 继续由宿主机的
8014/18080 服务提供，容器通过 `host.docker.internal` 访问它们。

## 首次部署

```bash
cd ~/math-agent/src/agent8000/deploy
cp .env.production.example .env.production
# 编辑 .env.production，设置唯一管理员账号和密码
docker compose -f docker-compose.production.yml --env-file .env.production up -d --build
docker compose -f docker-compose.production.yml ps
curl -fsS http://127.0.0.1:8001/healthz
```

在切换前，先停止旧的手工 uvicorn 8001 进程；8014 与 18080 保持运行。

## 日常检查

```bash
docker compose -f docker-compose.production.yml logs -f api worker
curl -fsS http://127.0.0.1:8001/healthz
docker compose -f docker-compose.production.yml ps
```

`healthz` 仅检查服务进程、存储目录和数据库连接，不泄露学生资料。

## 更新与回滚

更新前保留上一镜像标签或提交号。更新后先执行健康检查，再让反向代理/隧道切流。
若异常：

```bash
docker compose -f docker-compose.production.yml down
# 切回上一份代码或上一镜像标签
docker compose -f docker-compose.production.yml --env-file .env.production up -d
```

Docker 卷不会被 `down` 删除；只有明确执行 `down -v` 才会删除持久数据，生产环境禁止这样操作。
