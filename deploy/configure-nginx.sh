#!/usr/bin/env bash
# Ubuntu 24.04: instalar la unica entrada privada de la API.
# Uso: bash deploy/configure-nginx.sh API_IP_PRIVADA FRONTEND_IP_PRIVADA
set -euo pipefail
test "$(id -u)" = 0
if [[ $# != 2 ]]; then
    echo 'Uso: configure-nginx.sh API_IP_PRIVADA FRONTEND_IP_PRIVADA' >&2
    exit 2
fi
private_ipv4() {
    local a b c d
    [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS=. read -r a b c d <<< "$1"
    a=$((10#$a)); b=$((10#$b)); c=$((10#$c)); d=$((10#$d))
    (( a <= 255 && b <= 255 && c <= 255 && d <= 255 )) || return 1
    (( a == 10 || (a == 172 && b >= 16 && b <= 31) || (a == 192 && b == 168) ))
}
if ! private_ipv4 "$1" || ! private_ipv4 "$2" || [[ "$1" = "$2" ]]; then
    echo 'Indicar dos IPv4 privadas distintas de la VPC.' >&2
    exit 2
fi
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
candidate="$(mktemp -d)"
trap 'rm -rf -- "$candidate"' EXIT
# Solo se reemplazan estos marcadores; las variables propias de Nginx se conservan.
sed -e "s/@@API_PRIVATE_IP@@/$1/g" -e "s/@@FRONTEND_PRIVATE_IP@@/$2/g" \
    "$script_dir/nginx-api.conf" > "$candidate/site.conf"
printf 'events {}\nhttp { include %s/site.conf; }\n' "$candidate" > "$candidate/nginx.conf"
nginx -t -c "$candidate/nginx.conf"
site=/etc/nginx/sites-available/mira-api
enabled=/etc/nginx/sites-enabled/mira-api
default=/etc/nginx/sites-enabled/default
test ! -e "$enabled" || test "$(readlink "$enabled")" = "$site"
test ! -e "$default" || test "$(readlink "$default")" = /etc/nginx/sites-available/default
test ! -f "$site" || cp -p "$site" "$candidate/previous"
had_default=false
if test -L "$default"; then had_default=true; unlink "$default"; fi
rollback() {
    if test -f "$candidate/previous"; then
        cp -p "$candidate/previous" "$site"
    else
        rm -f -- "$enabled" "$site"
    fi
    if "$had_default"; then ln -sfn /etc/nginx/sites-available/default "$default"; fi
}
trap 'rollback' ERR
install -m 0644 "$candidate/site.conf" "$site"
ln -sfn "$site" "$enabled"
nginx -t
if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
systemctl enable nginx
trap - ERR
echo 'API privada configurada. Validar desde el frontend y comprobar ss -lnt.'
