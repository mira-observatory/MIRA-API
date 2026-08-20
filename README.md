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
servicio devuelve **ambos candidatos con sus conteos reales** y una senal de posible
duplicado. Nunca los fusiona, nunca los suma. La ambiguedad no es un defecto del
sistema: es un hallazgo sobre la calidad de la publicacion oficial.

## Estado

En construccion. Fase 0 del plan de arquitectura.

## Arquitectura

La version actual no tiene catalogo de consultas predefinidas: toda pregunta contestable se
responde con SQL generado por el modelo, validado programaticamente antes de
ejecutarse. Cada consulta -la pregunta, el SQL generado, si fue aceptada o
rechazada y por que, cuantas filas devolvio- se guarda en `analytics.*`. Ese
registro es la materia prima con la que mas adelante se construira el catalogo
de consultas parametrizadas; no es un adorno de observabilidad.

```text
pregunta ->
  normalizacion              (codigo)
  clasificacion + params     (modelo rapido)
  resolucion de entidades    (PostgreSQL, sin IA)
  generacion de SQL          (modelo potente: Opus 5 o Sonnet 5)
  validacion sqlglot         (codigo)      <- frontera de seguridad
  ejecucion                  (PostgreSQL, rol de solo lectura)
  redaccion + verificacion   (modelo rapido + codigo)
-> QueryResponse
```

Solo tres etapas involucran al modelo de IA, y ninguna de ellas toca un numero.

## Portabilidad

El servicio se conecta a un PostgreSQL estandar mediante `DATABASE_URL`. Hoy ese
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

Pruebas y calidad:

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/mypy
```

## Estructura

| Ruta | Responsabilidad |
|---|---|
| `src/mira_api/api/` | Endpoints HTTP y esquemas de request/response |
| `src/mira_api/db/` | Pool de conexiones y ejecutor de solo lectura. Unica frontera con el driver |
| `src/mira_api/nlq/` | Resolucion de entidades, validador de SQL, pipeline de generacion |
| `src/mira_api/audit/` | Registro de consultas para auditoria y mejora continua |

### Cobertura publica

`GET /coverage` es un endpoint determinista respaldado por una consulta SQL
constante sobre `web.coverage_sources`. Usa su propio pool y el rol `mira_web`;
no invoca modelos, no consume cuota y no pasa por el validador de SQL generado.

Las tres conexiones del servicio tienen privilegios separados:

| Variable | Rol | Acceso |
|---|---|---|
| `DATABASE_URL` | `mira_query` | Vistas permitidas de `query` para NLQ |
| `DATABASE_URL_WEB` | `mira_web` | `web.coverage_sources` |
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
