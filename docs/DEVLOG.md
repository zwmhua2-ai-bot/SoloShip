# DEVLOG — 开发过程记录

按时间倒序记录。每条包含：任务编号、提交、原因、关键技术决策、测试结果、遗留问题。
**无法从仓库核实的内容一律标注「未核实」，不做推测。**

---

## SS-V01-AC-001 · 逐条 AC 验收证据矩阵（本条目）

- **提交**：见交付报告
- **基线**：`6970901b`
- **原因**：P04 验收助手已能给出逐条 AC 的线索级结论，但每条 AC **没有可追溯的证据记录**：
  看不到对应哪次测试、哪个 Candidate SHA、Reviewer 是否审过同一提交、缺什么证据、
  人工还要做什么。任务卡要求补齐矩阵，且**不得把线索升级为验收通过**。
- **决策**：
  1. 复用现有协议：`acceptance` 条目**保留全部原字段**（`id/description/status/verified/
     matched_*/basis/…`），新增 `ac_id / verification / candidate_sha / coverage_status /
     test_evidence / review_evidence / missing_evidence / human_action`。
  2. `_review_binding()`：只回答"审查结论能不能绑到当前 candidate SHA 上"——
     Reviewer 未回报 SHA、或与 candidate 不一致 → `bound=False`，并在 risks 明示
     "审查证据不可采信"；**不伪造逐条 AC 审查结论**。
  3. Manual AC（`verification != automated`）→ `coverage_status = manual_pending`，
     `verified=False`，`human_action` 保留人工验收要求；缺 `verification` 字段时按原逻辑走，
     不改变既有结论。
  4. `missing_evidence` 显式列出缺什么（无 candidate SHA / 测试未过 / 无用例名 /
     diff 不可用 / 无关键词命中 / Reviewer 未绑定）。
  5. 顶层新增 `candidate_sha`、`review_binding`、`evidence_matrix`（证据位置 + 各 AC 覆盖度），
     Markdown 增加「证据矩阵」表。`acceptance_verified` / `auto_merge` 恒为 False。
  6. 只读原始证据：仍只写 `exchange/p04/acceptance/`，不改 P02 报告/回执/Journal。
- **测试**：新增 `tests/test_p04_ac_matrix.py` 12 项（AC-01..AC-09）：字段完整性、
  真实测试证据引用、测试失败不验证、名称相似无断言不算覆盖、证据缺失显式未验证、
  Manual AC、候选 SHA 与证据路径、Reviewer SHA 不一致/缺失/一致、重复生成一致且不改原始证据。
  修复前 10 项 KeyError/FAIL。全量 `414 OK`（2026-10-24 09:33，465.183s，渲染微调后复跑）；
  既有 `test_p04_acceptance` 等 56 项未受影响。
- **兼容性**：旧字段与输出文件路径不变；新增字段为纯追加，旧消费者不受影响。
  行为变化：Reviewer 未绑定 candidate SHA 时多一条风险（原来只报"Review 状态缺失"）。
- **遗留**：`test_evidence.test_names` 仍来自 `report.tests.stderr_tail` 的解析，
  不是逐条测试的结构化日志；若报告没有 stderr_tail，矩阵只能标"无用例名"。
  逐条 AC 的语义验证仍必须由人完成（设计约束）。

---

## SS-V01-006 · P05-C：Supervisor 中断恢复与安全重调度

- **提交**：见交付报告
- **基线**：`7eb8988`
- **原因**：`claim()` 检出失联认领后只做 `_defer()`（`UNFINISHED_CLAIM`），任务被永久停放；
  启动确认只看 `state.json` 的 pid/state，不校验锁与心跳；恢复无互斥；
  `P02_NEEDS_HUMAN` 无正式入口。
