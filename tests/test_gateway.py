"""Exercise failure states, including stale or misleading positive conditions."""
import copy
import importlib.util
from pathlib import Path
import unittest

import celpy
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_check", ROOT / "scripts/check-gateway.py")
gateway_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_check)
spec = importlib.util.spec_from_file_location("validate", ROOT / "scripts/validate.py")
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


def conditions(*names, generation=2):
    return [{"type": name, "status": "True", "observedGeneration": generation} for name in names]


def route(name, namespace, section):
    parent = {"name": "internal", "namespace": "gateway-system", "sectionName": section}
    return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": namespace, "generation": 2},
            "spec": {"parentRefs": [parent]}, "status": {"parents": [{
                "parentRef": copy.deepcopy(parent), "controllerName": "io.cilium/gateway-controller",
                "conditions": conditions("Accepted", "ResolvedRefs")}]}}


def snapshot():
    return {
        "ingress": {"items": []},
        "cilium": {"data": {"enable-gateway-api": "true", "gateway-api-hostnetwork-enabled": "true",
                            "gateway-api-hostnetwork-nodelabelselector": "elektro.internal/edge=true"}},
        "class": {"metadata": {"name": "cilium", "generation": 2},
                  "status": {"conditions": conditions("Accepted")}},
        "gateway": {"metadata": {"name": "internal", "namespace": "gateway-system", "generation": 2},
                    "spec": {"listeners": [{"name": n} for n in ("http", "apps-https", "admin-https")]},
                    "status": {"conditions": conditions("Accepted", "Programmed"), "listeners": [
                        {"name": n, "conditions": conditions("Accepted", "Programmed", "ResolvedRefs")}
                        for n in ("http", "apps-https", "admin-https")]}},
        "routes": {"items": [route("redirect-https", "gateway-system", "http"),
                             route("administration", "administration", "admin-https")]},
        "edge": {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "192.168.2.153"}]}}]},
    }


class GatewayChecksTests(unittest.TestCase):
    def test_current_gateway_and_routes_pass(self):
        self.assertEqual(gateway_check.check(snapshot(), "192.168.2.153"), [])

    def test_partial_listener_and_stale_routes_fail(self):
        for change in ("listener", "stale", "wrong-parent", "missing-route", "duplicate-edge", "ingress"):
            data = snapshot()
            if change == "listener":
                data["gateway"]["status"]["listeners"][2]["conditions"][2]["status"] = "False"
            elif change == "stale":
                data["routes"]["items"][1]["metadata"]["generation"] = 3
            elif change == "wrong-parent":
                data["routes"]["items"][1]["status"]["parents"][0]["parentRef"]["sectionName"] = "apps-https"
            elif change == "missing-route":
                data["routes"]["items"].pop()
            elif change == "duplicate-edge":
                data["edge"]["items"] *= 2
            else:
                data["ingress"]["items"] = [{"kind": "Ingress", "metadata": {"name": "unsupported-ui", "namespace": "apps"}}]
            with self.subTest(change=change):
                self.assertTrue(gateway_check.check(data, "192.168.2.153"))

    def test_routing_validation_rejects_ingress_objects_and_annotations(self):
        for doc in [
            {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "ingress"}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "IngressClass", "metadata": {"name": "ingress"}},
            {"apiVersion": "gateway.networking.k8s.io/v1beta1", "kind": "Gateway", "metadata": {"name": "unsupported-api"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "app", "annotations": {"kubernetes.io/ingress.class": "nginx"}}},
            {"apiVersion": "cert-manager.io/v1", "kind": "Issuer", "metadata": {"name": "acme"},
             "spec": {"acme": {"solvers": [{"http01": {"ingress": {"class": "nginx"}}}]}}},
        ]:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                validate.validate_gateway_only([doc])
        # Network-policy ingress is traffic direction, not the Ingress API.
        validate.validate_gateway_only([{"apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
                                        "metadata": {"name": "dns"}, "spec": {"ingress": []}}])


class GatewayHealthExpressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        checks = yaml.safe_load((ROOT / "clusters/lan/gateway-health.yaml").read_text())[0]["value"]
        environment = celpy.Environment()
        cls.programs = {check["kind"]: environment.program(environment.compile(check["current"])) for check in checks}

    def healthy(self, kind, obj):
        try:
            return bool(self.programs[kind].evaluate({k: celpy.json_to_cel(v) for k, v in obj.items()}))
        except celpy.CELEvalError:
            # Flux treats unavailable top-level status as not yet healthy.
            return False

    def test_healthy_resources_pass_real_cel_expressions(self):
        data = snapshot()
        for kind, obj in [("GatewayClass", data["class"]), ("Gateway", data["gateway"]),
                          ("HTTPRoute", data["routes"]["items"][1])]:
            with self.subTest(kind=kind):
                self.assertTrue(self.healthy(kind, obj))

    def test_empty_missing_and_stale_status_never_pass(self):
        data = snapshot()
        for kind, original in [("GatewayClass", data["class"]), ("Gateway", data["gateway"]),
                               ("HTTPRoute", data["routes"]["items"][1])]:
            for change in ("empty", "missing", "stale"):
                obj = copy.deepcopy(original)
                if change == "missing":
                    del obj["status"]
                elif change == "empty":
                    obj["status"] = {"conditions": [], "listeners": [], "parents": []}
                else:
                    obj["metadata"]["generation"] += 1
                with self.subTest(kind=kind, change=change):
                    self.assertFalse(self.healthy(kind, obj))

    def test_one_failed_listener_or_backend_blocks_readiness(self):
        data = snapshot()
        data["gateway"]["status"]["listeners"][2]["conditions"][2]["status"] = "False"
        self.assertFalse(self.healthy("Gateway", data["gateway"]))
        route = data["routes"]["items"][1]
        route["status"]["parents"][0]["conditions"][1]["status"] = "False"
        self.assertFalse(self.healthy("HTTPRoute", route))

    def test_other_parent_cannot_satisfy_route_readiness(self):
        obj = snapshot()["routes"]["items"][1]
        obj["status"]["parents"][0]["parentRef"]["sectionName"] = "apps-https"
        self.assertFalse(self.healthy("HTTPRoute", obj))


if __name__ == "__main__":
    unittest.main()
