#!/usr/bin/env python3
"""Add/remove a temporary, unauthenticated Hubble HTTPS route on a k3s server.

Uses KUBECONFIG, defaulting to /etc/rancher/k3s/k3s.yaml. Only the Python
standard library and the installed k3s binary are required. Nothing expires
automatically: run remove when testing is finished. Render makes no API calls.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import config

MANAGER = "elektro-hubble-test"
LABEL = "app.kubernetes.io/managed-by"
TARGETS = (("ReferenceGrant", "kube-system"), ("HTTPRoute", "administration"))
NAME = "hubble-test"
PARENT = {"name": "internal", "namespace": "gateway-system", "sectionName": "admin-https"}


def manifests(domain):
    specs = [
        {"from": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute",
                   "namespace": "administration"}],
         "to": [{"group": "", "kind": "Service", "name": "hubble-ui"}]},
        {"parentRefs": [PARENT.copy()], "hostnames": [f"hubble.admin.{domain}"],
         "rules": [{"backendRefs": [{"name": "hubble-ui", "namespace": "kube-system", "port": 80}]}]},
    ]
    return [{"apiVersion": "gateway.networking.k8s.io/v1", "kind": kind,
             "metadata": {"name": NAME, "namespace": namespace, "labels": {LABEL: MANAGER}},
             "spec": spec} for (kind, namespace), spec in zip(TARGETS, specs)]


def kubectl(*args, body=None):
    return subprocess.run(["/usr/local/bin/k3s", "kubectl", "--request-timeout=30s", *args],
                          input=json.dumps(body) if body is not None else None,
                          stdout=subprocess.PIPE, text=True, check=True).stdout


def get(kind, namespace, name, optional=False):
    args = ["-n", namespace, "get", kind, name, "-o", "json"]
    if optional:
        args.append("--ignore-not-found=true")
    output = kubectl(*args)
    return json.loads(output) if output.strip() else None


def owned_resources():
    """Read both identities before changing either; API failures must propagate."""
    objects = []
    for kind, namespace in TARGETS:
        obj = get(kind, namespace, NAME, optional=True)
        if obj is None:
            continue
        metadata = obj["metadata"]
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        if (labels.get(LABEL) != MANAGER or metadata.get("ownerReferences") or
                any(k.startswith("kustomize.toolkit.fluxcd.io/") for k in labels) or
                "meta.helm.sh/release-name" in annotations):
            raise ValueError(f"Refusing to change {kind} {namespace}/{NAME}: it is not owned exclusively by this script.")
        objects.append(obj)
    return objects


def current_conditions(obj, conditions, required):
    generation = obj["metadata"]["generation"]
    return all(any(c.get("type") == name and c.get("status") == "True" and
                   c.get("observedGeneration") == generation for c in conditions)
               for name in required)


def route_ready(route):
    return any(p.get("controllerName") == "io.cilium/gateway-controller" and
               all(p.get("parentRef", {}).get(k) == v for k, v in PARENT.items()) and
               p["parentRef"].get("group", "gateway.networking.k8s.io") == "gateway.networking.k8s.io" and
               p["parentRef"].get("kind", "Gateway") == "Gateway" and
               current_conditions(route, p.get("conditions", []), ["Accepted", "ResolvedRefs"])
               for p in route.get("status", {}).get("parents", []))


def check_prerequisites(hostname):
    gateway = get("gateway", "gateway-system", "internal")
    status = gateway.get("status", {})
    listener = next((item for item in status.get("listeners", []) if item["name"] == "admin-https"), {})
    if (not current_conditions(gateway, status.get("conditions", []), ["Accepted", "Programmed"]) or
            not current_conditions(gateway, listener.get("conditions", []), ["Accepted", "Programmed", "ResolvedRefs"])):
        raise ValueError("Gateway internal/admin-https is not ready; run scripts/check-gateway.sh first.")
    namespace = get("namespace", "administration", "administration")
    if namespace["metadata"].get("labels", {}).get("elektro.internal/route-scope") != "administration":
        raise ValueError("Namespace administration must have elektro.internal/route-scope=administration.")
    service = get("service", "kube-system", "hubble-ui")
    if not any(port["port"] == 80 for port in service["spec"]["ports"]):
        raise ValueError("Service kube-system/hubble-ui must expose port 80.")
    routes = json.loads(kubectl("get", "httproute", "-A", "-o", "json"))["items"]
    for route in routes:
        metadata = route["metadata"]
        if (metadata["namespace"], metadata["name"]) == ("administration", NAME):
            continue
        attached = any(p["name"] == "internal" and
                       p.get("group", "gateway.networking.k8s.io") == "gateway.networking.k8s.io" and
                       p.get("kind", "Gateway") == "Gateway" and
                       p.get("namespace", metadata["namespace"]) == "gateway-system" and
                       p.get("sectionName", "admin-https") == "admin-https" and
                       p.get("port", 443) == 443 for p in route["spec"].get("parentRefs", []))
        hostnames = route["spec"].get("hostnames", [])
        matches = not hostnames or any(h == hostname or (h.startswith("*.") and hostname.endswith(h[1:]))
                                      for h in hostnames)
        if attached and matches:
            raise ValueError(f"HTTPRoute {metadata['namespace']}/{metadata['name']} already claims {hostname}; resolve the route conflict first.")


def add(c, timeout):
    hostname = f"hubble.admin.{c['domain']}"
    owned_resources()
    check_prerequisites(hostname)
    # Permission first, then the route. Retrying add repairs a partial apply.
    for obj in manifests(c["domain"]):
        print(kubectl("apply", "--server-side", f"--field-manager={MANAGER}", "-f", "-", body=obj), end="")
    deadline = time.monotonic() + timeout
    while True:
        route = get("httproute", "administration", NAME)
        if route_ready(route):
            print(f"Temporary Hubble route ready: https://{hostname} (LAN address {c['api_ip']}).")
            print("This route has no login. Run scripts/hubble-route.py remove when testing is finished.")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Timed out waiting for the Hubble route's current Accepted/ResolvedRefs conditions. "
                             "Inspect kubectl -n administration describe httproute hubble-test. "
                             "Resources remain for diagnosis; rerun add or run remove to clean up.")
        time.sleep(min(2, remaining))


def remove():
    # Route first, then its permission. Removal does not need the local config,
    # Gateway, Service or a healthy Cilium deployment.
    for obj in reversed(owned_resources()):
        metadata = obj["metadata"]
        print(kubectl("-n", metadata["namespace"], "delete", obj["kind"], metadata["name"],
                      "--ignore-not-found=true", "--wait=true", "--timeout=60s"), end="")
    print("Temporary Hubble route and grant removed (already absent resources are skipped).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("add", "remove", "render"))
    parser.add_argument("--config", type=Path, default=config.CONFIG,
                        help="cluster JSON for add/render; defaults to config/cluster.json")
    parser.add_argument("--timeout", type=int, default=120, help="route readiness timeout in seconds (default: 120)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    os.environ.setdefault("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
    if args.action == "remove":
        remove()
        return
    c = config.load(args.config)
    if args.action == "render":
        print(json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests(c["domain"])}, indent=2))
    else:
        add(c, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Hubble route: {error}")
