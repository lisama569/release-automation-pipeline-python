# 发布自动化流水线

一套可完全在本地验证的发布能力：**版本推导 → 多目标构建矩阵 → 构建缓存 → 分阶段上线与自动回滚**。
不依赖任何外部服务或凭据，本地与 CI 执行同一份代码、同一套判定，退出码语义一致。

- 包：`release_pipeline/`
- CLI：`release-pipeline`（或 `uv run release-pipeline`）
- 默认工作目录：`.release-work/`（缓存、产物、模拟环境、发布状态都在这里）

---

## 1. 版本推导规则

版本只由仓库内容与显式输入决定，相同输入**永远**得到相同版本（PEP 440）。

### 1.1 来源优先级

1. 显式参数 `--set-version X.Y.Z`（最高优先级）
2. 仓库根的 `VERSION` 文件
3. HEAD 可达的最高语义化标签（`v1.2.3` 或 `1.2.3`）
4. 兜底基础版本 `0.0.0`

冲突处理：不同来源不一致时，**按优先级裁决，但在 `resolution` 与 changelog 中显式记录冲突**，
绝不静默选择。非法显式版本（非 `X.Y.Z`、空串、注入字符）直接拒绝。

### 1.2 版本后缀

| 情形 | 形式 | 示例 |
| --- | --- | --- |
| HEAD 恰好被语义化标签命中、工作区干净 | 纯发布版本 | `1.2.3` |
| 基线标签之后有 N 个提交 | `base.devN` | `1.2.3.dev4` |
| 工作区有未提交改动 | `+dirty.<内容摘要12位>` | `1.2.3.dev4+dirty.9f3a1c7d2e8b` |
| 无提交（unborn HEAD）/ 非 git 目录 | `+tree.<工作区摘要12位>` | `0.0.0.dev0+tree.2ad600d506f8` |

- dirty 摘要覆盖 `git status` 中每个条目的状态、路径与文件内容；**改一个字节版本就不同**。
- git 不可用 / 非仓库 / 无历史时，对工作区全部非 `.git` 文件做内容哈希，仍可复现，
  降级原因写入 changelog 与 resolution。
- HEAD 上存在多个语义化标签时，按字典序确定性选取，并记录该选择。

变更记录（changelog）取 `base_tag..HEAD` 的提交；无基线时明确标注“历史信息缺失”。

```bash
uv run release-pipeline version            # 打印版本
uv run release-pipeline version --json     # 完整结论：来源、冲突裁决、changelog
uv run release-pipeline version --set-version 9.9.9
```

---

## 2. 构建矩阵与缓存规则

### 2.1 目标与命令

构建矩阵来自 `release.toml`（缺省时内置 4 个模拟目标）。目标名与分阶段计划对应：
`canary`、`partial-a`、`partial-b`、`full`。

| 命令 | 含义 |
| --- | --- |
| `sim:ok` + `options.health=ok|fail|ambiguous|inconsistent` | 本地确定性模拟构建，随产物生成健康清单 |
| `sim:fail:spec|build|package` | 对应阶段确定性失败，用于异常演练 |
| `cmd:-- <argv...>` | 真实命令；在临时工作区执行，须产出 `payload`（可附 `health.json`） |

构建产物是确定性 tar bundle（固定成员 `payload` + 可选 `health.json`，固定 mtime），
同输入字节级一致。单个目标的任何异常都被收敛为该目标的 `FAILED`，**不影响其他目标**。

### 2.2 缓存键（覆盖全部影响产物的输入）

```
cache_key = sha256(target | version | spec_digest | sources_digest | env_digest)
```

- `spec_digest`：构建命令与选项（命令/选项变 → 必失效）
- `sources_digest`：`inputs` 声明的所有文件的相对路径 + 内容（内容变 → 必失效）
- `env_digest`：**白名单**环境变量（见下），排序后哈希
- `version`：含 dirty/tree 摘要的完整版本结论

进入缓存键的环境变量白名单（`release_pipeline.runtime.CACHE_ENV_WHITELIST`）：
`PIP_INDEX_URL`、`PIP_EXTRA_INDEX_URL`、`UV_INDEX_URL`、`UV_HTTP_TIMEOUT`、
`RELEASE_BUILD_PROFILE`、`RELEASE_TARGET_ARCH`、`RELEASE_TARGET_OS`。

- 白名单之外的变量变化不会抖动缓存；
- 白名单之内的任何变化必然改变缓存键，不会命中过期缓存；
- `inputs` 声明了不存在的路径时直接失败——拒绝在输入不明时构建/复用。

### 2.3 命中校验与损坏降级

命中后仍校验：元数据字段完整、`cache_key` 一致、产物存在、产物 sha256 与记录一致。
任一不符即判定 **corrupt**：条目与产物移入 `quarantine/` 并按 miss **重建**，
重建结果与正常产物一致时才视为成功——绝不产出错误结果。

并发安全：
- 每个缓存键一把 flock；产物“临时文件 + 原子 rename”，元数据原子 JSON 写；
- 多线程/多进程并发构建同一目标，最终只留一个条目，产物摘要一致。

---

## 3. 上线与回滚策略

### 3.1 模拟目标环境（无需外部服务）

每个目标就是 `<work>/env/<target>/`：

```
versions/<v>/payload, health.json   # 已暂存版本
current -> versions/<v>             # 对外可见版本（原子符号链接）
```