- **决策**：
  1. 新增四分类 `classify_interrupted()`：**ACTIVE**（持有者存活，不接管）/
     **SAFE_TO_RESUME**（P00 无副作用；无入库且无 worktree；`finalizing/` Journal 完整；
     `finalized/` 清单完整；终态且报告在位）/ **NEEDS_HUMAN**（未入库但有 worktree；
     有 job 无 Journal；发布结果不可对账；终态但报告缺失；worktree 状态不一致）/
     **DEFERRED**（门禁等停放任务，保持停放）。
  2. `recover_on_start()`：持 `recovery.lock`（O_EXCL，持锁者失联才接管）→
     先重放最终化 Journal → 逐条分类 → 安全的重排队（claim 转 `requeued`，
     任务文件从 `deferred/` 回 inbox，**历史认领记录保留在 `history`**）、
     证据不足的写 `.control/supervisor/needs_human/<kind>__<task>.json` 并把认领转
     `needs_human`（拒绝自动执行）→ 写 `recovery_summary.json`。
  3. `claim()` 支持 `requeued`（可重新认领，继承 history）与 `needs_human`（拒绝执行）；
     `tick()` 对 `needs_human` 走 defer，**绝不自动执行**。
  4. `startup_ready(root, pid)`：单实例锁 + PID 存活 + 心跳新鲜度（30s）三者同时满足
     才算就绪；`_wait_ready()` 改用它，启动失败带明确原因，不谎报 RUNNING。
  5. `build_status()` 增 `recovery` 摘要；`cmd_recovery` 扩展为只读输出
     待恢复清单 + 恢复摘要 + `needs_human` 明细。**没有任何"标记成功"能力。**
  6. 无数据库 schema 变更、无任务协议变更；`.control/` 下新增
     `needs_human/`、`recovery.lock`、`recovery_summary.json`。
- **测试**：新增 `tests/test_p05c_recovery.py` 20 项（AC-01..AC-14），含双恢复竞争、
  失联持锁者接管、陈旧 worktree 不删除、重复恢复幂等、安全任务重排队后真实跑通到
  `P02_READY_FOR_HUMAN`。修复前全部 ERROR/FAIL。全量 `402 OK`
  （2026-10-24 04:38，421.460s）；既有 `test_p05b_supervisor.py` 31 项未受影响。
- **兼容性**：`tick()` 对运行中失联认领仍按 `UNFINISHED_CLAIM` 停放（保守默认）；
  启动路径（`start --foreground` 与 `_run`）在 `detect_unfinished()` 之后调用
  `recover_on_start()`。真实 Supervisor 未重启。
- **遗留**：SAFE_TO_RESUME 目前不区分"任务已完成只需归档"与"需要重新执行"，
  统一交给 P02 幂等处理（DUPLICATE/恢复路径）；预算/限流类 Deferred 重试策略未实现；
  `needs_human` 需人工在核对后自行决定后续动作（本期不提供强制成功入口）。

---

## SS-V01-005-R1 · GitHub PR 来源仓库身份验证

- **提交**：见交付报告
- **基线**：`0fa7f45`
- **原因**：SS-V01-005 验收补齐——原 `_inspect_pr()` 只校验 Base/Head 分支、状态、Draft 与 Head SHA，
  **未校验 PR 的来源仓库**。Fork 中同名分支的 PR 只要 `OPEN + Draft + headRefName` 匹配就会被复用，
  等于把别人的 PR 当成自己已发布的 PR。另有两个相关问题：
  查询未传 `--limit`，命中数达默认上限时无法区分"没有更多"与"被截断"，
  于是"未查到"可能被误判成"不存在"。
- **决策**：
  1. `PR_REQUIRED_FIELDS` 增加 `headRepository`、`headRepositoryOwner`、`isCrossRepository`，
     **缺失即"结果不完整"**，拒绝复用。
  2. 身份规则：`isCrossRepository` 必须为 `False`；
     `headRepositoryOwner.login` + `headRepository.name` 必须与目标仓库 `owner/name` 完全一致；
     任一不符 → `BLOCKED_PR_STATE`。SoloShip 只发布本仓库分支，不自动支持跨 Fork。
  3. 查询显式加 `--limit 100`；命中数**达到上限**即按"可能不完整"拒绝，
     不把"未查到"当作"不存在"（上限检查先于歧义与存在性判断）。
  4. 既有 Base/Head 分支、`OPEN`+`Draft`、Head SHA 一致性检查全部保留，顺序未变。
