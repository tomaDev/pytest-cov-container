import json


def test_sync_positive(invoke):
    response = invoke("SyncFunction", {"n": 1})
    assert json.loads(response["body"]) == {"sign": "POSITIVE"}


def test_sync_non_positive(invoke):
    response = invoke("SyncFunction", {"n": -1})
    assert json.loads(response["body"]) == {"sign": "NON-POSITIVE"}

