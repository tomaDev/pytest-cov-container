import json


def test_sync_positive(invoke):
    response = invoke("SyncFunction", {"n": 1})
    assert json.loads(response["body"]) == {"sign": "POSITIVE", "greeting": "hi"}


def test_sync_non_positive(invoke):
    response = invoke("SyncFunction", {"n": -1})
    assert json.loads(response["body"]) == {"sign": "NON-POSITIVE", "greeting": "hi"}



def test_uv_built(invoke):
    response = invoke("UvFunction", {"loud": True})
    assert json.loads(response["body"]) == {"built_by": "UV", "greeting": "HI"}
