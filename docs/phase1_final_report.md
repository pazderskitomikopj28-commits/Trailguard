# TrailGuard Phase 1 Final Report

验收对象：`/home/asus_gx10/dgx-spark-hackathon`（工作树真实状态）
验收依据：`TrailGuard_比赛版开发冻结规格_v2.1.md` + 本轮 §0–§30 驾驶指令
验收方式：本报告每一条结论都来自本轮直接执行的真实命令与真实文件读取。**没有一条数字沿用此前的工作流摘要或我自己的转述**——凡本报告引用的数值，均在写入前用真实运行重新取过一次。

---

## 1. Repository Status

| 项目 | 真实状态 |
|---|---|
| Git 仓库 | 是（`git rev-parse --is-inside-work-tree` → `true`） |
| 当前分支 | `main` |
| Commit 数量 | **1**（Phase 0 冻结提交 `3c41ae3`） |
| Tag | `phase0-freeze`（Phase 0 冻结点，未移动） |
| 远程仓库 | **未创建、未配置、未推送**（`git remote -v` 空） |
| `git status --short` | 33 条目：18 modified + 15 untracked（含本报告文件自身） |
| 本轮操作 | **未 `git add`、未 commit、未 push、未建分支**——等验收 |
| Phase 0 冻结资产 | `replay/weather/scn_weather_snapshot.json` 与 `trailguard/rules/rules_v1.yaml` **字节未变**（见 §11） |
| 用户既有文件 | 未删除、未改动 TrailGuard 工作区之外任何文件；未修改系统级 Git 配置；未读取/打印/提交任何凭据 |

有一处必须主动交代：本轮我一度把 `replay/weather/scn_weather_snapshot.json` 改写成了 TrailGuard 原生形状，**那会静默改变冻结数据**。我发现后已 `git checkout HEAD --` 还原，并把格式转换移进解析器。详见 §11 第 1 条。

---

## 2. Test Results

真实执行（仓库根目录）：

```
.venv/bin/python -m pytest -q
487 passed in 4.05s

.venv/bin/python -m pytest -v   （末尾）
============================= 487 passed in 4.05s ==============================
```

| 指标 | 数值 |
|---|---|
| Collected | 487 |
| Passed | **487** |
| Failed | 0 |
| Skipped | 0 |
| Xfail / Error | 0 |
| Warnings | 0 |
| 执行时间 | ~4.05 s |

**测试增长过程（全部可审计）**：

| | Collected | Passed | Failed | 说明 |
|---|---|---|---|---|
| Phase 0 冻结树 | 368 | 367 | **1** | 用 `git worktree add /tmp/p1_p0 phase0-freeze` 检出真仓库重跑（不是某份摘要里写的 374——以我实测为准）。失败项见下 |
| 本轮 Phase 1 | 487 | **487** | 0 | 120 个新增测试全部通过 |

净增 **+119**：新增 132 个（三个新文件 113 + 既有文件 19），删除 13 个，两数相抵。

Phase 0 基准树上唯一失败的测试：

```
tests/test_secrets_git.py::test_scanned_set_is_not_empty
AssertionError: no committable files were found; the secret scan would be vacuous
```

原因值得记录：Phase 0 的 `_committable_files()` 只读 `git status --porcelain -uall`。Phase 0 当时的工作树是脏的，所以有输出；一旦树干净（Phase 0 冻结提交之后正是如此），它返回空表，**整个密钥扫描会在零文件上"通过"**——一个设计上就空洞的守卫。Phase 1 的修法是让 `_committable_files()` 与 `git ls-files` 取并集。我已在完全干净的树（0 modified，即上面的 worktree）里验证：Phase 1 版本 21 passed，Phase 0 版本同条件下失败。**这是加固，不是放宽。**

**Phase 0 测试的增减清单**（`pytest --collect-only -q` 双向 `comm` diff，无一项被悄悄削弱）：

- **删除 13 个，全部是"入口点应抛 NotImplementedError"的 stub 守卫**，因为那四个入口点现在已经真的实现了，留着就是假守卫：
  - `test_harness.py`：6 个（5 个按工具名参数化的 `test_every_registered_tool_records_not_implemented[<tool>]` + 1 个 `test_phase_zero_stub_tool_is_recorded_as_not_implemented`）
  - `test_tool_contracts.py`：6 个（`test_assess_risk_stub_does_not_return_a_verdict` + 5 个参数化的 `test_every_tool_raises_not_implemented`）
  - `test_risk_core.py`：1 个（`test_soft_score_aggregation_defers_to_phase_one`）
