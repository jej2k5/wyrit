"""Serve one benchmark fixture over MCP stdio."""
import argparse
import json
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

if __package__:
    from .tasks import canonical, downstream_server
else:
    from tasks import canonical, downstream_server


async def serve(args):
    def record(request, outcome):
        with args.log.open("a", encoding="utf-8") as stream:
            stream.write(canonical({"request": request, "outcome": outcome}) + "\n")

    task = json.loads(args.fixture.read_text(encoding="utf-8"))
    server = downstream_server(task, record, deny_writes_at_entry=args.direct)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("log", type=Path)
    parser.add_argument("--direct", action="store_true")
    anyio.run(serve, parser.parse_args())
