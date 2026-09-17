# Debian 13 · k3s · Cilium · Flux

A configurable LAN cluster with Debian host provisioning, Cilium networking,
Hubble and GitOps. **`192.168.2.153` is the only required static LAN address.**
The Kubernetes API, LAN DNS and HTTP/HTTPS gateway all use that node. Additional
controllers, workers and hybrids can use DHCP.

| Setting | Default |
| --- | --- |
| LAN | `192.168.0.0/20` |
| API / joining endpoint | `192.168.2.153:6443` |
| LAN DNS | `192.168.2.153:53`, TCP and UDP |
| HTTP / HTTPS gateway | `192.168.2.153:80` / `:443` |
| Application names | `*.internal` → `192.168.2.153` |
| Administration names | `*.admin.internal` → `192.168.2.153` |
| Pod / Service networks | `10.42.0.0/16` / `10.43.0.0/16` |

Cilium includes Hubble UI/Relay, flow metrics, agent/operator/Envoy metrics and
its bundled dashboard definitions. **No Prometheus, Grafana, Alertmanager,
Longhorn, GPU Operator or GPU device plugin is installed.** Longhorn and GPU
preparation is limited to host prerequisites. Cert-manager handles private TLS.
Hubble UI is available directly over HTTPS without a login during bootstrap;
its gateway backend can be switched to an SSO proxy later.

HTTP/HTTPS routing uses **Gateway API v1**. Cilium Ingress, Hubble Ingress and
cert-manager ingress-shim are explicitly disabled. See the
[Gateway guide](docs/gateway.md) for routes and readiness checks.

For a replacement cluster or a copy at a different Git URL, follow
[Reusing this repository](docs/reusing-repository.md). It covers retiring the
old cluster, preserving manifests, regenerating `clusters/lan/flux-system`,
and publishing a clean repository with its own history.

## Configure and bootstrap

