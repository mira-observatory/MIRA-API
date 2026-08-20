# MIRA-API

Servicio de consultas en lenguaje natural sobre los datos de contrataciones publicas
de Centroamerica que produce [MIRA-ETL](https://github.com/byronalb146/MIRA-ETL).

Traduce una pregunta en espanol a una consulta SQL de solo lectura, la valida, la
ejecuta y devuelve los datos junto con el SQL que se uso.

## El principio que ordena todo el servicio

**El que cuenta es PostgreSQL. El modelo de lenguaje solo traduce y redacta.**

Ningun numero que vea el usuario puede existir sin haber salido de una celda de un
resultado de consulta. De ahi se derivan cuatro reglas que este repositorio hace
cumplir por construccion:

1. Los datos llegan al usuario aunque la redaccion falle. La tabla es la respuesta;
   el parrafo es un acompanante prescindible.
2. La resolucion de entidades es determinista, hecha con SQL y trigramas, nunca por
   el modelo.
3. Todo numero del parrafo se verifica programaticamente contra las celdas del
   resultado antes de mostrarlo.
4. El SQL ejecutado se devuelve al usuario. Es la prueba de que el numero no fue
   inventado.

### Ambiguedad de entidades

Si existen `Karro y Limon S.A` con 6 procesos y `Carro y Limon S.A` con 9, el
servicio devuelve **ambos candidatos con sus conteos reales**. Nunca los fusiona,
nunca los suma. No se senala si dos candidatos parecen duplicados entre si --
decision de producto (2026-08-15): nombres parecidos pueden ser entidades
distintas a proposito.

## Estado

En construccion. Fase 0 del plan de arquitectura.

El esquema `query` ya existe en [MIRA-ETL](https://github.com/mira-observatory/MIRA-ETL)
(verificado contra produccion 2026-08-15): `v_process`, `v_buyers`, `v_suppliers`,
`v_process_buyers`, `v_items`, `v_awards`, `v_award_items`, `v_award_suppliers`.
Es un diseno normalizado -- un proceso tiene adjudicaciones, cada adjudicacion
tiene proveedores e items; el monto vive en la adjudicacion, no en el proceso.
Ver `docs/proposed-query-schema.md` para el detalle.

**Dependencia bloqueante restante:** `query.v_coverage` (distingue "cero real" de
"cero por falta de datos") y los indices de trigrama sobre `mart.suppliers` /
`mart.buyers` (necesarios para que la resolucion de entidades del Hito 1 sea
rapida) todavia no existen en MIRA-ETL. El DDL de las vistas y del esquema
`query` es responsabilidad exclusiva de MIRA-ETL -- **no vive en este repo**.

## Arquitectura

Esta version **no tiene catalogo de consultas predefinidas**. Toda pregunta se
responde con SQL generado por el modelo, validado programaticamente antes de
ejecutarse. Ese es el objetivo de esta fase: el registro de cada intento -pregunta,
SQL generado, aceptado o rechazado y por que, filas devueltas- es la materia prima
con la que mas adelante se construira un catalogo de consultas parametrizadas.

```text
pregunta ->
  normalizacion            (codigo)
  clasificacion + params   (modelo rapido)
  resolucion de entidades  (PostgreSQL, sin IA)
  generacion de SQL        (modelo potente)
  validacion sqlglot       (codigo)
  ejecucion                (PostgreSQL, rol de solo lectura)
  redaccion + verificacion (modelo rapido + codigo)
-> QueryResponse
```

Solo tres etapas involucran al modelo de IA, y ninguna de ellas toca un numero.

## Portabilidad

El servicio se conecta a un PostgreSQL estandar mediante `DATABASE_URL_QUERY`. Hoy ese
PostgreSQL esta alojado en Supabase; **no se usa ninguna funcionalidad propia de
Supabase** (ni PostgREST, ni auth, ni RLS como autorizacion de la aplicacion). El
dia de la migracion cambia el valor de una variable de entorno.

La unica frontera que toca el driver es `src/mira_api/db/`. El resto del servicio
nunca importa `psycopg`.

## Desarrollo

En macOS o Linux, instala el proyecto y sus dependencias de desarrollo con:

```bash
./scripts/install.sh
```

El instalador crea `.venv`, instala el paquete y crea `.env` desde
`.env.example` si todavía no existe. Completa las credenciales de `.env` y
arranca el servidor con:

```bash
./scripts/run.sh
```

Por defecto escucha en `http://127.0.0.1:8000` con recarga automática. El host
y el puerto se pueden configurar, y los argumentos adicionales se pasan a
Uvicorn:

```bash
HOST=0.0.0.0 PORT=8080 ./scripts/run.sh --log-level debug
```

En Windows PowerShell, usa los scripts equivalentes:

```powershell
.\scripts\install.ps1
```

Completa las credenciales de `.env` y arranca el servidor con:

```powershell
.\scripts\run.ps1
```

Para cambiar el host, el puerto o pasar argumentos adicionales a Uvicorn:

```powershell
$env:HOST = "0.0.0.0"
$env:PORT = "8080"
.\scripts\run.ps1 --log-level debug
```

Pruebas y calidad:

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/mypy
```

## Verificacion despues de una recarga del ETL

Una recarga puede dejar los datos perfectos y aun asi romper el servicio: si se
recrean los esquemas, los permisos de `mira_query` y `mira_logger` se pierden, y
esos se otorgan a mano (MIRA-ETL, `docs/database_security.md`), no desde `sql/`.

**Primero, el contrato con la base.** No llama al modelo, no cuesta nada:

```bash
.venv/bin/python -m mira_api.evals.contract
```

Comprueba que `mira_query` lee las vistas permitidas y **no** alcanza `mart.*`,
que `query.f_unaccent` responde, que el diccionario semantico esta poblado, que
`mira_logger` puede escribir, que el `CHECK` de `outcome` cubre la taxonomia
completa, y que no hay montos absurdos en USD/EUR. Sale con codigo 1 si algo
falla, y dice que revisar.

**Despues, las preguntas de referencia.** Cada una es una llamada real a Claude
con costo real, asi que no corre con `pytest`:

```bash
.venv/bin/python -m mira_api.evals.runner
```

Ninguna espera un valor concreto: los datos cambian en cada recarga y una suite
que se cae porque hay otro numero de procesos no mide nada. Afirman invariantes
--que la consulta filtre por el pais pedido, que toque las vistas correctas, que
la narrativa no cite un numero ausente del resultado, que lo que esta fuera de
dominio no genere SQL-- y varias vigilan regresiones de bugs concretos, anotadas
en `src/mira_api/evals/cases.py`.

## Estructura

| Ruta | Responsabilidad |
|---|---|
| `src/mira_api/api/` | Endpoints HTTP y esquemas de request/response |
| `src/mira_api/db/` | Pool de conexiones y ejecutor de solo lectura. Unica frontera con el driver |
| `src/mira_api/nlq/` | Resolucion de entidades, generacion y validacion de SQL, pipeline de la consulta |
| `src/mira_api/audit/` | Registro de consultas para auditoria y mejora continua |
| `src/mira_api/quota/` | Presupuesto global y cuota por sujeto (escrita, inactiva) |
| `src/mira_api/evals/` | Contrato con la base y preguntas de referencia. No corre con pytest |

### Cobertura publica

`GET /coverage` es un endpoint determinista respaldado por una consulta SQL
constante sobre `web.countries` y `web.coverage_sources`. Usa su propio pool y
el rol `mira_web`; no invoca modelos, no consume cuota y no pasa por el
validador de SQL generado.

Las tres conexiones del servicio tienen privilegios separados:

| Variable | Rol | Acceso |
|---|---|---|
| `DATABASE_URL_QUERY` | `mira_query` | Vistas permitidas de `query` para NLQ |
| `DATABASE_URL_WEB` | `mira_web` | `web.countries`, `web.coverage_sources` |
| `DATABASE_URL_LOG` | `mira_logger` | Escritura de auditoria en `analytics` |

## Lo que este repositorio nunca hace

- **Nunca ejecuta DDL.** Todo el esquema, las vistas de consulta y los roles viven
  en `MIRA-ETL/sql/`.
- **No tiene catalogo de consultas predefinidas.** Toda pregunta contestable
  se responde con SQL generado por el modelo y validado antes de ejecutarse. El
  catalogo, si llega, se derivara despues del registro de auditoria.
- **Nunca normaliza nombres de entidades.** Esa logica es del ETL; aqui se consumen
  las columnas ya normalizadas.
- **Nunca consulta `mart`, `raw`, `staging` ni `audit` directamente.** Solo el
  esquema `query`, y el rol de base de datos lo hace imposible de violar.

## Licencia

MIT. Ver [LICENSE](LICENSE).