- **这些守卫覆盖的行为没有消失**：harness 的 `not_implemented` 记录路径仍被测试，只是改由唯一剩下的 stub `get_weather_for_route_time` 行使。
- **存量文件的测试数没有一个下降**（Phase 0 → Phase 1，实测）：

  | 文件 | Phase 0 | Phase 1 | 变化 |
  |---|---|---|---|
  | `test_harness.py` | 35 | 36 | +1 |
  | `test_tool_contracts.py` | 41 | 42 | +1 |
  | `test_risk_core.py` | 23 | 27 | +4 |
  | `test_evals.py` / `test_packaging.py` / `test_providers.py` | 37 / 10 / 22 | 37 / 10 / 22 | 0 |
  | `test_risk_level.py` / `test_rules_pack.py` / `test_schemas.py` | 17 / 24 / 49 | 17 / 24 / 49 | 0 |
  | `test_scenario_fixtures.py` / `test_secrets_git.py` / `test_trace_schema.py` | 42 / 21 / 47 | 42 / 21 / 47 | 0 |

- 新增三个测试文件共 113 个：`tests/test_deterministic_core.py`（60）、`tests/test_device_feeds.py`（32）、`tests/test_replay_weather.py`（21）；既有三个文件的 19 个新增覆盖了失去 stub 守卫的那三处，以及 Phase 1 新实现的直接断言。

---

## 3. Architecture Verification

Phase 1 新增的落位（逐文件核实）：

- **`trailguard/routes/`**：`geometry.py`（测距与最近点求解）、`registry.py`（资产定位）。`gpxpy` / `shapely` / `pyproj` 走 `pyproject.toml` 的 `route` extra，**惰性导入**——契约层与核心层在没有它们时仍可导入，报错信息点名 extra。
- **`trailguard/risk_core/`**：`engine.py`（`DeterministicRiskEngine`）、`features.py`（`FeatureVector`，契约字段→命名特征的唯一入口）、`movement.py`（冻结速度模型 / ETA / pace drop）、`scoring.py`、`confidence.py`、`calibration.py`（`calibration_v1.yaml` 加载 + 交叉校验）。
- **`trailguard/tools/`**：四个真实实现 + `contracts.py` / `_arguments.py` 两个把调用参数收敛到契约的辅助模块。
- **`trailguard/replay/`**：`devices.py`（设备时间线回放）、`weather.py`（open-meteo 形状快照解析器）。
- **`trailguard/rules/calibration_v1.yaml`**（新）：评估元数据，与 `rules_v1.yaml` 分离。
- **`trailguard/smoke.py`**（新）：`python -m trailguard.smoke` 把整条链真跑一遍并打印每一阶段。

未改动：`SKILL.md`（一字未改）、`trailguard/agent/`、`trailguard/providers/`、`evals/`、`trailguard/schemas/`（除 `risk.py`  Minor 增补）。

**关键设计落位**：安全核心不含任何阈值常量。阈值在 `rules_v1.yaml`，"拿哪个特征比、往哪个方向比、落在哪个 band" 在 `calibration_v1.yaml` 的 clause 表里。`Calibration.validate_against(pack)` 强制执行三条不变式（每条规则都有评估条目且无多余条目；每个数值阈值恰被一个 clause 占用或有显式 `non_evaluable_thresholds` 说明；clause 的 severity 不超过规则声明的 severity）。引擎控制流里没有规则 id——除了查这张表。

---

## 4. Critical Boundary Checks

