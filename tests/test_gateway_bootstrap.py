"""Exercise the bootstrap CRD installation against an isolated fake API."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CRDS = [f"customresourcedefinition.apiextensions.k8s.io/{name}.gateway.networking.k8s.io"
        for name in ("backendtlspolicies", "gatewayclasses", "gateways", "grpcroutes",
                     "httproutes", "listenersets", "referencegrants", "tcproutes",
                     "tlsroutes", "udproutes")]
POLICIES = [f"{kind}.admissionregistration.k8s.io/safe-upgrades.gateway.networking.k8s.io"
            for kind in ("validatingadmissionpolicy", "validatingadmissionpolicybinding")]


class GatewayBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "applied").write_text("\n".join(CRDS + POLICIES) + "\n")
        (self.root / "kubectl.py").write_text('''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["TEST_ROOT"])
args = sys.argv[1:]
with (root / "calls").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[0] == "apply":
    assert "--server-side" in args and "-k" in args
    assert args[-2:] == ["-o", "name"]
    print((root / "applied").read_text(), end="")
    sys.exit(int(os.environ.get("APPLY_EXIT", "0")))
if args[0] == "wait":
    assert "-k" not in args and "--kustomize" not in args
    assert "--for=condition=Established" in args and "--timeout=120s" in args
    sys.exit(int(os.environ.get("WAIT_EXIT", "0")))
raise AssertionError(args)
''')

    def run_install(self, **environment):
        script = r'''
set -Eeuo pipefail
source "$COMMON"
kubectl() { python3 "$TEST_ROOT/kubectl.py" "$@"; }
install_gateway_api
touch "$TEST_ROOT/continued"
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, TEST_ROOT=str(self.root),
                                         COMMON=str(ROOT / "scripts/lib/common.sh"), **environment))
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        return result, calls

    def test_waits_for_all_applied_crds_but_not_admission_policies(self):
        result, calls = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[0] for args in calls], ["apply", "wait"])
        self.assertEqual([arg for arg in calls[1][1:] if not arg.startswith("-")], CRDS)
        self.assertTrue((self.root / "continued").exists())

    def test_partial_apply_failure_stops_before_wait(self):
        result, calls = self.run_install(APPLY_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[0] for args in calls], ["apply"])
        self.assertFalse((self.root / "continued").exists())

    def test_bundle_without_crds_is_rejected(self):
        (self.root / "applied").write_text("\n".join(POLICIES) + "\n")
        result, calls = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not contain any CRDs", result.stderr)
        self.assertEqual([args[0] for args in calls], ["apply"])
        self.assertFalse((self.root / "continued").exists())

    def test_wait_failure_stops_bootstrap(self):
        result, calls = self.run_install(WAIT_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([args[0] for args in calls], ["apply", "wait"])
        self.assertFalse((self.root / "continued").exists())


if __name__ == "__main__":
    unittest.main()
