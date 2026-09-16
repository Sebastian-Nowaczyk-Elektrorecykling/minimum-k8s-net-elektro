import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cluster_config", ROOT / "scripts/config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.c = config.load()

    def reject(self, **overrides):
        c = copy.deepcopy(self.c)
        c.update(overrides)
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "cluster.json"
            p.write_text(json.dumps(c))
            with self.assertRaises(ValueError):
                config.load(p)

    def test_rejects_overlapping_networks(self):
        self.reject(pod_cidr="192.168.0.0/16")
        self.reject(service_cidr="10.42.0.0/16")

    def test_rejects_ip_collisions(self):
        self.reject(dns_ip=self.c["api_ip"])
        self.reject(lb_start=self.c["api_ip"])
        self.reject(lan_dns_service_ip=self.c["cluster_dns_ip"])
        self.reject(lan_dns_service_ip="10.43.0.1")

    def test_rejects_dns_loop(self):
        self.reject(upstream_dns=[self.c["api_ip"]])
        self.reject(upstream_dns=[self.c["cluster_dns_ip"]])
        self.reject(upstream_dns=["127.0.0.1"])

    def test_rejects_network_broadcast_and_outside(self):
        self.reject(api_ip="192.168.15.255")
        self.reject(api_ip="192.168.16.1")
        self.reject(api_ip="192.168.0.0")

    def test_rejects_separate_lan_endpoints_and_monitoring_stack(self):
        self.reject(dns_ip="192.168.2.154")
        self.reject(gateway_ip="192.168.2.155")
        self.reject(monitoring_version="91.4.1")

    def test_roles_and_first_server_are_distinct(self):
        worker = config.node_config(self.c, "worker", "192.168.2.170", "eno1")
        self.assertIn("token-file", worker)
        self.assertNotIn("cluster-init", worker)
        self.assertNotIn("disable", worker)
        controller = config.node_config(self.c, "controller", "192.168.2.171", "eno1")
        self.assertIn("node-taint", controller)
        hybrid = config.node_config(self.c, "hybrid", "192.168.2.172", "eno1")
        self.assertNotIn("node-taint", hybrid)
        bootstrap = config.node_config(self.c, "hybrid", self.c["api_ip"], "eno1", True)
        self.assertTrue(bootstrap["cluster-init"])
        self.assertNotIn("server", bootstrap)
        self.assertNotIn("token-file", bootstrap)
        self.assertIn("elektro.internal/edge=true", bootstrap["node-label"])
        self.assertNotIn("elektro.internal/edge=true", hybrid["node-label"])

    def test_bootstrap_cannot_target_worker_or_other_ip(self):
        with self.assertRaises(ValueError):
            config.node_config(self.c, "worker", self.c["api_ip"], "eno1", True)
        with self.assertRaises(ValueError):
            config.node_config(self.c, "hybrid", "192.168.2.170", "eno1", True)

    def test_joining_node_cannot_take_edge_address(self):
        with self.assertRaises(ValueError):
            config.node_config(self.c, "worker", self.c["api_ip"], "eno1")

    def test_cilium_and_k3s_networks_agree(self):
        cni = config.cilium_values(self.c)
        node = config.node_config(self.c, "hybrid", self.c["api_ip"], "eno1", True)
        self.assertEqual(cni["ipam"]["mode"], "kubernetes")
        self.assertEqual(cni["k8sServiceHost"], self.c["api_ip"])
        self.assertTrue(node["disable-kube-proxy"])
        self.assertEqual(node["flannel-backend"], "none")
        self.assertTrue(cni["gatewayAPI"]["hostNetwork"]["enabled"])
        self.assertEqual(cni["gatewayAPI"]["hostNetwork"]["nodes"]["matchLabels"], {"elektro.internal/edge": "true"})
        self.assertIn("NET_BIND_SERVICE", cni["envoy"]["securityContext"]["capabilities"]["envoy"])
        self.assertTrue(cni["envoy"]["securityContext"]["capabilities"]["keepCapNetBindService"])
        self.assertFalse(cni["l2announcements"]["enabled"])
        self.assertNotIn("serviceMonitor", cni["prometheus"])

    def test_change_restarts_configuration_consumers(self):
        def revision(c):
            return config.outputs(c)["clusters/lan/cluster-settings.yaml"]["data"]["CONFIG_REVISION"]
        c = copy.deepcopy(self.c)
        c["upstream_dns"] = ["8.8.8.8"]
        self.assertNotEqual(revision(c), revision(self.c))


if __name__ == "__main__":
    unittest.main()
