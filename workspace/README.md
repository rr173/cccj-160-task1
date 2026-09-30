# 大模型提示模板 · 版本治理与可重演服务

模板 = **系统指令 + 变量契约 + 工具权限 + 模型参数（+ model_id）**。
系统对模板做不可变版本化、发布前固定测试集证据闸门、原子热更新/回滚、
线上请求精确元数据绑定，以及沿 **INPUT / MODEL / TEMPLATE** 三轴归因的重演。

零第三方依赖：Python 3.10+ 标准库（`http.server` + SQLite WAL）。

## 快速开始

```bash
python3 -m app.server --db data/governance.db --port 8080
# 浏览器：
#   /                模板注册表 / 版本 / 发布闸门 / 热更新 / 在线推理
#   /evaluate.html   评估页面（固定测试集、证据、迟到标记）
#   /replay.html     重演报告（三轴归因）
```

端到端演示脚本（覆盖需求中全部场景）：

```bash
python3 scripts/demo.py http://localhost:8080
```

测试：

```bash
python3 -m unittest discover -s tests -v     # 27 tests
```

## 需求 → 实现对照

| 需求 | 实现 |
|---|---|
| 模板四要素 | `app/canonical.py:normalize_body` 规范化 → `content_hash`；版本再有 `version_hash`（含 template_id/semver/作者） |
| 版本不可变 | `template_versions` 行内容字段写入后不再改；同内容重复版本被 409；只有 `status`/证据快照经受控流转更新 |
| 固定测试集 + 发布前评估证据 | 测试集可 `freeze`（首次评估也会自动冻结），评估记录 `testset_hash`；逐用例记录 model revision/fingerprint、prompt_sha；`evidence_hash = H(rows + summary + testset_hash)` |
| 发布闸门 | 必须有 **冻结测试集上 100% 通过的完成评估** + `publish` 审批；失败返回 412（证据/闸门）或 403（权限审批） |
| 评估结果迟到 | 发布后完成/启动的评估一律 `late=1` 并回填 `publish_ref`；**发布时钉住的 evidence_hash 永不被覆盖**（测试 `test_published_version_is_immutable`） |
| 线上请求绑定 | 每个请求落库：`version_hash`、`semver`、`model_id`、`model_revision`、`model_fingerprint`、`input_digest`、`params`、`prompt_sha`、`output` |
| 重演区分三类变化 | `INPUT`：input_digest 比较；`MODEL`：同 model_id 下指纹比较（模拟上游静默换版）；`TEMPLATE`：version/content hash + 字段级 diff。三轴皆未变而输出不同 = `unexpected_drift` |
| 热更新 / 并发回滚只见一个完整版本 | 每个模板单行 `active_deployment` 指针，切换在单事务 `BEGIN IMMEDIATE` + 进程写锁内完成；读者永远读到旧的或新的完整版本。冲突切换 409，支持 `expected_active` 乐观锁 |
| 旧请求按原元数据复核 | 请求归档不随部署变化；重演 `mode=recorded` 始终按归档版本+归档模型参数执行，`mode=active` 才用当前线上版本 |
| 变量契约升级 | 结构 diff + 语义分级：新增必填/删变量/改类型/枚举收窄 = major（破坏）；新增可选/放宽 = minor；见 `app/diffing.py`。请求按契约校验，未声明变量拒绝，保证 input_digest 语义无歧义 |
| 工具权限扩大必须重新审批 | 新相对上一版本增加 grant → 版本记 `expansion_of_hash` + grants 快照哈希；发布时强制存在 **针对该版本、且 grants_snapshot_hash 匹配** 的 `permission_expansion` 已批准审批；审批被拒后需重新申请；审批后 grants 快照漂移会再次拦截 |
| 模板 API / 评估页面 / 重演报告 | JSON API（下）+ 三个内置页面 |

### 版本号与变更分级

`1.0.0` 起步，创建版本时自动推导（也可显式传 semver，必须递增）：

