#!/usr/bin/env bash
# Entrada unica para un servidor nuevo con Ubuntu 24.04.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo 'Uso: bash deploy/bootstrap-ubuntu.sh API_IP_PRIVADA FRONTEND_IP_PRIVADA' >&2
    exit 2
fi
test "$(id -u)" = 0
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
if [[ ! -f .env.digitalocean ]]; then
    echo 'Crear .env.digitalocean desde .env.example y completar los secretos antes de continuar.' >&2
    exit 1
fi
. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
export DEBIAN_FRONTEND=noninteractive

apt-get -o DPkg::Lock::Timeout=120 update
echo '1/3 Instalando Docker y Nginx...'
apt-get -o DPkg::Lock::Timeout=120 install -y ca-certificates curl nginx git
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get -o DPkg::Lock::Timeout=120 update
    apt-get -o DPkg::Lock::Timeout=120 install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Un backend nuevo no debe conservar la pagina HTTP publica predeterminada.
if [[ -L /etc/nginx/sites-enabled/default ]] && \
   [[ "$(readlink /etc/nginx/sites-enabled/default)" = /etc/nginx/sites-available/default ]]; then
    unlink /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl enable --now docker nginx
systemctl reload nginx
install -d -m 0750 /opt/mira-api
docker --version
docker compose version
nginx -v

echo '2/3 Construyendo y arrancando la API...'
chmod 600 .env.digitalocean
compose=(docker compose --env-file .env.digitalocean)
"${compose[@]}" config --quiet
"${compose[@]}" build
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
"${compose[@]}" exec -T api python scripts/check_db.py
curl --fail --silent --show-error http://127.0.0.1:8080/healthz

echo '3/3 Configurando la entrada privada de Nginx...'
bash "$script_dir/configure-nginx.sh" "$1" "$2"
echo 'API preparada. Comprobar la conexion desde el frontend, segun docs/digitalocean.md.'
