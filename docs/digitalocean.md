# API en DigitalOcean con Docker Compose

Destino: `mira-app-prod` (`104.131.184.162`). La API corre en un contenedor y
PostgreSQL permanece en `mira-db-prod`. El despliegue no modifica el esquema.

## Despliegue verificado el 11 de septiembre de 2026

- Imagen instalada: `mira-api:cebfc72-do1`, en `/opt/mira-api`.
- Entrada inicial de prueba por IP retirada al completar HTTPS.
- API saludable, conexiones de los tres roles y endpoints de cobertura,
  procedimientos y estados verificados desde la red publica.
- Consulta real a Anthropic y eventos SSE verificados con una pregunta de
  conteo de Guatemala. La respuesta informa que faltan datos cargados.
- Desde el 18 de septiembre, acceso HTTPS a traves de la web:
  `https://proyectomira.org/api` y `https://www.proyectomira.org/api`.
  Swagger esta en `/api/docs`. No hay entrada HTTP publica directa al backend.
- Configuracion del servidor en `/opt/mira-api/.env.digitalocean`, fuera de
  la imagen y con permisos de lectura restringidos.

## Preparacion

Se necesita SSH, Docker Engine con Compose v2 o posterior y Nginx en el servidor.
Para un Droplet nuevo con Ubuntu 24.04, ejecutar como root
`bash deploy/bootstrap-ubuntu.sh`. Instala Docker desde su repositorio oficial
y habilita Docker y Nginx. Para otras distribuciones, seguir la guia de Docker:
https://docs.docker.com/engine/install/.

Agregar `mira-app-prod` a Trusted Sources de la base. Si ambos recursos
comparten VPC, usar el host de **VPC network** de Connection Details con los
tres usuarios de la API, conservando base `mira`, puerto y TLS.

Colocar el repositorio en `/opt/mira-api`. `.dockerignore` limita el contexto
a los archivos del paquete y los scripts, excluyendo credenciales y Git.

## Configuracion y arranque

Crear `.env.digitalocean` a partir de la plantilla unica `.env.example` solo
si no existe. Completar las conexiones, clave de Anthropic y un secreto HMAC
propio del ambiente, reemplazando el valor de desarrollo. Ajustar
`CORS_ORIGINS` al origen de la web y `COOKIE_SAMESITE` segun lo indicado en
Publicacion. Asignar `MIRA_IMAGE_TAG` y `APP_VERSION` para identificar la version.
El archivo local ya preparado contiene las conexiones de DigitalOcean.
Usar siempre `--env-file .env.digitalocean`: `.env` es de desarrollo.

Desde la raiz del repositorio en Linux:

```bash
chmod 600 .env.digitalocean
docker compose --env-file .env.digitalocean config --quiet
docker compose --env-file .env.digitalocean build --pull
docker compose --env-file .env.digitalocean up -d --wait --wait-timeout 120
docker compose --env-file .env.digitalocean exec -T api python scripts/check_db.py
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/coverage
```

`config --quiet` valida sin imprimir secretos. El healthcheck comprueba HTTP;
`check_db.py` verifica las tres conexiones. Estas comprobaciones no llaman a
Anthropic. La cobertura inicial no contiene contrataciones cargadas.

## Publicacion

La API escucha en `127.0.0.1:8080` del host. Instalar `deploy/nginx-api.conf`
como sitio de Nginx segun la distribucion, revisar los sitios existentes,
comprobar `sudo nginx -t` y recargar Nginx. Deshabilitar el sitio predeterminado
de Nginx; el backend no debe escuchar en los puertos publicos 80/443.
Mantener SSH disponible y permitir el puerto privado 8081 desde el frontend.

`deploy/nginx-api.conf` es la unica configuracion del sitio y escucha solo
en la VPC (`10.108.0.4:8081`) y admite exclusivamente al frontend
(`10.108.0.2`). El frontend termina TLS, elimina el prefijo `/api` y reemplaza
las cabeceras de IP antes de reenviar. El gateway privado conserva esa IP y
el esquema HTTPS para Uvicorn. El acceso publico a la API pasa por el frontend;
se eliminaron la entrada HTTP anterior y el archivo separado del gateway.

En `.env.digitalocean` usar `UVICORN_ROOT_PATH=/api`,
`CORS_ORIGINS=https://proyectomira.org,https://www.proyectomira.org` y
`COOKIE_SAMESITE=lax`. `UVICORN_ROOT_PATH` lo lee Uvicorn del entorno y permite
que Swagger genere URLs con el prefijo correcto. Los certificados y su
renovacion se administran en el servidor frontend, segun el README de MIRA-WEB.
La comunicacion entre los dos servidores usa HTTP dentro de la VPC.

## Operacion

```bash
docker compose --env-file .env.digitalocean ps
docker compose --env-file .env.digitalocean logs --tail 100 api
```

Docker reinicia el contenedor al salir inesperadamente o reiniciarse el motor;
el estado `unhealthy` por si solo no provoca un reinicio automatico.

Para actualizar, conservar la imagen anterior, asignar nuevos `MIRA_IMAGE_TAG`
y `APP_VERSION`, construir y repetir `up` y las verificaciones. Una instancia
tiene una breve interrupcion durante su reemplazo.

Para recuperar una imagen anterior disponible localmente, restaurar su
`MIRA_IMAGE_TAG` y la configuracion compatible y ejecutar:

```bash
docker compose --env-file .env.digitalocean up -d --no-build --pull never --wait
```

GitHub Actions, Terraform y Kubernetes pueden incorporarse despues.