- **测试**：新增 `HeadRepositoryTests` 8 项（AC-R1..R4）：Fork PR、同名不同 Owner、
  三类字段缺失、非对象字段、达上限截断；AC-R2/R5 由既有用例覆盖并扩展。
  修复前 6 项 FAIL / 1 项 ERROR（`PR_QUERY_LIMIT` 不存在）。全量 `382 OK`
  （2026-10-24 04:05，446.311s）。
- **兼容性**：同仓库 Draft PR 流程不变（`fake_gh` 的 `pr create` 现在按 `--repo` 记录来源字段，
  既有 P03 用例无需改动即通过）。行为变化：来源身份不明确或跨 Fork 时拒绝复用并交人工。
- **遗留**：身份判定依赖 gh 返回的 `headRepositoryOwner`/`headRepository` 字段；
  若 gh 版本不返回这些字段，所有 PR 都会被判"不完整"而拒绝（fail closed，需人工核对）。

---

## SS-V01-005 · GitHub Publisher 发布门禁与幂等性加固

- **提交**：见交付报告
- **基线**：`0db54fc`
- **原因**：核对 `github_publisher.py` 发现 5 处不确定性：
  1. `_remote_branch_sha()` 返回 `None`（读取失败/分支不存在）时，base 校验被**跳过**；
  2. `expected_base_sha` 为 `None` 时完全不校验远端 base；
  3. `_find_pr()` 只取"第一条带 url 的记录"，不校验 repo / base / head / state / isDraft / Head SHA，
     多条匹配时静默取第一条；
  4. 已 CLOSED / MERGED / 非 Draft 的 PR 会被直接复用；
  5. 推送与创建 PR 的 `subprocess` 调用没有超时对账，超时会直接冒泡成
     `PUBLISH_FAILED_EXCEPTION`（"超时"被当成"没创建"）。
- **决策**：
  1. **base 必须可读且合法**：读不到、空值、非 40/64 位十六进制 → `BLOCKED_REMOTE`；
     与 `expected_base_sha` 不一致 → `BLOCKED_BASE_MOVED`；推送后、建 PR 前再读一次，
     期间变化 → `BLOCKED_BASE_MOVED`。
  2. **PR 查询前移到任何写操作之前**，并改为 `_inspect_pr()` 严格核对：
     查询失败 / 非 JSON / 含非对象 / 缺字段 / 多条匹配（歧义）→ `BLOCKED_PR_STATE`；
     `MERGED` / `CLOSED` / 非 `OPEN` / 非 Draft → `BLOCKED_PR_STATE`（不自动复用、不重开、不建替代）；
     Head 分支或 Base 分支不符 → `BLOCKED_PR_STATE`；
     Head SHA 与 candidate 不一致 → `BLOCKED_BRANCH_CONFLICT`。
     只有 `OPEN + Draft + 分支与 SHA 全一致` 才复用，且复用时**不推送**。
  3. **不确定结果一律先对账**：推送超时 → 读远端 SHA，落地才继续，否则 `FAILED_PUSH` 且不重推；
     建 PR 超时 → 重新严格查询，命中则 `PR_RECOVERED`，未命中则 `FAILED_PR` 并声明"需人工对账"。
  4. `gh pr create` 返回 "already exists" 时也必须重新严格核对才按已发布处理。
  5. 未实现的能力保持未实现：源码中无 `--force` / `pr merge` / deploy 路径（有源码级回归守护）。
- **测试**：新增 `tests/test_p03_gate.py` 28 项（AC-01..AC-14）；
  抽取 `tests/p03_fixture.py` 共享夹具（原 `test_p03.py` 的内联 setUp 迁入，断言未改）；
  `fake_gh.py` 增加 `listfail` / `badjson` 模式，`pr create` 改为记录真实 candidate 与分支名。
  `test_p03.py::test_push_failure` 的构造方式随之调整（原写法现在会先被 base 校验拦下），
  改为"远端可达、仅推送被拒"，**未下调任何断言**。全量 `374 OK`（2026-10-24 03:43，416.465s）。
