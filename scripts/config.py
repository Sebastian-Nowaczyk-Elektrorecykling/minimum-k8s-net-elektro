#!/usr/bin/env python3
"""Validate the single configuration source and generate reproducible manifests.

Only Python's standard library is needed on a fresh host. Generated manifests
use JSON, which is also valid YAML, to avoid a host-side templating dependency.
"""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/cluster.json"
CONFIG_KEYS = set("""cluster_name git_url git_branch lan_cidr api_ip pod_cidr
service_cidr cluster_dns_ip lan_dns_service_ip domain hubble_backend_service
hubble_backend_namespace hubble_backend_port upstream_dns cilium_devices
operator_replicas k3s_version cilium_version gateway_api_version flux_version
helm_version cert_manager_version coredns_image nvidia_toolkit_version""".split())


def load(path=CONFIG):
    c = json.loads(Path(path).read_text())
    if not isinstance(c, dict):
        raise ValueError("Configuration must be a JSON object")
    if set(c) != CONFIG_KEYS:
        raise ValueError(f"Missing settings: {sorted(CONFIG_KEYS - set(c))}; unknown settings: {sorted(set(c) - CONFIG_KEYS)}")
    for key in CONFIG_KEYS - {"upstream_dns", "cilium_devices", "operator_replicas", "hubble_backend_port"}:
        if not isinstance(c[key], str) or not c[key]:
            raise ValueError(f"{key} must be a nonempty string")
    for key in ("upstream_dns", "cilium_devices"):
        if not isinstance(c[key], list) or any(not isinstance(v, str) or not v for v in c[key]):
            raise ValueError(f"{key} must be a list of nonempty strings")
    for key in CONFIG_KEYS:
        if key.endswith("_version") and not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", c[key]):
            raise ValueError(f"{key} must be a pinned release version")
    if not re.fullmatch(r"v\d+\.\d+\.\d+\+k3s\d+", c["k3s_version"]):
        raise ValueError("k3s_version must be a release tag such as v1.36.4+k3s1")
    if c["cilium_version"].startswith("v"):
        raise ValueError("cilium_version must omit the v prefix")
    for key in ("flux_version", "gateway_api_version", "helm_version"):
        if not c[key].startswith("v"):
            raise ValueError(f"{key} must include the v prefix")
    networks = {k: ipaddress.IPv4Network(c[k]) for k in
                ("lan_cidr", "pod_cidr", "service_cidr")}
    for a, b in (("lan_cidr", "pod_cidr"), ("lan_cidr", "service_cidr"),
                 ("pod_cidr", "service_cidr")):
        if networks[a].overlaps(networks[b]):
            raise ValueError(f"{a} overlaps {b}")
    for key in ("api_ip",
                "cluster_dns_ip", "lan_dns_service_ip"):
        ip = ipaddress.IPv4Address(c[key])
        net = networks["service_cidr" if key in
                       ("cluster_dns_ip", "lan_dns_service_ip") else "lan_cidr"]
        if ip not in net or ip in (net.network_address, net.broadcast_address):
            raise ValueError(f"{key} must be a usable address in {net}")
    reserved = networks["service_cidr"].network_address + 1
    if c["cluster_dns_ip"] == c["lan_dns_service_ip"] or any(
            ipaddress.ip_address(c[k]) == reserved
            for k in ("cluster_dns_ip", "lan_dns_service_ip")):
        raise ValueError("DNS Service addresses collide with each other or Kubernetes")
    if len("hubble.admin." + c["domain"]) > 253 or any(
            len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", label)
            for label in c["domain"].split(".")):
        raise ValueError("domain must be a lowercase DNS name")
    if c["domain"] == "cluster.local" or c["domain"].endswith(".cluster.local") or "cluster.local".endswith("." + c["domain"]):
        raise ValueError("domain must not overlap Kubernetes' cluster.local DNS zone")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", c["cluster_name"]):
        raise ValueError("Invalid cluster_name")
    for key in ("hubble_backend_service", "hubble_backend_namespace"):
        value = c[key]
        first = "[a-z]" if key == "hubble_backend_service" else "[a-z0-9]"
        if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(
                first + r"(?:[-a-z0-9]*[a-z0-9])?", value):
            raise ValueError(f"Invalid Kubernetes name for {key}")
    port = c["hubble_backend_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("hubble_backend_port must be an integer from 1 to 65535")
    if not c["upstream_dns"]:
        raise ValueError("At least one upstream DNS address is required")
    for value in c["upstream_dns"]:
        ip = ipaddress.ip_address(value)
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or str(ip) in (c["api_ip"], c["lan_dns_service_ip"], c["cluster_dns_ip"]):
            raise ValueError("Recursive DNS loop in upstream_dns")
    for key in ("operator_replicas",):
        if not isinstance(c[key], int) or isinstance(c[key], bool) or c[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not c["git_url"].startswith("https://") or not c["git_url"].endswith(".git"):
        raise ValueError("git_url must be an HTTPS .git URL")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", c["git_branch"]):
        raise ValueError("Invalid git_branch")
    return c


def cilium_values(c):
    tolerations = [{"operator": "Exists"}]
    v = {
        "cluster": {"name": c["cluster_name"], "id": 1},
        "kubeProxyReplacement": True,
        "k8sServiceHost": c["api_ip"], "k8sServicePort": 6443,
        "ipam": {"mode": "kubernetes"},
        "routingMode": "tunnel", "tunnelProtocol": "vxlan",
        "bpf": {"masquerade": True},
        "l2announcements": {"enabled": False},
        # Gateway API owns north-south routing. Do not inherit Ingress defaults
        # from a future chart or accidentally expose Hubble through a second path.
        "ingressController": {"enabled": False, "default": False},
        "l7Proxy": True,
        "gatewayAPI": {"enabled": True, "gatewayClass": {"create": "false"},
                       "hostNetwork": {"enabled": True, "nodes": {
                           "matchLabels": {"elektro.internal/edge": "true"}}}},
        "envoy": {"enabled": True, "prometheus": {"enabled": True},
                  "securityContext": {"capabilities": {
                      "keepCapNetBindService": True,
                      "envoy": ["NET_ADMIN", "SYS_ADMIN", "NET_BIND_SERVICE"]}}},
        "operator": {"replicas": c["operator_replicas"],
                     "tolerations": tolerations, "prometheus": {"enabled": True},
                     "dashboards": {"enabled": True}},
        "dashboards": {"enabled": True},
        "prometheus": {"enabled": True},
        "certgen": {"tolerations": tolerations},
        "hubble": {
            "enabled": True,
            "tls": {"enabled": True, "auto": {"enabled": True, "method": "cronJob"}},
            "relay": {"enabled": True, "tolerations": tolerations,
                      "prometheus": {"enabled": True}},
            "ui": {"enabled": True, "tolerations": tolerations,
                   "ingress": {"enabled": False}},
            "metrics": {"enabled": ["dns", "drop", "tcp", "flow", "icmp",
                         "httpV2:exemplars=true;labelsContext=source_namespace,destination_namespace"],
                        "enableOpenMetrics": True, "dashboards": {"enabled": True}}},
    }
    if c["cilium_devices"]:
        v["devices"] = c["cilium_devices"]
    return v


def manifest(kind, name, namespace=None, **kwargs):
    obj = {"apiVersion": "v1", "kind": kind, "metadata": {"name": name}}
    if namespace:
        obj["metadata"]["namespace"] = namespace
    return dict(obj, **kwargs)


def outputs(c):
    settings = {key.upper(): str(value) for key, value in c.items()
                if isinstance(value, (str, int))}
    settings["UPSTREAM_DNS"] = " ".join(c["upstream_dns"])
    settings["ADMIN_DOMAIN"] = "admin." + c["domain"]
    settings["CONFIG_REVISION"] = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]
    k = lambda resources: {"apiVersion": "kustomize.config.k8s.io/v1beta1",
                           "kind": "Kustomization", "resources": resources}
    source = {
        "apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
        "metadata": {"name": "flux-system", "namespace": "flux-system"},
        "spec": {"interval": "1m", "url": c["git_url"],
                 "ref": {"branch": c["git_branch"]}}}
    return {
        "clusters/lan/cluster-settings.yaml": manifest(
            "ConfigMap", "cluster-settings", "flux-system", data=settings),
        "infrastructure/cilium/values.yaml": cilium_values(c),
        "clusters/lan/flux-system/source.yaml": source,
        "clusters/lan/flux-system/kustomization.yaml": k([
            "../../../infrastructure/flux", "source.yaml", "sync.yaml"]),
        "infrastructure/flux/kustomization.yaml": dict(k([
            f"https://github.com/fluxcd/flux2/releases/download/{c['flux_version']}/install.yaml"]),
            patches=[{"target": {"kind": "Deployment"}, "patch": json.dumps([
                {"op": "add", "path": "/spec/template/spec/tolerations", "value": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}]}])},
                {"target": {"kind": "Deployment", "name": "kustomize-controller"},
                 "patch": json.dumps([{"op": "add", "path": "/spec/template/spec/containers/0/args/-",
                                      "value": "--feature-gates=StrictPostBuildSubstitutions=true"}])}]),
        "infrastructure/gateway-api/kustomization.yaml": k([
            f"https://github.com/kubernetes-sigs/gateway-api/releases/download/{c['gateway_api_version']}/standard-install.yaml"]),
    }


