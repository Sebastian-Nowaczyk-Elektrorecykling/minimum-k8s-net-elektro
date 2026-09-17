#!/usr/bin/env python3
"""Refuse node removal while a local/hostPath PV can use this node."""
import json
import sys


def requirement_matches(requirement, fields):
    key, operator = requirement["key"], requirement["operator"]
    values = requirement.get("values", [])
    if operator == "In":
        return key in fields and fields[key] in values
    if operator == "NotIn":
        return key not in fields or fields[key] not in values
    if operator == "Exists":
        return key in fields
    if operator == "DoesNotExist":
        return key not in fields
    if operator in ("Gt", "Lt"):
        if key not in fields:
            return False
        try:
            value, boundary = int(fields[key]), int(values[0])
        except (ValueError, IndexError):
            return False
        return value > boundary if operator == "Gt" else value < boundary
    raise ValueError(f"Unsupported node affinity operator {operator}; inspect local storage manually")


def blocking_volumes(node, volumes):
    labels = node["metadata"].get("labels", {})
    fields = {"metadata.name": node["metadata"]["name"]}
    blocked = []
    for volume in volumes:
        spec = volume["spec"]
        if "local" not in spec and "hostPath" not in spec:
            continue
        required = spec.get("nodeAffinity", {}).get("required")
        matches = required is None  # Unscoped hostPath can hold data on any node.
        for term in (required or {}).get("nodeSelectorTerms", []):
            expressions, match_fields = term.get("matchExpressions", []), term.get("matchFields", [])
            if not expressions and not match_fields:
                continue  # Kubernetes specifies that an empty term matches no nodes.
            if all(requirement_matches(r, labels) for r in expressions) and all(
                    requirement_matches(r, fields) for r in match_fields):
                matches = True
        if matches:
            blocked.append(volume["metadata"]["name"])
    return blocked


if __name__ == "__main__":
    data = json.load(sys.stdin)
    volumes = blocking_volumes(data["node"], data["volumes"]["items"])
    if volumes:
        raise SystemExit("Migrate or retire local/hostPath PVs before removing this node: " + ", ".join(volumes))