- **兼容性**：`publish()` 签名与 P02 调用方式未变；`github.enabled=false` 时仍为 `SKIPPED_DISABLED`；
  正常 Draft PR 流程（推送 → 建 PR → 复用）不变。行为变化：远端状态不可读/不一致时**提前拒绝**，
  而不是带着不确定性继续。
- **遗留**：PR 查询只覆盖 head 分支；跨 fork 的 head 归属未单独校验（依赖 `--repo` + headRefName）；
  `--state all` 仍一次取全部匹配，歧义时交人工；发布动作本身仍需人工授权后才可能真实发生
  （当前 `github.enabled=false`）。

---

## SS-V01-004 · Reviewer Candidate SHA 严格绑定

- **提交**：见交付报告
- **基线**：`66cb2ef`
- **原因**：审查版本校验用的是**双向前缀匹配**——`reviewer.py`（旧）：
  `len(candidate) >= 7` 且 `head_sha.startswith(candidate) or candidate.startswith(head_sha)`。
  先写回归测试证明缺陷（新增 23 项，其中 8 项在修复前失败）：
  - 7 位前缀 → 通过（应拒绝）
  - 12 位前缀 → 通过（应拒绝）
  - 完整 SHA 后加 `0` / `-patched` → 通过（应拒绝）
  - 大写十六进制 → 被拒（不应因大小写变化误判）
  修复前的端到端表现：`shortsha`/`suffixsha` 模式下 Reviewer 自报 APPROVED 也能一路走到底。
- **决策**：
  1. `_check_object_id()`：只接受十六进制、长度 40（SHA-1）或 64（SHA-256）的**完整**对象 ID；
     **不写死算法**——要求 `len(candidate) == len(head_sha)`，由真实提交决定。
  2. `candidate.lower() == head_sha.lower()` 才算一致：大小写差异允许（同一对象 ID），
     前缀、截断、附加字符、另一个合法 SHA、空值、非十六进制一律拒绝。
  3. `head_sha` 自身非法时也拒绝（Bridge 侧内部错误不得变成"审查通过"）。
  4. 复用既有失败路径：`REVIEW_INVALID` → `P02_BLOCKED_REVIEW_INVALID`，
     **未新增状态**、不进修复轮次、不进 READY。
  5. `schemas/review.schema.json` 同步收紧（`pattern` + `minLength:40` / `maxLength:64`），
     把"完整 SHA"的信号也传给模型；真正的强制仍在 Python 侧。
  6. 审查对象绑定沿用既有 `worktree_unchanged()`（HEAD + 工作区），未重复造机制。
- **测试**：新增 `tests/test_reviewer_sha.py` 23 项（AC-01..AC-09，含 SHA-256 64 位格式用例）；
  `tests/fake_codex.py` 增加 `shortsha` / `suffixsha` 两种审查模式。全量 `346 OK`
  （2026-10-24 03:03，298.894s）。
- **兼容性**：完整正确 SHA 正常通过（`approve` 模式端到端仍到 `P02_READY_FOR_HUMAN`）；
  Builder / 修复轮次 / 最终化 Journal / Publisher 流程未受影响。
- **遗留**：Reviewer 仍是模型自报 SHA，Bridge 侧只做一致性校验；若模型幻觉出一个
  与真实提交长度相同的合法 ID，会被长度+相等性挡下，但无法从语义上证明"它审的就是这段 diff"——
  这部分依赖 diff 文本本身随 prompt 下发这一事实。

---

## SS-V01-003-R1 · 测试解释器可信路径验证