| 检查项 | 真实结果 |
|---|---|
| 仍是 5 个原子工具 | 是。`TOOL_SPECS` / `TOOL_FUNCTIONS` / `tool_schema_dicts()` 均为 5 条，未增未减 |
| `forecast_risk` / `predict_all_horizons` / `calculate_future_risk` | **均不存在**。`grep` 命中只出现在说明"它不存在"的 docstring 里 |
| 一次调用只答一个 horizon | 是。`project_route_state` 与 `assess_risk` 各自单 horizon；30/60/120 循环在 Skill 侧，不在工具里 |
| 安全核心无 LLM | 是。§20 测试删掉 `STEPFUN_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 后仍返回判决：`test_the_core_needs_no_model_no_network_and_no_gpu` 1 passed |
| 真实环境无 key 验证 | `env -u STEPFUN_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY .venv/bin/python -m trailguard.smoke` → exit 0，123 行真实输出 |
| 无 CUDA / GPU / Numba / Torch | 是。grep 仅命中两处**散文**（`providers/local.py` 描述 Phase 4 目标、`smoke.py` 结尾声明"未触碰 GPU"），无 import、无计算 |
| 无 `datetime.now()` 等隐藏时钟 | 所有业务函数的时刻都由入参给定；`analysis_time=None` 直接抛错而非读系统时钟（`test_a_projection_refuses_to_read_the_wall_clock`） |
| 确定性 | 两次相同 `assess_risk` 的 `model_dump(mode="json")` 完全一致；所有场景 × 所有 horizon 扫描无非有限数；`RiskResult.computed_at == analysis_time` |
| 无 Phase 2+ 范围 | 未触碰 harness 的 skill 挂载、未接 provider、未建 Streamlit、未写 UI、未接在线天气 |
| `battery < X → RED` 被拒绝 | 是。R-E 投影到路线终点的电量并叠加网络可用性，不是裸电量阈值 |

---

## 5. Rule Pack & Calibration Review

| 文件 | 本轮状态 |
|---|---|
| `rules_v1.yaml` | **字节未变**（`git status` 无该文件；`git diff HEAD -- trailguard/rules/` 空） |
| `calibration_v1.yaml` | 新增，携带显著的 CALIBRATION NOTICE（`is_calibrated: false` / `review_status: pending`） |

六条规则全部保留 `competition_demo_calibration` 标记。`trigger_type` 在 pack 里（R-A/R-B 为 `hard`，R-C…R-F 为 `soft`）；`kind` 在校准里（`threshold_clauses` 或 `compound`）。**hard 与 compound 两类都从 soft score 中豁免，而豁免是数据推导的**——读 pack 的 `trigger_type` 和校准的 `kind: compound`，不是引擎里写死的规则 id。

需要说明的一处数据职责划分：`calibration_v1.yaml` 里的 `kind` 取值只有两种——`threshold_clauses` 与 `compound`。我最初在文档里把它写成了 `hard` / `soft`，那是**我编造的 YAML 片段**，已在核实 pack 后改正。（这类错误正是 §26 要求逐字核对的原因。）

---

## 6. Deterministic Core Verification

### 6.1 几何（真实 GPX，非工厂数据）

`scn_route_sample.gpx`：12 个 trackpoint，总长 11 006.823672712168 m。S01/S04 起点处的真实输出（已 pin 进测试）：

```
route_progress           = 0.34992459777308427
remaining_distance_km    = 7.1552653262791
off_route_distance_m     = 0.060984366915525015
remaining_ascent_m       = 726.0433618422851
current_altitude_m       = 1378.956638157715
data_quality             = 1.0
route_polyline_ref       = replay/routes/scn_route_sample.gpx
```

**每一米都是 WGS-84 测地线**（`pyproj.Geod`）：`Geod.inv` 算段长与距离，`Geod.fwd` 算沿 polyline 走 d 米后的点。局部 AEQD + Shapely 只用来回答"polyline 上哪个点最近"，其结果换算回来后再**重新测地测量**，所以投影方式不会泄漏进任何上报数值。无 DEM、无栅格、无高程服务——高程取自 GPX 本身，`remaining_ascent_m` 是剩余 polyline 上**正**高程增量之和。

### 6.2 投影：一次调用一个 horizon

四个 horizon 的真实投影（S04，fatigue 0.12）：

| horizon | projected progress | projected arrival | projection_confidence |
|---|---|---|---|
| NOW | 0.34992459777308427 | +0 min | 1.0 |
| +30 | 0.39807654567009315 | +30 min | 0.97 |
| +60 | 0.446228493567102 | +60 min | 0.9 → 0.94 |
| +120 | 0.5425323893611197 | +120 min | 0.9 |

`projected_arrival_time = analysis_time + min(horizon, eta)` 已用 `estimate_minutes` + `minutes_between` 独立重算核对（本轮 S04 的 ETA = 405.0150184686282 min，四个 horizon 均未触顶）。越过路线终点的投影**停在终点**，不"跑到地图外"。非 0/30/60/120 的 horizon 抛错，缺 `analysis_time` 抛错。

`projection_confidence = route_data_quality × horizon_reliability × fatigue_term`，其中 fatigue_term 在调用方披露疲劳指数时为 1.0、未披露时 0.5——**未披露是被计费，不是被假定为完美**。

### 6.3 速度模型、ETA 与日落余量

```
base_speed    = 0.5*avg_speed_10m + 0.3*avg_speed_30m + 0.2*current_speed
effective     = base_speed * (1 - min(fatigue_index, fatigue_penalty_cap))     # cap = 0.6
pace_drop_pct = max(avg_speed_30m - avg_speed_10m, 0) / avg_speed_30m * 100
```

权重与 cap 全在 `calibration_v1.yaml`，标记 `competition_demo_calibration`。**退化比值返回 `None`，绝不返回 0 / inf / NaN**：静止徒步者的 ETA、`time_margin_min`、`projected_battery_percent` 均为 `None`，引擎在 evidence trace 里显式披露每一项不能计算，且没有规则在无法计算的特征上开火。（返回 0.0 比没用更糟：`0 < 30` 会关掉一个阈值，`0 > 50` 也会关掉一个——说谎的方向取决于比较符。）

`time_margin_min = minutes_to_sunset(projected_arrival) - estimated_minutes_to_finish(projected_remaining)`。它由**时刻**推导（`time_state.sunset_time` 与投影到达时刻），不读 `TimeState.minutes_to_sunset` 字段——测试已 pin 这一点。它是**日光余量的分钟数**，不是概率、不是事故风险，从未被当作概率再用。

### 6.4 规则执行与定级

```
features → triggered rules（clause 表）
         → hard rules 设 severity floor（按 rank 取 max）
         → soft rules 聚合分数（hard 与 R-F 豁免）
         → 分数 → band（GREEN 0-24 / YELLOW 25-49 / ORANGE 50-74 / RED 75-100）
         → R-F compound 升级（+1 级，封顶 RED）
         → 最终级 = max(band, floor, escalated)    ← 任何一步都不得下调级别
