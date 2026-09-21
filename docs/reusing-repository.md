# Reusing this repository

You can build a replacement cluster from the same repository while keeping
your infrastructure, applications, scripts and configuration. You can also
publish a snapshot at a new Git URL with a single initial commit.

The examples below use the default `main` branch and `clusters/lan` path. Run
repository commands from a clean checkout on an administration machine.

## What to keep

| Item | Purpose when building another cluster |
| --- | --- |
| `config/cluster.json` | Addressing, domain, Git URL/branch and software versions; review for the destination |
| `apps/`, `infrastructure/`, `clusters/lan/*.yaml` | Desired workloads, shared services and reconciliation dependencies; keep these |
| `scripts/`, `tests/`, `examples/`, documentation and CI | Keep these with the configuration |
| `clusters/lan/flux-system/` | Three generated bootstrap files; keep them or delete and regenerate the complete directory |
| `infrastructure/flux/` | Pinned Flux controller installation; keep this directory |
| Live Secrets, persistent volumes, CA private key and etcd state | Stored outside Git; back up or recreate separately |

`clusters/lan/flux-system` contains a Git source, the root reconciliation object
and a Kustomize entry point. It contains no node identity, deploy key, token or
live Flux inventory. Deleting it is optional. Running
`python3 scripts/config.py generate` recreates all three files from the current
configuration.

Never push a commit containing only that directory's deletion to a running
cluster. The root build requires it, and Flux pruning can remove managed
objects when their definitions are removed. Keep custom Git authentication
or source settings in `scripts/config.py`, since generation overwrites these
files. Keep actual credentials outside Git.

## Retire the cluster

Do this before changing a repository/branch that the old cluster watches, or
before assigning its IP address to a replacement host.

1. Back up data you intend to retain: application/database backups, Longhorn
   volume backups, workload Secrets and any Git decryption keys. Store them
   outside the hosts being rebuilt. For disaster recovery, also save an etcd
   snapshot together with `/var/lib/rancher/k3s/server/token` and both
   `/etc/elektro/secrets/ca.crt` and `ca.key` securely. Git alone cannot restore
   these. Restoring an etcd snapshot recovers that cluster's state; a fresh
   bootstrap creates a new cluster.
