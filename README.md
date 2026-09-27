# Release Automation Pipeline（本地优先发布自动化）

一套可完全在本地验证的发布自动化能力，不依赖任何外部服务或凭据。覆盖：

1. **版本推导**：从当前仓库状态推导唯一、可复现的版本号与变更记录；
2. **多目标构建矩阵**：目标间结果严格隔离，部分失败可明确区分；
3. **内容寻址构建缓存**：键覆盖全部产物相关输入，损坏自动降级重建；
4. **分阶段上线与自动回滚**：本地模拟目标环境，健康不明绝不推进；
5. **本地 / CI 一致性**：两种执行路径结论一致，差异显式拒绝或降级；
6. **并发与重试安全**：文件锁 + 原子写 + 临时资源清理；
7. **预演（dry-run）**：执行前给出机器可读预测，全程零痕迹；
8. **发布记录**：每次真实执行产出确定性、可比较、可校验的机器可读记录。

## 安装与运行

仅需 Python 3.14（标准库实现，无运行时第三方依赖）：

```bash
# 推导版本
python -m release_pipeline version --repo .

# 仅跑构建矩阵（可模拟某目标失败）
python -m release_pipeline build --repo . --targets t1,t2 --fail t2

# 端到端：构建 + staging/canary/full 三阶段上线
python -m release_pipeline run --repo . --work-dir .release-work

# 预演：只预测，不落任何痕迹（work-dir 都不会创建）
python -m release_pipeline plan --repo . --work-dir .release-work

# 校验发布记录与实际产物自洽；比较两份记录
python -m release_pipeline verify-record --work-dir .release-work
python -m release_pipeline diff-records record-a.json record-b.json

# 校验本地/CI 一致性契约
python -m release_pipeline verify-parity --mode local
```

所有过程诊断打印到 **stderr**，**stdout 只输出 JSON**，方便 CI 解析。
测试：

```bash
pytest -q          # 112 个用例，全部在本地临时目录执行
```

## 退出状态码（CLI 与编排层一致）

| 码 | 含义 |
|----|------|
| 0  | 所有目标构建成功，且每个参与部署的目标都到达了**请求的最后一个阶段** |
| 1  | 契约类错误（空目标集、非法阶段序列、未知目标/阶段、版本冲突、CI 下不支持的参数、状态损坏），或某目标已有**别的版本发布卡在中间阶段**（`release_in_progress`），本次推进被明确拒绝 |
| 2  | 部分（非全部）构建目标失败（其他目标可能已成功并上线） |
| 3  | 某上线阶段健康门禁未通过（不健康或结论不明确）并已回滚 |
| 4  | 全部构建目标失败（与部分失败明确区分，不合并为同一结论） |

`verify-record` 在校验出不一致时返回 1；`diff-records` 在两份记录不同
时返回 1（相同返回 0），便于 CI 直接门控。

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

* **阶段链**：`none → staging → canary → full`，每次 `rollout` 调用只
  推进一阶段。
* **请求序列只是“要到哪里”**：`--stages` 必须是
  `staging → canary → full` 的**严格递增子序列**（允许跳过中间阶段，
  如 `staging,full` 或单独 `canary`）。真实执行以请求的**最后一个
  阶段为目的地**，沿规范链**逐阶段推进到那里**——被跳过的中间阶段
  仍然要真实经过、其健康关卡照常生效。比如请求 `staging,full` 时会
  依次通过 `staging → canary → full` 三道关卡，绝不能停在 `canary`
  就报成功；请求 `full` 时也要先过 `staging`、`canary`。
* **必须到达目的地才算成功**：只要还有目标没到达请求的最终阶段，就
  不会出现“成功（退出 0）”的结论。
* **健康门禁**：只有 `healthy=true 且 conclusive=true` 才推进。
  - 不健康 → 回滚；
  - **健康结论不明确（inconclusive）同样按失败处理并回滚**，绝不带着
    不确定性推进；
  - 回滚后该目标停在上一个已确认健康的阶段（被拦下的阶段记录在
    `blocked_at`），退出码 3。
* **原子切换**：发布物先安装到独立目录，可见性切换是一次符号链接的
  原子 rename；观察者只能看到旧版或新版，看不到半成品。
