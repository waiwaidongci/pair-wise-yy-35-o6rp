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
- `POST /api/items/{id}/correction`，提交`new_quantity`、`reason`和`expected_version`更正剂量
- `GET /api/items/{id}/corrections`，查询保留的新旧值更正历史
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

### 剂量更正

剂量填错后不再新建事件，而是由dosimetrist在详情页提交更正：

- 系统保留每次更正的旧值、新值、原因、基准/新版本和操作人（见`dose_corrections`表）。
- 按新读数重算优先级、响应期限与`escalation_required`。
- 事件处于调查或随访时退回到`reviewing`待复评；已关闭事件保留原关闭记录，重新打开并生成`re_review`复评待办。
- 更正同样采用乐观锁：两人基于同一版本提交时，后提交者收到409版本冲突，只有先提交者的变更与`correct_dose`审计事件落库。
- 更新、更正历史、复评待办和审计事件在同一SQLite事务内写入，重启服务后审计哈希链仍可逐笔核对来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
