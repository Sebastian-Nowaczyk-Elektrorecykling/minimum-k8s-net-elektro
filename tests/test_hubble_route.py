"""Exercise temporary routing lifecycle without a cluster or host changes."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("hubble_route", ROOT / "scripts/hubble-route.py")
hubble = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hubble)


def conditions(*names):
    return [{"type": name, "status": "True", "observedGeneration": 1} for name in names]


def ready_route(obj):
    obj = copy.deepcopy(obj)
    obj["metadata"]["generation"] = 1
    obj["status"] = {"parents": [{
        "controllerName": "io.cilium/gateway-controller",
        "parentRef": dict(hubble.PARENT, group="gateway.networking.k8s.io", kind="Gateway"),
        "conditions": conditions("Accepted", "ResolvedRefs"),
    }]}
    return obj


class HubbleRouteTests(unittest.TestCase):
    def setUp(self):
        self.c = hubble.config.load()
        self.objects = {}
        self.writes = []
        self.routes = []
        self.ready = True
        self.gateway = {"metadata": {"generation": 1}, "status": {
            "conditions": conditions("Accepted", "Programmed"), "listeners": [
                {"name": "admin-https", "conditions": conditions("Accepted", "Programmed", "ResolvedRefs")}]}}
        self.namespace = {"metadata": {"labels": {"elektro.internal/route-scope": "administration"}}}
        self.enterContext(patch.object(hubble, "kubectl", side_effect=self.kubectl))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def kubectl(self, *args, body=None):
        args = list(args)
        namespace = None
        if args[0] == "-n":
            namespace, args = args[1], args[2:]
        action = args[0]
        if action == "get":
            kind, name = args[1:3]
            if name == "-A":
                return json.dumps({"items": self.routes + [obj for (k, _, _), obj in self.objects.items() if k == "httproute"]})
            if kind == "gateway":
                return json.dumps(self.gateway)
            if kind == "namespace":
                return json.dumps(self.namespace)
            if kind == "service":
                return json.dumps({"spec": {"ports": [{"port": 80}]}})
            obj = self.objects.get((kind.lower(), namespace, name))
            if obj is None:
                if "--ignore-not-found=true" in args:
                    return ""
                raise subprocess.CalledProcessError(1, args)
            return json.dumps(obj)
        self.writes.append((action, namespace, args, copy.deepcopy(body)))
        if action == "apply":
            self.assertIn("--server-side", args)
            self.assertNotIn("--force-conflicts", args)
            self.assertIn(f"--field-manager={hubble.MANAGER}", args)
            obj = ready_route(body) if body["kind"] == "HTTPRoute" else copy.deepcopy(body)
            if not self.ready:
                obj.pop("status", None)
            key = (obj["kind"].lower(), obj["metadata"]["namespace"], obj["metadata"]["name"])
            self.objects[key] = obj
        elif action == "delete":
            self.assertIn("--ignore-not-found=true", args)
            self.objects.pop((args[1].lower(), namespace, args[2]), None)
        else:
            raise AssertionError(args)
        return ""

    def test_add_remove_and_repeated_calls_only_touch_the_two_test_objects(self):
        hubble.add(self.c, 10)
        hubble.add(self.c, 10)
        self.assertEqual(len(self.objects), 2)
        route = self.objects[("httproute", "administration", "hubble-test")]
        self.assertEqual(route["spec"]["hostnames"], ["hubble.admin.internal"])
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"], [
            {"name": "hubble-ui", "namespace": "kube-system", "port": 80}])
        self.assertEqual([w[3]["kind"] for w in self.writes], ["ReferenceGrant", "HTTPRoute"] * 2)
        hubble.remove()
        hubble.remove()
        self.assertEqual(self.objects, {})
        self.assertEqual([(w[1], w[2][1:3]) for w in self.writes if w[0] == "delete"], [
            ("administration", ["HTTPRoute", "hubble-test"]), ("kube-system", ["ReferenceGrant", "hubble-test"])])

    def test_render_honors_custom_domain_without_k3s_or_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "cluster.json"
            config.write_text(json.dumps(dict(self.c, domain="example.test", api_ip="192.168.2.154")))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/hubble-route.py"), "render", "--config", str(config)],
                                    text=True, capture_output=True, check=True)
        objects = json.loads(result.stdout)["items"]
        self.assertEqual(objects[1]["spec"]["hostnames"], ["hubble.admin.example.test"])
        self.assertEqual(objects[0]["spec"]["to"], [{"group": "", "kind": "Service", "name": "hubble-ui"}])
        self.assertEqual(self.writes, [])

    def test_unowned_or_gitops_managed_identity_is_never_changed(self):
        for action in (lambda: hubble.add(self.c, 10), hubble.remove):
            for owner in ("unowned", "flux", "helm"):
                grant, route = hubble.manifests("internal")
                if owner == "unowned":
                    route["metadata"]["labels"] = {}
                elif owner == "flux":
                    route["metadata"]["labels"]["kustomize.toolkit.fluxcd.io/name"] = "another-repository"
                else:
                    route["metadata"]["annotations"] = {"meta.helm.sh/release-name": "another-release"}
                self.objects = {("referencegrant", "kube-system", "hubble-test"): grant,
                                ("httproute", "administration", "hubble-test"): route}
                with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, "not owned exclusively"):
                    action()
                self.assertEqual(self.writes, [])

    def test_removal_handles_a_partial_apply_without_loading_config_or_gateway(self):
        grant = hubble.manifests("internal")[0]
        self.objects[("referencegrant", "kube-system", "hubble-test")] = grant
        with patch.object(hubble.config, "load", side_effect=AssertionError("must not load config")), \
                patch.object(hubble, "check_prerequisites", side_effect=AssertionError("must not check gateway")), \
                patch.object(sys, "argv", ["hubble-route.py", "remove", "--config", "/missing.json"]):
            hubble.main()
        self.assertEqual(self.objects, {})

    def test_api_read_error_is_not_treated_as_missing_and_prevents_all_writes(self):
        grant = hubble.manifests("internal")[0]
        with patch.object(hubble, "get", side_effect=[grant, subprocess.CalledProcessError(1, ["kubectl"])]):
            with self.assertRaises(subprocess.CalledProcessError):
                hubble.remove()
        self.assertEqual(self.writes, [])

    def test_competing_exact_wildcard_or_all_host_route_blocks_add(self):
        for hostnames in (["hubble.admin.internal"], ["*.admin.internal"], []):
            other = hubble.manifests("internal")[1]
            other["metadata"]["name"] = "other-owner"
            other["spec"]["hostnames"] = hostnames
            self.routes = [other]
            with self.subTest(hostnames=hostnames), self.assertRaisesRegex(ValueError, "already claims"):
                hubble.add(self.c, 10)
            self.assertEqual(self.writes, [])

    def test_shared_http_redirect_does_not_conflict(self):
        other = hubble.manifests("internal")[1]
        other["metadata"].update(name="redirect-https", namespace="gateway-system")
        other["spec"]["parentRefs"][0]["sectionName"] = "http"
        other["spec"]["hostnames"] = ["*.internal"]
        self.routes = [other]
        hubble.add(self.c, 10)
        self.assertEqual(len(self.writes), 2)

    def test_gateway_or_namespace_failure_prevents_exposure(self):
        self.gateway["metadata"]["generation"] = 2
        with self.assertRaisesRegex(ValueError, "not ready"):
            hubble.add(self.c, 10)
        self.gateway["metadata"]["generation"] = 1
        self.namespace["metadata"]["labels"] = {}
        with self.assertRaisesRegex(ValueError, "route-scope"):
            hubble.add(self.c, 10)
        self.assertEqual(self.writes, [])

    def test_route_status_requires_current_generation_and_correct_parent(self):
        original = ready_route(hubble.manifests("internal")[1])
        self.assertTrue(hubble.route_ready(original))
        for case in ("generation", "parent", "controller", "missing", "backend"):
            route = copy.deepcopy(original)
            if case == "generation":
                route["metadata"]["generation"] += 1
            elif case == "parent":
                route["status"]["parents"][0]["parentRef"]["sectionName"] = "apps-https"
            elif case == "controller":
                route["status"]["parents"][0]["controllerName"] = "another-controller"
            elif case == "missing":
                route["status"] = {}
            else:
                route["status"]["parents"][0]["conditions"][1]["status"] = "False"
            with self.subTest(case=case):
                self.assertFalse(hubble.route_ready(route))

    def test_wait_timeout_reports_failure_and_remove_cleans_up(self):
        self.ready = False
        with patch.object(hubble.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(ValueError, "Timed out"):
                hubble.add(self.c, 10)
        self.assertEqual(len(self.objects), 2)
        hubble.remove()
        self.assertEqual(self.objects, {})

if __name__ == "__main__":
    unittest.main()
