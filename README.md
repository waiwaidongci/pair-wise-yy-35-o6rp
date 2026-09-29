# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/corrections`
- `POST /api/items/{id}/corrections`，必须提交`new_quantity`、`reason`和`expected_version`
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

### 剂量更正

剂量人员（dosimetrist）在详情页提交`new_quantity`、`reason`和当前`expected_version`即可更正填错的读数，无需新建事件：

- 新旧读数、原因、版本与操作人写入`dose_corrections`并永久保留，`GET /api/items/{id}`返回`corrections`历史。
- 系统按新读数重算`priority`、`deadline_hours`与`escalation_required`。
- 事件处于`investigation`/`follow_up`时自动退回`reviewing`待复评，并把已关闭的调查/随访记录重开；处于`closed`时保留关闭状态与全部关闭记录，仅新增一条`re_review`复评待办记录。
- 两人同时基于同一版本更正时，后提交者收到`409 ConflictError`，只有先提交者的变更与`correct_dose`审计记录落库；审计事件接入SHA-256哈希链，重启后可通过`verify_audit_chain`查清每次改动的来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
