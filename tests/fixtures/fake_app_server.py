import json
import sys

resumed = False


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/resume":
        if "--reject-resume" in sys.argv:
            send({"id": message["id"], "error": {"message": "thread unavailable"}})
        else:
            resumed = True
            send(
                {
                    "id": message["id"],
                    "result": {"thread": {"id": message["params"]["threadId"]}},
                }
            )
    elif method == "thread/start":
        send({"id": message["id"], "result": {"thread": {"id": "thread-test"}}})
    elif method == "turn/start":
        send({"id": message["id"], "result": {"turn": {"id": "turn-test"}}})
        send(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "inputTokens": 110 if resumed else 10,
                        "outputTokens": 55 if resumed else 5,
                        "totalTokens": 165 if resumed else 15,
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
