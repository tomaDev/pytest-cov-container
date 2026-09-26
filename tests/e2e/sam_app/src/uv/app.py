import json

from common import greet


def handler(event, context):
    if event.get("loud"):
        return {"statusCode": 200, "body": json.dumps({"built_by": "UV", "greeting": greet.greet(True)})}
    return {"statusCode": 200, "body": json.dumps({"built_by": "uv", "greeting": greet.greet(False)})}
