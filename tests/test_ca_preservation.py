"""Run the CA script against a fake API and isolated files; never modify a host."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CAPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        (self.root / "lib").mkdir()
        # Redirect only the host path; exercise the production script's reads,
        # error handling, cryptographic checks and file replacement unchanged.
        script = (ROOT / "scripts/initialize-secrets.sh").read_text().replace(
            "secret_dir=/etc/elektro/secrets", "secret_dir=" + shlex.quote(str(self.secrets)))
        (self.root / "initialize-secrets.sh").write_text(script)
        (self.root / "lib/common.sh").write_text(r'''
require_root() { :; }
load_config() { CLUSTER_NAME=test; }
log() { printf '%s\n' "$*" >&2; }
die() { log "$*"; exit 1; }
kubectl() {
  case "$*" in
    'create namespace '*) printf '{}\n' ;;
    'apply -f -') cat >/dev/null ;;
    '-n cert-manager get secret internal-ca --ignore-not-found -o json')
      if [[ -f $TEST_ROOT/api-failure ]]; then return 1; fi
      cat "$TEST_ROOT/secret.json" ;;
    '-n cert-manager create secret tls internal-ca '*) touch "$TEST_ROOT/created" ;;
    *) return 99 ;;
  esac
}
''')
        (self.root / "secret.json").write_text("")

    def run_script(self):
        return subprocess.run(["bash", str(self.root / "initialize-secrets.sh")],
                              env=dict(os.environ, TEST_ROOT=str(self.root)), capture_output=True, text=True)

    def make_pair(self, prefix):
        cert, key = self.root / f"{prefix}.crt", self.root / f"{prefix}.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "1", "-subj", "/CN=test", "-keyout", str(key), "-out", str(cert)],
                       capture_output=True, check=True)
        return cert.read_bytes(), key.read_bytes()

    def test_api_failure_does_not_truncate_backups_or_create_new_ca(self):
        for name in ("ca.crt", "ca.key"):
            (self.secrets / name).write_bytes(b"existing-backup")
        (self.root / "api-failure").touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.root / "created").exists())
        for name in ("ca.crt", "ca.key"):
            self.assertEqual((self.secrets / name).read_bytes(), b"existing-backup")

    def test_partial_backup_does_not_rotate_the_trust_root(self):
        (self.secrets / "ca.key").write_bytes(b"retain-this-key")
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertEqual((self.secrets / "ca.key").read_bytes(), b"retain-this-key")
        self.assertFalse((self.secrets / "ca.crt").exists())
        self.assertFalse((self.root / "created").exists())

    def test_mismatched_api_certificate_and_key_preserve_backups(self):
        cert, _ = self.make_pair("first")
        _, key = self.make_pair("second")
        (self.root / "secret.json").write_text(json.dumps({"data": {
            "tls.crt": base64.b64encode(cert).decode(), "tls.key": base64.b64encode(key).decode()}}))
        for name in ("ca.crt", "ca.key"):
            (self.secrets / name).write_bytes(b"existing-backup")
        self.assertNotEqual(self.run_script().returncode, 0)
        for name in ("ca.crt", "ca.key"):
            self.assertEqual((self.secrets / name).read_bytes(), b"existing-backup")

    def test_existing_valid_pair_is_restored_without_new_secret(self):
        cert, key = self.make_pair("valid")
        (self.root / "secret.json").write_text(json.dumps({"data": {
            "tls.crt": base64.b64encode(cert).decode(), "tls.key": base64.b64encode(key).decode()}}))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.secrets / "ca.crt").read_bytes(), cert)
        self.assertEqual((self.secrets / "ca.key").read_bytes(), key)
        self.assertFalse((self.root / "created").exists())
