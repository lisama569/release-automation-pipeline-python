# Release Automation Pipeline（本地优先发布自动化）

一套可完全在本地验证的发布自动化能力，不依赖任何外部服务或凭据。覆盖：

1. **版本推导**：从当前仓库状态推导唯一、可复现的版本号与变更记录；
2. **多目标构建矩阵**：目标间结果严格隔离，部分失败可明确区分；
3. **内容寻址构建缓存**：键覆盖全部产物相关输入，损坏自动降级重建；
4. **分阶段上线与自动回滚**：本地模拟目标环境，健康不明绝不推进；
5. **本地 / CI 一致性**：两种执行路径结论一致，差异显式拒绝或降级；
6. **并发与重试安全**：文件锁 + 原子写 + 临时资源清理；
7. **预演（preview）**：真正执行前给出机器可读结论，逐目标判定
   “复用已有产物 / 需要重建”，逐阶段判定推进到哪里、在哪一步被拦下；
   预演**完全零痕迹**，且与随后的真实执行逐项一致；
8. **发布记录（release record）**：每次真实执行产出可长期保存、可互比、
   可与磁盘产物自洽校验的确定性记录。

## 安装与运行

仅需 Python 3.14（标准库实现，无运行时第三方依赖）：

```bash
# 推导版本
python -m release_pipeline version --repo .

# 仅跑构建矩阵（可模拟某目标失败）
python -m release_pipeline build --repo . --targets t1,t2 --fail t2

# 端到端：构建 + staging/canary/full 三阶段上线
python -m release_pipeline run --repo . --work-dir .release-work

# 预演：只输出 JSON 结论，不创建/修改/删除任何东西
python -m release_pipeline preview --repo . --work-dir .release-work

# 发布记录自洽校验与两份记录逐字段比较
python -m release_pipeline record-verify --work-dir .release-work \
    .release-work/records/<id>.json
python -m release_pipeline record-compare <a.json> <b.json>

# 校验本地/CI 一致性契约
python -m release_pipeline verify-parity --mode local
```

所有过程诊断打印到 **stderr**，**stdout 只输出 JSON**，方便 CI 解析。
测试：

```bash
pytest -q          # 92 个用例，全部在本地临时目录执行
```

## 退出状态码（CLI 与编排层一致）

| 码 | 含义 |
|----|------|
| 0  | 所有目标构建成功，且所有阶段推进成功 |
| 2  | 一个或多个构建目标失败（其他目标可能已成功并上线） |
| 3  | 某上线阶段健康门禁未通过并已回滚 |
| 1  | 契约类错误（版本冲突、CI 下不支持的参数、状态损坏、请求契约违例等） |
| 4  | `record-verify` / `record-compare` 发现不一致（仅这两个子命令） |

`preview` 子命令本身总是以 0 退出（它是一次成功的检查），预测的执行
退出码在输出的 `predicted_exit_code` 字段中。

## 请求契约（执行与预演共同遵守）

以下请求在**任何目录被创建、任何状态被触碰之前**就会被拒绝，抛出
`ContractViolationError`（CLI 退出 1），`code` 字段可机器区分：

| code | 含义 |
|------|------|
| `empty_targets` | 目标集合为空 |
| `duplicate_target` | 同一目标被请求两次 |
| `unknown_target` | 目标不在已注册目标集合内 |
| `unknown_fail_target` | 为未请求的目标注入了失败 |
| `empty_stages` | 阶段序列为空 |
| `unknown_stage` | 阶段不在法定链 `staging → canary → full` 上 |
| `invalid_stage_order` | 阶段序列不是法定链的连续单调前缀（乱序/跳空/重复） |

已注册目标集合见 `contract.DEFAULT_KNOWN_TARGETS`（`linux-x86_64`、
`linux-aarch64`、`darwin-x86_64`、`darwin-aarch64`、`windows-x86_64`），
可通过 `PipelineConfig.known_targets` 显式替换。

全部失败与部分失败也严格区分：结果的 `outcome` 字段取值
`all-succeeded` / `partial-failure` / `all-failed`，上线被拦截时追加
`+rollout-blocked` 后缀，两类失败绝不合并成同一结论。

## 版本推导规则

版本格式（PEP 440 本地段）：

