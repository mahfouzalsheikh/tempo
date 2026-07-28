import json
import sys


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        send({"id": message["id"], "result": {"thread": {"id": "thread-test"}}})
    elif method == "turn/start":
        send({"id": message["id"], "result": {"turn": {"id": "turn-test"}}})
        send(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "totalTokens": 15,
                    }
                },
            }
        )
        send(
            {
                "id": 99,
                "method": "item/tool/call",
                "params": {
                    "tool": "tempo_complete",
                    "arguments": {"reason": "The fixture requires no code changes."},
                },
            }
        )
    elif message.get("id") == 99:
        send(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-test", "status": "completed"}},
            }
        )
