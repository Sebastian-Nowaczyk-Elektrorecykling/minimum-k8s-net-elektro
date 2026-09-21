# Operations

## First-run acceptance

Run these on the bootstrap server after Flux has reconciled:

```bash
sudo ./scripts/status.sh
sudo ./scripts/check-gateway.sh
sudo k3s kubectl get pods -A
sudo k3s kubectl -n kube-system get helmrelease cilium
sudo k3s kubectl -n gateway-system describe gateway internal
sudo k3s kubectl -n gateway-system get certificate internal-wildcard
sudo k3s kubectl -n gateway-system describe httproute redirect-https
sudo k3s kubectl get nodes -l elektro.internal/edge=true -o wide
sudo k3s kubectl -n lan-system get pods -o wide
```

Check Gateway conditions `Accepted=True` and `Programmed=True`, and HTTPRoute
parent conditions `Accepted=True` and `ResolvedRefs=True`. All Flux Kustomizations
and HelmReleases should become Ready. The Cilium Helm release must be the same
release created by bootstrap (`helm history cilium -n kube-system`).

Gateway readiness includes every listener's references and the current object
generation. See the [Gateway guide](gateway.md) for route configuration and checks.

From a LAN client with `dig` installed, check both DNS transports and private
AAAA behavior:

```bash
dig @192.168.2.153 hubble.admin.internal A +short
dig @192.168.2.153 openbudget.management.internal A +short
dig @192.168.2.153 openbudget.management.internal A +tcp +short
dig @192.168.2.153 app.internal A +tcp +short
dig @192.168.2.153 app.testing.internal A +short
dig @192.168.2.153 app.staging.internal A +tcp +short
dig @192.168.2.153 api.internal A +short
dig @192.168.2.153 app.internal AAAA
dig @192.168.2.153 openbudget.management.internal AAAA
dig @192.168.2.153 debian.org A +short

curl --cacert ca.crt -I --resolve hubble.admin.internal:443:192.168.2.153 \
  https://hubble.admin.internal   # expect 404 when no Hubble route is installed
```