```
MAJOR.MINOR.PATCH+<commit_count>[.dirty].g<short_sha>
# 例：1.2.3+42.g3f9a2c1
#     1.2.3+42.dirty.g3f9a2c1        （工作区有未提交改动）
#     0.0.0+0.g0000000               （无 git 历史时的文档化兜底）
```

* **基线**取当前提交可达的最近一个 `vX.Y.Z` 标签；无标签时兜底为 `0.0.0`，
  并在输出的 `source` 字段注明（`git-tag` / `fallback-no-tag` /
  `fallback-no-git` / `declared+...`）。
* **稳定性**：版本只依赖提交历史与 dirty 标记；除 git 自身定位变量外，
  环境变量（如 `CI_RUN_ID`）一律不参与，因此同一输入在任何机器、任何时间
  重复触发都得到完全相同的 `VersionInfo`（含 16 位 `reproducible_hash`）。
* **来源冲突不猜测**：若显式传入 `--declared-version` 且与 git 标签基线
  不一致，直接报 `version_conflict` 失败，绝不二选一。
* **历史缺失**：不是 git 仓库或没有提交时，使用文档化哨兵
  `0.0.0+0.g0000000`，而不是抛错或随机生成。
* **变更记录**：`changelog` 以 NUL 分隔字段从 `git log` 解析，按新→旧
  返回 `(short_sha, subject)`，避免异常提交信息造成歧义。

## 缓存规则

缓存目录：`<work-dir>/cache/`。每个缓存键是对以下全部输入的 SHA-256：

```
format(缓存格式版本) · target · version · source_tree_hash
· env_fingerprint · config_fingerprint · toolchain
```

* **环境/配置变化必失效**：环境指纹来自显式允许列表（或 `--env-file`
  提供的映射），构建配置指纹来自规范化 JSON；任一字段变化 → 键变化 →
  不可能命中旧产物。
* **源码树哈希**：排除 `.git`、`__pycache__`、`.release-work`、`.venv`
  等目录，按相对路径排序逐文件哈希，路径或内容变化即变化。
* **读取时全量校验**：重新计算产物 SHA-256 并逐字段比对 `meta.json`。
  任何不一致、截断、JSON 损坏都判定为**缓存损坏**：逐出整个条目并重建，
  绝不会产出错误结果（`CacheCorruptedError` 语义对调用方表现为 miss）。
* **发布原子性**：`临时文件 → fsync → rename`，并按键加 `fcntl` 排他锁，
  并发生产者只会看到完整条目或看不到条目。
* **格式升级**：键内含缓存格式版本，格式不兼容时旧条目天然不可命中。

## 上线与回滚策略

本地用磁盘模拟目标环境（`SimulatedEnvironment`）：

```
<env>/releases/<version>/artifact.bin   # 先完整落盘的发布物
<env>/current -> releases/<version>     # 唯一对外可见入口（符号链接）
```

* **阶段链**：`none → staging → canary → full`，每次调用只推进一阶段。
* **健康门禁**：只有 `healthy=true 且 conclusive=true` 才推进。
  - 不健康 → 回滚；
  - **健康结论不明确（inconclusive）同样按失败处理并回滚**，绝不带着
    不确定性推进。
* **原子切换**：发布物先安装到独立目录，可见性切换是一次符号链接的
  原子 rename；观察者只能看到旧版或新版，看不到半成品。
* **回滚可解释且可见状态一致**：回滚把 `current` 精确恢复到本次尝试前
  的指向；若上线前没有任何版本，则删除 `current`。回滚后会断言
  `visible_version == 尝试前版本`，不一致则抛 `rollback_visibility_mismatch`。
  每次推进/回滚都追加到带锁状态文件的 `history` 中。
* **重试安全**：已完全发布的版本重复调用是幂等 no-op；回滚后修复健康
  策略可重新推进；中断遗留的 `.staging-*` / `.current.tmp-*` 在环境
  初始化时被清扫，不会泄漏临时资源。

## 预演（preview）

`python -m release_pipeline preview ...` 接受与 `run` 完全相同的参数，
输出机器可读的预演结论：

