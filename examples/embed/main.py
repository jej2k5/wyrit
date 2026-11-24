"""Run with python examples/embed/main.py after installing Wyrit."""

import asyncio
import json

from wyrit.embed import Capability, CapabilityError, Host, RunContext


SCHEMA = {"type": "object", "maxProperties": 0}
ORDERS = Capability("sql.orders", "Read visible orders", SCHEMA, classification="read")
TOTAL = Capability("sql.total", "Read the tenant's order total", SCHEMA, classification="read")
ROWS = {"north": [12, 23], "south": [8]}
PERMISSIONS = {
    ("north", "alice"): {ORDERS.id, TOTAL.id},
    ("south", "bob"): {ORDERS.id},
}


async def dispatch(capability_id, arguments, context):
    if capability_id not in PERMISSIONS.get((context.tenant, context.principal), ()):
        raise CapabilityError("DENIED", "Tenant policy requires reporting access")
    rows = ROWS[context.tenant]
    return list(rows) if capability_id == ORDERS.id else sum(rows)


async def main():
    host = Host([ORDERS, TOTAL], dispatch)
    source = """
        const orders = await tools.call("sql.orders", {});
        try {
            return {orders, total: await tools.call("sql.total", {})};
        } catch (error) {
            return {orders, totalError: {code: error.code, reason: error.reason}};
        }
    """
    users = [RunContext("example-a", "alice", "north"), RunContext("example-b", "bob", "south")]
    results = await asyncio.gather(*(host.execute(source, user) for user in users))
    for user, result in zip(users, results):
        if result["status"] != "SUCCESS":
            raise RuntimeError(result["reason"])
        print(user.principal, json.dumps(result["value"]))


if __name__ == "__main__":
    asyncio.run(main())
