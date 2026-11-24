"""Generate ticket fixtures and print their canonical SHA-256 digests."""

from random import Random


def generate():
    for name, seed, description_sentences, comments, comment_sentences in (
        ("fan-out", 1705, 5, 3, 2),
        ("fan-out-large", 1706, 24, 10, 3),
    ):
        random = Random(seed)

        def prose(sentences):
            return " ".join(
                f"The {random.choice(('support', 'platform', 'integration', 'operations'))} "
                f"team {random.choice(('reviewed', 'documented', 'investigated', 'discussed'))} "
                f"the {random.choice(('dashboard', 'workflow', 'notification', 'reporting'))} "
                f"behavior and {random.choice(('recorded', 'confirmed', 'requested'))} "
                f"the {random.choice(('remaining', 'recommended', 'necessary'))} follow-up actions."
                for _ in range(sentences)
            )

        tickets = [
            {
                "id": f"TKT-{index:03d}",
                "title": f"Review service behavior for request {index}",
                "priority": random.choice(("low", "medium", "high")),
                "status": random.choice(("open", "closed")),
                "hours_logged": random.randint(1, 24),
                "description": prose(description_sentences),
                "comments": [prose(comment_sentences) for _ in range(comments)],
            }
            for index in range(1, 31)
        ]
        ids = [ticket["id"] for ticket in tickets]
        matches = [t for t in tickets if t["priority"] == "high" and t["status"] == "open"]
        yield {
            "id": name,
            "kind": "fixture",
            "prompt": (
                "List the ticket IDs and fetch details for every listed ticket. "
                "Return the count and sum of hours_logged for tickets that are high priority and open "
                "as a JSON object with keys count and hours_logged."
            ),
            "tools": {
                "list-tickets": {
                    "description": "List the 30 ticket IDs",
                    "cases": [{"arguments": {}, "result": {"ids": ids}}],
                },
                "get-ticket": {
                    "description": "Fetch a ticket's details by ID",
                    "input_schema": {
                        "type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"], "additionalProperties": False,
                    },
                    "cases": [{"arguments": {"id": t["id"]}, "result": t} for t in tickets],
                },
            },
            "required_requests": [
                {"tool": "list-tickets", "arguments": {}},
                *({"tool": "get-ticket", "arguments": {"id": ticket_id}} for ticket_id in ids),
            ],
            "expected": {"count": len(matches), "hours_logged": sum(t["hours_logged"] for t in matches)},
            "recipe": {
                "name": "summarize-open-high-priority-tickets",
                "description": "Count open high priority tickets and sum their logged hours across the full ticket list.",
                "input_schema": {"type": "object", "additionalProperties": False},
                "source": """const {ids} = await tools.call("bench.list-tickets", {});
let count = 0;
let hours_logged = 0;
for (const id of ids) {
    const ticket = await tools.call("bench.get-ticket", {id});
    if (ticket.priority === "high" && ticket.status === "open") {
        count++;
        hours_logged += ticket.hours_logged;
    }
}
return {count, hours_logged};""",
            },
        }


if __name__ == "__main__":
    import json
    from .tasks import digest

    print(json.dumps({task["id"]: digest(task) for task in generate()}, indent=2))