2. Detach the old cluster from Git before repurposing the source. With a Flux
   CLI matching the pinned release, select and verify the old kubeconfig
   context, then uninstall Flux:

   ```bash
   # Replace old-cluster with the context of the cluster being retired.
   kubectl --context old-cluster get nodes -o wide
   flux uninstall --context old-cluster --namespace flux-system --keep-namespace
   ```

   Review the CLI confirmation. This removes Flux controllers and its custom
   resources while leaving reconciled workloads and installed Helm releases.
   Keeping the namespace also preserves non-Flux objects you may have placed
   there. It stops reconciliation from all repositories managed by this Flux
   installation. This does not uninstall k3s or remove application data.
   Use the supported [Flux uninstall procedure](https://fluxcd.io/flux/installation/uninstall/),
   rather than deleting its namespace with kubectl. If the entire old cluster
   is permanently shut down and its disks will be rebuilt, detaching Flux first
   is optional; do not bring it back online against the repurposed source.
3. For a complete rebuild, shut down the old cluster and prepare fresh Debian
   13 installations on the intended OS disks, after verifying the backups.
   Ensure every old node is either rebuilt or remains off. Release the old
   static address before the replacement host takes it. Reinstallation of the
   OS is the straightforward full reset for a multi-server cluster.

For selective retirement while keeping the cluster running, use the
[node-removal procedure](operations.md#node-removal). Its storage and etcd
checks remain necessary. Routine server removal refuses to reduce a
two-server etcd cluster; it is not an automated whole-cluster teardown loop.
The explicit final-server mode applies only when exactly one node remains.

If reusing a host without reinstalling Debian, complete its supported node
removal first and reboot. The removal script preserves host prerequisites,
Longhorn files and the local CA backup. Inspect those retained files before
repurposing it. A raw [k3s uninstaller](https://docs.k3s.io/installation/uninstall)
deletes the local datastore and local-storage volume data; it does not perform
this repository's storage, membership or Cilium cleanup checks. Running the
bootstrap script on an installed server does not reset that server.

## Option A: keep the same repository URL

After retiring or detaching the old cluster, keep all your tracked files.
Update the checkout, then optionally remove just the generated Flux directory:

```bash
git pull --ff-only
git status --short                     # Must be empty before starting.
rm -rf -- clusters/lan/flux-system     # Optional, local generated files only.
```

Review `config/cluster.json`. For a replacement on the same LAN you can keep
`192.168.0.0/20`, `192.168.2.153`, `internal`, the existing Git URL, `main`, and
the software versions. If the environment changes, update the relevant
settings using the checklist below.

Recreate the complete configuration before committing:

```bash
python3 scripts/config.py generate
python3 scripts/config.py check
git diff --check
git diff
git add -A
# Only if there are staged changes:
git commit -m "Configure replacement cluster"
git push origin main
```

With unchanged settings, deleting and regenerating the Flux directory produces
the same files, so there is nothing to commit. Retain the normal Git history of
this repository. No Kubernetes or Flux identifiers need to be cleared in Git.
Continue with [Bootstrap the destination](#bootstrap-the-destination).

## Option B: publish a clean copy at another URL

Create an empty destination repository, without an initial README or license
commit. Export the committed source into a new sibling directory:

```bash
# From the source repository; commit any desired changes before exporting.
git status --short
mkdir ../another-cluster
git archive HEAD | tar -x -C ../another-cluster
cd ../another-cluster
git init -b main
git remote add origin https://github.com/YOUR-OWNER/YOUR-CLUSTER.git
```

`git archive` copies tracked files without the source repository's history,
remotes or untracked files. It includes all committed application definitions;
review them for the new cluster. GitHub repository settings, Actions secrets
and deploy keys are not copied.

Edit `config/cluster.json`: set `git_url` to the **destination** clone URL,
`git_branch` to `main`, and choose a `cluster_name`. Review the network and
workload settings below. Changing `origin` alone does not change Flux's source.

```bash
rm -rf -- clusters/lan/flux-system     # Optional; generation overwrites it.
python3 scripts/config.py generate
python3 scripts/config.py check
python3 -m json.tool config/cluster.json          # Review destination settings.
git add .
git diff --cached --check
git diff --cached --stat
git commit -m "Initial cluster configuration"
git push -u origin main
```

This creates an independent repository with one initial commit and leaves the
source repository intact. Enable Actions in the destination and wait for its
validation workflow before bootstrapping. If using a branch other than `main`,
use it consistently in Git, `git_branch`, clone/push commands and the CI push
branch filter in `.github/workflows/validate.yaml`.

The default setup assumes Flux can read the repository over public HTTPS. A
private destination also needs a Flux Git authentication Secret and a
`spec.secretRef` in the generated GitRepository. Configure that reference in
`scripts/config.py` and provision the Secret in `flux-system` before waiting
for the source to become Ready. A successful authenticated host clone does not
give Flux access. See [Flux Git authentication](https://fluxcd.io/flux/components/source/gitrepositories/#secret-reference)
for the fields required by your Git transport.

## Configuration checklist

| Setting or dependency | Review for the destination |
| --- | --- |
| `git_url`, `git_branch` | Must identify the pushed configuration that this cluster will reconcile |
| `cluster_name` | Administrative name; changing it alone does not isolate clusters |
| `lan_cidr`, `api_ip` | The static API, DNS and web endpoint must exist on the bootstrap host and be excluded from DHCP |
| `domain`, `upstream_dns` | Match LAN resolver forwarding; upstreams must not loop back through this cluster |
| Pod/Service CIDRs and DNS Service IPs | Choose before installation; avoid overlapping routed networks and keep Service IPs inside their CIDR |
| `cilium_devices` | Empty for auto-detection, or interfaces appropriate to the new hardware |
| Applications and other Git sources | Review external Git URLs, dependencies, namespaces, hostnames, node selectors, storage classes and Secrets |
| CA and data | Decide what to restore from backup and what to create fresh |

The labels and resource names under `elektro.internal` are a stable internal
contract. Keep them when changing `cluster_name` or the user-facing DNS domain.
Adjust any hardcoded hostnames in your own applications and examples yourself.
The administration, testing and staging suffixes follow the configured `domain`
automatically, including DNS, Gateway listeners and the private TLS certificate.

If both clusters will run on the same LAN, give the new cluster a distinct
static `api_ip` and DNS domain, and configure separate conditional forwarding.
Two clusters cannot both own `192.168.2.153` or receive the same `.internal`
forwarding rule. Identical network defaults are suitable for isolated LANs or
for a replacement after the original cluster is retired. A different Git URL
alone does not isolate network entry points.

Keeping manifests means Flux will attempt to deploy them on the new cluster.
Arrange storage operators, volume restores, Secret/decryption setup and other
repository dependencies before dependent workloads need them. If these require
staging, temporarily remove only the dependent resources from their Kustomize
resource lists while keeping their files, commit that bootstrap configuration,
then add the references back when prerequisites are ready. Avoid a dependency
cycle where bootstrap waits for applications that need a manual restore first.

## Bootstrap the destination

1. On the fresh Debian 13 bootstrap host, assign the configured `api_ip` with
   the correct LAN prefix. Check that ports 53, 80 and 443 are available. Clone
   the configured repository/branch and verify the generated files:

   ```bash
   # Use this URL for Option A, or substitute the destination URL for Option B.
   git clone --branch main https://github.com/Sebastian-Nowaczyk-Elektrorecykling/minimum-k8s-net-elektro.git cluster
   cd cluster
   python3 scripts/config.py check
   ip -br address
   sudo ./scripts/bootstrap-hybrid.sh --interface eno1
   ```

   Replace `eno1` with this host's LAN interface. The checkout must be clean
   and match the configured remote branch. Follow the [README](../README.md)
   for a dedicated controller instead of a hybrid, GPU preparation and reboots.
   Use these bootstrap scripts to install Cilium before Flux adopts it; do not
   add a separate `flux bootstrap github` layout on top of this one.
2. Join workers and additional controllers using tokens from the **new**
   cluster. Use its new kubeconfig for administration. Tokens and kubeconfigs
   from a retired cluster do not apply to a fresh bootstrap.
3. Configure LAN DNS forwarding and client CA trust, then run the
   [first-run acceptance checks](operations.md#first-run-acceptance).
4. Restore application data and Secrets as planned and reconnect any additional
   Flux repositories.

A fresh bootstrap creates a private CA unless a complete pair already exists
in `/etc/elektro/secrets` or in the cluster. If intentionally retaining the CA,
securely restore both `ca.crt` and `ca.key` on the fresh bootstrap host before
running the scripts; keep the directory root-only. Otherwise distribute the
new public `ca.crt` to clients. Retaining CA trust does not retain cluster
tokens, Kubernetes identities or workload data. Never commit the private key.