- `patch`：仅系统指令文本变化
- `minor`：向后兼容的契约变化（新增可选变量、枚举放宽）、工具权限收窄、模型参数调整
- `major`：契约破坏（新增必填、删除变量、改类型）、model_id 切换、**工具权限扩大**

## 关键 API

```
GET  /api/meta
POST /api/templates                       {name, description, actor}
GET  /api/templates/{id}
POST /api/templates/{id}/versions         {system_instructions, variables, tools,
                                           model_params, model_id, semver?, actor}
GET  /api/templates/{id}/versions
POST /api/versions/{vh}/evaluate          {testset_id, delay?, background?, actor}
POST /api/versions/{vh}/approvals         {kind: publish|permission_expansion, reason}
POST /api/approvals/{aid}/decision        {approve, note, actor}
POST /api/versions/{vh}/publish           {actor}
GET  /api/versions/diff/{a}/{b}           字段级差异
POST /api/templates/{id}/activate         {version_hash, expected_active?, reason}
POST /api/templates/{id}/rollback         {version_hash, expected_active?, reason}
GET  /api/templates/{id}/deployments

POST /api/testsets                        {name, cases:[{case_id,inputs,expect}]}
POST /api/testsets/{id}/freeze
GET  /api/evaluations/{id}                含逐用例结果与模型指纹
POST /api/evaluations/finish              {timeout}   # 等待后台（迟到）评估

POST /api/templates/{id}/infer            {inputs}                 # 线上推理
GET  /api/requests/{rid}                  # 归档元数据 + 输出
POST /api/requests/{rid}/replay           {mode: recorded|active, inputs?}
GET  /api/replays/{rpid}                  # 重演报告

POST /api/models/rotate                   {model_id, revision}     # 模拟上游静默换版
GET  /api/audit                           # 全量审计流水
```

重演报告结论（`verdict`）：

- `exact_reproduction`：执行相关轴均未变且输出逐字一致
- `changed_but_stable`：有关联变化轴，但输出恰好稳定
- `changed_output`：输出变化，`primary_axis` 给出最可能主因（MODEL > INPUT > TEMPLATE）
- `unexpected_drift`：三轴都没动输出却变了（指纹面之外的非确定性）
- `failed`：重演无法执行（如模型已下线）

`axes_changed` 报告环境差异（线上指针/模型当前状态），`relevant_axes_changed`
只报告实际参与本次重演执行的轴——按归档版本复核旧请求时，线上已热更新
不影响其“精确复现”结论。

## 模块

```
app/
  canonical.py   规范序列化、content/version/input/grants/evidence 哈希
  diffing.py     契约/工具/参数/指令 diff 与 major/minor/patch 分级
  models.py      确定性模型 Provider（可轮换 revision 模拟静默换版）
  store.py       SQLite schema（不可变版本表、证据表、单行部署指针、请求/重演/审计）
  governance.py  模板/版本/审批/发布闸门/原子部署
  evaluation.py  固定测试集、证据生成、迟到标记、契约校验
  serving.py     线上推理与请求绑定
  replay.py      三轴归因重演
  http_api.py    路由与 HTTP
  web/           三个页面（无框架原生 JS）
scripts/demo.py  端到端演示
tests/           27 个单元/集成/并发测试
```

## 设计说明

- **确定性 Provider**：真实 LLM 非确定且依赖网络。内置 provider 由
  `(provider,revision,渲染后prompt,变量,参数)` 派生输出，指纹可在运行时
  `rotate`——同一 `model_id` 指纹变化即代表上游静默换权重，用于真实演示
  MODEL 轴归因。接入真实模型时替换 `ModelProvider.complete` 即可，其余
  治理/证据/重演机制不变（生产环境应同时持久化供应商返回的 model revision）。
- **并发模型**：多线程 HTTP；写操作经进程内 `RLock` 串行化并使用
  `BEGIN IMMEDIATE`，内存库共享单连接，文件库每线程一连接（WAL + busy_timeout）。
- **证据防篡改**：证据哈希覆盖每个用例的输出、判定、模型指纹与 prompt 哈希，
  以及汇总与冻结测试集哈希。
