import json


def handler(event, context):
    if event.get("loud"):
        return {"statusCode": 200, "body": json.dumps({"built_by": "UV"})}
    return {"statusCode": 200, "body": json.dumps({"built_by": "uv"})}
