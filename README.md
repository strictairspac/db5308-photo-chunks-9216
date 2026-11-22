# 光坞影像·底片块库验收

以 SHA-256 内容寻址去重，从选中快照复原并列出损坏或缺失块。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 底片快照
- 内容块目录
- 还原查验
- 留存清单
- 占用账单

## 占用账单口径

验收记录按验收时的归属月份和制作组入账。每个内容块按实测字节计一次，在所有引用它的制作组间平分，余数按组名排序分配；同一组重复引用只算一次。账期关账时发布原始账单，关账后收到的迟到记录或撤销自动追加差额账单。账单发布后不再改写，原账单及其依据可随时通过接口回查。

入账只写记录表，不持有账期行锁，因此关账期间仍可继续收单。结算以账期行锁串行化，并以记录快照摘要做幂等：重试关账或补差不会重复出账。某月全部原始账单与差额账单逐组、逐块相加，等于修正后的实占字节。

接口：

- `POST /billing/records` 登记验收记录（含 `blocks` 或 `run_id`），`record_key` 唯一
- `POST /billing/records/{id}/reverse` 撤销记录并补差
- `POST /billing/periods/{month}/close` 关账并发布原始账单
- `POST /billing/periods/{month}/settle` 对账并补出差额账单
- `GET /billing/overview`、`/billing/records`、`/billing/periods`、`/billing/settlements`、`/billing/bills`、`/billing/summary?month=YYYY-MM` 回查

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18297`
- FastAPI 文档 `http://127.0.0.1:19297/docs`
- MySQL 8.0 端口 `127.0.0.1:17797`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
