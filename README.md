# release-automation-pipeline-python

本地优先、可在 CI 中零差异复用的发布自动化流水线：确定性版本推导、多目标构建矩阵、
内容寻址构建缓存、分阶段上线与自动回滚。**无需任何外部服务或凭据。**

## 快速开始

```bash
uv sync

# 查看执行环境与能力门禁
uv run release-pipeline env --json

# 推导版本（含来源裁决与 changelog）
uv run release-pipeline version --json

# 全流程：构建（带缓存）→ canary/partial/full 分阶段上线
uv run release-pipeline --verbose release --set-version 1.2.3

# 再跑一次：版本一致、构建全部命中缓存、上线幂等
uv run release-pipeline release --set-version 1.2.3

# 测试
uv run pytest -q
```

## 目录

- `release_pipeline/` 流水线实现
  - `versioning.py` / `vcs.py`：版本推导（优先级、冲突裁决、dirty/tree 摘要）
  - `cache.py` / `builder.py`：缓存键、损坏降级重建、并发构建矩阵
  - `targets.py` / `health.py` / `rollout.py`：原子部署、三态健康门、自动回滚
  - `pipeline.py` / `runtime.py` / `cli.py`：编排、能力门禁、本地/CI 统一入口
- `release.toml` 构建矩阵示例
- `tests/` 异常链路测试（重复执行 / 缓存失效 / 部分失败 / 回滚 / 并发 / 中断恢复）
- `docs/release.md` 版本与缓存规则、上线回滚策略、退出码与本地验证手册
