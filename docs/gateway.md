# Gateway API

Cilium serves HTTP/HTTPS through Gateway API. The GatewayClass, Gateway,
HTTPRoutes and ReferenceGrant use `gateway.networking.k8s.io/v1`. All LAN web
traffic enters through `api_ip`, which defaults to `192.168.2.153`.

Cilium's Ingress controller and Hubble's chart-generated Ingress are disabled.
K3s disables Traefik. Cert-manager's `ingress-shim` is disabled; explicit
`Certificate` resources own TLS.

## Adding an application

Use [the whoami example](../examples/whoami.yaml) as the HTTPRoute pattern:

| Concern | Configuration |
| --- | --- |
| User application | Namespace label `elektro.internal/route-scope: applications`; parent listener `apps-https`; hostname such as `app.internal` |
| Administration tool | Namespace label `elektro.internal/route-scope: administration`; parent listener `admin-https`; hostname such as `tool.admin.internal` |
| Gateway parent | `name: internal`, `namespace: gateway-system`, explicit `sectionName` |
| Service backend | `backendRefs` with the Service name and **Service port**, not container port |
| Cross-namespace backend | A narrowly scoped ReferenceGrant in the backend namespace |
| HTTPS certificate | Gateway's `internal-wildcard-tls` Secret, renewed by the explicit Certificate |
| HTTP redirect | Shared `redirect-https` HTTPRoute on port 80 |
| Authentication | An SSO proxy backend as described in [Hubble SSO](hubble-sso.md) |

Keep application hostnames to one label below `internal`, and admin hostnames
to one label below `admin.internal`: wildcard certificate matching only covers
one label. Substitute your configured domain if it differs from the default.
Disable `ingress.enabled` in application Helm charts and create an HTTPRoute
in the application's owning repository. Configure redirects, rewrites and
headers with Gateway API filters supported by the pinned Cilium version.

The shared Gateway owns host ports 80/443 on the edge node. Attach additional
HTTPRoutes to its listeners; a second Gateway on those ports would conflict.
DNS directs private names to the edge address, but a Service and HTTPRoute
must exist before an application can answer requests.

## Verify routing

Run on a controller after Flux reconciliation:

```bash
sudo ./scripts/check-gateway.sh
sudo ./scripts/status.sh
```

The read-only checks reject Ingress/IngressClass resources, an active Cilium
Ingress controller, incorrect edge selection, stale GatewayClass/Gateway
conditions, broken listeners and HTTPRoute parent/backend references. Bootstrap
runs the same check after Flux is ready. Flux uses CEL health checks for
GatewayClass, Gateway and HTTPRoute that require current-generation conditions
and valid references.

Then test DNS and HTTP from a LAN client, using the public CA certificate:

```bash
dig @192.168.2.153 hubble.admin.internal A +short
curl --cacert ca.crt --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal/
curl -I --resolve hubble.admin.internal:80:192.168.2.153 \
  http://hubble.admin.internal/
```

Expect the Hubble UI (or your configured SSO flow) over HTTPS and an HTTPS
redirect over HTTP. Repeat with each application's hostname. API status checks
do not prove backend availability, certificate trust or end-to-end connectivity.

## Validation and maintenance

CI builds Flux targets, deployable examples and both pinned Helm charts. It
rejects Ingress/IngressClass resources, Ingress routing annotations and Ingress
ACME solvers, and verifies the rendered Cilium and cert-manager controller
configuration. Gateway API resources must use stable v1; Kubernetes built-ins
are checked against the pinned Kubernetes version and custom resources against
served, non-deprecated versions of the pinned CRDs. Unknown custom schemas fail
validation. Other Flux repositories need their own equivalent checks.

Network-policy `ingress` fields and Cilium's reserved `ingress` identity describe
incoming traffic; they are unrelated to the Kubernetes Ingress resource.

Gateway API CRDs are installed and established before Cilium starts. Their Flux
target does not prune CRDs. For CRD upgrades, review `status.storedVersions` and
the upstream storage-version requirements before removing a served/storage
version. Deleting a CRD deletes its resources.

See [Cilium Gateway API](https://docs.cilium.io/en/stable/network/servicemesh/gateway-api/gateway-api/)
and [Gateway API reference permissions](https://gateway-api.sigs.k8s.io/api-types/referencegrant/)
for supported routing and cross-namespace references.