* **回滚可解释且可见状态一致**：回滚把 `current` 精确恢复到本次尝试前
  的指向；若上线前没有任何版本，则删除 `current`。回滚后会断言
  `visible_version == 尝试前版本`，不一致则抛 `rollback_visibility_mismatch`。
  每次推进/回滚都追加到带锁状态文件的 `history` 中。
* **重试安全**：已完全发布的版本重复调用是幂等 no-op；回滚后修复健康
  策略可重新推进；中断遗留的 `.staging-*` / `.current.tmp-*` 在下一次
  上线尝试时、**持有该目标状态锁的前提下**被清扫——既不会泄漏临时
  资源，也不会误删并发执行正在使用的暂存目录。

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

## 请求契约校验

`run` 与 `plan` 在任何副作用（建目录、写缓存、改状态）之前先做统一
校验（`validate.validate_config`），违例直接抛 `InvalidRequestError`
并以机器可区分的 `code` 拒绝——不照常执行，也不悄悄推进一部分：

| code | 含义 |
|------|------|
| `empty_targets` | 请求的目标集合为空 |
| `duplicate_target` | 目标名重复 |
| `unknown_target` | `--fail` 引用了未请求的目标 |
| `empty_stages` | 阶段序列为空 |
| `unknown_stage` | 阶段名（或健康策略键）不在 `staging/canary/full` 内 |
| `invalid_stage_sequence` | 阶段序列不是 `staging→canary→full` 的严格递增子序列（回退、重复同样拒绝） |

> 阶段序列表达的是“目的地”而不是“只走这几步”：允许写
> `staging,full` 或单独 `canary`，但真实执行会把中间跳过的阶段
> （及其健康关卡）全部补齐，直到最后一个请求阶段。

构建结果层面，`failure_kind` 字段（`none` / `partial` / `all`）与退出码
2/4 把“部分失败”和“全部失败”明确分开，记录与摘要中均可机器读取。

## 预演（dry-run）

`python -m release_pipeline plan ...` 接受与 `run` 完全相同的参数，输出
机器可读预测（JSON）：

* **逐目标**：`reuse`（缓存命中，将复用产物）/ `rebuild`（缓存缺失或
  损坏，将重新构建）/ `fail`（目标被配置为失败），附缓存键与缓存状态
  （`hit` / `miss` / `corrupted`）；
* **逐阶段**：从持久化状态出发，用与真实执行**完全相同**的规则模拟
  阶段链，给出 `from_stage`（模拟开始时持久化的阶段）、`final_stage`
  （最终落在哪里）、`blocked_at`（会在哪一步被拦下）与原因
  （`health-failed` / `health-inconclusive` / `release-in-progress`；
  目标版本已不低于请求阶段时给出 `already-beyond-requested-stages`）。
  模拟沿规范链一路走到**请求的最后一个阶段**，被请求跳过的中间阶段
  同样参与健康判定；
* **总体**：`would_exit_code` 使用与真实执行相同的优先级规则
  （全部构建失败 4 > 部分失败 2 > 版本冲突/发布进行中 1 >
  健康拦截 3 > 成功 0）。

### 怎么读预演结论，以及它与真实执行的一致性

同一组输入、同一个（预演前尚未被改动的）work-dir 下，先 `plan` 再
`run`，两份结论逐项一致：

* 预演逐目标的 `reuse`/`rebuild`/`fail` ↔ 真实执行的
  `cache-hit`/`built`/`failed`；
* 预演逐目标的 `final_stage` ↔ 真实执行后持久化状态里的 `stage`
  （以及 `current` 实际指向）；
* 预演说在某阶段 `blocked_at` 被拦下 ↔ 真实执行在**同一个阶段**回滚，
  停在同一个 `final_stage`，原因类别相同
  （`health-failed` / `health-inconclusive` / `release-in-progress`）；
* `would_exit_code` ↔ `exit_code`。

