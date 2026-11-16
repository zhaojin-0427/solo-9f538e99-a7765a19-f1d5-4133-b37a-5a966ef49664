# 实验室宽表 → 长表在线迁移 + 质控签发

把旧采集程序的宽表 `samples(id, temperature, ph)` 在线迁移为按指标存放的长表
`sample_metrics(sample_id, metric, value)`，并提供 FastAPI 接口用于**发起迁移、查询进度、按样本/按指标查询**。
迁移期间旧程序可继续用原 SQL 增删改；切换是单事务原子完成的，切换后 `samples` 变成带
`INSTEAD OF` 触发器的兼容视图，旧 `SELECT / INSERT / UPDATE / DELETE` 无需改动即可继续工作。

在此之上提供**质控签发**：一次接收至多 1000 个互异样本 ID 和温度、pH 两个闭区间，按同一已提交
状态核对后才生成不可变批次；任何样本有问题都会返回该样本及原因且不落批次。签发时的原值被
快照保存，之后旧程序的更正不会改变已签发内容。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d

# 健康检查
curl -s localhost:8000/healthz

# 发起迁移（幂等）
curl -s -X POST localhost:8000/migrations

# 查询进度
curl -s localhost:8000/migrations/current

# 按样本查指标
curl -s localhost:8000/samples/1/metrics
# {"sample_id":1,"temperature":36.5,"ph":7.2}

# 按指标装载长表（keyset 分页）
curl -s 'localhost:8000/metrics/temperature?limit=5'

# 质控签发：全部合格才生成批次（201）；有任何问题返回 422 + 逐样本原因
curl -s -X POST localhost:8000/issuances \
  -H 'Content-Type: application/json' \
  -d '{"sample_ids":[1,2,5],
       "temperature":{"min":36.0,"max":37.2},
       "ph":{"min":7.0,"max":7.5}}'
# {"action":"issued","batch":{"batch_id":1,...,"sample_count":3,"source":"wide_table"}}

# 读回签发时的原值（按样本 ID 键集分页；后来更正不影响）
curl -s 'localhost:8000/issuances/1/items?limit=100'
curl -s 'localhost:8000/issuances?limit=10'

# 迁移期间/切换后用旧程序直接读写 samples
docker compose exec legacy-acquisition \
  python -m legacy.acquisition --db /app/data/lab.db select 1
docker compose exec legacy-acquisition \
  python -m legacy.acquisition --db /app/data/lab.db update 1 --temperature 37.0
docker compose exec legacy-acquisition \
  python -m legacy.acquisition --db /app/data/lab.db insert --temperature 36.6 --ph 7.1
```

API 文档：<http://localhost:8000/docs>。

演示脚本（需 API 已启动、数据库为初始宽表状态）：

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=. python scripts/demo.py --db data/lab.db --api http://127.0.0.1:8000
```

本地非 Docker 启动：

```bash
python -m pip install -r requirements.txt
DATABASE_PATH=data/lab.db SEED_DATABASE_PATH=seed/lab_wide.db \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动时，若数据库不存在，会把只读的示例库 `seed/lab_wide.db`（含 18 行宽表，
覆盖 NULL、id 空洞）复制到 `DATABASE_PATH`。示例库本身永不被修改。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/migrations` | 发起迁移；幂等。已完成 → `action=reused` 不重建；运行中 → 返回进度；失败 → 从检查点续跑 |
| GET | `/migrations/current` | 迁移状态与进度（`not_started / copying / completed / failed`） |
| GET | `/migrations/current/events` | 迁移事件日志 |
| GET | `/metrics/{metric}` | 按指标装载长表，`after_id` + `limit` 分页；`metric ∈ {temperature, ph}` |
| GET | `/samples/{sample_id}/metrics` | 按样本查指标（缺失返回 404） |
| GET | `/samples` | 兼容视图分页读取（等价旧程序 SELECT） |
| GET | `/samples/{sample_id}` | 兼容视图单行读取 |
| POST | `/issuances` | 质控签发；校验全部通过 → 201 批次，否则 422 + 逐样本原因，不落批次 |
| GET | `/issuances` | 按 `after_batch_id` + `limit` 分页列出批次摘要 |
| GET | `/issuances/{batch_id}` | 批次摘要（含所用闭区间、样本数、读取来源） |
| GET | `/issuances/{batch_id}/items` | 签发时原值，`after_sample_id` + `limit` 键集分页 |
| GET | `/issuances/max-samples` | 单批上限（1000） |
| GET | `/healthz` | 健康检查 + 迁移状态 |

### 质控签发规则

请求体：

```json
{
  "sample_ids": [1, 2, 5],
  "temperature": {"min": 36.0, "max": 37.2},
  "ph": {"min": 7.0, "max": 7.5}
}
```

