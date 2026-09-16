#!/usr/bin/env python3
"""Select a node's LAN IPv4 address and refresh k3s before each service start."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile


def select_address(addresses, lan_cidr, api_ip, fixed_ip=None, edge=False):
    network = ipaddress.IPv4Network(lan_cidr)
    candidates = set()
    for interface in addresses:
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet" or address.get("scope") != "global":
                continue
            ip = ipaddress.IPv4Address(address["local"])
            if ip in network and ip not in (network.network_address, network.broadcast_address):
                candidates.add(str(ip))
    if edge:
        if fixed_ip != api_ip:
            raise ValueError("The bootstrap/edge node must use api_ip as its fixed address")
    else:
        candidates.discard(api_ip)
    if fixed_ip:
        if fixed_ip not in candidates:
            raise ValueError(f"Configured address {fixed_ip} is not assigned to this LAN interface")
        return fixed_ip
    if len(candidates) != 1:
        raise ValueError("Expected exactly one usable LAN IPv4 on the selected interface; "
                         "check DHCP or specify --node-ip explicitly")
    return candidates.pop()


def discover(settings):
    addresses = json.loads(subprocess.check_output(
        ["ip", "-j", "-4", "addr", "show", "dev", settings["interface"]], text=True))
    return select_address(addresses, **{k: v for k, v in settings.items() if k != "interface"})


def refresh(settings_path, config_path):
    settings = json.loads(settings_path.read_text())
    node_ip = discover(settings)
    config = json.loads(config_path.read_text())
    if config["node-ip"] == node_ip:
        return
    config["node-ip"] = node_ip
    # Same-directory rename is atomic; never expose token paths/config as world readable.
    fd, temporary = tempfile.mkstemp(prefix=".node-ip-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, config_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Updated k3s node-ip to {node_ip}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["detect", "refresh"])
    parser.add_argument("--settings", type=Path, default=Path("/etc/elektro/node-network.json"))
    parser.add_argument("--config", type=Path, default=Path("/etc/rancher/k3s/config.yaml"))
    args = parser.parse_args()
    if args.command == "detect":
        print(discover(json.loads(args.settings.read_text())))
    else:
        refresh(args.settings, args.config)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
