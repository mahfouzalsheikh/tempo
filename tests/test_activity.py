from tempo.activity import activity_from_event, append_activity


def test_agent_message_deltas_are_visible_and_coalesced():
    items = []
    first = activity_from_event(
        {"event": "item/agentMessage/delta", "payload": {"delta": "Working "}},
        "2026-01-01T00:00:00Z",
    )
    second = activity_from_event(
        {"event": "item/agentMessage/delta", "payload": {"delta": "on tests."}},
        "2026-01-01T00:00:01Z",
    )
    append_activity(items, first)
    append_activity(items, second)
    assert len(items) == 1
    assert items[0]["title"] == "Agent update"
    assert items[0]["text"] == "Working on tests."


def test_private_reasoning_text_is_not_exposed():
    activity = activity_from_event(
        {
            "event": "item/reasoning/summaryTextDelta",
            "payload": {"delta": "private internal reasoning"},
        },
        "2026-01-01T00:00:00Z",
    )
    assert activity["title"] == "Agent is reasoning"
    assert activity["text"] == ""


def test_command_output_is_shown_as_agent_activity():
    activity = activity_from_event(
        {
            "event": "item/commandExecution/outputDelta",
            "payload": {"command": "pytest -q", "delta": "12 passed"},
        },
        "2026-01-01T00:00:00Z",
    )
    assert activity["kind"] == "command"
    assert "pytest -q" in activity["text"]
    assert "12 passed" in activity["text"]


def test_validation_output_delta_is_visible():
    activity = activity_from_event(
        {
            "event": "validation_command_output_delta",
            "name": "Integration tests",
            "delta": "container ready\n",
        },
        "2026-01-01T00:00:00Z",
    )
    assert activity["kind"] == "command"
    assert activity["coalesce"] is True
    assert activity["text"] == "container ready\n"