def node_config(c, role, node_ip, interface, bootstrap=False):
    ip = ipaddress.ip_address(node_ip)
    net = ipaddress.ip_network(c["lan_cidr"])
    if ip not in net or ip in (net.network_address, net.broadcast_address):
        raise ValueError("Node IP must belong to lan_cidr")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", interface):
        raise ValueError("Invalid interface")
    if bootstrap and (role == "worker" or node_ip != c["api_ip"]):
        raise ValueError("Bootstrap must be a server on api_ip")
    if not bootstrap and node_ip == c["api_ip"]:
        raise ValueError("A joining node cannot use the API/edge address")
    result = {
        "node-ip": node_ip, "flannel-iface": interface,
        "node-label": [f"elektro.internal/role={role}"],
        "resolv-conf": "/etc/rancher/k3s/upstream-resolv.conf",
    }
    if bootstrap:
        result["node-label"].append("elektro.internal/edge=true")
    if not bootstrap:
        result.update({"server": f"https://{c['api_ip']}:6443",
                       "token-file": "/etc/rancher/k3s/join.token"})
    if role != "worker":
        result.update({
            "flannel-backend": "none", "disable-kube-proxy": True,
            "disable-network-policy": True,
            "disable": ["traefik", "servicelb", "local-storage"],
            "cluster-cidr": c["pod_cidr"], "service-cidr": c["service_cidr"],
            "cluster-dns": c["cluster_dns_ip"], "cluster-domain": "cluster.local",
            "tls-san": [c["api_ip"], f"api.{c['domain']}"],
            "write-kubeconfig-mode": "0600", "secrets-encryption": True,
            "etcd-expose-metrics": True,
            "etcd-snapshot-retention": 14, "etcd-snapshot-schedule-cron": "0 */12 * * *",
        })
        if bootstrap:
            result["cluster-init"] = True
        if role == "controller":
            result["node-taint"] = ["node-role.kubernetes.io/control-plane=true:NoSchedule"]
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["generate", "check", "env", "node"])
    p.add_argument("--config", type=Path, default=CONFIG)
    p.add_argument("--role", choices=["worker", "controller", "hybrid"])
    p.add_argument("--node-ip")
    p.add_argument("--interface")
    p.add_argument("--bootstrap", action="store_true")
    args = p.parse_args()
    c = load(args.config)
    if args.command == "env":
        for key, value in c.items():
            if isinstance(value, (str, int)):
                print(f"{key.upper()}={shlex.quote(str(value))}")
        print("UPSTREAM_DNS=" + shlex.quote(" ".join(c["upstream_dns"])))
    elif args.command == "node":
        if not all((args.role, args.node_ip, args.interface)):
            p.error("node needs --role, --node-ip and --interface")
        print(json.dumps(node_config(c, args.role, args.node_ip, args.interface,
                                     args.bootstrap), indent=2))
    else:
        different = []
        for relative, value in outputs(c).items():
            path = ROOT / relative
            text = json.dumps(value, indent=2) + "\n"
            if args.command == "generate":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            elif not path.exists() or path.read_text() != text:
                different.append(relative)
        if different:
            sys.exit("Run python3 scripts/config.py generate; stale files: " + ", ".join(different))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(str(error))
