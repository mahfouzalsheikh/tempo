"""Read graph-wide usage without changing individual runtime counters."""


def run_usage(entry, session=None):
    sessions = (
        list(entry.node_sessions.values())
        if entry.node_sessions and not entry.node_sessions_aggregated
        else [session or entry.session]
    )
    return {
        name: sum(getattr(item, f"codex_{name}") for item in sessions)
        for name in ("input_tokens", "output_tokens", "total_tokens")
    }