```json
{
  "version": "1.0.0+2.g44ee317",
  "predicted_exit_code": 0,
  "outcome": "all-succeeded",
  "builds":  [{"target": "...", "action": "reuse|rebuild|fail",
               "reason": "...", "cache_key": "...", "content_hash": "..."}],
  "deploys": [{"target": "...", "current_stage": "none",
               "planned_stages": ["staging", "canary", "full"],
               "final_stage": "full", "blocked_at": "",
               "blocked_reason": ""}]
}
```

* **零痕迹**：预演不创建、不修改、不删除任何产物、缓存条目、发布目录、
  状态文件、记录或对外可见状态——工作目录不存在时预演后依然不存在；
  已存在时逐字节不变。预演中途出错（如状态文件损坏）同样不留半成品。
* **与执行逐项一致**：预演与真实执行共用同一套纯规划函数
  （`planner.plan_rollout` / `health_verdict_for`）与同一套指纹、缓存键、
  退出码规则，因此同一组输入下：复用/重建判定、推进/拦截判定、最终
  阶段、退出码、outcome 全部一致（有测试逐项断言）。
* **缓存损坏**：预演探测到损坏条目时判定为 `rebuild`（与真实执行的
  “逐出并重建”结论一致），但**不会**真的逐出。
* **并发安全**：预演只读共享状态（状态文件本身原子落盘，读到的永远是
  完整快照），不持锁、不写文件，既不破坏并发执行，也不会被并发执行
  带偏出与快照不符的结论。
* **结论只取决于声明的仓库根与显式输入**：与当前工作目录、无关环境
  变量、local/CI 模式无关（有测试覆盖）。

## 发布记录（release record）

每次**真实执行**（`run`）结束后，在 `<work-dir>/records/<id>.json`
原子落盘一份记录；预演绝不产生记录。记录包含：

* `version`：版本号、commit、dirty、来源、reproducible_hash、变更记录；
* `inputs`：全部会影响结论的显式输入指纹（目标集、阶段序列、源码树
  哈希、环境指纹、配置指纹、声明版本、健康策略、失败注入）；
* `targets`：逐目标的构建状态、产物名、缓存键、内容哈希（完整性摘要）、
  最终阶段、是否被拦截；
* `outcome` / `exit_code` 与记录自身的 `id`（正文内容的 SHA-256）。

**确定性**：记录不含时间戳、绝对路径、执行模式等环境痕迹，因此同一组
输入重复执行、换工作目录、换机器、local 与 CI 之间，记录**逐字节一致**
（`id` 相同即文件相同，幂等写不会重复落盘）。注意 `built` 与
`cache-hit` 的区别属于机器既有缓存状态而非请求本身，因此记录中统一
归一为 `success`，保证重复执行记录稳定。

**解读与比较**：

```bash
# 与磁盘产物自洽校验：产物缺失/被改、缓存条目缺失/被改、记录被改都能发现
python -m release_pipeline record-verify --work-dir .release-work <record.json>
# 退出 0 = 完全自洽；退出 4 且列出 artifact_missing:<t> /
# artifact_hash_mismatch:<t> / cache_missing:<t> / cache_hash_mismatch:<t> /
# cache_meta_mismatch:<t> / record_id_mismatch

# 两份记录逐字段比较：明确指出哪些输入或结果发生了变化
python -m release_pipeline record-compare a.json b.json
# 输出形如 inputs.config_fingerprint: 'a1b2...' -> 'c3d4...'、
# targets.linux-x86_64.final_stage: 'staging' -> 'full' 等，逐条列出
```

记录文件被篡改但 `id` 未重算时，`load_record` 直接以
`record_id_mismatch` 拒绝——不一致不会被悄悄当成正常。

## 本地 / CI 一致性

`parity.check_parity(mode=...)` 在每次流水线开始时执行：

* 有效环境指纹只来自显式映射或允许列表，本地 shell 里额外 export 的
  变量无法改变判定；
* **不支持的参数**：`ci` 模式直接拒绝（`unsupported_param`，退出 1），
  `local` 模式显式降级并在结果的 `degraded` 字段中说明原因，绝不静默
  忽略；
* 可选能力缺失（如无 `fcntl` 锁）时，CI 拒绝执行，本地说明降级路径；
* 对相同的显式输入，`mode=local` 与 `mode=ci` 产生相同版本、相同缓存
  决策与相同上线结论（有测试断言）。

