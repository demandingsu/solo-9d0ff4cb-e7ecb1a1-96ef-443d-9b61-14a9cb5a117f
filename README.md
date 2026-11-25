# 公交验票终端换包 API

FastAPI + SQLite 实现的验票规则包换版服务。数据员提交**候选包、在用包号和批复的新增/撤销清单**，
系统核对实际增减与批复是否逐项相符，通过后在换版时复核在用包号仍一致，候选包才生效。

## 规则模型

- 包内按**票种**列**车站清单**、**介质清单**和**禁用搭配**。
- 有效搭配 = 车站 × 介质（笛卡尔积）− 禁用搭配；车站与介质可任意组合，禁用的除外。
- 每条批复 = 票种 + 车站 + 介质，分**新增**、**撤销**两类清单。
- **首次换包**基准留空（`base_package_no: null`），按空包核对：全部有效搭配须列入批复新增，批复撤销须为空。

## 核对报告

核对**不逐一枚举**车站与介质的搭配，只汇总：

| 字段 | 含义 |
| --- | --- |
| `over_changed` | **多改**：实际改动超出批复（项数 + 一例） |
| `under_changed` | **漏改**：批复要求但未落实（项数 + 一例） |
| `actual_additions` / `actual_removals` | 实际增/减搭配数 |
| `approved_additions` / `approved_removals` | 批复增/减条数（去重后） |

`result = pass`（多改、漏改均为 0）时提交单状态为 `verified`，方可换版。

## 接口（详见 /docs）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/submissions` | 交包并核对（候选包 + 在用包号 + 批复清单） |
| POST | `/submissions/{id}/activate` | 换版生效（事务内复核在用包号仍一致） |
| GET | `/packages/current` | 查在用包 |
| GET | `/packages/{package_no}` | 按包号查包（候选/在用/已退役） |
| GET | `/packages` | 包列表 |
| GET | `/submissions` / `/submissions/{id}` | 提交单列表 / 详情（含核对报告） |

换版语义：

- 仅 `verified` 的提交单可换版；`rejected` / `stale` / 已 `activated` 均返回 409。
- 换版在 `BEGIN IMMEDIATE` 事务内复核**替换时在用包号仍与提交基准一致**；
  交错换包时后到者失败（提交单置为 `stale`），**在用包照常可查**。
- 交包时基准包号须等于当前在用包号（首次须留空），否则 409。

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `8000` 端口，打开 <http://localhost:8000/docs> 即可交包、查包。
SQLite 数据库落在命名卷 `package-data`（容器内 `/data/packages.db`），重启不丢在用包。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

## 自测

```bash
.venv/bin/python -m pytest -q        # 单元/接口测试（16 项）
.venv/bin/python scripts/smoke.py    # 对运行中的服务做端到端冒烟
```

覆盖：首次空包核对、多改/漏改计数与举例、禁用搭配增撤、交错换包（含 8 线程并发仅一个生效）、
换版失败后在用包可查、包号查重、基准不符拒绝等。

## 目录

```
app/
  main.py    # 路由与交包/换版流程
  core.py    # 有效搭配集合与批复比对（纯函数）
  models.py  # Pydantic 模型（/docs 中文说明）
  store.py   # SQLite 持久化与事务换版
tests/test_api.py
scripts/smoke.py
Dockerfile / docker-compose.yml
```
