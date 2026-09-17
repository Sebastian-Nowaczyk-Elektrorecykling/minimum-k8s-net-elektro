# Gateway API and migration from Ingress

This repository uses Cilium Gateway API for HTTP/HTTPS. Its GatewayClass,
Gateway, HTTPRoutes and ReferenceGrant use `gateway.networking.k8s.io/v1`.
There were no Ingress or IngressClass objects in the audited baseline
`6875354`; no application routes needed conversion.

Kubernetes' Ingress API is **frozen, not removed**. The separate ingress-nginx
project retired in March 2026. Gateway API is the recommended direction for new
routing. See [Kubernetes Ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/)
and the [ingress-nginx retirement announcement](https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/).

The base configuration explicitly disables Cilium's Ingress controller and
Hubble's chart-generated Ingress. K3s disables Traefik. Cert-manager's
`ingress-shim` is disabled; explicit `Certificate` resources own TLS. Neither an
Ingress controller nor an Ingress annotation is required for private TLS.

## Adding an application

Use [the whoami example](../examples/whoami.yaml) as the HTTPRoute pattern:

| Concern | Configuration |
| --- | --- |
| Public application | Namespace label `elektro.internal/route-scope: applications`; parent listener `apps-https`; hostname such as `app.internal` |
| Administration tool | Namespace label `elektro.internal/route-scope: administration`; parent listener `admin-https`; hostname such as `tool.admin.internal` |
| Gateway parent | `name: internal`, `namespace: gateway-system`, explicit `sectionName` |
| Service backend | `backendRefs` with the Service name and **Service port**, not container port |
| Cross-namespace backend | A narrowly scoped ReferenceGrant in the backend namespace |
| HTTPS certificate | Gateway's `internal-wildcard-tls` Secret, renewed by the explicit Certificate |
| HTTP redirect | Shared `redirect-https` HTTPRoute on port 80 |
| Authentication | An SSO proxy backend as described in [Hubble SSO](hubble-sso.md) |

Keep application hostnames to one label below `internal`, and admin hostnames
to one label below `admin.internal`: wildcard certificate matching only covers
one label. Do not put NGINX, Traefik or Ingress-class annotations on HTTPRoutes.
Disable `ingress.enabled` in application Helm charts and create an HTTPRoute
in the application's owning repository. Network-policy `ingress` fields and
Cilium's reserved `ingress` identity describe incoming traffic; they are not
the Kubernetes Ingress resource and must not be removed.

## Existing Ingress resources from another repository or installation

Inventory before rolling out changes if other software has been installed:

```bash
sudo k3s kubectl get ingress,ingressclass -A
sudo k3s kubectl get helmrelease -A
sudo k3s kubectl get deployment,daemonset -A
sudo k3s kubectl get certificate -A
```

For each legacy route, identify its Flux/Helm owner, hostname, path matching,
backend Service/port, TLS certificate and controller-specific annotations.
Replace it in that owner's repository with an HTTPRoute attached to the shared
Gateway. Translate redirects, rewrites and headers to Gateway API filters;
implement external authentication through the chosen SSO proxy. Preserve an
explicit Certificate for any certificate previously created by ingress-shim.
Cilium's [migration guide](https://docs.cilium.io/en/stable/network/servicemesh/ingress-to-gateway/ingress-to-gateway/)
describes the supported mappings and limitations.

Keep all production names pointed at `192.168.2.153`; no spare static IP is
needed. Two controllers cannot both bind host ports 80/443 on that node. If a
legacy controller currently owns those ports, test the replacement on temporary
high ports (or another test host), then schedule the port handover. The base
Gateway is configured for 80/443; adding an unplanned second Gateway on the same
ports causes conflicts.

After the Gateway/HTTPRoute replacement serves the expected traffic, remove
the old Ingress and its controller through their owning repository or release.
Do not delete all Ingresses or uninstall unrelated releases blindly. A Flux
repository can recreate an object removed only with kubectl. Changing this
repository cannot convert resources owned by a different repository.

## Verify after reconciliation

```bash
git pull --ff-only
sudo ./scripts/check-gateway.sh
sudo ./scripts/status.sh
```

The read-only audit rejects remaining Ingress/IngressClass objects, an active
Cilium Ingress controller, incorrect edge selection, stale GatewayClass/Gateway
conditions, broken listeners and HTTPRoute parent/backend references. Bootstrap
runs the same check after Flux is ready. Flux has equivalent CEL health checks
for GatewayClass, Gateway and HTTPRoute, so missing references and stale
conditions no longer count as a healthy deployment.

Then test real DNS and HTTP from a LAN client, using the public CA certificate:

```bash
dig @192.168.2.153 hubble.admin.internal A +short
curl --cacert ca.crt --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal/
curl -I --resolve hubble.admin.internal:80:192.168.2.153 \
  http://hubble.admin.internal/
```

Expect the Hubble UI (or your configured SSO flow) over HTTPS and an HTTPS
redirect over HTTP. Repeat with each migrated application. API status checks
do not prove backend availability, certificate trust or end-to-end connectivity.

## Validation policy

CI builds Flux targets, deployable examples and both pinned Helm charts. It
rejects Ingress/IngressClass resources, legacy routing annotations and Ingress
ACME solvers, and verifies the rendered Cilium and cert-manager controller
configuration. Gateway API resources must use stable v1; Kubernetes built-ins
are checked against the pinned Kubernetes version and custom resources against
served, non-deprecated versions of the pinned CRDs. Unknown custom schemas fail
validation. These checks cover this repository; other Flux repositories need
their own equivalent gates.

Gateway API CRDs are installed and established before Cilium starts. Their Flux
target does not prune CRDs. When upgrading an older cluster's CRDs, review
`status.storedVersions` and upstream storage-version migration instructions
before removing a served/storage version. Do not delete CRDs to fix a version
conflict; that would delete their resources.
