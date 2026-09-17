#!/usr/bin/env python3
"""Build all reconciliation targets, substitute settings, and verify invariants.

Requires PyYAML, kustomize, helm. Optional KUBECONFORM enables upstream schema
validation; missing custom schemas are checked separately against pinned CRDs.
No cluster, root access, or host changes are needed.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

# Kubernetes uses YAML 1.2 strings here; PyYAML's YAML 1.1 resolver assigns the
# standalone '=' enum value a special tag without a constructor.
yaml.SafeLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: node.value)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import config  # noqa: E402


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def substitute(text, settings):
    def replacement(match):
        if match[1] not in settings:
            raise ValueError(f"Undefined Flux variable {match[1]}")
        return settings[match[1]]
    return re.sub(r"\$\{([A-Z_]+)\}", replacement, text)


def validate_hubble_route(docs, c):
    route = next(d for d in docs if d["kind"] == "HTTPRoute" and
                 d["metadata"] == {"name": "administration", "namespace": "administration"})
    assert route["spec"]["parentRefs"] == [
        {"name": "internal", "namespace": "gateway-system", "sectionName": "admin-https"}]
    assert route["spec"]["hostnames"] == [f"hubble.admin.{c['domain']}"]
    # Exactly one configured backend: the SSO route must not retain a direct
    # Hubble backend that could bypass authentication.
    assert route["spec"]["rules"] == [{"backendRefs": [{
        "name": c["hubble_backend_service"], "namespace": c["hubble_backend_namespace"],
        "port": c["hubble_backend_port"]}]}]
    grant = next(d for d in docs if d["kind"] == "ReferenceGrant" and
                 d["metadata"]["name"] == "hubble-backend")
    assert grant["metadata"]["namespace"] == c["hubble_backend_namespace"]
    assert grant["spec"] == {
        "from": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "namespace": "administration"}],
        "to": [{"group": "", "kind": "Service", "name": c["hubble_backend_service"]}]}


def main():
    run(sys.executable, "scripts/config.py", "check")
    c = config.load()
    settings = yaml.safe_load((ROOT / "clusters/lan/cluster-settings.yaml").read_text())["data"]
    cache = ROOT / ".cache/validation"
    cache.mkdir(parents=True, exist_ok=True)
    for previous in cache.glob("*.yaml"):
        previous.unlink()
    kustomize = os.environ.get("KUSTOMIZE", "kustomize")
    helm = os.environ.get("HELM", "helm")
    steps = list(yaml.safe_load_all((ROOT / "clusters/lan/infrastructure.yaml").read_text()))
    names = {x["metadata"]["name"] for x in steps}
    graph = {x["metadata"]["name"]: [d["name"] for d in x["spec"].get("dependsOn", [])] for x in steps}
    def visit(name, chain=()):
        assert name in names, f"Unknown dependency {name}"
        assert name not in chain, f"Dependency cycle: {chain} -> {name}"
        for dep in graph[name]:
            visit(dep, chain + (name,))
    for name in names:
        visit(name)
    targets = [("root", "clusters/lan")] + [(x["metadata"]["name"], x["spec"]["path"]) for x in steps]
    all_docs = []
    for name, path in targets:
        rendered = substitute(run(kustomize, "build", path), settings)
        (cache / f"{name}.yaml").write_text(rendered)
        docs = [d for d in yaml.safe_load_all(rendered) if d]
        seen = set()
        for d in docs:
            identity = (d["apiVersion"], d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])
            assert identity not in seen, f"Duplicate object {identity}"
            seen.add(identity)
            if d["kind"] != "CustomResourceDefinition":
                assert "${" not in yaml.safe_dump(d), f"Unsubstituted variable in {identity}"
        all_docs.extend(docs)
        print(f"Built {name}: {len(docs)} objects")
    for d in all_docs:
        if d["kind"] == "Secret":
            raise ValueError("Secrets must not be committed")
    validate_hubble_route(all_docs, c)
    # Exercise a future proxy in a different namespace and on a different
    # Service port. Rendering must update both the route and its grant together.
    sso = dict(c, hubble_backend_service="hubble-sso", hubble_backend_namespace="sso-system",
               hubble_backend_port=4180)
    sso_settings = config.outputs(sso)["clusters/lan/cluster-settings.yaml"]["data"]
    sso_rendered = substitute(run(kustomize, "build", "infrastructure/admin"), sso_settings)
    (cache / "admin-sso-example.yaml").write_text(sso_rendered)
    sso_docs = [d for d in yaml.safe_load_all(sso_rendered) if d]
    validate_hubble_route(sso_docs, sso)
    print("Validated configured Hubble route and cross-namespace SSO proxy example")
    releases = {d["metadata"]["name"]: d for d in all_docs if d["kind"] == "HelmRelease"}
    adoption = releases["cilium"]["spec"]
    assert (adoption["releaseName"], adoption["targetNamespace"], adoption["storageNamespace"]) == ("cilium", "kube-system", "kube-system")
    assert set(releases) == {"cilium", "cert-manager"}, "Unexpected additional software release"
    assert "monitoring" not in names and "observability" not in names
    assert not any(d["kind"] in ("ServiceMonitor", "PodMonitor", "Prometheus", "Alertmanager", "PrometheusRule") for d in all_docs)
    dns = next(d for d in all_docs if d["kind"] == "Service" and d["metadata"]["name"] == "lan-dns")
    assert {p["protocol"] for p in dns["spec"]["ports"]} == {"UDP", "TCP"}
    assert dns["spec"]["clusterIP"] == c["lan_dns_service_ip"]
    assert dns["spec"]["type"] == "ClusterIP"
    deployment = next(d for d in all_docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "lan-dns")
    assert deployment["spec"]["replicas"] == 1 and deployment["spec"]["strategy"]["type"] == "Recreate"
    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"elektro.internal/edge": "true"}
    dns_ports = [p for p in pod["containers"][0]["ports"] if p.get("hostPort")]
    assert {p["protocol"] for p in dns_ports} == {"TCP", "UDP"}
    assert all(p["hostIP"] == c["api_ip"] and p["hostPort"] == 53 for p in dns_ports)
    assert not any(d["kind"] in ("CiliumLoadBalancerIPPool", "CiliumL2AnnouncementPolicy") or
                   (d["kind"] == "Service" and d["spec"].get("type") == "LoadBalancer") for d in all_docs)
    gateway = next(d for d in all_docs if d["kind"] == "Gateway")
    assert "addresses" not in gateway["spec"], "Host-network gateway must not request a VIP"
    for listener in gateway["spec"]["listeners"]:
        if listener["protocol"] == "HTTPS":
            assert listener["tls"]["certificateRefs"][0]["name"] == "internal-wildcard-tls"
    charts = [("cilium", "kube-system", "https://helm.cilium.io", c["cilium_version"]),
              ("cert-manager", "cert-manager", "https://charts.jetstack.io", c["cert_manager_version"])]
    chart_dir = os.environ.get("CHART_DIR")
    for name, namespace, repository, version in charts:
        release = releases[name]["spec"]
        chart = release["chart"]["spec"]["chart"]
        values = config.cilium_values(c) if name == "cilium" else release["values"]
        values_file = cache / f"{name}-values.yaml"
        values_file.write_text(yaml.safe_dump(values))
        args = [helm, "template", name, "--namespace", namespace, "--include-crds",
                "--kube-version", c["k3s_version"].split("+")[0][1:], "--values", str(values_file)]
        if chart_dir:
            args += [str(Path(chart_dir).resolve() / f"{chart}-{version}.tgz")]
        else:
            args += [chart, "--repo", repository, "--version", version]
        output = run(*args)
        (cache / f"chart-{name}.yaml").write_text(output)
        print(f"Rendered {chart} {version}")
    # Future monitoring examples are never reconciled, but their selectors and
    # ports must match the Cilium endpoints we actually enable.
    cilium_docs = list(yaml.safe_load_all((cache / "chart-cilium.yaml").read_text()))
    for d in cilium_docs:
        if not d or d["kind"] not in ("Deployment", "DaemonSet", "Job", "CronJob"):
            continue
        pod = (d["spec"]["jobTemplate"]["spec"]["template"]["spec"] if d["kind"] == "CronJob"
               else d["spec"]["template"]["spec"])
        assert any(t.get("operator") == "Exists" and t.get("key", "") in
                   ("", "node-role.kubernetes.io/control-plane") for t in pod.get("tolerations", [])), \
            f"Cilium component cannot bootstrap on a dedicated controller: {d['metadata']['name']}"
    services = [d for d in cilium_docs if d and d["kind"] == "Service"]
    if (c["hubble_backend_service"], c["hubble_backend_namespace"]) == ("hubble-ui", "kube-system"):
        ui = next(d for d in services if d["metadata"]["name"] == "hubble-ui")
        assert any(p["port"] == c["hubble_backend_port"] for p in ui["spec"]["ports"]), \
            "Hubble HTTPRoute does not match the Service emitted by the Cilium chart"
    workloads = [d for d in cilium_docs if d and d["kind"] in ("Deployment", "DaemonSet")]
    monitors = yaml.safe_load_all((ROOT / "examples/monitoring/cilium-monitors.yaml").read_text())
    for monitor in monitors:
        selector = monitor["spec"]["selector"]["matchLabels"]
        if monitor["kind"] == "ServiceMonitor":
            matches = [d for d in services if all(d["metadata"]["labels"].get(k) == v for k,v in selector.items())]
            ports = {p["name"] for d in matches for p in d["spec"]["ports"] if "name" in p}
            endpoints = monitor["spec"]["endpoints"]
        else:
            matches = [d for d in workloads if all(d["spec"]["template"]["metadata"]["labels"].get(k) == v for k,v in selector.items())]
            ports = {p["name"] for d in matches for container in d["spec"]["template"]["spec"]["containers"] for p in container.get("ports", [])}
            endpoints = monitor["spec"]["podMetricsEndpoints"]
        assert matches and all(e["port"] in ports for e in endpoints), f"Broken monitor {monitor['metadata']['name']}"
    # Validate custom resources against the actual CRDs bundled with pinned charts
    # and release manifests. This catches fields that Helm templating alone misses.
    import jsonschema
    import urllib.request
    # Cilium's operator installs its CRDs, so they are not all bundled in Helm.
    for api, plural in [("v2", "ciliumnetworkpolicies")]:
        crd_file = cache / f"crd-{plural}.yaml"
        url = (f"https://raw.githubusercontent.com/cilium/cilium/v{c['cilium_version']}/"
               f"pkg/k8s/apis/cilium.io/client/crds/{api}/{plural}.yaml")
        with urllib.request.urlopen(url, timeout=60) as response:
            crd_file.write_bytes(response.read())
    schema_by_gvk = {}
    schema_docs = []
    for path in cache.glob("*.yaml"):
        if path.name.endswith("-values.yaml"):
            continue
        schema_docs.extend(d for d in yaml.safe_load_all(path.read_text()) if d)
    for d in schema_docs:
        if d.get("kind") == "CustomResourceDefinition":
            for v in d["spec"]["versions"]:
                schema_by_gvk[(d["spec"]["group"] + "/" + v["name"], d["spec"]["names"]["kind"])] = v["schema"]["openAPIV3Schema"]
    count = 0
    for d in all_docs + sso_docs:
        schema = schema_by_gvk.get((d["apiVersion"], d["kind"]))
        if schema:
            jsonschema.Draft7Validator(schema).validate(d)
            count += 1
    print(f"Validated {count} custom resources against pinned CRD schemas")
    if os.environ.get("KUBECONFORM"):
        files = [str(p) for p in cache.glob("*.yaml") if not p.name.endswith("-values.yaml")]
        subprocess.run([os.environ["KUBECONFORM"], "-summary", "-strict", "-ignore-missing-schemas", *files], check=True)
    print("Validation passed; host provisioning and LAN reachability still require a real Debian cluster.")


if __name__ == "__main__":
    main()
