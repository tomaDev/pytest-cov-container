import json

from shared import util


def handler(event, context):
    params = event.get("queryStringParameters") or event
    if int(params.get("n", 0)) > 0:
        sign = util.label("positive")
    else:
        sign = util.label("non-positive")
    return {"statusCode": 200, "body": json.dumps({"sign": sign})}
