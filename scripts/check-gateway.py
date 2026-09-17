#!/usr/bin/env python3
"""Check a read-only Gateway API snapshot collected by check-gateway.sh."""
import argparse
import json
from pathlib import Path


def current_conditions(obj, conditions, required):
    generation = obj["metadata"]["generation"]
    return all(any(c.get("type") == name and c.get("status") == "True" and
                   c.get("observedGeneration") == generation for c in conditions)
               for name in required)


def audit(snapshot, api_ip):
    errors = []
    for obj in snapshot["legacy"]["items"]:
        m = obj["metadata"]
        errors.append(f"Legacy {obj['kind']}: {m.get('namespace', '-')}/{m['name']}; migrate its owning repository/release")
    data = snapshot["cilium"].get("data", {})
    if data.get("enable-ingress-controller", "false") != "false":
        errors.append("Cilium Ingress controller is still enabled")
    for flag in ("enable-gateway-api", "gateway-api-hostnetwork-enabled"):
        if data.get(flag) != "true":
            errors.append(f"Cilium {flag} is not true")
    if data.get("gateway-api-hostnetwork-nodelabelselector") != "elektro.internal/edge=true":
        errors.append("Gateway listeners are not restricted to the edge node")
    gateway_class = snapshot["class"]
    if not current_conditions(gateway_class, gateway_class.get("status", {}).get("conditions", []), ["Accepted"]):
        errors.append("GatewayClass cilium is not Accepted at its current generation")
    gateway = snapshot["gateway"]
    status = gateway.get("status", {})
    if not current_conditions(gateway, status.get("conditions", []), ["Accepted", "Programmed"]):
        errors.append("Gateway internal is not Accepted and Programmed at its current generation")
    for expected in gateway["spec"]["listeners"]:
        actual = next((l for l in status.get("listeners", []) if l["name"] == expected["name"]), {})
        if not current_conditions(gateway, actual.get("conditions", []), ["Accepted", "Programmed", "ResolvedRefs"]):
            errors.append(f"Listener {expected['name']} is not ready; inspect its conditions and certificate references")
    found = set()
    for route in snapshot["routes"]["items"]:
        name = f"{route['metadata']['namespace']}/{route['metadata']['name']}"
        for parent in route["spec"].get("parentRefs", []):
            namespace = parent.get("namespace", route["metadata"]["namespace"])
            if parent["name"] != "internal" or namespace != "gateway-system":
                continue
            found.add(name)
            matches = [p for p in route.get("status", {}).get("parents", [])
                       if p.get("controllerName") == "io.cilium/gateway-controller"
                       and p["parentRef"]["name"] == parent["name"]
                       and p["parentRef"].get("namespace", route["metadata"]["namespace"]) == namespace
                       and p["parentRef"].get("sectionName") == parent.get("sectionName")]
            if not matches or not all(current_conditions(route, p.get("conditions", []),
                                                         ["Accepted", "ResolvedRefs"]) for p in matches):
                errors.append(f"HTTPRoute {name} is not Accepted with ResolvedRefs at its current generation")
    for required in ("gateway-system/redirect-https", "administration/administration"):
        if required not in found:
            errors.append(f"Missing required HTTPRoute {required}")
    nodes = snapshot["edge"]["items"]
    if len(nodes) != 1 or not any(a["type"] == "InternalIP" and a["address"] == api_ip
                                  for n in nodes for a in n.get("status", {}).get("addresses", [])):
        errors.append(f"Exactly one edge node must own {api_ip}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--api-ip", required=True)
    args = parser.parse_args()
    snapshot = {name: json.loads((args.snapshot / f"{name}.json").read_text())
                for name in ("legacy", "cilium", "class", "gateway", "routes", "edge")}
    errors = audit(snapshot, args.api_ip)
    if errors:
        raise SystemExit("Gateway audit failed:\n- " + "\n- ".join(errors))
    print("Gateway audit passed: Gateway API only, current route/listener conditions, one static edge node.")


if __name__ == "__main__":
    main()
