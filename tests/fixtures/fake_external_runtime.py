import json
import sys


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "session/start":
        send({"id": message["id"], "result": {"session_id": "external-test"}})
    elif method == "turn/run":
        send(
            {
                "event": "assistantMessage/completed",
                "payload": {"message": "External specialist finished its assignment."},
            }
        )
        send({"id": message["id"], "result": {"status": "completed"}})
    elif method == "session/stop":
        send({"id": message["id"], "result": {"status": "stopped"}})
        break