关键语义：**预演说会到达某阶段，真实执行就一定真的到达那里；预演说
会被拦下，真实执行也一定被同一关卡拦下。** 不会再出现“预演一路到
`full`、真实执行却停在 `canary` 还报成功”的错位。若持久化状态里已有
**另一个版本**发布在中间阶段（非干净完成的 `full`），`run` 不再让整个
进程崩掉，而是像预演预测的那样对该目标返回 `release_in_progress`
（退出 1，且不改动外版本、不安装新版本），并在 CLI 输出的
`deploy_errors` 中按目标列出。

保证：

* **零痕迹**：预演只读——不创建任何目录（包括 work-dir 本身）、不写
  缓存、不改状态、不动可见符号链接、不写记录；即使中途出错也不留
  半成品。缓存探测是只读的，**不会**像真实读取那样逐出损坏条目。
* **与执行一致**：同一组输入下，预演的复用/重建结论对应真实执行的
  `cache-hit`/`built`，拦截阶段与原因对应真实执行的回滚；有测试逐项
  断言（含跳过中间阶段的序列）。
* **并发安全**：所有被读对象（缓存条目、状态文件、符号链接）都以原子
  rename 发布，预演与其他执行并发时读到的是一致快照，既不破坏共享
  状态，也不会被带偏。真实执行的每次推进仍在每目标的状态锁内读改写，
  并发 `run` 之间、`plan` 与 `run` 之间都不会互相污染结论。

## 发布记录（release record）

每次**真实**执行（`run`，预演不产生）在 `<work-dir>/records/
record-<version>.json` 落一份机器可读记录，并随 CLI JSON 输出。记录
包含：

* `version`：版本号、提交、dirty 标记、来源、可复现哈希与完整变更
  记录；
* `inputs`：目标集、阶段序列、失败注入集、健康策略、环境指纹、配置
  指纹、源码树哈希——即“哪些输入会影响结论”的完整清单；
* `targets`：每个目标的状态与产物标识（产物名、`content_hash`、
  `cache_key`）；
* `stages_final`：每个目标最终落到的阶段、是否回滚、对外可见版本；
* `outcome`：退出码、`failure_kind`、是否有上线被拦。

**确定性**：记录只由声明的仓库根与显式输入决定——不含时间戳、绝对
路径、主机信息，也不含重复执行间会变化的字段（`cache_hit`、`reason`
字符串、尝试计数等被刻意排除）。因此同一组输入重复执行、切换工作目
录、环境变量不同、本地与 CI 两种路径下，记录**逐字节一致**
（`canonical_record_bytes`：键排序、紧凑分隔符的规范化 JSON）。

**比较**：`diff-records A B`（或 `record.diff_records`）递归比对两份
记录，输出 `{"identical", "changes": [{path, section, before, after}]}`；
`section` 区分是**输入**变了（`inputs.*`）还是**结果**变了
（`targets` / `stages_final` / `outcome` / `version`）。

**自洽校验**：`verify-record`（或 `record.verify_record`）重新哈希
`<work-dir>/artifacts/` 下的实际产物并与记录中的 `content_hash` 比对；
产物缺失或摘要不符都会以结构化 `mismatches` 报出（CLI 退出 1），绝不
悄悄当成正常。

## 并发与中断语义

| 共享资源 | 保护方式 |
|----------|----------|
| 构建缓存条目 | 每键 `fcntl` 排他锁 + 临时目录 + 原子 rename |
| 产物文件 | 唯一临时名 + `os.replace`，发布前再校验哈希 |
| 发布状态 `state.json` | 全局锁内读改写；`tmp + fsync + rename` 落盘 |
| 发布记录 `records/*.json` | 唯一临时名 + `fsync` + 原子 rename；同输入覆写为同字节 |
| 对外可见符号链接 | 每次切换用唯一临时链接名原子替换 |
| 异常中断 | 事务异常不落盘；临时目录/链接在持锁的下一次尝试中清扫 |
| 预演并发 | 全部只读 + 被读对象原子发布，天然无锁安全 |

## 目录结构

