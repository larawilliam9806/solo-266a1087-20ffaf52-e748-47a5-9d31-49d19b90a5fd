# 植物标本馆分类换版 API

用 FastAPI + SQLite 实现的标本管理服务：标本入库、分类换版核算、待重鉴名单与按种统计。

## 业务规则

- **入库**：提交标本号和种号，记入当前版；同号只计一次（重复标本号自动去重）。
- **换版**：注明现行版号，并给每个旧种号填一个或多个新种号。系统逐件按对照替换候选（候选集同号只计一次）：只剩一种便归入该种，多种则列为待重鉴并附候选。
- **重鉴**：馆员凭当前版号，从待重鉴标本的候选中选定一种。
- **核算期间**：换版核算以事务内最新状态为准，切换前已收的重鉴计入新版；按种统计与待重鉴两类名单随新版在同一事务里生效。
- **失败保护**：换版任一步失败（版号不符、对照表缺种、新版号已存在等）整体回滚，保留原版和已收的重鉴。
- **查询**：直接读算好的名单，响应附版号。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，SQLite 数据保存在命名卷 `herbarium-data` 中。
交互式文档见 `http://localhost:8000/docs`。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./herbarium.db`（容器内 `/data/herbarium.db`） | SQLite 文件路径 |
| `INITIAL_VERSION` | `1` | 首个版号 |

### 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## API

### 入库 `POST /api/specimens`

提交标本号和种号，支持单件、`{"items": [...]}` 或裸数组。

```json
{"items": [{"specimen_no": "S1", "species_no": "A"}, {"specimen_no": "S2", "species_no": "A"}]}
```

响应 `201`：`{"version": "1", "created": 2, "duplicates": 0}`

### 换版 `POST /api/version-switches`

```json
{
  "current_version": "1",
  "new_version": "2",
  "mapping": {"A": ["X", "Y"], "B": ["Z"]}
}
```

- `current_version` 必须与现行版号一致，否则 `409`；
- `new_version` 可省略，数字版号自动加一；
- 对照表须覆盖现行版实际出现的每个旧种号，每个旧种号至少一个新种号，否则 `400`。

响应 `201`：新版号、按种统计、待重鉴清单（含候选）。

### 重鉴 `POST /api/reidentifications`

```json
{"version": "2", "specimen_no": "S1", "species_no": "X"}
```

版号须为当前版号（否则 `409`），标本须在待重鉴（否则 `409`），所选种号须在候选内（否则 `400`）。

### 查询（均附版号）

| 接口 | 说明 |
| --- | --- |
| `GET /api/lists` | 两类名单：按种统计 + 待重鉴清单 |
| `GET /api/counts` | 仅按种统计 |
| `GET /api/pending` | 仅待重鉴清单 |
| `GET /api/version` | 当前版号 |
| `GET /api/specimens/{标本号}` | 单件标本当前状态 |
| `GET /health` | 健康检查 |

查询接口支持 `?version=` 查看历史版本名单。

## 数据模型

- `specimens`：标本登记册（标本号唯一，同号只计一次）；
- `specimen_states`：每版本每件标本的状态（`resolved` 归入种号 / `pending` 候选列表）；
- `species_counts`：每版本预先算好的按种统计；
- `versions` / `meta`：版本表与当前版号指针。

换版在单个 `BEGIN IMMEDIATE` 事务中完成：新名单落库与版号切换同时提交，失败整体回滚。
