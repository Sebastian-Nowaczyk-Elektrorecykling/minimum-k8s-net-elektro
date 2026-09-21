"""Optional real CoreDNS smoke test: COREDNS=/path/to/coredns python -m unittest..."""
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("COREDNS"), "Set COREDNS to run the real DNS service test")
class DNSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.directory = Path(cls.tmp.name)
        settings = yaml.safe_load((ROOT / "clusters/lan/cluster-settings.yaml").read_text())["data"]
        cls.settings = settings
        text = re.sub(r"\$\{([A-Z_]+)\}", lambda m: settings[m[1]],
                      (ROOT / "infrastructure/dns/dns.yaml").read_text())
        config = next(d["data"] for d in yaml.safe_load_all(text)
                      if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "lan-dns")
        corefile = config["Corefile"].replace("/etc/coredns", str(cls.directory))
        for before, after in [(":1053", ":15353"), (":8080", ":28080"),
                              (":8181", ":28181"), (":9153", ":29153")]:
            corefile = corefile.replace(before, after)
        (cls.directory / "Corefile").write_text(corefile)
        (cls.directory / "internal.zone").write_text(config["internal.zone"])
        cls.log = (cls.directory / "log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen([os.environ["COREDNS"], "-conf", str(cls.directory / "Corefile")],
                                       stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop)
        for _ in range(40):
            if cls.process.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(f"CoreDNS exited {cls.process.returncode}: {cls.log.read()}")
            try:
                with socket.create_connection(("127.0.0.1", 15353), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("CoreDNS did not start")

    @classmethod
    def stop(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            cls.process.wait(timeout=5)

    def query(self, name, qtype=1, tcp=False):
        labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"
        message = struct.pack("!HHHHHH", 1234, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", qtype, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("127.0.0.1", 15353))
            if tcp:
                s.sendall(struct.pack("!H", len(message)) + message)
                size = struct.unpack("!H", s.recv(2))[0]
                result = b""
                while len(result) < size:
                    part = s.recv(size - len(result))
                    if not part:
                        raise RuntimeError("Truncated TCP DNS response")
                    result += part
            else:
                s.send(message)
                result = s.recv(4096)
        fields = struct.unpack("!HHHHHH", result[:12])
        self.assertEqual(fields[0], 1234)
        self.assertEqual(fields[1] & 0xF, 0, "Expected NOERROR")
        self.assertTrue(fields[1] & 0x0400, "Private suffix must be authoritative")
        return fields, result

    def test_application_admin_management_and_environment_names_over_udp_and_tcp(self):
        for prefix in ("application", "hubble.admin", "management", "openbudget.management",
                       "testing", "application.testing",
                       "staging", "application.staging"):
            for tcp in (False, True):
                with self.subTest(prefix=prefix, tcp=tcp):
                    fields, data = self.query(prefix + "." + self.settings["DOMAIN"], tcp=tcp)
                    self.assertEqual(fields[3], 1)
                    self.assertIn(socket.inet_aton(self.settings["API_IP"]), data)

    def test_tcp_api_address(self):
        fields, data = self.query("api." + self.settings["DOMAIN"], tcp=True)
        self.assertEqual(fields[3], 1)
        self.assertIn(socket.inet_aton(self.settings["API_IP"]), data)

    def test_aaaa_is_empty_and_authoritative(self):
        for prefix in ("application", "hubble.admin", "management", "openbudget.management",
                       "testing", "application.testing",
                       "staging", "application.staging"):
            for tcp in (False, True):
                with self.subTest(prefix=prefix, tcp=tcp):
                    fields, _ = self.query(prefix + "." + self.settings["DOMAIN"], qtype=28, tcp=tcp)
                    self.assertEqual(fields[3], 0)


if __name__ == "__main__":
    unittest.main()
