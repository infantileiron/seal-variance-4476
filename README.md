# 和川包装 · 包装工艺方差评估

面向封合工艺工程师的小型业务应用：登记工艺参数与拉力测量，提交单因素 ANOVA，结合工程条件起草推荐，由负责人签核冻结并导出完整评议。

## 真实场景

模拟客户为和川包装工艺研究室：6 位工艺工程师、1 位负责人，比较同批 BOPP/PE 复合膜的三种封口参数。原先由实验员把试条拉力抄入工作簿，工程师手算平均数后在聊天里确认参数，负责人另存结论。三个痛点是异常样本是否剔除说不清、工艺参数与历史结论脱节、只看平均值而忽略组内变异。

上游是封口机参数单与独立试条测量记录；本应用替代人工统计、证据归档与结论确认；下游为负责人批准下一轮试产验证。应用不把统计显著性直接当作工艺合格判定。

## 业务模块

1. **方案库**：维护方案编号、封口温度、保压时间、压力、材料说明；支持修订。
2. **拉力录入**：按方案与材料批次登记最大拉力，支持筛选、异常标记与修订；标记样本保留在统计中。手工录入与采集批发布并存。
3. **曲线采集**：按采集批接收带序号的曲线分包与试条开夹/闭夹事件，跨包拼接每次夹持区间并提取最大拉力；同一试条多次夹持有据可查、仅选定一次入样。乱序分包可补齐，缺段或事件不配对先隔离；封批后核对预期试条，全齐才发布样本。
4. **方差分析**：选择至少两个方案与材料批次，后台 worker 计算 SS、F、p、η²；每次提交冻结参数和测量快照。
5. **工艺评议**：查看组别均值/标准差，填写推荐方案和至少20字业务理由，负责人签核后冻结，下载 JSON，负责人可查看操作审计。

### 角色与账号

| 角色 | 账号 | 权限与典型动作 |
|---|---|---|
| admin / 工艺负责人 | admin@demo.local | 所有业务编辑、计算、推荐、签核冻结、审计 |
| analyst / 工艺工程师 | analyst@demo.local | 方案与样本新增/修订、接收曲线分包、封批与夹持选择、提交计算、起草推荐；不能签核 |
| viewer / 只读观察员 | viewer@demo.local | 查看五模块、下载已完成结果；不能编辑或提交任务 |

