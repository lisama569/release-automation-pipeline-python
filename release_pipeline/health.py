"""本地健康检查：以明确的探针文件判定健康度。

探针读取目标 current 版本 payload 的清单文件 ``health.json``::

    {"status": "ok|fail", "attempts_required": 3, "inconsistent": false}

判定规则（三态，绝不把“不知道”当“健康”）：
- current 缺失 / payload 缺失 / 清单缺失或损坏 / 与期望版本不符 => ambiguous
- inconsistent=true 或多次探针结论互相矛盾                  => ambiguous
- status=fail                                              => unhealthy
- 连续 expect_attempts 次均 status=ok                      => healthy
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .models import HealthProbe
from .targets import LocalTargetEnvironment

logger = logging.getLogger(__name__)


def probe_target(
    env_root: Path,
    *,
    target: str,
    version: str,
    expect_attempts: int = 3,
    interval_s: float = 0.0,
) -> HealthProbe:
    """对模拟目标执行健康探针，返回健康/不健康/不明确三态结论。"""
    env = LocalTargetEnvironment(Path(env_root), target)
    current = env.current_version
    if current is None:
        return _result(target, ambiguous=True, detail="current 指针为空：对外无任何版本，结论不明确")
    if current != version:
        return _result(
            target,
            ambiguous=True,
            detail=f"对外版本 {current} 与期望版本 {version} 不一致，结论不明确",
        )

    payload_dir = env.versions_dir / version
    health_file = payload_dir / "health.json"
    if not health_file.is_file():
        return _result(target, ambiguous=True, detail=f"缺少健康清单 {health_file}，结论不明确")

    try:
        manifest = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(target, ambiguous=True, detail=f"健康清单损坏无法解析: {exc}")
    if not isinstance(manifest, dict):
        return _result(target, ambiguous=True, detail="健康清单结构非对象，结论不明确")

    if manifest.get("inconsistent") is True:
        return _result(target, ambiguous=True, detail="清单声明探针结果会互相矛盾（inconsistent）")

    status = manifest.get("status")
    if status not in ("ok", "fail"):
        return _result(target, ambiguous=True, detail=f"清单 status={status!r} 无法识别")

    required = int(manifest.get("attempts_required", expect_attempts))
    attempts = max(1, min(required, expect_attempts))
    observations: list[str] = []
    for i in range(attempts):
        try:
            manifest = json.loads(health_file.read_text(encoding="utf-8"))
            s = manifest.get("status")
        except (OSError, json.JSONDecodeError) as exc:
            return _result(target, ambiguous=True, detail=f"第 {i + 1} 次探针读取失败: {exc}")
        observations.append(str(s))
        if interval_s:
            time.sleep(interval_s)

    logger.info(
        "health probe target=%s version=%s 输入status=%s 观测=%s 期望次数=%s",
        target,
        version,
        status,
        observations,
        attempts,
    )
    if len(set(observations)) > 1:
        return _result(
            target,
            ambiguous=True,
            detail=f"多次探针结论矛盾 {observations}，结论不明确，禁止推进",
        )
    if observations[0] == "fail":
        return _result(
            target,
            healthy=False,
            ambiguous=False,
            detail=f"连续 {attempts} 次探针明确失败",
        )
    return _result(
        target,
        healthy=True,
        ambiguous=False,
        detail=f"连续 {attempts} 次探针均为 ok",
    )


def _result(
    target: str, *, healthy: bool = False, ambiguous: bool = False, detail: str
) -> HealthProbe:
    probe = HealthProbe(
        target=target, healthy=healthy, ambiguous=ambiguous, detail=detail
    )
    logger.info(
        "health probe target=%s 判定=%s 依据=%s",
        target,
        "ambiguous" if ambiguous else ("healthy" if healthy else "unhealthy"),
        detail,
    )
    return probe