对外可见状态**只**由 `current` 决定。stage（暂存，解包 bundle，不切指针）与
promote（原子切换指针）分离，读者只会看到旧版本或新版本，没有半更新。
正在对外服务的版本拒绝被同号不同内容覆盖；仅暂存未上线的失败遗留允许原子替换。

### 3.2 阶段计划

| 阶段 | 覆盖目标 |
| --- | --- |
| `canary` | canary |
| `partial` | canary, partial-a, partial-b |
| `full` | 全部 4 个 |

每阶段动作：清理遗留暂存 → 暂存全部目标产物 → 原子 promote → 逐目标健康探针。
阶段必须构成计划的**有序前缀**，跳过 canary 的参数（如只给 `--stage full`）会被明确拒绝。

### 3.3 健康门（三态，不明确绝不推进）

探针读取当前对外版本的 `health.json`，连续多次（默认 3 次）观测：

- 全部 `ok` → healthy，允许推进；
- 明确 `fail` → unhealthy，停止并回滚；
- 缺指针 / 版本不符 / 清单缺失或损坏 / 多次结论矛盾 → **ambiguous（不明确）**，同样停止并回滚。

`sim:ok` 的 `health=ambiguous`（不生成清单）、`health=fail`、`health=inconsistent`
可分别演练这三种结论。

### 3.4 自动回滚与对外状态校验

任一阶段未过门：立即停止推进，把所有已涉及目标的 `current` 原子切回**上线前快照版本**
（首次上线则移除指针），随后逐目标校验对外版本与快照**完全一致**；不一致则报
`ROLLBACK_INCONSISTENT`（退出码 21），要求人工介入，而不是假装回滚成功。

### 3.5 中断与重试安全

发布状态持久化在 `<work>/rollout-state/rollout-state.json`（flock + 原子写），记录：
版本、上线前快照、各阶段 done/rolled_back、已暂存/已推进名单。

- 重试时已完成阶段幂等跳过，不重复推进；
- 上次硬中断遗留的 `.*.stage-tmp` 暂存目录在每轮开始时清理，不泄漏临时资源；
- 版本守卫基于状态机判定，避免两个版本在同一环境交错推进：
  - 上一轮处于**终态**（deployed / rolled_back）时，允许发布新版本（常规升级路径，
    新版本的上线前快照就是当前对外版本）；
  - 同版本重试允许（幂等恢复）；
  - 上一轮仍在**进行中**（有阶段未完成且未回滚）就换版本，明确拒绝；
- 两个不同版本并发发布同一环境：全局 flock 串行化，持锁期间竞争者拿锁超时被明确拒绝，
  最终所有目标指向同一个版本，不产生混合状态；
- 正在对外服务的版本若被同号不同内容的产物覆盖，直接拒绝；仅暂存未上线的失败遗留
  允许原子替换，便于“同版本号修复后重试”。

### 3.6 退出码（本地/CI 一致）

| 码 | 含义 |
| --- | --- |
| 0 | 成功（全部构建成功；启用上线时全部阶段完成） |
| 10 | 构建部分失败：成功目标产物可用，失败目标无产物，不进入上线 |
| 11 | 构建全部失败，无可用产物 |
| 12 | 构建矩阵为空 |
| 20 | 健康门拦截，已自动回滚且对外状态恢复一致 |
| 21 | 上线/回滚本身失败（状态不一致等），需人工介入 |
| 30 | 环境能力/参数被拒绝（如无文件锁、非法阶段、显式配置缺失） |
| 31 | 版本推导失败 |
| 1 | 其他未预期错误 |

---

## 4. 本地与 CI 一致性

- `profile`（`local`/`ci`，通过 `CI`、`GITHUB_ACTIONS` 等标记变量识别）**只作为元数据输出**，
  不参与版本、缓存、健康判定；同一份代码在两种环境中结论一致。
- 能力门禁（`release-pipeline env`）显式列出支持/不支持项与原因；
  被请求但不支持的能力直接拒绝（退出码 30），不静默忽略、不偷偷改判定。
- 不支持的参数（非法阶段名、跳过 canary、显式指定但不存在的配置文件）一律显式报错。

```bash
uv run release-pipeline env --json
```

---

## 5. 本地验证方法

### 5.1 一键测试

```bash
uv sync          # 安装含 pytest 的开发依赖
uv run pytest -q # 69 项用例：版本、缓存、矩阵、健康门、回滚、并发、中断恢复、CLI
```

测试覆盖的异常链路：重复执行版本一致、缓存损坏重建、环境/输入/命令变化失效、
部分构建失败隔离、canary/partial 失败回滚、ambiguous 拦截、首次上线失败回到无版本、
中断后重试幂等、并发发布互斥、失败构建无临时残留。加 `-s --log-cli-level=INFO`
可看到“关键输入 → 判定依据”的完整日志。

### 5.2 手动演练（全部离线）

```bash
# 1) 查看环境能力
uv run release-pipeline env

# 2) 正常发布（默认四个 sim 目标）
uv run release-pipeline --verbose release --set-version 1.2.3 --json

# 3) 重复执行：第二次构建全部 cache=hit
uv run release-pipeline release --set-version 1.2.3 --json

# 4) 演练部分失败（退出码 10）：把 partial-a 的 command 改成 sim:fail:build

# 5) 演练健康失败自动回滚（退出码 20）：canary options.health="fail"

# 6) 白名单环境变量变化导致缓存失效
RELEASE_TARGET_ARCH=arm64 uv run release-pipeline build --json
```

所有产物与状态都在工作目录内，删掉工作目录即可完全复位：
`rm -rf .release-work`。