三个演示账号密码均为 `Demo123!`。权限由后端 JWT 与数据库角色共同校验；前端同步隐藏写入入口。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 首次启动自动 seed，手动重复执行同样安全：
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18204)、[后端 OpenAPI](http://localhost:19204/docs)。数据库映射 `localhost:17704`。五服务为 frontend、backend、worker、db、redis，均配置健康检查；后端与 worker 等待数据库和 Redis，前端等待后端。worker 每2秒检查队列与数据库待处理任务，处理后写数据库与共享产物卷。单 worker，重启时将中断任务恢复待执行。测试完成可 `docker compose down -v --remove-orphans`（删除本项目数据卷）。

Docker 默认网络池不足时使用独立可配置子网，默认 `10.253.105.0/24`；若与部署环境重叠，在 `.env` 修改 `NETWORK_SUBNET`。所有服务容器名基于 `COMPOSE_PROJECT_NAME`。

### 环境变量

| 变量 | 默认值 / 用途 |
|---|---|
| COMPOSE_PROJECT_NAME | seal-variance |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | 18204 / 19204 / 17704 |
| POSTGRES_PASSWORD | seal_local_pass，数据库凭据 |
| JWT_SECRET | 示例本地 JWT 签名密钥，可在 .env 替换 |
| SEED_DEMO | true，启动时执行幂等 seed；失败记日志而不阻塞服务 |
| NETWORK_SUBNET | 10.253.105.0/24 |

## 技术与本地开发

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML、CSS、JavaScript ES modules，Nginx 1.27 |
| 后端 | Python 3.11、FastAPI 0.115.6、SQLAlchemy 2.0.36、PyJWT |
| 计算 | SciPy 1.14.1 F 分布尾概率；其余方差分解由 Python 实算 |
| 队列/数据 | Redis 7、PostgreSQL 16 |

前端无构建步骤。后端可在 `backend/` 创建虚拟环境并安装 `requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`JWT_SECRET` 后运行 `uvicorn src.main:app --reload`，另开终端 `python -m src.worker`。开发时前端请求 `/api/`，Nginx 去掉此代理前缀；直接访问后端时路由不带 `/api`。数据库启动时由 SQLAlchemy 建表，`backend/migrations/001_initial.sql`、`002_acquisition.sql` 提供同等初始 DDL。后端测试用 SQLite 临时库：`pip install -r requirements-dev.txt && cd backend && python -m pytest tests -q`。

## 曲线采集口径

采集批登记方案、材料批次与**预期试条编号**，之后只接收同一批的曲线分包。分包报文：

```json
{"seq": 2, "declared_total": 3,
 "curve": [{"t": 2.0, "f": 12.3}, {"t": 2.25, "f": 18.9}],
 "events": [{"type": "open", "t": 1.8, "specimen": "HC-S1-901"},
            {"type": "close", "t": 3.1, "specimen": "HC-S1-901"}]}
```

- **拼接**：按 `t` 合并所有分包的曲线点，同一时刻同值视为分包重叠去重；同一时刻不同值记隔离原因。夹持区间取 `open` 到 `close` 之间的全部曲线点，峰值在拼接后的全局曲线上求取，因此峰值可跨越分包边界。
- **配对**：事件按时间排序后配成区间。开夹重叠、闭夹无配对开夹、开闭夹试条编号不一致、开夹后无闭夹都会记入隔离原因；无法识别试条的区间不入样。
- **重夹**：同一试条多次夹持全部保留为独立 `attempt`，每次都有区间、点数、峰值与曲线。默认取第一次有效夹持入样，工程师可在页面改选并填写依据；发布时样本备注写明第几次夹持、依据以及曲线原件路径，重夹样本 `flagged=true` 但仍纳入统计。
- **乱序与重复**：分包序号可乱序到达，缺包序号在页面与 API 中明示，补传后重新校核。完全相同的重传幂等（`duplicate=true`），内容不同的同序号分包拒绝（409）。
- **封批与隔离**：封批时重新清点分包、事件与预期试条。存在缺包、事件不配对、峰值超出 0–200 N、预期外试条、预期试条缺失或未选定夹持时，批次进入 `quarantined` 并保持数据不变；隔离批可直接补传（补传后回到 `collecting`），也可再点封批重试校核，全齐才进入发布。已封批未发布的批次则需先撤销封批才能补传。
- **发布与恢复**：发布先以原子写保存曲线原件（分包、事件、全部夹持与选定依据）并记录 SHA-256，再按夹持区间唯一键写入样本。原件或样本任一步失败，批次停在 `pending_recovery` 并记录原因，不部分发布；重试按已发布的夹持区间幂等补齐，不会重复入样。样本与原件都成功后才置 `published`。
- **并发**：收包、封批、发布、夹持改选对同一批次持有数据库行锁与进程内锁。封批之后的收包一律显式 409，既不静默丢弃，也不混入封批后的数据；两路并发时结果只可能是「先收后封」或「先封后拒」。
- **与手工录入的关系**：手工样本照旧在拉力录入页登记。采集发布时若样本编号已被手工记录占用，封批转为隔离而不是覆盖；`samples.source` 区分 `manual` 与 `acquisition`，`curve_ref` 指向曲线原件。

## 算法口径与失败态

输入为选定工艺与材料批次的全部有效拉力样本（单位 N，0 < N ≤ 200）。组数 2–8，每组至少2条；选重复工艺或不存在的方案时直接拒绝请求，样本不足时后台任务记为 failed，不静默移除小样本组。`lot=全部` 会跨材料批次合并，需工程师先确认可比性。

对 k 组、总样本数 N，整体均值为 x̄，组均值为 x̄ᵢ：

- SS_between = Σ nᵢ(x̄ᵢ − x̄)²；SS_within = ΣΣ(xᵢⱼ − x̄ᵢ)²。
- SS_total = SS_between + SS_within，df_between = k−1，df_within = N−k。
- F = (SS_between / df_between) / (SS_within / df_within)。
- p = SciPy F 分布 survival function；保留完整浮点，前端极小值科学计数法展示。
- η² = SS_between / SS_total，组均值差异的整体检验阈值 α=0.05。

组内方差为零（SS_within ≤ 1e−12）时明确失败，避免无穷 F 进入 JSON。所有标记异常的样本仍纳入；修订测量只影响新任务。结果保存组别 n、均值、样本标准差、最小/最大、异常标记数、所有平方和/自由度/均方与输入 SHA-256。

假设为独立样本、组内近似正态和方差齐性；本版不执行正态/方差齐性自动检验或事后多重比较，因此整体显著不说明具体哪两组不同。推荐需人工考虑产品下限、外观、热损伤与能耗，并安排验证。只有本次比较中的工艺可被推荐。签核需先有完整推荐；签核后评议不可覆盖，新建任务留痕。

## Seed 与一天的工作剧本

随机种子 `3105`。自然键为用户邮箱、工艺编号、样本编号、评议标题、采集批编号；已有对象不覆盖，重复 seed 不增行、不覆盖用户修改。初始3账号、3方案、105样本、1个待执行评议（worker 真实计算），以及1个演示采集批 `HC-ACQ-1115`（3 条预期试条、乱序到齐 3 个分包、其中 1 条重夹，处于收包中，供页面核查与封批演示）。

| 方案 | 温度 ℃ | 保压 s | 压力 MPa | 样本 |
|---|---:|---:|---:|---:|
| HC-S1 稳温基线 | 160 | 0.80 | 0.32 | 35 |
| HC-S2 延时封合 | 160 | 1.20 | 0.32 | 35 |
| HC-S3 升温短封 | 175 | 0.65 | 0.32 | 35 |

1. 08:50 观察员查看材料批次 `HC-1115` 的三方案台账。
2. 09:00 工程师核对 `HC-S1` 参数 160℃ / 0.8s / 0.32MPa。
3. 09:10 在 HC-S1 下录入 `HC-S1-036`，批次 HC-1115，最大拉力 23.40N。
4. 09:20 核对首件异常标记；本版保留该样本并在说明中解释。
5. 09:30 在方差分析选择 HC-S1、HC-S2、HC-S3 与 HC-1115，标题为“HC-1115 封合参数复核”。
6. 09:40 等待后台完成，核对平方和守恒、F/p/η²与组别标准差。
7. 10:00 起草推荐 HC-S2，说明额外保压时间可能增加能耗，需继续验证外观和封口完整性，不能只根据 p 值选型。
8. 10:10 负责人登录，检查推荐理由后签核，评议冻结。
9. 10:20 观察员下载 `seal-variance-<id>.json`，包含快照与签核者，交给试产负责人。

采集侧的另一天：拉力仪按 SEQ=1,2,3 推送曲线分包，网络抖动导致 2、3 先到、1 补传；工程师在曲线采集页核对跨包峰值，发现 `HC-S1-902` 重夹两次，改选第二次夹持并写明依据；封批时因缺 1 号包被隔离，补传后重新封批；若产物卷暂时不可写，批次停在待恢复，恢复后重试只补未入样的样本。

## 目录与共享枚举

```
frontend/src/  api / stores / types / components/common / hooks / pages / router / utils / constants
backend/src/   routes / controllers / services / models / schemas / middlewares / utils / constants / config
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
backend/migrations/002_acquisition.sql
```

共享枚举为 ROLES、TASK_STATUS/STATUS 与 ACQUISITION_STATUS：后端 `src/constants/enums.py`，认证判断在 `src/utils/security.py`，状态在 `src/services/assessments.py`、`src/services/acquisition.py` 与 `src/worker.py`；前端 `src/constants/enums.js`、`src/components/common/status.js`、`src/app.js`。角色枚举 admin/analyst/viewer；任务状态 queued/running/completed/failed，另有 approved 布尔业务状态；采集批状态 collecting/quarantined/sealed/pending_recovery/published。曲线拼接与事件配对规则集中在纯函数 `backend/src/services/acquisition_curves.py`，DB 状态机与发布/恢复在 `backend/src/services/acquisition.py`。

## 已知范围

小型项目逐条登记，不含 Excel/CSV 批量导入、删除、分页、自动取样设备直连驱动、事后多重比较或外部电子签名。曲线分包为 JSON 报文（页面粘贴或直接调用 API），不含设备串口/厂商协议解析。评议是本地数据库签核。时间戳以 UTC 留痕。前端字体从 Google Fonts 加载，离线时使用指定 serif 回退；核心业务不依赖字体服务。测试脚本、截图与 TESTING.md 位于批次 `test-artifacts/gb-3105/`，源码目录不存放这些证据。

MIT License，见 LICENSE。
