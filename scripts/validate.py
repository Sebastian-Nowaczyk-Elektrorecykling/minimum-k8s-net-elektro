#!/usr/bin/env python3
"""Build all reconciliation targets, substitute settings, and verify invariants.

Requires the test requirements, kustomize, helm and kubeconform. Built-in APIs
are validated against the pinned Kubernetes version, custom APIs against CRDs.
No cluster, root access, or host changes are needed.
"""
import json
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

BUILTIN_GROUPS = {
    "", "admissionregistration.k8s.io", "apiextensions.k8s.io", "apiregistration.k8s.io",
    "apps", "authentication.k8s.io", "authorization.k8s.io", "autoscaling", "batch",
    "certificates.k8s.io", "coordination.k8s.io", "discovery.k8s.io",
    "flowcontrol.apiserver.k8s.io", "networking.k8s.io", "node.k8s.io", "policy",
    "rbac.authorization.k8s.io", "resource.k8s.io", "scheduling.k8s.io", "storage.k8s.io",
}


def validate_gateway_only(docs):
    """Reject Ingress routing in authored resources and rendered Helm output."""
    for doc in docs:
        if doc["kind"] in ("Ingress", "IngressClass"):
            raise ValueError(f"Use Gateway API instead of {doc['kind']}: {doc['metadata']['name']}")
        if doc["apiVersion"].startswith("gateway.networking.k8s.io/") and doc["apiVersion"] != "gateway.networking.k8s.io/v1":
            raise ValueError(f"Use the stable Gateway API v1: {doc['kind']}/{doc['metadata']['name']}")
        annotations = doc.get("metadata", {}).get("annotations") or {}
        if any(key == "kubernetes.io/ingress.class" or key.startswith((
                "nginx.ingress.kubernetes.io/", "ingress.cilium.io/",
                "traefik.ingress.kubernetes.io/")) for key in annotations):
            raise ValueError(f"Unsupported Ingress annotation on {doc['kind']}/{doc['metadata']['name']}")
        for solver in doc.get("spec", {}).get("acme", {}).get("solvers", []):
            if "ingress" in solver.get("http01", {}):
                raise ValueError("ACME HTTP-01 must use Gateway HTTPRoute, not an Ingress solver")


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


