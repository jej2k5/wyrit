import json
import unittest

from src.wyrit.capabilities import Capability, CapabilityRegistry


def capability(id, description="Find customer records"):
    return Capability(id, description, {"type": "object"}, True)


class CapabilityTests(unittest.TestCase):
    def test_ids_are_explicit_and_stable(self):
        self.assertEqual(capability("customers.search", "New description").id,
                         capability("customers.search").id)
        for invalid in ("", "Customers.Search", "customers..search", " customers.search"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                capability(invalid)

    def test_duplicate_rejection_preserves_original(self):
        original = capability("customers.search")
        registry = CapabilityRegistry([original])
        for duplicate in (original, capability(original.id, "Changed description")):
            with self.assertRaises(ValueError):
                registry.register(duplicate)
        self.assertIs(registry.resolve(original.id), original)

    def test_serialization_is_order_independent(self):
        first = Capability("a", "Lookup", {"type": "object", "required": []}, True)
        equivalent = Capability("a", "Lookup", {"required": [], "type": "object"}, True)
        second = capability("b")
        left = CapabilityRegistry([first, second])
        right = CapabilityRegistry([second, equivalent])
        self.assertEqual(left.to_json(), right.to_json())
        self.assertEqual([item["id"] for item in json.loads(left.to_json())], ["a", "b"])

    def test_schema_and_metadata_are_snapshotted(self):
        schema = {"properties": {"name": {"type": "string"}}}
        metadata = {"binding": {"operation": "lookup"}}
        item = Capability("customers.search", "Lookup", schema, False, metadata)
        schema["properties"].clear()
        metadata["binding"].clear()
        item.input_schema["properties"].clear()
        item.adapter_metadata["binding"].clear()
        described = CapabilityRegistry([item]).describe(item.id)
        self.assertEqual(described["input_schema"]["properties"], {"name": {"type": "string"}})
        self.assertIs(described["output_schema"], False)
        self.assertNotIn("adapter_metadata", described)
        self.assertEqual(item.adapter_metadata, {"binding": {"operation": "lookup"}})

    def test_schema_rejects_non_json_values(self):
        for invalid in ([], None, {"properties": {1: True}}, {"default": (1,)}):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                Capability("lookup", "Lookup", invalid, True)
        with self.assertRaises(ValueError):
            Capability("lookup", "Lookup", {"default": float("nan")}, True)

    def test_search_ranks_and_pages_summaries(self):
        items = [
            capability("a.search"),
            capability("customers.search", "Lookup"),
            capability("b.search"),
            capability("c.lookup", "Search the customer records"),
            capability("d.archive", "Archive invoices"),
        ]
        registry = CapabilityRegistry(items)
        hits = registry.search("CUSTOMERS.SEARCH")
        self.assertEqual([hit["id"] for hit in hits],
                         ["customers.search", "a.search", "b.search", "c.lookup"])
        self.assertEqual(hits, CapabilityRegistry(reversed(items)).search("customers.search"))
        self.assertEqual(registry.search("customers.search", limit=2, offset=1), hits[1:3])
        self.assertEqual(hits[0], {
            "id": "customers.search", "description": "Lookup", "classification": "write",
        })
        self.assertEqual(registry.search(""), [])
        self.assertEqual(registry.search("unmatched"), [])
        self.assertEqual(registry.search("customer records")[0]["id"], "a.search")
        with self.assertRaises(ValueError):
            registry.search("search", limit=0)
        for lookup in (registry.resolve, registry.describe):
            with self.assertRaises(KeyError):
                lookup("unknown")

    def test_authorization_is_explicit_run_bound_and_immutable(self):
        registry = CapabilityRegistry([capability("lookup"), capability("write")])
        ids = {"lookup"}
        grants = registry.authorization_for("run-a", ids)
        ids.add("write")
        self.assertTrue(grants.allows("run-a", "lookup"))
        self.assertFalse(grants.allows("run-a", "write"))
        self.assertFalse(grants.allows("run-b", "lookup"))
        self.assertFalse(registry.authorization_for("run-a", []).allows("run-a", "lookup"))
        self.assertEqual(
            registry.authorization_for("run-a", ["write", "lookup"]).to_dict(),
            {"run_id": "run-a", "capability_ids": ["lookup", "write"]},
        )
        with self.assertRaises(KeyError):
            registry.authorization_for("run-a", ["missing"])
