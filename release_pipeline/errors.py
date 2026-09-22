"""流水线统一异常类型。

所有异常都携带可解释的 reason，便于本地与 CI 输出一致的判定依据。
"""


class PipelineError(Exception):
    """所有流水线错误的基类。"""

    def __init__(self, reason: str, *, details: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class VersionError(PipelineError):
    """版本推导失败（例如来源不可用且无法降级）。"""


class CacheError(PipelineError):
    """缓存读取/写入失败。"""


class BuildFailure(PipelineError):
    """构建目标失败（单个目标的失败信息载体）。"""


class HealthGateError(PipelineError):
    """健康度结论不明确或不健康，阻止推进。"""


class RolloutError(PipelineError):
    """上线/回滚失败，或回滚后状态校验不一致。"""


class EnvironmentRejected(PipelineError):
    """当前环境不支持被请求的能力或参数，必须显式拒绝。"""
