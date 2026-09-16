#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ${1:-} == --help ]]; then exec "$script_dir/node.sh" hybrid --help; fi
"$script_dir/node.sh" hybrid --bootstrap "$@"
exec "$script_dir/bootstrap-cluster.sh"
