# Gateway API

Cilium serves HTTP/HTTPS through Gateway API. The GatewayClass, Gateway,
HTTPRoutes and optional ReferenceGrants use `gateway.networking.k8s.io/v1`. All LAN web
traffic enters through `api_ip`, which defaults to `192.168.2.153`.

Cilium's Ingress controller and Hubble's chart-generated Ingress are disabled.
K3s disables Traefik. Cert-manager's `ingress-shim` is disabled; explicit
`Certificate` resources own TLS.

## Adding an application

Use [the whoami example](../examples/whoami.yaml) as the HTTPRoute pattern:

| Concern | Configuration |
| --- | --- |
| User application | Namespace label `elektro.internal/route-scope: applications`; parent listener `apps-https`; hostname such as `app.internal` |
| Testing application | Namespace label `elektro.internal/route-scope: applications`; parent listener `testing-https`; hostname such as `app.testing.internal` |
| Staging application | Namespace label `elektro.internal/route-scope: applications`; parent listener `staging-https`; hostname such as `app.staging.internal` |
| Administration tool | Namespace label `elektro.internal/route-scope: administration`; parent listener `admin-https`; hostname such as `tool.admin.internal` |
| Management tool | Namespace label `elektro.internal/route-scope: management`; parent listener `management-https`; hostname such as `openbudget.management.internal` |
| Gateway parent | `name: internal`, `namespace: gateway-system`, explicit `sectionName` |
| Service backend | `backendRefs` with the Service name and **Service port**, not container port |
| Cross-namespace backend | A narrowly scoped ReferenceGrant in the backend namespace |
| HTTPS certificate | Gateway's `internal-wildcard-tls` Secret, renewed by the explicit Certificate |
| HTTP redirect | Shared `redirect-https` HTTPRoute on port 80 |

Keep hostnames to one label below their chosen suffix: `internal`,
`admin.internal`, `management.internal`, `testing.internal` or `staging.internal`.
The gateway certificate includes a separate wildcard for each suffix because certificate
wildcards only cover one label. The generated `DOMAIN`, `ADMIN_DOMAIN`,
`MANAGEMENT_DOMAIN`, `TESTING_DOMAIN` and `STAGING_DOMAIN` settings all follow
`domain` in `config/cluster.json`; changing it to `example.test` gives
`management.example.test`, `testing.example.test` and `staging.example.test`
automatically.

For a testing application Service named `my-app` exposing port 80 in a namespace
labelled `elektro.internal/route-scope: applications`, use this HTTPRoute spec:

```yaml
spec:
  parentRefs:
    - name: internal
      namespace: gateway-system
      sectionName: testing-https
  hostnames: ['my-app.${TESTING_DOMAIN}']
  rules:
    - backendRefs: [{name: my-app, port: 80}]
```

Use `staging-https` and `${STAGING_DOMAIN}` for staging. Select the corresponding
listener explicitly, since it takes precedence over the broader `apps-https`
hostname. Flux substitutes these variables for this repository's targets;
another repository must supply its own substitution settings or use the actual
hostnames. DNS suffixes do not create application deployments or isolate their
data; choose namespaces, releases and policies for each workload as needed.

Disable `ingress.enabled` in application Helm charts and create an HTTPRoute
in the application's owning repository. Configure redirects, rewrites and
headers with Gateway API filters supported by the pinned Cilium version.

The shared Gateway owns host ports 80/443 on the edge node. Attach additional
HTTPRoutes to its listeners; a second Gateway on those ports would conflict.
DNS directs private names to the edge address, but a Service and HTTPRoute
must exist before an application can answer requests.
The existing HTTP redirect and LAN forwarding rule for `internal` cover all
five suffixes. No additional LAN address or router forwarding rule is needed.

## Management tools

Use `*.management.internal` for business tools intended for management users,
such as a future OpenBudget deployment. The `management` namespace is provided
with `elektro.internal/route-scope: management`; other tool namespaces can use
the same label. Only namespaces with this label can attach routes to the
`management-https` listener. Keep application Deployments, Services and routes
in the repository that owns the tool.

For example, a Service named `my-tool` exposing port 80 in `management` can use
this route:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: my-tool
  namespace: management
spec:
  parentRefs:
    - name: internal
      namespace: gateway-system
      sectionName: management-https
  hostnames: ['my-tool.${MANAGEMENT_DOMAIN}']
  rules:
    - backendRefs: [{name: my-tool, port: 80}]
```

Use the deployed tool's actual Service name and port. A separate Flux repository
must provide `MANAGEMENT_DOMAIN` itself or use the literal hostname. This
baseline supplies DNS, TLS, the listener and namespace; tools are deployed
separately. Authorize management users in the tool or its access
layer. The DNS suffix and namespace selector do not authenticate users or
restrict which LAN clients can connect.

See [Gateway route attachment](https://gateway-api.sigs.k8s.io/guides/user-guides/multiple-ns/)
for namespace selection and ownership of shared gateways.

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

For an end-to-end Hubble test, first run `sudo ./scripts/hubble-route.py add`
on a k3s server. Then test DNS and HTTP from a LAN client, using the public CA
certificate:

```bash
dig @192.168.2.153 hubble.admin.internal A +short
dig @192.168.2.153 openbudget.management.internal A +short
dig @192.168.2.153 app.testing.internal A +short
dig @192.168.2.153 app.staging.internal A +short
curl --cacert ca.crt --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal/
curl -I --resolve hubble.admin.internal:80:192.168.2.153 \
  http://hubble.admin.internal/
```

While the test route exists, expect Hubble UI over HTTPS and an HTTPS redirect
over HTTP. Finish with `sudo ./scripts/hubble-route.py remove` on the server.
Without a route, DNS still resolves but HTTPS does not serve Hubble. Repeat
with each application's hostname. API status checks do not prove backend
availability, certificate trust or end-to-end connectivity.

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
The [Gateway hostname rules](https://gateway-api.sigs.k8s.io/docs/concepts/hostnames/)
explain listener precedence and the difference between routing and certificate
wildcards.
