# API en DigitalOcean: preparacion y recuperacion

Los despliegues desde `main`, secretos de Actions y rollback se explican en
[GitHub Actions](github-actions.md). Esta guia cubre el aprovisionamiento del host.

Esta guia cubre MIRA-API. La [base y sus roles](https://github.com/byronalb146/MIRA-ETL/blob/main/docs/database_recovery.md)
se preparan en MIRA-ETL; el [frontend y HTTPS](https://github.com/byronalb146/MIRA-WEB/blob/main/docs/operations-and-recovery.md),
en MIRA-WEB.

## Servidor actual

- `mira-app-prod`, Ubuntu 24.04 x64, 4 GB; `/opt/mira-api`.
- IP publica `104.131.184.162`, privada `10.108.0.4`.
- Imagen al 18/09/2026: `mira-api:cebfc72-do1`.
- Docker publica solo `127.0.0.1:8080`; Nginx escucha en `10.108.0.4:8081`
  y admite solo al frontend `10.108.0.2`. No hay entrada HTTP publica directa.
- El frontend publica `https://proyectomira.org/api` y lo mismo en `www`.
  Swagger esta en `/api/docs`. Entre hosts se utiliza HTTP en la VPC.

## Preparar un servidor nuevo

Antes del comando:

1. Crear Ubuntu 24.04 x64 en una VPC accesible desde el frontend; 4 GB para
   repetir el entorno actual. Autorizar la llave SSH propia del administrador
   y verificar por consola la huella del servidor nuevo.
2. Clonar este repo en `/opt/mira-api`, eligiendo el commit aprobado. Si falta
   Git, instalarlo antes o transferir las fuentes de esa version.
3. Agregar el nuevo backend a Trusted Sources de PostgreSQL. Obtener los tres
   DSN de la boveda/administrador: host VPC si es accesible, puerto de DigitalOcean
   y TLS. Reutilizar la base existente no requiere recrear esquemas ni roles.
4. Crear `.env.digitalocean` desde la unica plantilla `.env.example`. Completar
   `DATABASE_URL_QUERY`, `DATABASE_URL_WEB`, `DATABASE_URL_LOG`,
   `ANTHROPIC_API_KEY` y un `TOKEN_HMAC_SECRET` del ambiente. Definir:

   ```dotenv
   MIRA_IMAGE_TAG=VERSION
   APP_VERSION=VERSION
   UVICORN_ROOT_PATH=/api
   CORS_ORIGINS=https://proyectomira.org,https://www.proyectomira.org
   COOKIE_SAMESITE=lax
   ```

   Reemplazar `VERSION` por la version elegida. No usar el HMAC de desarrollo.
   En un traslado, conservar el HMAC mantiene los tokens anonimos existentes;
   cambiarlo los invalida.

Desde el repo, como root, **un comando prepara el servidor**:

```bash
bash deploy/bootstrap-ubuntu.sh 10.108.0.4 10.108.0.2
```

Los argumentos son la IP privada del backend y la del frontend; cambiarlos al
mudar de VPC/Droplets. Instala Docker, Compose y Nginx; construye/arranca la API;
verifica salud y las tres conexiones; configura Nginx privado. Frena si falla
una etapa. No crea ni modifica la estructura de la BD.

El script no crea Droplets, reglas de DigitalOcean, Trusted Sources ni secretos.
Mantener SSH disponible y permitir 8081 privado desde el frontend. No exponer 8080.

## Archivos y ajustes de Nginx

| Archivo | Responsabilidad |
| --- | --- |
| `deploy/bootstrap-ubuntu.sh` | Entrada para preparar el servidor y arrancar la API |
| `deploy/configure-nginx.sh` | Sustituye IP, valida e instala Nginx; lo llama el bootstrap |
| `deploy/nginx-api.conf` | Unica plantilla del sitio: entrada privada y proxy al contenedor |
| `compose.yaml` | Contenedor, entorno, puerto local, reinicio y logs |
| `Dockerfile` / `requirements-production.txt` | Construccion y versiones de dependencias |

La plantilla contiene `@@...@@`: no instalarla directamente. Si cambia solo la red,
ejecutar esta etapa sin reconstruir la API:

```bash
bash deploy/configure-nginx.sh 10.108.0.4 10.108.0.2
```

El sitio se instala en `/etc/nginx/sites-available/mira-api`, enlazado desde
`sites-enabled`; se deshabilita el sitio predeterminado. No se necesitan Python
auxiliares para generar Nginx. `scripts/check_db.py`, propio de la API, se conserva.

El frontend elimina `/api` al reenviar y establece las cabeceras de cliente/HTTPS.
El backend las conserva; `UVICORN_ROOT_PATH=/api` hace que Swagger use las URLs
correctas. El certificado se administra en el servidor web.

## Comprobaciones

El bootstrap verifica salud y las tres conexiones sin llamar a Anthropic.
Desde el frontend comprobar:

```bash
curl --fail http://10.108.0.4:8081/healthz
curl --fail http://10.108.0.4:8081/coverage
```

Desde el backend, `ss -lnt` debe mostrar 8080 local y 8081 privado, sin 80/443
publicos. Desde fuera debe fallar el HTTP a la IP publica, mientras
`https://proyectomira.org/api/healthz` responde. Si cambia la IP del backend,
actualizar el proxy en el frontend segun su guia.

## Actualizaciones y recuperacion

No reinstalar Ubuntu para actualizar la API. Conservar la imagen anterior,
elegir nuevos `MIRA_IMAGE_TAG` y `APP_VERSION`, y ejecutar con las fuentes nuevas:

```bash
docker compose --env-file .env.digitalocean config --quiet
docker compose --env-file .env.digitalocean build
docker compose --env-file .env.digitalocean up -d --no-build --pull never --wait --wait-timeout 120
docker compose --env-file .env.digitalocean exec -T api python scripts/check_db.py
docker compose --env-file .env.digitalocean ps
```

Tambien se puede construir fuera y transferir con `docker save`/`load`. El
primer despliegue copio archivos: no asumir que `/opt/mira-api` sea un clon Git.
Obtener la version nueva desde un clon autorizado, conservando el entorno del
destino; revisar Compose cuando cambie. Para volver atras, restaurar etiqueta y
configuracion compatibles y repetir `up` con `--no-build --pull never --wait`.
Hay una breve interrupcion al reemplazar una instancia unica.
Docker reinicia al salir el proceso o reiniciarse el motor; `unhealthy` por si solo
no provoca reinicio automatico.

## Custodia y reproducibilidad

Git conserva fuentes, Dockerfile, Compose, scripts, plantilla y guia. El archivo
`.env.digitalocean` queda fuera de Git/imagen con permisos 600. Guardar en la
boveda los tres DSN, Anthropic y HMAC, con responsables de cuentas/recuperacion.
El equipo aun debe elegir la boveda y sus custodios.

Esos secretos ya existen en el servidor: un administrador autorizado puede
recuperarlos por consola/SSH. No dependen solo de la PC original. Usar una llave
SSH por persona; la custodia y backups de PostgreSQL se explican en MIRA-ETL.

El Dockerfile fija Python por digest y restringe dependencias mediante
`requirements-production.txt`; no necesita una `.venv` local. Actualizar con
pruebas para recibir parches. Reconstruir no promete bytes identicos: conservar
imagenes en un registry/respaldo permite recuperar un artefacto exacto.
La auditoria reconstruyo la imagen desde fuentes limpias y verifico dependencias
y configuracion; falta ensayar el bootstrap completo en un Droplet nuevo.
Publicar estos cambios en Git es necesario para completar el traspaso al equipo.

## Diagnostico del catalogo y del chat (19/09/2026)

`/healthz` confirma que el proceso responde; no prueba una consulta de catalogo
ni autentica contra Anthropic. Verificar tambien `/procedures`,
`/procedures/statuses` y una pregunta de prueba despues de desplegar.

El catalogo usa SQL fijo y no necesita una clave de IA. Con mas de 1.25 millones
de procesos, las ventanas `count(*) over (...)` provocaron cancelaciones por el
limite de 8 segundos. La correccion agrupa los estados directamente y separa
la pagina del conteo exacto dentro de una misma sentencia y snapshot. Conserva
filtros, orden, paginacion y total exacto; no aumenta el timeout.

Los indices necesarios y su instalacion en bases existentes pertenecen a
MIRA-ETL: ver `sql/002_indexes_and_views.sql` y su guia `docs/digitalocean.md`.
Aplicarlos antes de desplegar la API corregida.

El error del chat observado ese dia fue distinto: Anthropic rechazo la clave
con HTTP 401 (`API key is invalid`). Reemplazar `ANTHROPIC_API_KEY` en el entorno
del servidor y recrear el contenedor con Compose; un simple `restart` no aplica
cambios del archivo de entorno. No guardar claves en Git ni en capturas/logs.

Version desplegada: `mira-api:20260919-catalog1`. La pagina localiza primero los
identificadores con el indice y despues lee los textos de las filas visibles,
tambien al saltar a paginas avanzadas. Se uso el Dockerfile y el archivo de
dependencias fijadas del repositorio, conservando el entorno del servidor.

Validacion: 261 pruebas pasaron, cuatro quedaron omitidas y se excluyo la prueba
de concurrencia de cuotas que necesita una BD externa (su conexion local fallo
por restricciones de red). Pasaron las cuatro pruebas del catalogo y Ruff.
Se probaron ademas los endpoints reales con PostgreSQL de produccion: primera
y segunda pagina, pais, estado, fechas, texto, numero, pagina avanzada y cero
coincidencias. El dominio publico devolvio HTTP 200 para el catalogo, sus diez
estados, Honduras y la busqueda `medicamentos` (7,752 coincidencias en unos
2.5 segundos). El total observado fue 1,257,847 procedimientos. Estos tiempos
dependen del filtro y de la carga; no garantizan que toda busqueda amplia o
de muy pocos caracteres termine dentro del limite de ocho segundos.
