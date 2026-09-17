# Hubble access and SSO

During bootstrap, `https://hubble.admin.internal` opens Hubble without a login.
Cilium's HTTPS gateway routes straight to `kube-system/hubble-ui:80`. The private
CA and wildcard certificate still apply; LAN DNS and the gateway still use
`192.168.2.153`. No Nginx authentication proxy or bootstrap password is created.

Hubble server, Relay, UI, TLS between agents and Relay, and metrics remain part
of the existing Cilium Helm release. SSO is added at the HTTP access layer.

## Ownership contract

| Resource | Owner |
| --- | --- |
| Cilium HelmRelease and Hubble components | This repository |
| HTTPS Gateway and certificate | This repository |
| HTTPRoute `administration/administration` | This repository's `admin` Flux Kustomization |
| ReferenceGrant `hubble-backend` in the selected backend namespace | The same `admin` Kustomization |
| Identity provider, SSO proxy Deployment/Service and its secrets | Your second Flux repository |

The second repository supplies an HTTP Service that authenticates requests and
proxies accepted requests to:

```text
http://hubble-ui.kube-system.svc.cluster.local:80
```

Use that internal upstream, not `https://hubble.admin.internal`, which would
send the proxy back to itself after the route switch. The proxy should support
Hubble UI's streaming connections. Configure its external URL as
`https://hubble.admin.internal`, its identity-provider issuer and callback URLs,
its session/client secrets, and your intended user/group access rules. If the
identity provider uses this cluster's private CA, the proxy must trust it.
Keep those provider-specific resources and configuration in the second repo.

Do not create a competing Hubble HTTPRoute or edit the chart-managed `hubble-ui`
Service. No Cilium values change or second Cilium installation is needed for SSO.
The second repository does not need to own resources in `kube-system` when its
proxy is deployed in `administration` or another chosen namespace.

## Switch to the SSO proxy

1. Deploy the identity provider and proxy from the second repository. Ensure its
   namespace exists, its Service has ready endpoints, and the intended Service
   port serves HTTP. If that namespace has network policies, permit gateway
   ingress to the proxy and proxy egress to Hubble, DNS and the identity provider.
2. Set the backend in **this repository's** `config/cluster.json`. For example,
   for a proxy Service named `hubble-sso` in `administration` exposing port 4180:

   ```json
   "hubble_backend_service": "hubble-sso",
   "hubble_backend_namespace": "administration",
   "hubble_backend_port": 4180
   ```

   The port is the Service's `spec.ports[].port`, which may differ from its Pod's
   container port. These are deployment settings, not credentials.
3. Run `python3 scripts/config.py generate` and `python3 scripts/config.py check`,
   then commit and push the JSON and generated settings. Flux updates the same
   HTTPRoute and creates a ReferenceGrant scoped to the selected Service. It
   prunes the old grant when the target namespace changes.
4. Check `Accepted=True` and `ResolvedRefs=True` on the route, then visit Hubble
   in a fresh browser session to verify login and the live flow view:

   ```bash
   sudo k3s kubectl -n administration describe httproute administration
   sudo k3s kubectl -n administration get referencegrant hubble-backend
   ```

The existing administration namespace already exists during bootstrap, making
it a convenient home for the proxy. A ReferenceGrant in that same namespace is
harmless; cross-namespace backends require it. The grant permits references only
from HTTPRoutes in `administration` to the one selected Service.

There is no automatic fallback to unauthenticated Hubble. Once the settings point
to the SSO proxy, deleting or stopping that proxy makes the endpoint unavailable.
Returning to direct access requires deliberately restoring the three bootstrap
backend settings in Git. Deleting the second repository's resources does not
reset these settings.

This protects the LAN web route. Kubernetes administrators with port-forward
access and trusted in-cluster callers can still reach internal Services according
to their RBAC/network permissions; SSO does not change Kubernetes authorization.

## Existing installations with Basic authentication

On reconciliation, Flux changes the existing administration route and prunes
its old `admin-proxy` Deployment, Service, generated ConfigMap and network policy.
There can be a brief interruption while Envoy adopts the updated route. Cilium
and the Hubble workloads are not reinstalled. Verify the direct bootstrap URL:

```bash
curl --cacert ca.crt -I --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal
```

Expect HTTP 200 without a Basic authentication challenge. Old
`administration/admin-credentials` and `/etc/elektro/secrets/{username,password,auth}`
are no longer used. They were created outside Flux inventories, so they are not
pruned automatically; the bootstrap script leaves existing copies untouched.
The CA files remain necessary for HTTPS.

## References

- [Gateway API cross-namespace references](https://gateway-api.sigs.k8s.io/api-types/referencegrant/)
- [Flux reconciliation and pruning](https://fluxcd.io/flux/components/kustomize/kustomizations/)
- [OAuth2 Proxy OpenID Connect integration](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/openid_connect/)
