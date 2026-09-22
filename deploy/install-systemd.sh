#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: sudo $0 --repo DIR --user USER [--group GROUP] [--env-file FILE]"
}

repo_dir=""
service_user=""
service_group=""
env_file=""
while (($#)); do
  case "$1" in
    --repo) repo_dir=${2:?}; shift 2 ;;
    --user) service_user=${2:?}; shift 2 ;;
    --group) service_group=${2:?}; shift 2 ;;
    --env-file) env_file=${2:?}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo; the services themselves run as the selected non-root user." >&2
  exit 1
fi
if [[ -z $repo_dir || -z $service_user ]]; then
  usage >&2
  exit 2
fi

repo_dir=$(realpath "$repo_dir")
service_group=${service_group:-$service_user}
env_file=${env_file:-$repo_dir/.env}
env_file=$(realpath "$env_file")
venv_dir=$repo_dir/.venv

id "$service_user" >/dev/null
getent group "$service_group" >/dev/null
test -r "$env_file"
test -x "$venv_dir/bin/mypyrag-service"
test -x "$venv_dir/bin/mypyrag-mcp"

template_dir=$repo_dir/deploy/systemd
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

render() {
  local source=$1 target=$2
  sed \
    -e "s|@SERVICE_USER@|$service_user|g" \
    -e "s|@SERVICE_GROUP@|$service_group|g" \
    -e "s|@REPO_DIR@|$repo_dir|g" \
    -e "s|@ENV_FILE@|$env_file|g" \
    -e "s|@VENV_DIR@|$venv_dir|g" \
    "$source" > "$tmp_dir/$target"
  install -m 0644 -b --suffix=.bak "$tmp_dir/$target" "/etc/systemd/system/$target"
}

render "$template_dir/mypyrag-service.service.in" mypyrag-service.service
render "$template_dir/mypyrag-mcp.service.in" mypyrag-mcp.service
systemctl daemon-reload
systemctl enable mypyrag-service.service mypyrag-mcp.service
systemctl restart mypyrag-service.service
systemctl restart mypyrag-mcp.service
systemctl --no-pager --full status mypyrag-service.service mypyrag-mcp.service
