# 冷链探头超温台

记录员上报探头编号与摄氏温度，后台工人用数据库行锁认领待处理队列，按 **8℃** 上限判定 **合格** 或 **超温**。

## 技术栈

| 层 | 选型 |
|----|------|
| 接口 | Python aiohttp + asyncpg |
| 工人 | `worker.py`（psycopg，`FOR UPDATE SKIP LOCKED`） |
| 页面 | Preact + Vite，nginx 反代 `/api` |
| 数据库 | PostgreSQL 16 |

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3197 |
| 接口 | http://localhost:8197 |
| PostgreSQL | localhost:54397（库名 `coldchain`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| logger | log123456 | 记录员，可提交读数、退役/恢复探头 |
| watcher | watch123456 | 值班员，只读（读数、在役/退役表与流水） |

## 启动

```bash
cd projects/18-coldchain-probe-desk
docker compose up --build
```

健康检查：`GET http://localhost:8197/api/health` → `{"status":"ok","service":"coldchain-probe-desk"}`

## 探头退役封存

记录员可在顶栏「退役管理」页将探头**退役封存**，也可将已退役探头**恢复在役**；值班员只读，只能查看三张表。

- 在役探头才能交温；已退役探头再交新温会被挡回（HTTP 409）：
  `该探头已退役封存，禁止再交新温；恢复在役后才可继续提交`。
- 退役管理页含三张表：**在役探头**（带「退役封存」按钮）、**已退役探头**（带「恢复在役」按钮）、**退役流水**（retire / restore 动作、操作人、时间，永久留痕）。
- 退役与交温在同一数据库事务内锁定探头行，几乎同时点「退役」和「提交」同一代号只有一种结局：抢在退役前的交温入队，退役后的交温一律挡回，不会出现既退役又收进新温。
- 没有任何读数记录的代号不在注册表中，无法退役（返回 404）；先交一次温即可登记为在役探头。

| 接口 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/api/probes` | GET | 登录 | 全部探头及在役/退役状态 |
| `/api/probe-events` | GET | 登录 | 退役/恢复流水 |
| `/api/probes/retire` | POST | 记录员 | 退役封存，重复退役 409 |
| `/api/probes/restore` | POST | 记录员 | 恢复在役，重复恢复 409 |

## 种子数据

| 探头 | 温度 | 结论 |
|------|------|------|
| 探头A01 | 4.2℃ | 合格 |
| 探头B02 | 12.5℃ | 超温 |

## 本地开发（可选）

```bash
# 需本机 PostgreSQL 或仅起 db 容器
cd backend && pip install -r requirements.txt && python api.py
cd backend && python worker.py
cd frontend && npm install && npm run dev
```

接口进程默认监听容器内 **8000**，对外映射 **8197**。
