"""Opt-in image execution regression: COREDNS_CONTAINER_TEST=1 requires Docker.

Unlike the downloaded release binary, the image's /coredns has file capabilities.
Execute that image with the manifest's user, capabilities and hardening settings
so CI detects startup failures at execve, before the DNS protocol tests can run.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("COREDNS_CONTAINER_TEST") == "1",
                     "Set COREDNS_CONTAINER_TEST=1 to test the image with Docker")
class CoreDNSImageTests(unittest.TestCase):
    def test_image_executes_with_deployment_security_context(self):
        settings = yaml.safe_load((ROOT / "clusters/lan/cluster-settings.yaml").read_text())["data"]
        manifest = re.sub(r"\$\{([A-Z_]+)\}", lambda m: settings[m[1]],
                          (ROOT / "infrastructure/dns/dns.yaml").read_text())
        deployment = next(d for d in yaml.safe_load_all(manifest)
                          if d["kind"] == "Deployment" and d["metadata"]["name"] == "lan-dns")
        pod = deployment["spec"]["template"]["spec"]
        container = next(c for c in pod["containers"] if c["name"] == "coredns")
        security = dict(pod.get("securityContext", {}), **container.get("securityContext", {}))
        name = "elektro-coredns-exec-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--name", name, "--network", "none",
                   "--user", f"{security['runAsUser']}:{security['runAsGroup']}"]
        for action in ("drop", "add"):
            for capability in security.get("capabilities", {}).get(action, []):
                command += [f"--cap-{action}", capability]
        if not security.get("allowPrivilegeEscalation", True):
            command += ["--security-opt", "no-new-privileges=true"]
        if security.get("readOnlyRootFilesystem"):
            command += ["--read-only"]
        # Docker's default seccomp profile is its RuntimeDefault equivalent.
        self.assertEqual(security["seccompProfile"]["type"], "RuntimeDefault")
        command += [container["image"], "-version"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0,
                             f"CoreDNS image could not execute:\n{result.stdout}\n{result.stderr}")
            self.assertIn("CoreDNS-", result.stdout + result.stderr)
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