Use fresh Debian 13 hosts with unique hostnames and mutually reachable LAN
addresses. Configure `.153/20` on the first host with your normal Debian network
manager, and exclude it from the DHCP pool. The scripts do not overwrite host
network configuration or router settings. Free TCP/UDP 53 and TCP 80/443 on that
host; bootstrap checks for existing listeners. Use `ip -br address` to find its
interface name. Consult [firewall requirements](docs/operations.md#firewall-and-routing).

Clone this repository on each host (install `git` and `ca-certificates` first if
needed). All commands below run from its root. Review `config/cluster.json`;
it is the source for IPs, DNS, repository/branch and pinned versions. To change it:

```bash
python3 scripts/config.py generate
python3 scripts/config.py check
# Commit and push the JSON and generated files to the configured Git branch.
```

Bootstrap requires a clean checkout at the same commit as the configured remote
branch, which defaults to `main`. Commit and push configuration before running
bootstrap so Flux adopts exactly the configuration used to create the cluster.

On the `.153` host, replace `eno1` with its actual LAN interface:

```bash
sudo ./scripts/bootstrap-hybrid.sh --interface eno1
```

This prepares the host, starts an embedded-etcd k3s server that also runs
workloads, installs the pinned Cilium release, creates the private CA,
and installs Flux. Flux then adopts Cilium and reconciles DNS, TLS, gateway and
administration resources from this repository. NVIDIA driver installation may
require a reboot; after `nvidia-smi` works, rerun the command. Use
`sudo GPU_VENDOR=none ...` on a host where GPU preparation is not wanted.

If cluster software setup is interrupted after k3s has started, resume from a
clean, up-to-date checkout with `sudo ./scripts/bootstrap-cluster.sh`.

For a first **dedicated controller** instead:

```bash
sudo ./scripts/install-controller.sh --bootstrap --interface eno1
sudo ./scripts/bootstrap-cluster.sh
```

Essential cluster components tolerate the controller taint; ordinary apps need
a worker or hybrid. No second static address or LoadBalancer address pool is
required. Because all LAN entry points are on `.153`, that host is a single point
of failure for API entry, DNS and web access, even with additional etcd servers.

## Join nodes

Copy a join token securely to a root-only file outside this repo. Servers need
`/var/lib/rancher/k3s/server/token`; workers can use
`/var/lib/rancher/k3s/server/agent-token`. Keep tokens out of Git and command-line
arguments. On separate hosts, using their actual LAN interfaces:

```bash
sudo ./scripts/install-controller.sh --interface eno1 --token-file /root/elektro-join.token
sudo ./scripts/install-hybrid.sh --interface eno1 --token-file /root/elektro-join.token
sudo ./scripts/install-worker.sh --interface eno1 --token-file /root/elektro-join.token
```

Joining scripts discover the interface's usable LAN IPv4 address. A systemd
pre-start helper refreshes k3s `node-ip` from that interface on every service
start. If an interface has multiple LAN addresses, use `--node-ip IP` to select
an already assigned fixed address. No DHCP address is hardcoded in Git. An
address change while a node is running requires a controlled k3s restart; see
[operations](docs/operations.md#dhcp-and-node-address-changes).

| Script | Role |
| --- | --- |
| `install-controller.sh` | Joining server + etcd, tainted against ordinary workloads |
| `install-worker.sh` | Agent running workloads |
| `install-hybrid.sh` | Joining server + etcd + workloads |
| `bootstrap-hybrid.sh` | First hybrid and full Flux bootstrap |
| `install-controller.sh --bootstrap` | First dedicated controller; follow with `bootstrap-cluster.sh` |
| `prepare-host.sh` | Host prerequisites only |
| `configure-power.sh` | Apply the node power policy without reinstalling k3s |
| `check-gateway.sh` | Read-only Gateway API readiness and routing checks |
| `remove-node.sh` | Safety checks, drain, cluster removal and remote uninstall |

Dedicated controllers retain kubelet/Cilium. Add two more servers for a
three-member etcd cluster; two members do not provide failure tolerance.

Host preparation keeps every node awake when idle or when its lid closes,
including on battery, external power, or a dock. Sleep/hibernate keys and
desktop suspend requests are disabled. A **short press of the physical power
button suspends** the machine. To apply this to an existing node after pulling
the repository, run `sudo ./scripts/configure-power.sh`. See the
[power policy](docs/operations.md#node-power-policy) for existing desktop
sessions and verification. `--skip-host-preparation` also skips this policy.

## LAN DNS and HTTPS

Configure your router/resolver to **conditionally forward `internal` to
`192.168.2.153`**, or advertise **`192.168.2.153` as the DNS server through DHCP**.
The resolver forwards other names to `upstream_dns`. Public secondary DNS
servers cannot resolve the private suffix; clients may choose them first.
Avoid an upstream resolver that forwards all queries back to this cluster.

Cluster CoreDNS forwards `.internal` to the dedicated DNS Service; Kubernetes
`cluster.local` keeps its usual meaning. All private A records, including
`api.internal`, `ns.internal` and both wildcard families, answer `.153`. IPv6
queries for these IPv4-only names receive authoritative empty answers. DNS
alone does not deploy an application; each application also needs an HTTPRoute.
The gateway handles HTTP/HTTPS, not LAN default routing or NAT.

Install the public certificate `/etc/elektro/secrets/ca.crt` in client trust
stores. On a Debian client, after securely copying that certificate:

```bash
sudo install -m 0644 ca.crt /usr/local/share/ca-certificates/elektro.crt
sudo update-ca-certificates
```

Cert-manager renews the `*.internal` and `*.admin.internal` gateway certificate.
Browsers with independent trust stores may need an import too. Back up the CA
key securely; never distribute it or commit it.

Open **`https://hubble.admin.internal`** for Hubble's flow view and service map.
There is **no username/password prompt during bootstrap**. The gateway routes
directly to Cilium's `hubble-ui` Service, using the same private HTTPS certificate.

When your SSO platform is ready, deploy its authentication proxy from your
second Flux repository and set these fields in `config/cluster.json` to that
proxy's Service:

| Setting | Bootstrap default |
| --- | --- |
| `hubble_backend_service` | `hubble-ui` |
| `hubble_backend_namespace` | `kube-system` |
| `hubble_backend_port` | `80` |

Regenerate and commit the configuration to switch the existing route. Hubble
itself stays in the Cilium release. [docs/hubble-sso.md](docs/hubble-sso.md)
describes resource ownership, proxy requirements and how to connect SSO.

## Applications, storage and GPUs

Add application manifests to `apps/` and `apps/kustomization.yaml`. Application
namespaces carry `elektro.internal/route-scope: applications`; administration
namespaces use `administration`. [examples/whoami.yaml](examples/whoami.yaml)
provides an opt-in application and HTTPRoute at `https://whoami.internal`.

Host preparation installs Longhorn V1 prerequisites: iSCSI, NFS clients,
cryptsetup, dmsetup and filesystem utilities. It loads required modules and
starts iscsid, without formatting disks. NVIDIA hosts get a Debian driver and
NVIDIA Container Toolkit; k3s discovers the runtime in its bundled containerd.
AMD/Intel hosts get firmware for their kernel drivers. Userspace compute/media
libraries belong in workload images.

GPU resources only become schedulable after you separately deploy a compatible
device plugin/operator. NVIDIA workloads normally use `runtimeClassName: nvidia`.
Longhorn V2/SPDK requires additional hardware-specific preparation. Driver,
Secure Boot and storage details are in [operations](docs/operations.md).

## Future monitoring and validation

The only deployed network observability tools are Cilium's Hubble components.
Metrics endpoints and bundled dashboard ConfigMaps are ready for a future
collector. Optional [monitor examples](examples/monitoring/cilium-monitors.yaml)
are **not referenced by Flux** and require your future Prometheus Operator.
See [Cilium observability](docs/operations.md#cilium-observability) for ports and
CLI access. No time-series storage or alert processing is deployed.

```bash
sudo ./scripts/status.sh
sudo k3s kubectl get pods -A
```

For local repository checks, install Helm, Kustomize, kubeconform and ShellCheck,
then:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
shellcheck -x -P SCRIPTDIR scripts/*.sh scripts/lib/*.sh
python3 scripts/validate.py
```

Set `COREDNS` to a local CoreDNS binary to run the DNS protocol tests; CI does so.
Validation builds every Flux target, renders pinned charts, checks network and
ownership invariants, and validates custom resources against pinned CRDs.
Physical Debian provisioning, GPU compatibility and LAN reachability still
require the [first-run acceptance checks](docs/operations.md#first-run-acceptance).
