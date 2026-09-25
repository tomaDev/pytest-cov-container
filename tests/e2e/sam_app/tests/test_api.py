import warnings

from pytest_cov_container import collect_container_coverage


def test_flush_pushes_each_warm_python_container(api):
    assert api("/sync?n=1") == {"sign": "POSITIVE"}
    assert api("/node") == {"node": True}
    # Any plugin warning (a failed signal, a missed push) fails the test. The
    # Node container has no coverage process and must be passed over quietly.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        assert collect_container_coverage() == 1
