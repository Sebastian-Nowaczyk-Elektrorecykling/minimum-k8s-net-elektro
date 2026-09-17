# Operations

## First-run acceptance

Run these on the bootstrap server after Flux has reconciled:

```bash
sudo ./scripts/status.sh
sudo k3s kubectl get pods -A
sudo k3s kubectl -n kube-system get helmrelease cilium
sudo k3s kubectl -n gateway-system describe gateway internal
sudo k3s kubectl -n gateway-system get certificate internal-wildcard
sudo k3s kubectl -n administration describe httproute administration
sudo k3s kubectl get referencegrant hubble-backend -n kube-system
sudo k3s kubectl get nodes -l elektro.internal/edge=true -o wide
sudo k3s kubectl -n lan-system get pods -o wide
```

Check Gateway conditions `Accepted=True` and `Programmed=True`, and HTTPRoute
parent conditions `Accepted=True` and `ResolvedRefs=True`. All Flux Kustomizations
and HelmReleases should become Ready. The Cilium Helm release must be the same
release created by bootstrap (`helm history cilium -n kube-system`).

From a LAN client with `dig` installed, check both DNS transports and private
AAAA behavior:

```bash
dig @192.168.2.153 hubble.admin.internal A +short
dig @192.168.2.153 app.internal A +tcp +short
dig @192.168.2.153 api.internal A +short
dig @192.168.2.153 app.internal AAAA
dig @192.168.2.153 debian.org A +short

curl --cacert ca.crt -I --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal   # expect 200; no login during bootstrap
```

All three A answers should be `192.168.2.153`.
Private AAAA queries should have no external answer. The app hostname will give
an HTTP 404 until an HTTPRoute exists. To deploy a smoke-test application:

```bash
cp examples/whoami.yaml apps/whoami.yaml
# Add whoami.yaml to resources in apps/kustomization.yaml, commit and push.
```

Check `https://whoami.internal` from the LAN and `.internal` lookups from an
application Pod. Reboot the bootstrap host to verify persistent setup, then
join at least one worker and repeat cross-node traffic tests. The API, DNS and
gateway all depend on the `.153` node; they do not fail over to DHCP nodes.
For a configured SSO backend, inspect `hubble-backend` in that backend
namespace and expect the configured login flow instead of an anonymous 200.
See [Hubble SSO](hubble-sso.md) for switching the route and upgrading from the
previous Basic authentication proxy.

## LAN DNS fails with `exec /coredns: operation not permitted`