- `sample_ids` 必须非空、至多 1000 个、互不相同、为整数（`true`/`1.0`/字符串均拒绝）。
- 两个区间均为闭区间且 `min <= max`；`temperature` / `ph` 都是必填。
- 全部样本在**同一个已提交快照**中检查：样本必须存在，两项数值都非 NULL、有限且落在闭区间内。
- 任一不合格 → HTTP 422，响应含每个问题样本的 `reasons`（`not_found` / `null` /
  `out_of_range` / `not_numeric`），**不会生成任何批次**（不做部分签发）。
- 全部合格 → HTTP 201，保存批次（所用区间、样本数、读取来源）和每个样本签发时的
  `temperature` / `ph` 原值。批次与明细表由 schema 触发器强制 append-only：
  `UPDATE` / `DELETE`（含 `INSERT OR REPLACE`）都会报 `issued ... immutable`。
- 批次读取按 `batch_id` / `sample_id` 键集分页，不依赖 `OFFSET`。

### 签发与迁移、旧程序更正如何共存

- **不长时间锁写**：核查第一阶段是只读事务（`BEGIN`），只读取 `samples`；旧采集程序
  的更正/插入/删除可以继续提交，1000 个样本的核查期间不会持有 SQLite 写锁。
- **提交前复核**：真正写入是一个很短的 `BEGIN IMMEDIATE` 事务，在写锁内**重新读取并重新
  校验**同一批样本，再插入批次和明细。若核查后有更正先提交：
  - 新值仍合格 → 用最新值签发（批次保存的是复核时的一致值）；
  - 新值不合格（含被删除）→ 422 拒绝，且不留批次。
- **不混用搬迁中的长表**：签发只读 `samples` 这个名称——迁移期间它是真实宽表，切换后它是
  兼容视图。单条读事务不会跨越原子切换，也从不直接读还在拷贝中的 `sample_metrics`，
  因此不会把宽表与半拷贝的长表混在一起判断。
- **不可变**：签发时把原值复制进 `issuance_items`，之后旧程序的更正既不改变批次内容，
  也不受批次影响。

## 关键保证与实现

### 迁移期间旧程序可写，迟到搬迁不覆盖更正

- 宽表 `samples` 在迁移期间**仍是真实表**，旧程序原样读写。
- 启动迁移的同一个事务里装上三个 `AFTER INSERT/UPDATE/DELETE` 触发器，把每笔**已提交**的
  旧程序写入实时镜像进 `sample_metrics`（含 id 变更、删除、同号重建）。
- 搬迁按 `id` 顺序分块，每块只 `INSERT ... ON CONFLICT DO NOTHING`：**只补缺，不覆盖**。
  因此更晚执行的块不可能把触发器已写入的新值改回旧值；删除过的样本不会被重新拷回
  （分块时只读当时仍存在的 id）。
- 切换前最后一个 `BEGIN IMMEDIATE` 事务做终态追赶（以宽表为准 upsert）、删除已删样本的
  长表行、校验“每样本恰好 2 行”。

### 原子切换、不保留宽表副本

- 同一事务内：drop 镜像触发器 → `DROP TABLE samples` → 创建 `samples` 视图 + 三个
  `INSTEAD OF` 触发器。SQLite DDL 是事务性的，失败整体回滚，宽表原样保留。
- WAL 模式下读者只会看到“旧表”或“新视图”，不会看到中间态；每次 API 查询在单个读事务内完成。
- 切换后只有长表 `sample_metrics`，没有宽表副本；样本 id、数值、NULL 均保持不变，
  每个样本每项指标恰好一行（NULL 也有一行）。

### 切换后旧 SQL 继续有效

- `samples` 视图列名/顺序与旧表一致；`SELECT *`、按 id 等值/排序查询均可用。
- `INSERT`：显式重复 id 抛 `UNIQUE constraint failed: samples.id`；省略 id 时按原表语义分配
  `MAX(id)+1`；两指标行一次写入。
- `UPDATE`：改单个指标只覆盖该指标；`SET id=` 会整体搬移；写 NULL 会落成 NULL 行；
  `SET id = NULL` 与原表一样报 `datatype mismatch`，改成已存在的 id 报唯一约束错误。
- `DELETE`：删除该样本的两条指标行；同号重建正常。
- 旧程序提交的写入立刻出现在新 API 的查询里（两者读同一份已提交状态）。

> 说明：SQLite 不允许触发器修改 `last_insert_rowid()`。旧程序若 INSERT 时省略 id，
> 兼容层把分配的 id 记录在 `_insert_context`（单行辅助表）。仓库内的旧程序客户端
> `legacy/acquisition.py` 会读取它；第三方旧程序若依赖 `cursor.lastrowid`，请改用该表
> 或显式传 id。

### 中断重启、失败与幂等

- 进度 `last_id` 与数据块在同一事务提交；进程被杀后重启（`AUTO_RESUME=true`）从检查点续跑。
- 任意时刻崩溃：切换提交前的状态都保留宽表，旧程序仍可用；已提交数据不丢。
- 迁移失败（如磁盘错误）状态记为 `failed` 并保留错误；**不会自动重试**，避免崩溃循环，
  再次 `POST /migrations` 从检查点续跑。
