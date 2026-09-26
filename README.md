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
- `static/index.html`：演示页面（接班台、交班发起、待办、交接历史）。
- `tests/`：完整流程、规则计算、失败场景和指挥交接台测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8323
```

默认端口为`8323`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面（含指挥交接台）。
- `GET /api/records`：记录列表，可带`state`、`limit`、`unfinished=1`（仅未结束）、`owner=<用户ID>`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线（含交接事件）。
- `GET /api/records/{id}/handovers`：该记录的历次交接批次。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 指挥交接台

- `POST /api/handovers`：交班人发起交接，请求体为`{"to_user":"接班负责人ID","note":"班次交代","items":[{"record_id":1,"expected_outcome":"下一班要看到的结果"}]}`；仅未结束记录可交接，同一记录同时只能有一个待接收交接项。
- `GET /api/handover-desk`：接班负责人视角的交接台，返回`pending`（待接收）、`signed`（已签收）、`returned`（已退回）、`invalidated`（记录有新动作而作废）、`todo`（签收后进入本人待办的未结束记录）和`incoming`/`outgoing`批次。
- `GET /api/handovers?direction=incoming|outgoing&status=active|closed`：交接批次存档，重开服务后仍可查。
- `GET /api/handovers/{id}`：交接批次详情（仅交接双方或admin可见）。
- `POST /api/handover-items/{id}/sign`：接班负责人签收，请求体`{"note":"可选备注"}`；签收后记录归属转移并进入接班人待办。
- `POST /api/handover-items/{id}/return`：接班负责人退回，请求体`{"note":"退回原因（必填）"}`；记录归属不变。

交接期间记录发生新业务动作时，未决交接项自动作废（`invalidated`），需交班人重新发起交接，记录原归属不变；交接的发起、签收、退回、作废都会写入记录审计时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及交接发起、签收转移归属、退回、动作作废与重新发起、重开持久化。