The upstream CoreDNS image marks `/coredns` with the file capability
`cap_net_bind_service=ep`. Dropping that capability from the container's bounding
set can make Linux reject execution with EPERM, before the Corefile is read.
Listening on port 1053 does not avoid this executable-file check. See the
[pinned image Dockerfile](https://github.com/coredns/coredns/blob/v1.14.7/Dockerfile)
and [Linux capability execution checks](https://man7.org/linux/man-pages/man7/capabilities.7.html).

The deployment drops all capabilities and adds back only `NET_BIND_SERVICE`.
It still runs as UID/GID 65532, with privilege escalation disabled, a read-only
root filesystem and RuntimeDefault seccomp. CI executes the actual container
image with these settings as well as testing DNS queries against CoreDNS.

Flux will reconcile the corrected manifest from `main`. If DNS needs immediate
recovery before reconciliation, apply the same change to the running deployment:

```bash
sudo k3s kubectl -n lan-system patch deployment lan-dns --type=strategic \
  -p '{"spec":{"template":{"spec":{"containers":[{"name":"coredns","securityContext":{"capabilities":{"drop":["ALL"],"add":["NET_BIND_SERVICE"]}}}]}}}'
sudo k3s kubectl -n lan-system rollout status deployment/lan-dns --timeout=180s
```

This rolls the DNS pod. Ensure Flux has fetched the fixed Git revision so it
does not restore the old security context. No host security policy changes are
needed for this image/manifest mismatch.

## Cilium observability

For deeper diagnostics, install the upstream Cilium and Hubble CLIs on an
administration machine, and run `cilium status --wait` and
`cilium connectivity test` with its kubeconfig. The latter creates temporary
test workloads. To observe via the CLI without exposing gRPC on the LAN:

```bash
sudo k3s kubectl -n kube-system port-forward service/hubble-relay 4245:80
# In another terminal with the Hubble CLI:
hubble observe --server localhost:4245 --follow
```

Cilium agent/operator/Envoy metrics, Hubble flow metrics and Relay metrics are
enabled. The Cilium chart also installs its built-in dashboard ConfigMaps for a
future dashboard server. No collector, Grafana, Prometheus or Alertmanager runs
in this cluster.

| Endpoint | Port |
| --- | --- |
| Cilium agent | 9962 |
| Cilium operator | 9963 |
| Cilium Envoy | 9964 |
| Hubble metrics | 9965 |
| Hubble Relay metrics | 9966 |

Connect a future collector inside the cluster or through a secured connection.
`examples/monitoring/cilium-monitors.yaml` contains optional PodMonitor and
ServiceMonitor objects with validated chart selectors/ports. Apply them only
after installing a compatible Prometheus Operator and configuring its selectors
to discover them; Flux does not deploy these examples. Keep metrics private.

Network L3/L4 flows are available immediately; detailed HTTP/DNS L7 events require corresponding Cilium
L7 policies/visibility rules for the selected workloads. Start with scoped
policies; do not turn on cluster-wide L7 interception without testing.

## Firewall and routing

The scripts do not flush or replace an existing host/router firewall. Permit
these paths in your firewall while keeping management and metrics limited to
the appropriate administrators/nodes. Cilium VXLAN requires MTU headroom (usually
50 bytes); auto-detection is used. Override Cilium values if the underlay needs
a smaller MTU. Cilium Gateway host networking binds all interfaces on the edge
node, so restrict web ports to the intended LAN with your firewall.

| Traffic | Protocol/port | Sources and destinations |
| --- | --- | --- |
| SSH maintenance | TCP 22 | administrators → hosts |
| k3s API/registration | TCP 6443 | cluster nodes and administrators → servers |
| etcd | TCP 2379–2380 | servers → servers only |
| kubelet | TCP 10250 | control plane/metrics → nodes |
| Cilium VXLAN | UDP 8472 | cluster nodes ↔ cluster nodes |
| Cilium health | TCP 4240 and ICMP | cluster nodes ↔ cluster nodes |
| Hubble peers | TCP 4244 | Hubble relay/cluster nodes → nodes |
| Cilium metrics | TCP 9962–9966 | future authorized collector → relevant endpoints |
| LAN DNS | TCP/UDP 53 | LAN → `192.168.2.153` |
| Web gateway | TCP 80/443 | LAN → `192.168.2.153` |
| Package/image/Git downloads | TCP 443 (and configured Debian mirrors) | nodes/pods → upstream services |
| DNS upstreams / time | TCP/UDP 53, UDP 123 | resolver/nodes → configured upstreams |

No extra LAN addresses are allocated. Only the bootstrap host has the
`elektro.internal/edge=true` label. Do not put that label on DHCP nodes. On
multi-NIC hosts, configure `cilium_devices` to include the interfaces carrying
node traffic. Node IPs must remain reachable from each other.

## DHCP and node address changes

Without `--node-ip`, a joining script selects the one usable IPv4 address from
`lan_cidr` on the specified interface. It saves the selection policy in
`/etc/elektro/node-network.json`, and installs a systemd pre-start helper that
updates only `node-ip` in k3s's config before every service start. If DHCP has not
assigned a suitable address yet, startup fails and systemd retries; it never
silently picks an address on another network. Explicit `--node-ip` opts into a
fixed address and verifies it is still assigned at startup. The bootstrap node
always uses the configured static `api_ip`.

The helper runs at service start, not at every DHCP renewal. Keep leases stable
during normal operation. If a running node changes address, drain it when
possible, restart `k3s` (server/hybrid) or `k3s-agent` (worker), then confirm its
InternalIP, Cilium health and cross-node connectivity before uncordoning it.
Restart one server at a time, and check etcd health/membership before touching
the next. This refresh does not promise uninterrupted sessions or automatic
recovery of a damaged etcd quorum. Re-running an installation script also
restarts its k3s service and must be planned accordingly.

## Longhorn and GPU readiness

Longhorn's default filesystem path is `/var/lib/longhorn`. Mount a suitable
dedicated ext4/XFS filesystem there before adding Longhorn if desired. The
scripts create the directory without formatting or consuming a disk. Validate
mount propagation and available capacity against the Longhorn version you
choose. A future Flux HelmRelease can install Longhorn independently.

```bash
systemctl is-active iscsid
lsmod | grep iscsi_tcp
iscsiadm --version
mount.nfs -V
cryptsetup --version
dmsetup version
findmnt /var/lib/longhorn
```

If multipathd is installed, apply Longhorn's documented device exclusions for
its iSCSI disks. Do not disable multipath blindly on a SAN host. Longhorn's V2
SPDK engine additionally needs dedicated CPU/hugepages and hardware-specific
kernel preparation; it is intentionally not enabled by this V1 baseline.

`GPU_VENDOR=auto` selects NVIDIA, then AMD, then Intel among PCI display-class
devices. For mixed-vendor compute hosts, run `prepare-host.sh` explicitly for
each needed vendor before joining, then use `--skip-host-preparation`.

For NVIDIA, `nvidia-smi` must succeed before k3s starts. The script installs the
Debian driver and matching running-kernel headers, then the pinned NVIDIA
Container Toolkit. An unavailable `linux-headers-$(uname -r)` package means you
should update/reboot into an available Debian kernel first. Secure Boot may
require enrolling the Debian DKMS MOK and rebooting. Older/newer GPUs may need a
different supported driver; `NVIDIA_DRIVER_PACKAGE` selects a Debian package.
The automated proprietary NVIDIA driver path is amd64 only. On ARM/SBSA/Jetson,
provision the platform-supported driver/runtime separately and use
`GPU_VENDOR=none` to preserve it.

Do not run generic `nvidia-ctk runtime configure` against `/etc/containerd` for
this k3s installation. K3s owns containerd and discovers the installed NVIDIA
runtime when its service starts. Verify:

```bash
nvidia-smi
sudo grep nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml
sudo k3s kubectl get runtimeclass
```

After separately deploying a compatible device plugin, check node allocatable
GPU resources and run your vendor's CUDA/ROCm/oneAPI sample Pod. Use
`runtimeClassName: nvidia` for NVIDIA workloads. If using GPU Operator later,
disable its driver and toolkit installation when retaining this host-managed
setup. AMD needs an appropriately supported GPU with `/dev/kfd` and `/dev/dri`;
Intel uses `/dev/dri`. Their userspace application runtimes belong in container
images. Firmware installation alone cannot make unsupported hardware support a
compute runtime.

## Node removal

Run from a surviving server with root access, its admin kubeconfig, and SSH
access to the target. First preview:

```bash
sudo ./scripts/remove-node.sh --node worker-01 --ssh root@192.168.2.172
# If the plan and identity are correct:
sudo ./scripts/remove-node.sh --node worker-01 --ssh root@192.168.2.172 --yes
```

The script fails closed on Kubernetes discovery errors, active Longhorn replicas
or attached volumes, and node-bound local PVs. In Longhorn, disable scheduling
on the node, request replica eviction, wait for healthy replicas elsewhere, and
migrate/detach workloads first. It then honors PodDisruptionBudgets during
drain. Unmanaged Pods block removal; inspect them rather than bypassing the
guard. `--delete-emptydir-data` explicitly allows losing emptyDir contents.

For a server it requires at least three healthy etcd members before routine
removal, compares Kubernetes servers with real etcd membership, checks endpoint
health, and creates a snapshot. After drain, it stops k3s on the target and
deletes the Node; k3s removes the embedded-etcd member. It verifies membership
removal before uninstalling. If this check fails, the host remains stopped with
its local data intact for recovery. Re-establish an odd number of servers after
maintenance. Removing a failed quorum member is a separate recovery procedure,
not something this routine script forces through.

Removal cleans the Cilium interfaces/firewall entries before the k3s uninstaller,
then removes the local role marker and address-refresh settings/systemd drop-in. Reboot before rejoining to clear residual
BPF state. `/var/lib/longhorn`, host packages and host preparation are retained.
Review and erase retained storage separately only after its contents are no
longer needed. Do not use the k3s uninstaller directly on an active cluster node.

The node owning `api_ip` cannot be routinely removed until you migrate the
shared API/DNS/gateway endpoint. To dismantle an entire cluster, remove all other nodes, evacuate all
storage, and back up the final server's etcd snapshot and token **off-host**.
Only then use the explicit final-node mode:

```bash
sudo ETCD_BACKUP_CONFIRMED=yes ./scripts/remove-node.sh \
  --node final-server --ssh root@192.168.2.153 --destroy-last-server --yes
```

This destroys the last k3s server and its local cluster state. It does not
delete retained Longhorn files or the external backups.

## Backups, configuration and upgrades

Back up `/var/lib/rancher/k3s/server/token` together with etcd snapshots. The
token is needed to restore encrypted bootstrap data. Server snapshots are taken
twice daily and retain 14 local snapshots; local retention is not off-host
backup. Configure an appropriate S3/backup destination separately. Also preserve
the CA and any future SSO/workload secrets outside Git.

Changing `config/cluster.json` plus regeneration changes Git-managed software;
it does not modify already installed host settings. For k3s upgrades, review
the supported minor upgrade path, snapshot etcd, and upgrade/drain one server at
a time, then workers. The node scripts reject changes to existing generated
node configuration and reject role conversion. For intentional server-flag or
IP changes, use a planned migration of `/etc/rancher/k3s/config.yaml` on every
server. Match critical k3s flags across servers. Do not change pod/service CIDRs
in place; rebuild/migrate the cluster.

For an edge endpoint migration, plan a maintenance window: `.153` owns the API,
DNS and gateway together. Back up etcd, tokens, CA and workload secrets. Arrange the
replacement host/address, add the required API SANs on all servers, and verify
API access before updating Git's `api_ip`, Cilium values, join endpoints,
kubeconfigs, router DNS forwarding and host address-discovery settings. Move
`elektro.internal/edge=true` to the replacement and remove it from the old host;
never assign the same LAN IP to two live hosts. Reconcile DNS hostIP and gateway
selection, verify LAN acceptance checks, then remove the retired node. This is a
coordinated migration, not a routine removal command. If retaining `.153` on new
hardware, transfer the address only after the original host has released it.

For private Git repositories, create a Flux Git authentication Secret outside
Git and add `spec.secretRef` to its GitRepository; retain that customization in
`scripts/config.py` so regeneration preserves it. The default repository is
public and the Flux source is read-only.