- **提交**：见交付报告
- **基线**：`dc25135`
- **原因**：复查 `test_policy` 时怀疑"PATH 参与信任推导 + 执行原始命令名"会让 PATH 劫持生效。
  **先证实再改**：把临时目录前置到 PATH（目录内放空的 `python.exe`，仅用于路径解析、绝不执行），
  在 `dc25135` 上实测得到 `resolve() -> True`，且该目录出现在 `trusted_dirs` 中。
  同一手法对 `py` 启动器同样成立。
- **根因**：
  1. `_trusted_dirs()` 把 `shutil.which("python"/"python3"/"py")` 命中的**父目录**加入可信集合
     → PATH 变成信任来源；
  2. `resolve()` 放行时返回**原始 argv**，`subprocess.run` 由 OS 再次按 PATH 解析
     → 校验路径与执行路径可以不同（TOCTOU）。
- **决策**：
  1. 受信目录改为固定集合：`sys.executable` 父目录、`base_prefix`/`prefix`、
     Windows `SystemRoot`（`py.exe` 固定位置）、配置 `trusted_interpreter_dirs`。
     **不再从 PATH 推导**；`which()` 仅用于定位候选文件。
  2. `resolve()` 放行时返回 `[验证后的绝对路径, *原始参数]`（保留 `py -3` 的 `-3`）。
     执行侧（P01 执行块、P02 `run_tests`）本就使用返回值，因此自动改为执行绝对路径。
  3. 未新增第二套授权机制：仍复用 `trusted_interpreter_dirs`；策略缺失仍一律拒绝。
- **测试**：新增 `PathHijackTests` 8 项（AC-R1..R5，含 `py` 启动器劫持、执行路径一致性、
  显式授权目录、原有兼容、拒绝先于 `subprocess.run`）。全量 `323 OK`（2026-10-24 02:48，282.039s）。
- **兼容性**：`sys.executable`、`py -3 ... discover`、`unittest discover` 全部通过。
  行为变化：**裸 `python` 只有在受信目录内才通过**；若环境里 PATH 的 python 与 Supervisor
  解释器不同（例如另一套独立安装），会被拒绝。需要时由人工把该安装目录写进
  `trusted_interpreter_dirs`。
- **遗留**：信任粒度仍是"目录"而非文件哈希；`SystemRoot` 被整体信任（普通用户不可写）。

---

## SS-V01-003 · 测试命令授权加固

- **提交**：见交付报告
- **原因**：`validate_p01_task` 只用 `Path(cmd[0]).name not in RUNNERS **and** not Path(cmd[0]).exists()`
  判断首元素，参数完全不校验。实测（旧版文件从 `git show HEAD:bridge/p01_bridge.py` 加载后运行）：
  `["python","-c","print('not tests')"]`、`["python","-m","http.server"]`、
  `["C:\\Windows\\System32\\notepad.exe"]` **全部被判为合规**；三者在旧逻辑下无任何错误项。
- **决策**：
  1. 新增 `bridge/test_policy.py` 作为唯一判定入口：解释器必须解析到**受信任目录**
     （当前解释器目录 / `base_prefix` / PATH 上可解析的解释器 / 配置追加项），
     去掉解释器后的 argv 必须与 `approved` 中某条**完整相等**。
  2. 授权清单进 `config/bridge.json` → `test_command_policy.approved`（2 条）；
     任务 JSON 只能引用。**配置缺失/非法/空列表一律拒绝**，不回退内置白名单（技术决策：方案 B）。
  3. 执行层复检，不只信受理阶段：P01 执行前、P02 `_run_job`（建 worktree/调 Codex 之前）、
     P02 `run_tests` 各判一次。拒绝时**不启动进程**、不产出"测试通过"、不进修复轮次、
     不归类为 `P02_BUSY`；新状态 `P01_BLOCKED_TEST_COMMAND` / `P02_BLOCKED_TEST_COMMAND`
     （后者已加入 `TERMINAL_STATUSES`，恢复流程按终态处理）。
  4. `py` 启动器允许紧随一个 `-3` / `-3.x` 版本参数，其余参数按原样匹配——
     历史任务 `py -3 -m unittest discover -s tests [-v]` 继续有效。
  5. P01 的测试子进程改用 `env_guard.scrub_secrets(os.environ)`（P02 早已如此）。
