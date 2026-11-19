# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10836 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 聚合切断（停用源不进水位）

- **缺陷口径**：停用源仍进水位聚合抬高绿灯、史迹被误删。本仓已切断。
- **择定甲路径**（当日不与乙路混用）：停用牌写入与水位剔除捆成同库提交，失败则整段撤销；读侧不删史迹。
- **聚合切断卡**：择定后写入 `agg_card` 并封档（`GET /api/agg/card`），当日不许改口；偏离卡的请求拒写并在切断册记「聚合偏了」。
- **聚合锁**：停用与水位刷新同时进行须占锁，后交只记「停用聚合争用」（409），痕迹入 `agg_events`（`GET /api/agg/events`）。
- **排除与史迹**：策略卡片（`GET /api/aggregate` 读 `waterline` 落库）、源目录（`GET /api/sources` 默认排除停用，`?include_disabled=1` 查全量）、水位队列排除停用；到达详情与对账附页（`/api/arrivals`、`/api/catchups`）史迹可读，禁止物理删除。
- **入口**：停用/启用沿用时限台现页「滞后分级」页签，不另建聚合页；登录会话沿用聚合侧；库文件挂聚合专卷 `./data:/app/data`。

| 接口 | 说明 |
|----|-----|
| `POST /api/sources/{name}/disable` | 停用：停用牌+水位剔除同库提交，失败整段撤销 |
| `POST /api/sources/{name}/enable` | 启用回挂：与水位并入同库提交 |
| `GET /api/aggregate` | 水位聚合读数（在册/绿灯/冻结/降级/已停用） |
| `GET /api/agg/card` · `GET /api/agg/events` | 聚合切断卡 · 聚合切断册 |

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10836/  
健康检查：GET http://127.0.0.1:10836/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
