# Release Automation Pipeline（本地优先发布自动化）

一套可完全在本地验证的发布自动化能力，不依赖任何外部服务或凭据。覆盖：

1. **版本推导**：从当前仓库状态推导唯一、可复现的版本号与变更记录；
2. **多目标构建矩阵**：目标间结果严格隔离，部分失败可明确区分；
3. **内容寻址构建缓存**：键覆盖全部产物相关输入，损坏自动降级重建；
4. **分阶段上线与自动回滚**：本地模拟目标环境，健康不明绝不推进；
5. **本地 / CI 一致性**：两种执行路径结论一致，差异显式拒绝或降级；
6. **并发与重试安全**：文件锁 + 原子写 + 临时资源清理。

## 安装与运行

仅需 Python 3.14（标准库实现，无运行时第三方依赖）：

```bash
# 推导版本
python -m release_pipeline version --repo .

# 仅跑构建矩阵（可模拟某目标失败）
python -m release_pipeline build --repo . --targets t1,t2 --fail t2

# 端到端：构建 + staging/canary/full 三阶段上线
python -m release_pipeline run --repo . --work-dir .release-work

# 校验本地/CI 一致性契约
python -m release_pipeline verify-parity --mode local
```

所有过程诊断打印到 **stderr**，**stdout 只输出 JSON**，方便 CI 解析。
测试：

```bash
pytest -q          # 48 个用例，全部在本地临时目录执行
```

## 退出状态码（CLI 与编排层一致）

| 码 | 含义 |
|----|------|
| 0  | 所有目标构建成功，且所有阶段推进成功 |
| 2  | 一个或多个构建目标失败（其他目标可能已成功并上线） |
| 3  | 某上线阶段健康门禁未通过并已回滚 |
| 1  | 契约类错误（版本冲突、CI 下不支持的参数、状态损坏等） |

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
| 对外可见符号链接 | 每次切换用唯一临时链接名原子替换 |
| 异常中断 | 事务异常不落盘；临时目录/链接在下次启动清扫 |

## 目录结构

```
release_pipeline/
  versioning.py   版本与变更记录推导
  cache.py        内容寻址缓存（校验/逐出/原子发布）
  builder.py      多目标构建矩阵（源码哈希/环境与配置指纹）
  deploy.py       模拟环境、阶段推进、健康门禁、回滚
  state.py        加锁的原子状态存储
  parity.py       本地/CI 一致性契约
  pipeline.py     端到端编排与退出码
  cli.py          命令行（version/build/run/verify-parity）
tests/            48 个用例：重复执行、缓存失效、部分失败、回滚、并发等
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
```