def validate_gateway_domains(docs, c):
    gateway = next(d for d in docs if d["kind"] == "Gateway")
    assert "addresses" not in gateway["spec"], "Host-network gateway must not request a VIP"
    listeners = {listener["name"]: listener for listener in gateway["spec"]["listeners"]}
    domains = {"apps-https": c["domain"], "admin-https": f"admin.{c['domain']}",
               "testing-https": f"testing.{c['domain']}", "staging-https": f"staging.{c['domain']}"}
    certificate = next(d for d in docs if d["kind"] == "Certificate" and
                       d["metadata"]["name"] == "internal-wildcard")
    assert certificate["spec"]["secretName"] == "internal-wildcard-tls"
    for name, domain in domains.items():
        listener = listeners[name]
        assert listener["hostname"] == f"*.{domain}"
        assert listener["port"] == 443 and listener["protocol"] == "HTTPS"
        assert listener["tls"]["mode"] == "Terminate"
        assert listener["tls"]["certificateRefs"] == [{"kind": "Secret", "name": "internal-wildcard-tls"}]
        assert f"*.{domain}" in certificate["spec"]["dnsNames"], f"Missing TLS coverage for {domain}"
        scope = "administration" if name == "admin-https" else "applications"
        assert listener["allowedRoutes"]["namespaces"] == {
            "from": "Selector", "selector": {"matchLabels": {"elektro.internal/route-scope": scope}}}
    # Gateway HTTP hostname wildcards include multiple labels, so the shared
    # redirect covers the administration, testing and staging subdomains too.
    assert listeners["http"]["hostname"] == f"*.{c['domain']}"
    redirect = next(d for d in docs if d["kind"] == "HTTPRoute" and d["metadata"]["name"] == "redirect-https")
    assert redirect["spec"]["hostnames"] == [f"*.{c['domain']}"]


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
    # Examples are deployable inputs too; catch regressions before they are copied.
    example_text = substitute((ROOT / "examples/whoami.yaml").read_text(), settings)
    (cache / "example-whoami.yaml").write_text(example_text)
    example_docs = list(yaml.safe_load_all(example_text))
    validate_gateway_only(all_docs + example_docs)
    root = list(yaml.safe_load_all((cache / "root.yaml").read_text()))
    for name in ("network", "gateway", "admin", "apps"):
        step = next(d for d in root if d["kind"] == "Kustomization" and d["metadata"]["name"] == name)
        assert {h["kind"] for h in step["spec"]["healthCheckExprs"]} == {"GatewayClass", "Gateway", "HTTPRoute"}
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
    validate_gateway_domains(all_docs, c)
    renamed = dict(c, domain="example.test")
    renamed_settings = config.outputs(renamed)["clusters/lan/cluster-settings.yaml"]["data"]
    renamed_docs = []
    for path in ("infrastructure/gateway/gateway.yaml", "infrastructure/pki/certificates.yaml"):
        renamed_docs.extend(yaml.safe_load_all(substitute((ROOT / path).read_text(), renamed_settings)))
    validate_gateway_domains(renamed_docs, renamed)
    print("Validated application, admin, testing and staging HTTPS domains with a configurable base domain")
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
        validate_gateway_only([d for d in yaml.safe_load_all(output) if d])
        (cache / f"chart-{name}.yaml").write_text(output)
        print(f"Rendered {chart} {version}")
    # Future monitoring examples are never reconciled, but their selectors and
    # ports must match the Cilium endpoints we actually enable.
    cilium_docs = list(yaml.safe_load_all((cache / "chart-cilium.yaml").read_text()))
    cilium_config = next(d["data"] for d in cilium_docs if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cilium-config")
    assert cilium_config.get("enable-ingress-controller", "false") == "false"
    assert cilium_config["enable-gateway-api"] == "true"
    assert cilium_config["gateway-api-hostnetwork-enabled"] == "true"
    assert cilium_config["gateway-api-hostnetwork-nodelabelselector"] == "elektro.internal/edge=true"
    operator_role = next(d for d in cilium_docs if d and d["kind"] == "ClusterRole" and d["metadata"]["name"] == "cilium-operator")
    assert not any(r.startswith("ingresses") or r == "ingressclasses"
                   for rule in operator_role["rules"] for r in rule.get("resources", []))
    certificate_docs = [d for d in yaml.safe_load_all((cache / "chart-cert-manager.yaml").read_text()) if d]
    controller = next(d for d in certificate_docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "cert-manager")
    assert "--controllers=*,-ingress-shim" in controller["spec"]["template"]["spec"]["containers"][0]["args"]
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
                if not v["served"] or v.get("deprecated", False):
                    continue
                schema_by_gvk[(d["spec"]["group"] + "/" + v["name"], d["spec"]["names"]["kind"])] = v["schema"]["openAPIV3Schema"]
    # The kubeconform catalog omits the CRD definition schema for some releases.
    # Validate definitions against Kubernetes' own versioned OpenAPI instead of
    # globally ignoring missing schemas (which would hide real mistakes).
    kubernetes_tag = c["k3s_version"].split("+")[0]
    crd_url = (f"https://raw.githubusercontent.com/kubernetes/kubernetes/{kubernetes_tag}/"
               "api/openapi-spec/v3/apis__apiextensions.k8s.io__v1_openapi.json")
    with urllib.request.urlopen(crd_url, timeout=60) as response:
        crd_openapi = json.load(response)
    crd_validator = jsonschema.Draft7Validator({
        "$ref": "#/components/schemas/io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.CustomResourceDefinition",
        "components": crd_openapi["components"]})
    count = 0
    crd_count = 0
    builtins = []
    for d in all_docs + sso_docs + example_docs + [d for d in cilium_docs if d] + certificate_docs:
        if d["apiVersion"] == "apiextensions.k8s.io/v1" and d["kind"] == "CustomResourceDefinition":
            crd_validator.validate(d)
            crd_count += 1
            continue
        schema = schema_by_gvk.get((d["apiVersion"], d["kind"]))
        if schema:
            jsonschema.Draft7Validator(schema).validate(d)
            count += 1
        elif d["apiVersion"].partition("/")[0] in BUILTIN_GROUPS or d["apiVersion"] == "v1":
            builtins.append(d)
        else:
            raise ValueError(f"No served, non-deprecated CRD schema for {d['apiVersion']} {d['kind']}")
    print(f"Validated {count} custom resources against pinned CRD schemas")
    print(f"Validated {crd_count} CRD definitions against Kubernetes {kubernetes_tag} OpenAPI")
    builtin_file = cache / "builtin-resources.yaml"
    builtin_file.write_text(yaml.safe_dump_all(builtins))
    subprocess.run([os.environ.get("KUBECONFORM", "kubeconform"), "-summary", "-strict",
                    "-kubernetes-version", c["k3s_version"].split("+")[0][1:],
                    str(builtin_file)], check=True)
    print("Validation passed; host provisioning and LAN reachability still require a real Debian cluster.")


if __name__ == "__main__":
    main()
