import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("storage", ROOT / "scripts/check-local-volumes.py")
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class LocalVolumeRemovalTests(unittest.TestCase):
    node = {"metadata": {"name": "node-1", "labels": {"kubernetes.io/hostname": "custom-hostname", "rack": "a"}}}

    def volumes(self, *terms, unscoped=False):
        spec = {"hostPath": {"path": "/data"}}
        if not unscoped:
            spec["nodeAffinity"] = {"required": {"nodeSelectorTerms": list(terms)}}
        return [{"metadata": {"name": "data"}, "spec": spec}]

    def test_custom_hostname_and_nonhostname_affinity_block_removal(self):
        for key, value in [("kubernetes.io/hostname", "custom-hostname"), ("rack", "a")]:
            terms = {"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]}
            self.assertEqual(storage.blocking_volumes(self.node, self.volumes(terms)), ["data"])

    def test_unscoped_hostpath_blocks_removal(self):
        self.assertEqual(storage.blocking_volumes(self.node, self.volumes(unscoped=True)), ["data"])

    def test_notin_and_or_semantics(self):
        excluded = {"matchExpressions": [{"key": "rack", "operator": "NotIn", "values": ["a"]}]}
        included = {"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["node-1"]}]}
        self.assertEqual(storage.blocking_volumes(self.node, self.volumes(excluded)), [])
        self.assertEqual(storage.blocking_volumes(self.node, self.volumes(excluded, included)), ["data"])

    def test_empty_affinity_matches_no_node(self):
        self.assertEqual(storage.blocking_volumes(self.node, self.volumes({})), [])

    def test_unknown_selector_fails_closed(self):
        terms = {"matchExpressions": [{"key": "rack", "operator": "Typo", "values": ["a"]}]}
        with self.assertRaises(ValueError):
            storage.blocking_volumes(self.node, self.volumes(terms))
