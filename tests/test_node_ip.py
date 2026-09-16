import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('node_ip', ROOT / 'scripts/refresh-node-ip.py')
node_ip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_ip)


def addresses(*values):
    return [{'addr_info': [dict(family='inet', scope='global', local=ip) for ip in values]}]


class NodeIPTests(unittest.TestCase):
    def select(self, *values, **kwargs):
        return node_ip.select_address(addresses(*values), '192.168.0.0/20', '192.168.2.153', **kwargs)

    def test_selects_dhcp_lan_address_and_ignores_other_networks(self):
        self.assertEqual(self.select('10.9.0.3', '192.168.9.71'), '192.168.9.71')

    def test_rejects_missing_ambiguous_and_reserved_addresses(self):
        for values in [(), ('192.168.3.1', '192.168.3.2'), ('192.168.0.0',),
                       ('192.168.15.255',), ('192.168.2.153',)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.select(*values)

    def test_fixed_address_must_be_assigned(self):
        self.assertEqual(self.select('192.168.3.1', '192.168.3.2', fixed_ip='192.168.3.2'), '192.168.3.2')
        with self.assertRaises(ValueError):
            self.select('192.168.3.1', fixed_ip='192.168.3.2')

    def test_edge_node_requires_static_api_address(self):
        self.assertEqual(self.select('192.168.2.153', fixed_ip='192.168.2.153', edge=True), '192.168.2.153')
        with self.assertRaises(ValueError):
            self.select('192.168.3.1', fixed_ip='192.168.3.1', edge=True)

    def test_service_restart_refresh_preserves_other_flags_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            settings = Path(directory) / 'network.json'
            settings.write_text('{}')
            original = {'node-ip': '192.168.2.100', 'server': 'https://192.168.2.153:6443',
                        'token-file': '/etc/rancher/k3s/join.token', 'disable-kube-proxy': True}
            config.write_text(json.dumps(original))
            with patch.object(node_ip, 'discover', return_value='192.168.2.101'):
                node_ip.refresh(settings, config)
            self.assertEqual(json.loads(config.read_text()), dict(original, **{'node-ip': '192.168.2.101'}))
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            before = config.read_bytes()
            with patch.object(node_ip, 'discover', side_effect=ValueError('DHCP not ready')):
                with self.assertRaises(ValueError):
                    node_ip.refresh(settings, config)
            self.assertEqual(config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
