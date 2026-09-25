# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、补件暂停/顺延（tolling）和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 补件暂停与决定期限顺延（tolling）

- `request_evidence`发出补件通知时记录暂停段：暂停开始日`request_day`、补件截止日`due_day = request_day + allowed_days`，决定时钟进入`paused`，等待期间经办人执行`decide`会被拒绝（409）。
- `respond`收到材料时按实际等待天数顺延决定日：`tolled_days = min(收到日 - 通知日, allowed_days)`。按时收到按实际等待顺延；补件逾期则只顺延到截止日，截止日之后的天数继续消耗决定期限。逾期回应不再被拒绝，暂停段会标记`late=true`。
- 支持多次补件，每段独立编号并累计顺延。
- 记录详情与动作返回中带有`deadline`视图，区分：
  - `original_deadline_day`：收案时定死的原决定日；
  - `pauses`：各段暂停（通知日、截止日、收到日、等待天数、顺延天数、是否逾期）；
  - `current_deadline_day`：当前决定日；`clock_state`为`paused`时`clock_day`冻结在暂停开始日。
- 暂停段与决定日持久化在记录payload中，服务重开后记录详情和`/api/records/{id}/audit`仍可查回；审计事件的`details.deadline`记录每次动作前后的期限变化。
- `/api/stats`返回`by_state`状态计数，以及`paused_for_evidence`（等待补件中）、`deadline_overdue`（决定期限已逾期）、`overdue_evidence_responses`（发生过补件逾期）、`multi_pause_cases`（多次补件）、`total_tolled_days`（累计顺延天数）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、补件暂停与顺延（含逾期、多次补件、重开恢复）、重复引用、权限拒绝和版本冲突。
