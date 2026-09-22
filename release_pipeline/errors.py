"""Exception hierarchy for the release pipeline."""


class PipelineError(Exception):
    """Base class for all pipeline errors.

    Every raised pipeline error MUST carry a machine readable ``code`` so
    that failure causes remain distinguishable in logs and exit statuses.
    """

    code = "pipeline_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class VersionConflictError(PipelineError):
    """Two or more version sources disagree for the same input."""

    code = "version_conflict"


class VersionSourceError(PipelineError):
    """A version source is unreadable or missing in an irreconcilable way."""

    code = "version_source_error"


class CacheCorruptedError(PipelineError):
    """A cached entry fails integrity validation and must be rebuilt."""

    code = "cache_corrupted"


class BuildFailedError(PipelineError):
    """One or more build targets failed."""

    code = "build_failed"


class HealthCheckError(PipelineError):
    """Target health is not good enough to advance the rollout."""

    code = "health_check_failed"


class HealthInconclusiveError(PipelineError):
    """Health cannot be determined; advancing is forbidden."""

    code = "health_inconclusive"


class RollbackError(PipelineError):
    """Rollback could not restore the pre-deploy state."""

    code = "rollback_failed"


class ParityError(PipelineError):
    """Local and CI execution paths would produce different conclusions."""

    code = "parity_violation"


class StateError(PipelineError):
    """Persistent release state is missing, locked or inconsistent."""

    code = "state_error"
