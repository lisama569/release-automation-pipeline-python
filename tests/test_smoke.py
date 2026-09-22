"""骨架冒烟测试：确认包可导入、模型可构造。"""


def test_import_package():
    import release_pipeline
    from release_pipeline import models  # noqa: F401

    assert release_pipeline.__version__


def test_models_constructible():
    from release_pipeline.models import (
        CacheStatus,
        MatrixConclusion,
        MatrixReport,
        TargetStatus,
    )

    report = MatrixReport(
        results=(),
        conclusion=MatrixConclusion.EMPTY,
        successful=(),
        failed=(),
        skipped=(),
    )
    assert report.conclusion is MatrixConclusion.EMPTY
    assert TargetStatus.SUCCESS.value == "success"
    assert CacheStatus.HIT.value == "hit"