```

- **各操作符在阈值处是严格的**：恰好等于阈值**不**开火，过去千分之一才开火。每一个边界都由测试**构造**出来而非从运行结果 pin 值：R-A 50 m、R-B 30 min（用测试自己算出的 ETA 反推 `sunset_time`）、R-C 45 km/h、R-D 15 %、R-E 20 %（令 `battery = drain + 20` 使投影电量恰好等于 20）。
- **soft score**：severity 权重 LOW 10 / MEDIUM 25 / HIGH 40 / CRITICAL 50，饱和 100。S04 的 65 = R-D 的 25 + R-E 的 40。
- **hard rule 只设 floor、不贡献分数**：R-A 单独触发（偏离 75.72 m）→ `ORANGE / score 0`，只有 R-A 一个 driver。这修正了一个真实缺陷（见 §11 第 2 条）。
- **R-F 只升级、不贡献分数**：用一组只有 R-D/R-E 开火、R-B 不开火的合成输入，把 pack 的 `escalation_levels` 分别设为 0/1/2，得到 ORANGE/RED/RED，**score 恒为 65**——这正是升级来自 compound 而非分数的证明，同时证明引擎读的是这个数而不是写死的 +1。（不能用 S04 做这个演示：R-B 是 CRITICAL 硬规则，单靠 floor 已把级别钉在 RED，`escalation_levels=0` 时 S04 仍是 RED/65，看不出 compound 的作用。）

### 6.5 confidence 是工程陈述，不是概率

五个加权项 + 每字段罚分（missing 0.05 / stale 0.03），未披露的项计 `confidence_unknown_value = 0.5`。S01 在 NOW 的 longhand 重算（从加载的校准逐项算，不是从运行结果抄）：

```
0.20*0.92 + 0.20*0.95 + 0.25*(6/7*0.50) + 0.15*1.00 + 0.20*0.93 = 0.817143
```

天气项是 0.50 而非 0.9：**冻结快照自己没声明 provider confidence**，加载器因此按未知计费，而不是假定完美。这是同一把尺子用在所有未披露项上的结果。

---

## 7. Scenario & Evidence Review

三个冻结场景 × 四个 horizon 的真实判决表（已 pin 进测试）：

| scenario | horizon | level | score | hard | drivers | margin (min) | confidence |
|---|---|---|---|---|---|---|---|
| S01 | 0 / 30 / 60 / 120 | GREEN | 0 | -- | -- | +354.859 | 0.817143 / 0.79925 / 0.784333 / 0.763182 |
| S04 | 0 / 30 / 60 / 120 | RED | 65 | R-B | R-B, R-D, R-E, R-F | −55.015 | 0.746286 / 0.736711 / 0.727878 / 0.714872 |
| S11 | 0 / 30 / 60 / 120 | GREEN | 0 | -- | -- | +74.382 | 0.366326 / 0.358413 / 0.350918 / 0.339909 |

- S04 的 hard 与两条 soft driver 是它自己场景文件声明的（`hard_rules_triggered: ['R-B']`、`drivers_expected: ['R-D','R-E']`）；R-B 本身与 compound 升级 R-F 是核心额外报告的。测试同时校验"horizon 循环属于调用方"——引擎不会自己答完四个 horizon。
- **S11 是这个 fixture 的意义所在**：stale fix、缺失的 pace drop、无网络，全部被大声披露（`pace_drop_percent is derived, not observed ... 8.824 %`、`weather snapshot is 250 minutes old, beyond the 180-minute budget`），而**没有被发明成风险**——仍是 GREEN / 0。它偏离路线 42.09 m，低于 R-A 的 50 m 阈值，所以没有规则开火，而偏离距离照常在 margins 里报出。
- **evidence trace 双向可用**：安静判决仍引用 pack / calibration / horizon / soft_score / risk_level / confidence；嘈杂判决引用 `rule:R-A:off_route_distance_m:off_route_distance_m` 并附 `off_route_distance_m=75 gt 50`。driver 按 severity **rank** 排序，不是按字符串——`"HIGH" < "MEDIUM"` 在字典序下是错的（这恰好是 §11 第 3 条那个缺陷的同一根因）。

---

## 8. Security Review

| 检查 | 结果 |
|---|---|
| 读取 / 打印 / 提交 `.env`、API Key、token、`~/.claude/settings.json`、SSH key | **无**。本轮未读取、未打印、未提交任何凭据文件 |
| 密钥写入代码 | 无。对新代码 grep `api_key` / `secret` / `token` / `password` / `bearer` 无命中 |
| `.env` 存在于仓库 | 不存在（`ls -a` 无 `.env`）。`git ls-files` 中唯一的 env 文件是 `.env.example` |
| `.env.example` 内容 | `STEPFUN_API_KEY=`（空）、`TG_LOCAL_BASE_URL=`（空）、`TG_LOCAL_MODEL=`（空）；其余为公开端点与模型名。**无真实值** |
| 自动 push | **未执行**。远程仓库根本未创建（`git remote -v` 空） |
| 修改 SSH / 系统配置 | 无 |
| 删除用户文件 | 无。仅 `git checkout HEAD --` 还原了**我自己**在本轮改坏的一个仓库内文件 |
| commit `.env` / token / `~/.claude/settings.json` | 未 commit 任何文件；工作树中不存在这些敏感文件 |
| 密钥扫描的有效性 | 已修复 Phase 0 的空洞守卫（见 §2），并在干净树中验证通过 |

---

## 9. Scope / Overengineering Review

**没有做的事**（对照 §21 逐条）：未写 UI、未建 Streamlit、未接在线天气、未让 LLM 参与任何风险计算、未建数据库、未引 Torch/Numba/CUDA、未加第七条规则、未训练模型、未做多 agent、未改 `SKILL.md`、未改 harness 的 skill 挂载机制、未接 provider、未进 Phase 2。

**做但保持最小的事**：新增的三个 YAML/JSON 数据文件都是 Phase 1 必需的输入（calibration 元数据、设备回放、device feeds）；未新增依赖，`route` extra 是 Phase 0 就声明的。

**一处需要点名的范围偏差**：`replay/weather/scn_weather_snapshot.json` 被我改写过又还原（§11 第 1 条）。还原后该文件与 Phase 0 字节一致，所以最终交付物没有越界，但过程本身是本轮唯一一次触碰冻结数据，如实记录。

---

## 10. Adversarial Review（自我审问）

**Q1：487 个测试是不是都真在断言东西，还是很多是"跑一遍不报错"？**
按设计分类核过：构造边界（R-A 50/50.0001、R-B 由自算 ETA 反推、R-E 令投影电量恰为 20）、longhand 重算（confidence 逐项、ETA 用 `estimate_minutes` 独立算）、数据驱动证明（改 `escalation_levels` 看级别移动、改 `severity_weights` 的字典序守卫）、以及"必须披露什么"的负向断言。`python -m trailguard.smoke` 是**故意不断言**的，所以它只是给人看的输出。

**Q2：会不会有"为了 PASS 而放宽"的地方？**
三处删除了 Phase 0 测试，全部是"现在已实现的入口点应抛 NotImplementedError"。我逐个核对了守卫覆盖的行为是否仍被测到：harness 的 `not_implemented` 路径改由 `get_weather_for_route_time` 行使；`test_risk_core.py` 23→27、`test_harness.py` 35→36、`test_tool_contracts.py` 41→42，三个文件的净增都是正数，没有任何断言被改成更弱的形式。

**Q3：`time_margin_min` 会不会被偷偷当作概率用？**
grep 确认该值只出现在 `safety_margins` 与 R-B 的阈值比较里，没有参与 confidence 计算，没有乘过任何系数。文档 §4 明确写了"它不是概率"。

**Q4：R-E 是不是变相的 `battery < X → RED`？**
不是。R-E 用 `estimated_minutes_to_finish` 把电量投影到路线终点，再叠加 `network_available` 作为上下文。测试覆盖了"短路线 + 高电量不等于风险"和"长路线 + 低电量才触发"。剩余的已知限制是 drainage rate 全设备一个值（§11 第 3 条）。

**Q5：规则是不是靠一串 if/elif 在引擎里实现的？**
不是。引擎读取 clause 表并执行；引擎源码里没有规则 id 出现在控制流中（除查表）。我改过 `escalation_levels` 与 severity 权重去处证明数字真的来自数据文件。

**Q6：投影会不会偷偷读系统时钟？**
不会。缺 `analysis_time` 直接抛错；测试专门 pin 了这个拒绝。所有时刻均由入参给定。

**Q7：`route_progress = 1.0` 会不会骗人？**
会，这是已知限制：远离路线端点的 fix 会被吸附到较近的那个端点，于是 `progress 1.0` / `remaining 0.0` / `off_route 2667.70 m`。我用真实 fix 复现过（`get_route_context("scn_route_sample", (46.15683, 7.82043))`），判决仍是诚实的（R-A 开火，ORANGE / score 0），但"路线已完成"会是误读。已写入 §11。

**Q8：天气数据的可信度是怎么处理的？**
冻结快照是 open-meteo 形状（并行 `hourly` 数组、`utc_offset_seconds`、`hourly_units`），三个转换在解析器里完成并有测试：**降水是百分数**→ 除 100（否则 `8` 会变成一个不可能的概率并误触 R-C 的 0.7 阈值）、**行时刻是 naive 本地墙钟**→ 按声明 offset 变 aware（否则 12:00 行会被当成 12:00Z 而它意指 10:00Z）、**声明了不认识的单位**→ 大声报错而非猜。快照未声明 confidence → 计 0.5。序列外请求不插值。

**Q9：`python -m trailguard.smoke` 的输出是真的吗？**
真跑过：exit 0、123 行。其中没有硬编码数值——每个数字都从资产读出或在本轮算出。文档里引用的每个数字我也在用真实运行重取之后才写。

**Q10：会不会其实还没到 Phase 1 完成度？**
对照 §0–§30 的 18 项必须实现逐条核过，全部落地。缺的只有 Phase 2+ 的东西（在线天气、Skill 侧 horizon 循环、provider、benchmark、demo），而那些按指令本来就不该做。

---

## 11. Fixes Performed, Known Limitations & Final Verdict

### 11.1 本轮修复的缺陷

1. **我改写了冻结的天气资产（范围偏差，已还原）**。`replay/weather/scn_weather_snapshot.json` 原本是 open-meteo 响应形状；我把它改写成 TrailGuard 原生形状，那会**静默改变冻结数据**：丢掉 06:00–09:00 四行、改动每一个温度值、新增一个文件从未声明的 `confidence: 0.9`。已 `git checkout HEAD --` 还原到 Phase 0 字节，并把格式转换移入 `trailguard/replay/weather.py`（那里 diff 可审）。附带收益：Phase 2 的真实解析器现在就被冻结资产行使。`tests/test_replay_weather.py` 里有一条测试专门守"不要把数据文件再规范化一遍"。
2. **hard rule 曾在污染 soft score**。R-A 单独开火时既走 severity floor 又贡献 40 分，与 `docs/contracts.md` 和引擎自己文档的流水线描述矛盾。修法：`aggregate_soft_score` 豁免 `trigger_type: hard` 的规则。
3. **severity 曾按字符串比较**。`"HIGH" < "MEDIUM"` 字典序成立，于是 R-F 的 `min_severity: MEDIUM` 过滤器悄悄排除了 HIGH/CRITICAL 搭档，compound 规则永远不可能开火。修法：`Severity.rank`（LOW 0…CRITICAL 3），与 `RiskLevel.rank` 一致。
4. **`_compound_detail` 被调用但从未定义**。修好缺陷 3 之后 R-F 第一次能开火，`assess_risk` 立刻 `AttributeError`。这个顺序本身就是教训：先修排序，再修崩溃。
5. **driver detail 的有效数字只有 4 位**，导致投影电量 19.999 % 被打印成 "20"，detail 读起来是 `projected_battery_percent=20 lt 20`——一句读者无法验证的话。改为 6 位有效数字并写明原因。
6. **Phase 0 的密钥扫描守卫是空洞的**（§2）：只读 `git status --porcelain`，树一干净就扫零文件并"通过"。已改为与 `git ls-files` 取并集，并在干净树中验证。

### 11.2 已知限制（刻意保留，不是缺陷）

1. **远离路线端点的 fix 会吸附到较近端点**。最近点吸附是无条件的：`scn_route_sample.gpx` 末端外 2 667.70 m 的一个 fix 会吸附到末端 trackpoint，于是核心报 `route_progress 1.0`、`remaining 0.0`、`off_route 2667.70 m`。判决仍然诚实（R-A 开火 → ORANGE / score 0，日落余量按被吸附的位置算出 490 min），但"路线已完成"会是误读。判断"这人已不在这条路线上"需要 MVP 没有的持续性模型，而凭空发明一个距离阈值等于伪造阈值。
2. **一条规则一个观测，不含持续性**。R-A 的 `persistence_observations` 阈值无法由无状态的单 horizon 调用检验，所以 driver detail 明说"不主张持续性"，而不是默默假定它成立。
3. **电量消耗率全设备同一个值**。`DeviceState` 无法携带每设备速率，R-E 按 `calibration_v1.yaml` 的 4 %/h 投影；更了解情况的调用方可以用另一份校准构建引擎。
4. **天气是回放-only**。`get_weather_for_route_time` 仍是 Phase 2 stub；核心读 open-meteo 形状的冻结快照，最近行胜出，不向序列外插值，过期快照按原样使用并同时报告其年龄（180 分钟演示新鲜度预算）。
5. **投影是趋势外推，不是预报**。它假设徒步者沿计划路线保持当前可观测趋势，并靠 `projection_confidence` 说明这一点；不对地形、疲劳动态或休息点建模。
6. **`weather_data_quality` 目前不参与 confidence**。三个场景都声明了它（0.9 / 0.85 / 0.35），但置信度模型只读快照自报的 provider confidence × 年龄。这是 Phase 1 已固定并已测试的设计选择；若 Phase 2 要把它纳入，应作为一次显式的校准修订。

### 11.3 最终结论

**PASS**

依据：487 个测试全绿（0 failed / 0 skipped / 0 warning）；18 项必须实现全部落地并逐条核实；五个原子工具的边界与 `forecast_risk` 禁令未被破坏；安全核心在无 key、无网络、无 GPU 下真实运行（`smoke` exit 0，123 行真实输出）；`rules_v1.yaml` 与 `replay/weather/scn_weather_snapshot.json` 两个 Phase 0 冻结资产字节未变；没有 Phase 0 测试被削弱（删除的 13 个全部是已实现入口点的 stub 守卫，且其覆盖行为仍有测试）；未 commit、未 push、未读取或提交任何凭据；发现并修复了 4 个真实缺陷 + 1 处我自己的范围偏差，全部如实记录在案。

按指令：**Phase 1 到此停止，不进入 Phase 2。** 工作树保持未提交状态等验收。