- **测试**：新增 `tests/test_test_command_policy.py`（26 项，AC-01..AC-07）+ `tests/policy_fixture.py`
  （共享夹具）；7 份既有夹具显式登记策略段。全量 `315 OK`（2026-10-24 02:35，290.468s）。
  改前 39 项红（夹具缺策略 + 门禁拒绝原因被校验抢先），逐项修复后转绿，未下调任何断言。
- **兼容性**：任务 JSON 结构不变；历史任务的 `py -3 ... discover -s tests` 全部通过。
  代价：**未写策略段的部署将拒绝执行测试**（方案 B 的已知代价，已记入 `AI_HANDOFF.md` §8）。
- **遗留**：`approved` 目前只覆盖 unittest discover；项目若换 `pytest`，需人工改配置。
  解释器可信目录按"目录"而非"文件哈希"判定，仍依赖该目录本身不被普通用户写入。

---

## SS-DOC-002 · 交接文档一致性修正

- **提交**：见交付报告（本文件随该提交一起入库，因此不写自身 SHA）
- **原因**：SS-DOC-001-V1 冷启动评估发现 4 类文档与实际状态不一致——
  分支名已过期、`allow_merge` 被写成运行时校验、worktree 记录与现场冲突、
  架构/数据契约缺口未登记。
- **决策**：
  - `AI_HANDOFF.md` §1 拆成「本地 Git 状态 / 分支现状 / 远端状态（UNKNOWN）」三块，
    每块带核实时间；远端只登记引用 SHA 与引用文件 mtime，不推测远端内容。
  - `AGENTS.md` §7 增加「发布开关真实生效方式」表，区分
    **配置声明 / 运行时检查 / 系统未实现的能力**，并写明新增远端写能力前必须先补运行时检查。
  - `DEVLOG.md` 迁移条目**追加**现状段，原文不改（遵守 §8「只能追加」）。
  - `PROGRESS.md` 新增 §7 文档缺口登记，**只登记不补写**。
- **做了**：4 个 Markdown 文件；未改 Python、未改配置、未动数据库、未删/注册 worktree。
- **测试**：仅文档改动，**未执行测试**。`289` 仍是 SS-DOC-001 记录的历史结果
  （2026-10-24 01:14），本次未复核，不得当成新证据。
- **遗留**：架构说明与数据契约仍缺失（`PROGRESS.md` §7）；孤立 worktree 目录未处置；
  远端真实状态须 `git fetch` 后才能确认。

---

## SS-DOC-001 · 交接文档体系

- **提交**：见交付报告（本文件随该提交一起入库，因此不写自身 SHA）
- **原因**：此前所有上下文只存在于对话记录里，新 AI 无法仅凭仓库接手。
- **决策**：
  - 建立 5 类文档，职责不重叠：`AGENTS.md`（长期约束）/`AI_HANDOFF.md`（现场快照）/`DEVLOG.md`（过程）/`RUNBOOK.md`（操作）/`PROGRESS.md`（进度）。
  - **不建** `REPORTS.md`：报告索引并入 `PROGRESS.md`，避免两份文件对"当前状态"给出不同说法。
  - 运行快照必须带核实时间；快照不等于事实。
- **测试**：文档改动不影响测试，提交前基线 `289 OK`（2026-10-24 01:14）。
- **遗留**：无。

---

## 仓库迁移至 E 盘

- **提交**：`b754467` `chore: 迁移到 E:/Documents/AI-Developer/repositories/SoloShip`
- **原因**：C 盘仅剩 2.4 GB，且原路径 `C:\Users\hua\SoloShip-P00` 位于用户目录下。
- **决策 / 过程**：
  1. 优雅停止 Supervisor（`graceful=true`）；
  2. `robocopy` 复制后做**逐文件 sha256 比对**（437 文件全等）才继续；
  3. 6 个 worktree 用 `git worktree repair` 修正 gitdir（4 个属 `demo/soloship-demo`，2 个属 `D:\SoloShip\repos\soloship-demo`）；
  4. 两个 PS 脚本默认 `$Root` 改成新路径；计划任务重新注册；
  5. 新路径启动并验证 `LastTaskResult=0`、单实例。
