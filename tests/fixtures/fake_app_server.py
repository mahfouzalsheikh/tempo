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
    elif method == "thread/compact/start":
        send({"id": message["id"], "result": {}})
        send(
            {
                "method": "item/started",
                "params": {"item": {"id": "compact-test", "type": "contextCompaction"}},
            }
        )
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
                "method": "item/completed",
                "params": {"item": {"id": "compact-test", "type": "contextCompaction"}},
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "compact-turn", "status": "completed"}},
            }
        )
    elif method == "turn/start":
        send({"id": message["id"], "result": {"turn": {"id": "turn-test"}}})
        budget_resume = "--budget-resume" in sys.argv
        send(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "tokenUsage": {
                        "inputTokens": (
                            17 if resumed and budget_resume else 110 if resumed else 10
                        ),
                        "outputTokens": (
                            8 if resumed and budget_resume else 55 if resumed else 5
                        ),
                        "totalTokens": (
                            25 if resumed and budget_resume else 165 if resumed else 15
                        ),
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
