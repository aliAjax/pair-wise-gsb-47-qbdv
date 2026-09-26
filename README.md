# 群体伤亡医院应急扩容协调

纯Python标准库实现的群体伤亡医院应急扩容协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、伤员资源需求、医院容量和分流和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `static/handovers.html`：指挥交接台页面（`/handovers`）。
- `tests/`：完整流程、规则计算、失败场景和指挥交接测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 指挥交接台

换班时交班人选定未结束记录（reported/allocated/accepted/transferred）并写下下一班要看到的结果，接班负责人逐项签收或退回；记录归属（`owner_id`）仅在签收后转移到接班人。

- `POST /api/handovers`：发起交接批次，请求体为`{"to_user":"接班人X-User-Id","items":[{"record_id":1,"expected_outcome":"下一班要看到的结果"}]}`。只有记录当前归属人可发起；同一记录存在待签收项时拒绝重复发起。
- `GET /api/handovers?status=pending|signed|returned|void&to_user=me&from_user=me&record_id=`：交接项列表。
- `POST /api/handovers/{item_id}/sign`：接班负责人签收，记录归属转移给接班人并进入其待办。
- `POST /api/handovers/{item_id}/return`：退回，请求体为`{"reason":"退回原因"}`，归属不变，交班人可重新发起。
- `GET /api/handover-batches` / `GET /api/handover-batches/{id}`：交接批次及明细，持久化保存，重启服务仍可查询。
- `GET /api/records?owner=me`：按归属人筛选待办记录。

交接项处于待签收期间，记录一旦有业务动作（版本前进），该交接项自动置为`void`（原归属不变、无法再签收/退回），交班人需重新发起交接。所有交接动作（发起/签收/退回/作废）均写入记录审计时间线（`handover_initiated`、`handover_signed`、`handover_returned`、`handover_voided`）。

注：交接期间不冻结业务动作（现场操作不能因换班中断），而是以版本快照保证"看到的和签收的是同一份记录"。

除`/health`和`/`、`/handovers`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