- **踩坑**：
  - 目标目录原本**非空**（含 `B-Lite-design-kit` 与旧 zip），robocopy 把无关内容混入仓库；已移到 `repositories\` 根目录后重新比对才全等。
  - bash 工具的工作目录会持久化，shell 停在旧路径会造成目录被占用。
- **测试**：新路径 `289 OK`。
- **遗留**：旧目录 `C:\Users\hua\SoloShip-P00` **无法重命名**（Access Denied，已排除进程与 shell cwd），仅写入 `_MIGRATED_TO_E.txt` 标记，需人工处理。
- **当前状态（核实 2026-10-24 01:53，与上述历史操作严格区分）**：
  - 上面写的「6 个 worktree 用 `git worktree repair` 修正 gitdir」是**迁移当时执行过的操作**，
    不代表现在仍然如此。
  - 现状：`git worktree list` 只返回主工作区
    （`E:/Documents/AI-Developer/repositories/SoloShip 29401af [main]`）；
    `.git/worktrees` **目录不存在**，即那 6 个 worktree 当前**均未注册**。
  - `.worktrees/` 下仍留有 6 个目录：`p01_demo_remove_001`、`p02_trim_remove_001`、
    `p02b_router_demo_001`、`p02b_router_demo_002`、`p03d_remove_001`、`p03d_remove_002`，
    属**孤立目录**（无 git 注册，不能直接当 worktree 用）。
  - 注册为何失效：**未核实**（仓库内没有对应记录）。
  - 处置：按 `AGENTS.md` §5 只登记风险，**不删除、不重新注册、不清理**。
    新任务需要 worktree 时由 P02 重新创建，不复用这些目录。

---

## SS-V01-002-R1 · 已完成任务的报告丢失恢复

- **提交**：`1e3f81e` `fix(p02): 已完成任务的报告可从持久证据清单恢复`
- **原因**：SS-V01-002 成功后删除 Journal，任务进入 `READY` 后若报告丢失，恢复流程没有任何可重放证据，只能标记 `REPORT_MISSING_NO_EVIDENCE`。
- **先证后改**：先写 4 项测试，改动前稳定失败（`report.json/md 应可恢复` 断言为 False），确认缺陷真实存在。
- **决策**：成功后把 Journal **移动**到 `.control/p02/finalized/<job_id>.json` 作为「已完成证据清单」，复用既有结构，不另建证据存储；`recover_finalizing()` 同时扫 `finalizing/`（补交付物+推进状态）与 `finalized/`（只补缺失文件，不改 DB、不移动 inbox、不删清单）。
- **测试**：新增 4 项 → `289 OK`。
- **遗留**：清单只对新增任务生效；清单与报告同盘；无保留策略；孤儿清单需人工。

---

## SS-V01-002 · P02 最终化一致性与报告恢复

- **提交**：`f407d88`（WIP 脚手架）+ `c63b926`（主体）
- **原因**：`finish()` 在报告生成前就写 `finished_at`。进程在最终化中途退出后，数据库显示已完成、报告缺失；再次投递被 `accept()` 判为 `DUPLICATE`，报告永远补不回来；`detect_unfinished` 以 `finished_at IS NULL` 为判据，根本看不见这类任务。
- **决策**：
  1. 顺序改为 **Journal → DB FINALIZING → 交付物 → 归档 → 终端状态 → 收尾**；Journal 先于 DB，故「Journal 已写、DB 未更新」也可恢复。
  2. Journal 记录 evidence / receipt / **三份交付物最终文本** / review 摘要，并自带 `payload_sha256`。存文本是为了恢复后逐字节一致、且能识别冲突。
  3. 恢复时：缺失补写、一致跳过、冲突 → `P02_NEEDS_HUMAN` 且不覆盖。
  4. 异常收窄：只有 `RequestBusy` 映射 `P02_BUSY`；最终化故障为 `P02_FINALIZE_ERROR`；其余异常不吞，保留原始类型与堆栈。
  5. 重复投递不再覆盖原回执，改写 `.duplicate.json`。
- **测试**：新增 12 项故障注入（AC-01..AC-11）→ `285 OK`。
- **遗留**：
  - Journal 写入前崩溃只停在 `ACCEPTED`，不进 `missing_reports`；
  - 并发恢复无互斥；
  - `NEEDS_HUMAN` 无复位入口。
- **外部改动插曲**：`f407d88` 承接的是 2026-10-24 00:22 出现在工作区、并非本会话写入的 34 行脚手架。按 `AGENTS.md §9` 保全现场（patch 导出到仓库外）并逐项核对后才继续。

---

## SS-V01-001 · 统一自动开发安全门禁

- **提交**：`b1b5430` `fix(security): 统一自动开发安全门禁，堵住 Supervisor 绕过路径`
- **原因**：`security.require_auto_dev()` 只在 CLI `main()` 里通过 `guard_or_exit()` 调用；`P01Bridge.process_one` / `P02Bridge.process_one` 没有检查，而 `Supervisor.execute()` 是**同进程直调** `process_one`，因此 `BLOCKED` 时仍可能触发 Codex Builder / Reviewer / 项目测试。
- **决策**：
  - 在 `process_one` 内部（格式校验之后、任何执行动作之前）强制检查；P02 新增 `require_auto_dev()` 每次实时读配置，不复用 `__init__` 缓存。
  - Supervisor 的拦截点放在 `claim()` **之前**：若放到 `execute()` 里失败，`tick()` 会把结果计入 `processed` 并把认领标 `done`，等于「未授权任务被标记成功」。
  - P00 交接不设门禁（文件交接不执行代码）。
- **测试**：新增 `tests/test_security_gate.py` 9 项 → `273 OK`。修复过程中发现 `test_p01/test_p02` 的临时根没有配置，加门禁后 19 项变红，补显式 `windows_test_mode` 授权块后恢复。
- **遗留**：`test_mode`（白名单）只在执行模式变化时刷新，长驻进程改白名单需重启。

---

## P00–P05B 阶段：无提交可追溯（部分未核实）

以下阶段在纳入 git 之前完成，**没有对应提交**，只能依据仓库内的报告与运行产物判断：

| 阶段 | 内容 | 证据 | 提交 |
| --- | --- | --- | --- |
| P00 | ChatGPT Work 交接 | `exchange/reports/demo-app__p00_*.report.md` | 无 |
| P01 | Codex CLI 接入 | `exchange/p01/reports/*.report.md` | 无 |
| P02 | 开发闭环 | `exchange/p02/reports/*.report.md` | 无 |
| P02-B | 模型路由 | `exchange/p02/reports/P02B-DELIVERY.md` | 无 |
| P02-S | 沙箱与 D 盘预检 | `exchange/security/P02S*.md` | 无 |
| P03 / P03-D | Draft PR 发布与首个实任务 | `exchange/p03/P03-DELIVERY.md`、`P03D-*.md` | 无 |
| P04 | 状态索引 / 集成指令 / 验收助手 | `docs/P04-完成报告.md` | 无 |
| P05-A | GitHub 实时状态 | `docs/P05A-完成报告.md` | 无 |
| P05-B | Supervisor 与登录自启 | `docs/P05B-*.md` | 无 |

首次纳入 git 的提交是 `24d0337`（`chore: 初始化 SoloShip 流水线源码（P00–P05B）`，46 个文件），
它把此前所有阶段的代码一次性提交，**不含任何中间历史**——因此这些阶段的具体改动时序**未核实**。

测试规模演进（191 → 233 → 257 → 259 → 262 → 264 → 273 → 285 → 289 → 315 → 323 → 346 → 374 → 382 → 402 → 414）来自各阶段报告与提交信息，属**可核实**范围。