- 已完成的迁移再次 `POST` 不会重建（提交时间、数据均不变）。
- 若切换事务已提交但进程在记录状态前崩溃，启动时会识别到 `samples` 已是视图，
  补记为 `completed`，不会重复迁移。

## 代码结构

```
app/config.py       环境变量配置
app/db.py           SQLite 连接 / 事务助手
app/schema.py       宽表、长表、迁移触发器、兼容视图与触发器、签发批次表
app/migration.py    在线分块复制 + 原子切换 + 断点续跑
app/repository.py   只读快照查询（按样本 / 按指标 / 兼容视图）
app/issuance.py     质控签发：校验、复核、不可变批次、分页读回
app/main.py         FastAPI 路由
legacy/acquisition.py  旧采集程序客户端（原样 SQL，可 CLI 使用）
seed/lab_wide.db    示例宽表库（只读样例，含 NULL 与 id 空洞）
scripts/make_seed.py   重新生成示例库
scripts/demo.py        端到端演示脚本
tests/                 单元 + API + 并发/中断测试
```

## 测试

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=. python -m pytest -q
```

覆盖：数值/NULL 保真、每样本每指标一行、旧程序切换后 CRUD 与重复 id、同号删除重建、
迁移中并发更正/删除/重建/新插入不被迟到块覆盖、失败重试、进程内中断续跑、
重复发起不重建、API 分页与校验、旧写入对 API 立即可见；签发侧覆盖缺样本/NULL/越界逐样本
原因且不落批次、闭区间边界、1000 个互异样本与分页读回、重复/非法请求形状、迁移前后读取来源
（宽表 → 兼容视图）、批次存储级不可变、核查后更正被复核（拒绝或采用新值）、长批核查期间旧程序
写入可提交。

## 已知限制

- SQLite 是单写者：切换事务执行期间旧程序的写会短暂等待（默认 busy timeout 30s），
  不会失败；切换本身只做一次全表追赶，数据量极大时可考虑分批预拷贝（本实现已预拷贝）。
- 迁移进程内嵌在 API 进程，由单实例部署；多副本同时运行会让它们竞争同一状态行
  （有事务保护不会损坏，但不建议），如需水平扩展请把迁移放到独立 job 并保持单执行者。
- 兼容视图按 `GROUP BY` 读取；单样本等值查询走主键，大范围全表扫描较真实表慢，
  这是不保留宽表副本的取舍。
- 未在此环境中实际执行 `docker compose up`（沙箱无 Docker daemon/CLI）：Compose/Dockerfile
  已完成静态校验，并用等价的本地 uvicorn 进程 + 同一份 `data` 路径 + 同一组环境变量做过
  端到端验证（见下方「自测结果」）。
- 旧程序若在 `INSERT` 中使用 `INSERT OR IGNORE/REPLACE` 依赖冲突忽略/替换语义，
  兼容视图的 `RAISE(ABORT)` 会直接报错（原表也报错，行为一致但不会“忽略”）。
- 签发批次没有撤销/作废流程（按要求不可变）；如需作废只能新增一条反向记录，当前未实现。
- SQLite 单写者：签发的复核写事务很短，但恰好与迁移切换事务重叠时会排队等待 busy timeout
  （默认 30s），不会失败。

## 自测结果（本次改动）

环境：Linux + Python 3.11.2 + SQLite 3.40.1（沙箱**没有 Docker CLI/daemon**）。

- `PYTHONPATH=. python -m pytest -q`：**50 passed**（原有 34 项 + 新增签发 16 项），连续运行 3 次稳定。
- 本地以与 Compose 相同的环境变量、端口换为 8123/8124 等启动 `uvicorn app.main:app`，实际请求验证
  （含用 `scripts/demo.py` 完整跑通迁移 + 签发，退出码 0）：
  - `GET /docs` 200，`/openapi.json` 含 `/issuances` 全部路由与请求模型；
  - 缺样本 / NULL / 越界均返回 422 且逐样本给出原因，批次表保持 0 行；
  - 合格请求返回 201，`source=wide_table`；签发后旧程序更正 id=1，批次原值不变而当前值变化；
  - 发起迁移至 `completed` 后再次签发返回 201 且 `source=compatibility_view`，旧批次内容不变；
  - 直接用 sqlite3 执行 `UPDATE issuance_items` 被触发器拒绝（`... immutable`）；
  - 1000 个样本的批次实际签发成功，`limit=333` 键集分页读回 1000 条且顺序/内容一致；
    不合规请求前后 `issuance_batches` 行数不变；
  - 在迁移拷贝与原子切换期间循环签发（4 轮共 ~400 次），无 5xx、无锁错误，来源从
    `wide_table` 平滑过渡到 `compatibility_view`。
- 未执行 `docker compose up`（环境无 Docker CLI/daemon），因此 Compose 的**实际启动**未在
  本环境验证；Dockerfile/Compose 仅做了静态检查（YAML 可解析、端口/卷/环境变量与实际运行一致）。
