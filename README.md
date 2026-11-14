# 数据洞察与自助分析平台

面向业务分析师的自助式数据洞察平台。覆盖数据集市构建、自助SQL查询、可视化拖拽分析、分析看板协作、指标异动归因、分析模型沉淀、查询优化建议与数据故事编排的完整洞察链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据集市构建 | `mart` | 集市定义 | 集市编号、集市名称、数据源表 |
| 自助SQL查询 | `sqlpad` | 查询脚本 | 脚本编号、脚本名称、SQL语句 |
| 拖拽可视化 | `vizdrag` | 可视化方案 | 方案编号、方案名称、数据源绑定 |
| 看板协作管理 | `collab` | 协作看板 | 看板编号、看板名称、协作成员 |
| 指标异动归因 | `anomaly` | 异动记录 | 记录编号、指标名称、异动方向 |
| 分析模型沉淀 | `model` | 模型注册 | 模型编号、模型名称、模型类型 |
| 查询优化建议 | `opt` | 优化建议 | 建议编号、目标查询、优化类型 |
| 数据故事编排 | `story` | 故事模板 | 模板编号、故事标题、分析主题 |
| 数据准备流水线 | `etl` | 准备任务 | 任务编号、任务名称、输入源 |
| 数据目录检索 | `catalog` | 目录条目 | 条目编号、数据集名称、业务域、字段清单、更新频率、数据等级 |
| 数据采样管理 | `sample` | 采样配置 | 配置编号、数据源名称、采样算法、输出表名 |
| 同环比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析导出中心 | `export` | 导出任务 | 任务编号、数据源名称、导出格式 |
| 分析告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 数据定时刷新 | `refresh` | 刷新策略 | 策略编号、目标数据集、刷新周期 |
| 查询审计日志 | `audit` | 审计记录 | 记录编号、查询语句、执行人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 数据目录增量更新与采样引用

字段清单、数据等级与采样引用是一条链路，规则集中在
`backend/app/services/catalog_fields.py`（纯规则）和
`backend/app/services/catalog.py` / `sample.py`（状态与引用）：

- **只重算受影响的条目**：字段清单或更新频率变了才升版本、重算数据等级；
  其余条目连版本号和快照都不动。批量更新按 `id` 逐条判定，返回
  `affected` / `unchanged` / `invalid` / `conflict` 四种结果。
- **数据等级规则**：字段命中高敏感词（手机号、身份证、薪酬等）为 L4，命中普通
  敏感词（金额、客户、订单等）为 L3，其余默认 L2；更新频率为实时/每小时时抬一级，
  上限 L4。等级判据是“字段签名 + 更新频率”，判据没变不重复重算。
- **下线立即移出可查看清单**：列表与导出默认只给 `草稿`/`已发布`；`已下线`/
  `已归档` 需显式 `include_hidden=true`（或 `status=已下线`）才返回，单条明细仍可追溯。
- **历史快照按当时等级保留**：每次字段变化新增一份快照，旧快照的数据等级不会被
  后续重算改写；同一份字段清单重复提交只保留一个版本。
- **并发优先级**：同一数据集同一时刻只允许一个批次写入。版本号即优先级——先通过
  字段校验的批次写入，后到批次不落库，返回 `conflict.已落定`（当前版本、字段、
  等级、批次号）。可用 `字段版本` 显式声明读到的版本，不传则以进入更新时的版本为基准。
- **定位口径**：字段变化后按 `业务域 → 条目编号` 排序返回受影响记录及 `定位序号`；
  重新进入列表时 `level` / `level_min` 仍然生效。

主要接口（前缀 `/api/catalog` 与 `/api/sample`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/catalog/{id}/fields` | 单条字段清单增量更新，带 `字段清单`、`更新频率`、`批次号`、可选 `字段版本` |
| POST | `/api/catalog/{id}/fields/preview` | 只算不写，返回重算后的等级 |
| GET | `/api/catalog/{id}/snapshots` | 历史字段清单快照（含当时等级） |
| POST | `/api/catalog/fields/batch` | 批量字段更新，只重算受影响条目 |
| POST | `/api/catalog/refresh` | 按 `entry_id`/`条目编号`/`业务域`/`字段变更` 增量刷新并定位 |
| POST | `/api/catalog/refresh-all` | 巡检式重算，未变化条目不升版本 |
| GET | `/api/catalog/viewable`、`/api/catalog/hidden` | 可查看 / 已下线清单 |
| GET | `/api/sample/references` | 样本输出表引用核对（字段、等级、引用状态） |
| GET | `/api/sample/references/check/{id}` | 单条采样引用与分层字段核对 |

回归用例：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

领域：数据工程
子方向：数仓与数据湖