## 并发与中断语义

| 共享资源 | 保护方式 |
|----------|----------|
| 构建缓存条目 | 每键 `fcntl` 排他锁 + 临时目录 + 原子 rename |
| 产物文件 | 唯一临时名 + `os.replace`，发布前再校验哈希 |
| 发布状态 `state.json` | 全局锁内读改写；`tmp + fsync + rename` 落盘 |
| 发布记录 `records/<id>.json` | 内容寻址文件名 + 唯一临时名原子替换，同内容幂等 |
| 对外可见符号链接 | 每次切换用唯一临时链接名原子替换 |
| 异常中断 | 事务异常不落盘；临时目录/链接在下次启动清扫 |
| 预演 | 全程只读：不建目录、不持锁、不逐出、不落记录 |

## 目录结构

```
release_pipeline/
  versioning.py   版本与变更记录推导
  cache.py        内容寻址缓存（校验/逐出/原子发布/只读探测）
  builder.py      多目标构建矩阵（源码哈希/环境与配置指纹）
  deploy.py       模拟环境、阶段推进、健康门禁、回滚
  state.py        加锁的原子状态存储（含只读快照）
  parity.py       本地/CI 一致性契约
  contract.py     请求契约校验（先拒绝，后生效）
  planner.py      纯上线规划（预演与执行共用）
  pipeline.py     端到端编排、退出码与结论分类
  preview.py      零痕迹机器可读预演
  record.py       确定性发布记录（生成/校验/比较）
  cli.py          命令行（version/build/run/preview/record-verify/
                  record-compare/verify-parity）
tests/            92 个用例：重复执行、缓存失效、部分失败、回滚、并发、
                  契约边界、预演零痕迹与一致性、记录确定性与可比较性
```

## 本地手工验证建议

```bash
# 1) 重复执行：版本与结论一致，第二次全部缓存命中
python -m release_pipeline run --repo . --work-dir /tmp/w
python -m release_pipeline run --repo . --work-dir /tmp/w   # no-op 推进

# 2) 部分失败：退出码 2，健康目标仍然上线
python -m release_pipeline run --repo . --work-dir /tmp/w2 \
    --targets t1,t2 --fail t2

# 3) 健康失败/不明确：退出码 3 并回滚
echo '{"staging":[false,true]}'  > /tmp/h-unhealthy.json
echo '{"canary":[true,false]}'   > /tmp/h-inconclusive.json
python -m release_pipeline run --repo . --work-dir /tmp/w3 \
    --health-file /tmp/h-unhealthy.json

# 4) 本地/CI 同输入同判定
python -m release_pipeline run --repo . --work-dir /tmp/l --mode local
python -m release_pipeline run --repo . --work-dir /tmp/c --mode ci

# 5) CI 对未知参数显式拒绝
python -m release_pipeline run --repo . --mode ci --set bogus=1

# 6) 预演零痕迹：目录不存在时预演后依然不存在
python -m release_pipeline preview --repo . --work-dir /tmp/pv
ls /tmp/pv   # -> No such file or directory

# 7) 预演与执行一致：先预演再执行，对比 predicted_exit_code 与实际退出码
python -m release_pipeline preview --repo . --work-dir /tmp/w4
python -m release_pipeline run --repo . --work-dir /tmp/w4
python -m release_pipeline preview --repo . --work-dir /tmp/w4   # 全部 reuse

# 8) 请求契约：空目标/非法阶段/未知目标均以退出 1 + 机器可读 code 拒绝
python -m release_pipeline run --repo . --work-dir /tmp/w5 --targets ""
python -m release_pipeline run --repo . --work-dir /tmp/w5 --stages canary,staging

# 9) 发布记录：重复执行记录逐字节一致；校验与比较
python -m release_pipeline run --repo . --work-dir /tmp/r1
python -m release_pipeline run --repo . --work-dir /tmp/r2
diff /tmp/r1/records/*.json /tmp/r2/records/*.json && echo "byte-identical"
python -m release_pipeline record-verify --work-dir /tmp/r1 /tmp/r1/records/*.json
python -m release_pipeline record-compare /tmp/r1/records/*.json /tmp/r2/records/*.json
```
