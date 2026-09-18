"""Exercise NVIDIA package selection and failure reporting without host changes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NvidiaPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def run_check(self, install=False, present=False, **environment):
        if present:
            (self.directory / "smi-installed").touch()
        script = r'''
set -Eeuo pipefail
source "$COMMON"
uname() { printf '%s\n' '6.12-test'; }
command() {
  if [[ $1 == -v && $2 == nvidia-smi && ! -e $TEST_ROOT/smi-installed ]]; then return 1; fi
  builtin command "$@"
}
apt-get() {
  printf '%s\n' "$@" > "$TEST_ROOT/apt-args"
  if [[ ${APT_EXIT:-0} != 0 ]]; then return "$APT_EXIT"; fi
  for package in "$@"; do
    if [[ $package == "${NVIDIA_SMI_PACKAGE:-nvidia-smi}" ]]; then touch "$TEST_ROOT/smi-installed"; fi
  done
}
nvidia-smi() {
  touch "$TEST_ROOT/smi-called"
  printf '%s\n' "${SMI_MESSAGE:-NVIDIA GPU available}" >&2
  return "${SMI_EXIT:-0}"
}
lspci() { printf '%s\n' '01:00.0 NVIDIA test GPU; Kernel driver in use: nouveau'; }
dkms() { printf '%s\n' 'nvidia-current/test, 6.12-test, x86_64: installed'; }
lsmod() { printf '%s\n' 'nouveau 123 1'; }
modprobe() {
  printf '%s\n' "$@" > "$TEST_ROOT/modprobe-args"
  printf '%s\n' 'insmod /lib/modules/6.12-test/nvidia.ko'
}
journalctl() { printf '%s\n' 'NVRM: test kernel diagnostic'; }
if [[ $INSTALL == yes ]]; then install_nvidia_driver; fi
check_nvidia_driver
touch "$TEST_ROOT/continued"
'''
        env = dict(os.environ, COMMON=str(ROOT / "scripts/lib/common.sh"),
                   TEST_ROOT=str(self.directory), INSTALL="yes" if install else "no",
                   NVIDIA_DRIVER_PACKAGE="nvidia-driver", NVIDIA_SMI_PACKAGE="nvidia-smi",
                   NVIDIA_CUDA_PACKAGE="libcuda1")
        env.update(environment)
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def test_minimal_install_provides_smi_and_host_cuda_driver_library(self):
        result = self.run_check(install=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = (self.directory / "apt-args").read_text().splitlines()
        for package in ("--no-install-recommends", "linux-headers-6.12-test",
                        "nvidia-driver", "nvidia-smi", "libcuda1"):
            self.assertIn(package, packages)
        self.assertNotIn("nvidia-cuda-toolkit", packages)
        self.assertTrue((self.directory / "smi-called").exists())
        self.assertTrue((self.directory / "continued").exists())

    def test_custom_driver_uses_matching_companion_packages(self):
        result = self.run_check(install=True, NVIDIA_DRIVER_PACKAGE="nvidia-tesla-driver",
                                NVIDIA_SMI_PACKAGE="nvidia-tesla-smi",
                                NVIDIA_CUDA_PACKAGE="libnvidia-tesla-cuda1")
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = (self.directory / "apt-args").read_text().splitlines()
        for package in ("nvidia-tesla-driver", "nvidia-tesla-smi", "libnvidia-tesla-cuda1"):
            self.assertIn(package, packages)
        self.assertNotIn("nvidia-smi", packages)
        self.assertNotIn("libcuda1", packages)

    def test_missing_command_is_distinct_from_driver_failure(self):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nvidia-smi is missing from PATH", result.stderr)
        self.assertIn("driver readiness has not been tested", result.stderr)
        self.assertFalse((self.directory / "smi-called").exists())
        self.assertFalse((self.directory / "continued").exists())

    def test_driver_error_and_kernel_diagnostics_are_visible(self):
        message = "NVIDIA-SMI has failed because it could not communicate with the NVIDIA driver."
        result = self.run_check(present=True, SMI_EXIT="9", SMI_MESSAGE=message)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertIn("exit status 9", result.stderr)
        self.assertIn("6.12-test", result.stderr)
        self.assertIn("NVRM: test kernel diagnostic", result.stdout)
        self.assertEqual((self.directory / "modprobe-args").read_text().splitlines(),
                         ["--dry-run", "--verbose", "nvidia"])
        self.assertFalse((self.directory / "continued").exists())

    def test_healthy_driver_does_not_change_modules_or_install_packages(self):
        result = self.run_check(present=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.directory / "apt-args").exists())
        self.assertFalse((self.directory / "modprobe-args").exists())
        self.assertTrue((self.directory / "continued").exists())

    def test_package_failure_stops_before_readiness(self):
        result = self.run_check(install=True, APT_EXIT="100")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / "smi-called").exists())
        self.assertFalse((self.directory / "continued").exists())


if __name__ == "__main__":
    unittest.main()