All private A answers should be `192.168.2.153`.
Private AAAA queries should have no external answer. Application, management
and Hubble hostnames give an HTTP 404 over HTTPS until their HTTPRoutes exist. To test Hubble, use
the [temporary route](#temporary-hubble-route). To deploy a smoke-test application:

```bash
cp examples/whoami.yaml apps/whoami.yaml
# Add whoami.yaml to resources in apps/kustomization.yaml, commit and push.
```

Check `https://whoami.internal` from the LAN and `.internal` lookups from an
application Pod. Reboot the bootstrap host to verify persistent setup, then
join at least one worker and repeat cross-node traffic tests. The API, DNS and
gateway all depend on the `.153` node; they do not fail over to DHCP nodes.

## Temporary Hubble route

Hubble UI and Relay run as Cilium components. Their Services stay inside the
cluster. To expose the UI through the shared HTTPS gateway for a test, run
on a k3s server with the cluster's checkout and administrator kubeconfig:

```bash
sudo ./scripts/hubble-route.py add
sudo k3s kubectl -n administration describe httproute hubble-test
sudo k3s kubectl -n kube-system get referencegrant hubble-test
```

The command uses `KUBECONFIG`, defaulting to `/etc/rancher/k3s/k3s.yaml`, and
reads `config/cluster.json`. It waits up to 120 seconds for the current route's
`Accepted` and `ResolvedRefs` conditions; use `--timeout 300` to wait longer.
`python3 scripts/hubble-route.py render` previews the two manifests without
contacting Kubernetes. Both resources use the name `hubble-test`, with the
route in `administration` and its Service permission in `kube-system`.

Open `https://hubble.admin.internal` from the LAN with the private CA trusted
(substitute your configured domain). The route forwards to `hubble-ui:80`
without authentication and stays present until explicitly removed. Remove it
after testing:

```bash
sudo ./scripts/hubble-route.py remove
```

Both actions can be repeated. Removal only deletes the script's labeled route
and grant, and works independently of local configuration or Gateway readiness.
The script refuses objects owned elsewhere and checks for conflicting routes
before adding its own. If applying or waiting fails, inspect the reported
objects, then retry `add` or use `remove` to clean up. Flux leaves these temporary
objects alone; Hubble deployments, the shared gateway, DNS and TLS are unaffected
by their removal.

For local access without creating a route, run this on the machine where you
will open the browser, with an appropriate kubeconfig:

```bash
kubectl -n kube-system port-forward service/hubble-ui 12000:80
# Open http://127.0.0.1:12000 while the command is running.
```

## LAN DNS runtime requirements

The pinned CoreDNS image marks `/coredns` with the file capability
`cap_net_bind_service=ep`. Dropping that capability from the container's bounding
set can make Linux reject execution with EPERM, before the Corefile is read.
Listening on port 1053 does not avoid this executable-file check. See the
[pinned image Dockerfile](https://github.com/coredns/coredns/blob/v1.14.7/Dockerfile)
and [Linux capability execution checks](https://man7.org/linux/man-pages/man7/capabilities.7.html).

The deployment drops all capabilities and adds back only `NET_BIND_SERVICE`.
It runs as UID/GID 65532, with privilege escalation disabled, a read-only
root filesystem and RuntimeDefault seccomp. CI executes the actual container
image with these settings as well as testing DNS queries against CoreDNS.

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

Host preparation persists and loads VXLAN and the `xt_socket`, `xt_TPROXY`,
`xt_mark`, `xt_CT` kernel modules needed by the configured Cilium proxy path.
Custom kernels must supply equivalent built-in/module support.

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

## Node power policy

All controller, worker and hybrid installation paths run `prepare-host.sh`,
which calls `configure-power.sh`. The policy persists across reboots. Apply it
to an existing node without reinstalling or restarting k3s:

```bash
git pull --ff-only
sudo ./scripts/configure-power.sh
```

Lid closure is ignored on battery, on external power and while docked. Idle
timers and short/long presses of sleep or hibernate keys do nothing. A short
press of the physical power button requests ordinary suspend; hibernation,
hybrid sleep and suspend-then-hibernate are disabled. Application sleep
inhibitors do not block that physical button. A firmware-enforced power-off
from holding the power button cannot be disabled by this operating-system
policy.

The script installs these files:

| Host file | Purpose |
| --- | --- |
| `/etc/systemd/logind.conf.d/90-elektro-node-power.conf` | Power button, lid, keys and idle policy |
| `/etc/systemd/sleep.conf.d/90-elektro-node-power.conf` | Keep plain suspend available; disable hibernation modes |
| `/etc/polkit-1/rules.d/10-elektro-node-power.rules` | Deny non-root desktop sleep requests and takeover of logind's key/lid handling |

The polkit rule loads automatically if polkit is installed, including when a
desktop is installed later. Root/sudo can still explicitly manage power; this
policy does not restrict privileged administrators or hardware firmware. The
script removes persistent/runtime masks on `sleep.target`, `suspend.target`
and `systemd-suspend.service`, since those masks would also prevent the power
button from working. It reloads logind without restarting user sessions.

If a desktop already holds a key/lid inhibitor, the script reports it. Log out
of local desktop sessions or reboot during maintenance before relying on the
power-button policy. Reloading configuration cannot revoke existing locks.
Inspect the merged settings and inhibitor holders with:

```bash
systemd-analyze cat-config systemd/logind.conf
systemd-analyze cat-config systemd/sleep.conf
systemd-inhibit --list
journalctl -b -u systemd-logind --no-pager
```

Later local drop-ins or earlier polkit rules can override this configuration;
check them if the effective behavior differs. After setup, verify the node
stays reachable with its lid closed and while idle. Test power-button
suspend/resume during maintenance, with workloads drained as appropriate:
the host's firmware and kernel must support suspend, and suspending the
static API/gateway/DNS node takes those entry points offline.

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
Debian driver, matching running-kernel headers, `nvidia-smi` and `libcuda1`, then
the pinned NVIDIA Container Toolkit. Debian supplies the SMI command and the
CUDA driver library as separate packages, so they are requested explicitly even
with `--no-install-recommends`. The host driver library is required by GPU
containers; the CUDA SDK and application libraries belong in workload images.
An unavailable `linux-headers-$(uname -r)` package means you
should update/reboot into an available Debian kernel first. Secure Boot may
require enrolling the Debian DKMS MOK and rebooting. Older/newer GPUs may need a
different supported driver; `NVIDIA_DRIVER_PACKAGE` selects a Debian package.
For a different Debian driver branch, set `NVIDIA_SMI_PACKAGE` and
`NVIDIA_CUDA_PACKAGE` to its matching packages too; their defaults are
`nvidia-smi` and `libcuda1`. An open kernel module from the same driver branch
uses the same userspace packages. Select packages for the GPU model rather
than switching driver branches in response to a generic installation error.
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

### NVIDIA readiness

APT reporting zero new packages means the requested packages are installed.
The count of packages not upgraded is not a driver health result. Installation
continues only when `nvidia-smi` succeeds; a missing command is reported
separately from a command that runs and fails to communicate with the driver.

Run the same read-only check independently on the GPU host:

```bash
sudo ./scripts/check-nvidia.sh
```

On failure it prints the original SMI error, running kernel, PCI device IDs and
active drivers, DKMS status, loaded modules, module lookup and matching kernel
messages. It does not reinstall packages, rebuild modules or change GPU bindings.

| Evidence | Next step |
| --- | --- |
| `nvidia-smi` command missing | Install the matching SMI package or rerun host preparation; a reboot does not install it |
| Module not found for the running kernel, or DKMS not installed for it | Check matching kernel headers and the NVIDIA DKMS build log under `/var/lib/dkms`; resolve the build error before retrying |
| Nouveau still owns the GPU | Verify the Debian driver package's blacklist/initramfs configuration and reboot after it is applied; do not unload the active display driver during setup |
| Driver/library version mismatch | Check that userspace packages match the installed kernel module; reboot if an older module remains loaded after an upgrade |
| Signature rejection or lockdown in the kernel log | Check the actual Secure Boot state and enroll the DKMS signing key when required |
| Unsupported GPU, no devices, or GPU bound to another driver such as `vfio-pci` | Use the PCI ID and kernel error to select a supported driver and intended device binding |

If the cause remains unclear, retain this output before making changes. Do not
use `GPU_VENDOR=none` to treat a failing GPU as ready. See Debian's
[`nvidia-driver`](https://packages.debian.org/trixie/nvidia-driver),
[`nvidia-smi`](https://packages.debian.org/trixie/nvidia-smi) and
[`libcuda1`](https://packages.debian.org/trixie/libcuda1) package definitions for
their roles and dependencies.

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

Routine removal rejects the controller executing the command: run it from a
different surviving controller. Local/hostPath checks use all node-affinity
terms and actual labels/fields, including custom hostname labels. An unscoped
hostPath PV conservatively blocks removal until migrated or retired.

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

The node owning `api_ip` cannot be routinely removed until you move the
shared API/DNS/gateway endpoint. For a cluster with exactly one remaining server
and no other nodes, evacuate storage and back up its etcd snapshot and token
**off-host** before using the explicit final-node mode:

```bash
sudo ETCD_BACKUP_CONFIRMED=yes ./scripts/remove-node.sh \
  --node final-server --ssh root@192.168.2.153 --destroy-last-server --yes
```

This destroys the last k3s server and its local cluster state. It does not
delete retained Longhorn files or the external backups.

Routine server removal deliberately stops at the etcd quorum guard; it cannot
shrink a multi-server cluster all the way to one node. For a complete rebuild,
follow [Retire the cluster](reusing-repository.md#retire-the-cluster) rather than
bypassing that guard.

## Backups, configuration and upgrades

Back up `/var/lib/rancher/k3s/server/token` together with etcd snapshots. The
token is needed to restore encrypted bootstrap data. Server snapshots are taken
twice daily and retain 14 local snapshots; local retention is not off-host
backup. Configure an appropriate S3/backup destination separately. Also preserve
the CA and workload secrets outside Git.

CA initialization preserves backups if the API read fails or a certificate/key
pair is invalid. A partial local backup is an error, not permission to generate
a replacement trust root; restore both files before retrying.

Changing `config/cluster.json` plus regeneration changes Git-managed software;
it does not modify already installed host settings. For k3s upgrades, review
the supported minor upgrade path, snapshot etcd, and upgrade/drain one server at
a time, then workers. The node scripts reject changes to existing generated
node configuration and reject role conversion. For intentional server-flag or
IP changes, plan coordinated updates to `/etc/rancher/k3s/config.yaml` on every
server. Match critical k3s flags across servers. Do not change pod/service CIDRs
in place; rebuild the cluster with the required addressing.

To move the edge endpoint, plan a maintenance window: `.153` owns the API,
DNS and gateway together. Back up etcd, tokens, CA and workload secrets. Arrange the
replacement host/address, add the required API SANs on all servers, and verify
API access before updating Git's `api_ip`, Cilium values, join endpoints,
kubeconfigs, router DNS forwarding and host address-discovery settings. Move
`elektro.internal/edge=true` to the replacement and remove it from the old host;
never assign the same LAN IP to two live hosts. Reconcile DNS hostIP and gateway
selection, verify LAN acceptance checks, then remove the retired node. This is a
coordinated operation, not a routine removal command. If retaining `.153` on new
hardware, transfer the address only after the original host has released it.

For private Git repositories, create a Flux Git authentication Secret outside
Git and add `spec.secretRef` to its GitRepository; retain that customization in
`scripts/config.py` so regeneration preserves it. The default repository is
public and the Flux source is read-only.