```
release_pipeline/
  versioning.py   版本与变更记录推导
  cache.py        内容寻址缓存（校验/逐出/原子发布 + 只读探测）
  builder.py      多目标构建矩阵（源码哈希/环境与配置指纹）
  deploy.py       模拟环境、阶段推进、健康门禁、回滚
  state.py        加锁的原子状态存储（含只读读取）
  validate.py     请求契约校验（空目标/非法阶段/未知目标等）
  plan.py         预演：零痕迹的机器可读预测
  record.py       发布记录：生成/规范化/比较/自洽校验
  parity.py       本地/CI 一致性契约
  pipeline.py     端到端编排与退出码
  cli.py          命令行（version/build/run/plan/verify-record/
                  diff-records/verify-parity）
tests/            112 个用例：重复执行、缓存失效、部分/全部失败、回滚、
                  并发、预演零痕迹与逐项一致性（含跳过中间阶段的序列）、
                  契约拒绝、记录确定性与可比较性等
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

# 6) 预演：不创建 work-dir，输出逐目标/逐阶段预测
python -m release_pipeline plan --repo . --work-dir /tmp/w6
test ! -e /tmp/w6 && echo "zero trace"

# 7) 预演与真实执行结论一致（先预演再执行，比对 would_exit_code）
python -m release_pipeline plan --repo . --work-dir /tmp/w7 | jq .would_exit_code
python -m release_pipeline run  --repo . --work-dir /tmp/w7 | jq .exit_code

# 7b) 跳过中间阶段：请求 staging,full 必须一路推进到 full（含 canary 关卡）
python -m release_pipeline plan --repo . --work-dir /tmp/w7b \
    --targets t1 --stages staging,full | jq '.stages[0].final_stage'  # "full"
python -m release_pipeline run  --repo . --work-dir /tmp/w7b \
    --targets t1 --stages staging,full | jq .exit_code               # 0
readlink /tmp/w7b/environments/t1/current                            # 已指向该版本

# 7c) 跳过阶段但被中间关卡拦下：预演与真实执行都停在 canary，退出 3
echo '{"canary":[false,true]}' > /tmp/h-block-canary.json
python -m release_pipeline plan --repo . --work-dir /tmp/w7c \
    --targets t1 --stages staging,full --health-file /tmp/h-block-canary.json \
    | jq '{final: .stages[0].final_stage, blocked: .stages[0].blocked_at, exit: .would_exit_code}'
python -m release_pipeline run  --repo . --work-dir /tmp/w7c \
    --targets t1 --stages staging,full --health-file /tmp/h-block-canary.json \
    | jq .exit_code                                                   # 3

# 8) 发布记录：重复执行逐字节一致；比较不同输入的两份记录
python -m release_pipeline run --repo . --work-dir /tmp/w8 >/dev/null
cp /tmp/w8/records/record-*.json /tmp/rec-a.json
python -m release_pipeline run --repo . --work-dir /tmp/w8 >/dev/null
diff /tmp/rec-a.json /tmp/w8/records/record-*.json && echo "byte-identical"
python -m release_pipeline run --repo . --work-dir /tmp/w9 \
    --targets t1,t3 >/dev/null
python -m release_pipeline diff-records \
    /tmp/rec-a.json /tmp/w9/records/record-*.json

# 9) 记录自洽校验：篡改产物必被发现
python -m release_pipeline verify-record --work-dir /tmp/w8
echo tampered > /tmp/w8/artifacts/app-0-*.bin
python -m release_pipeline verify-record --work-dir /tmp/w8   # 退出 1

# 10) 契约拒绝：空目标 / 空阶段 / 未知阶段 / 非法阶段序列 / 全部失败（退出码 4）
python -m release_pipeline run --repo . --targets ""            # empty_targets
python -m release_pipeline plan --repo . --stages ""            # empty_stages（plan 同样拒绝）
python -m release_pipeline run --repo . --stages staging,mars   # unknown_stage
python -m release_pipeline run --repo . --stages full,staging   # invalid_stage_sequence
python -m release_pipeline run --repo . --work-dir /tmp/w10 \
    --targets t1,t2 --fail t1,t2                                # 退出 4
```

非法请求在任何副作用（建目录、构建、部署、写状态/记录）**之前**被拒绝，
因此既不会照常上线，也不会只悄悄推进一部分；`plan` 与 `run` 使用同一套
校验、返回同一个机器可区分的 `code`（见“请求契约校验”一节）。
