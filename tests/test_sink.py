import threading
import time
import urllib.error
import urllib.request

import pytest

from pytest_cov_container import protocol
from pytest_cov_container.sink import CoverageSink


@pytest.fixture
def sink():
    pushes: list[tuple[str, str, bytes]] = []

    def on_push(ident, function, body):
        if function == "Boom":
            msg = "unmappable"
            raise RuntimeError(msg)
        pushes.append((ident, function, body))

    server = CoverageSink(bind="127.0.0.1", advertise_host="127.0.0.1", on_push=on_push)
    server.pushes = pushes
    server.start()
    yield server
    server.stop()


def _post(url: str, body: bytes = b"data", ident: str = "abc123def456-7", function: str = "Fn") -> int:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={protocol.ID_HEADER: ident, protocol.FUNCTION_HEADER: function}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_accepts_a_push(sink):
    assert _post(sink.url) == 204
    assert sink.pushes == [("abc123def456-7", "Fn", b"data")]
    assert sink.received == 1


def test_url_carries_the_advertised_host_and_token(sink):
    assert sink.url == f"http://127.0.0.1:{sink.port}/{sink.token}"


def test_rejects_a_wrong_token(sink):
    assert _post(f"http://127.0.0.1:{sink.port}/guess") == 404
    assert sink.pushes == []


@pytest.mark.parametrize("ident", ["", "../../etc/passwd", "a b"])
def test_rejects_an_unsafe_id(sink, ident):
    assert _post(sink.url, ident=ident) == 400


def test_rejects_an_empty_body(sink):
    assert _post(sink.url, body=b"") == 400


def test_a_failing_handler_answers_500_and_records_nothing(sink):
    assert _post(sink.url, function="Boom") == 500
    assert sink.received == 0


def test_wait_for_a_containers_push(sink):
    since = time.monotonic()
    threading.Timer(0.2, _post, args=(sink.url,)).start()
    assert sink.wait_for("abc123def456ffff", since=since, timeout=5)


def test_wait_for_ignores_older_pushes_and_other_containers(sink):
    _post(sink.url)
    since = time.monotonic()
    _post(sink.url, ident="0000000000aa-1")
    assert not sink.wait_for("abc123def456ffff", since=since, timeout=0.3)
