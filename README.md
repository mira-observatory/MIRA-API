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

**Dependencia bloqueante:** el esquema `query` (vistas `v_process`, `v_buyers`,
`v_suppliers`, `v_duplicate_hints`, `v_coverage`), los indices de trigrama y el rol
de solo lectura `mira_query` los crea [MIRA-ETL](https://github.com/mira-observatory/MIRA-ETL),
no este repo. A la fecha, MIRA-ETL solo tiene los esquemas `raw`, `staging`, `mart`
y `audit` (ver `sql/001_init.sql`); el esquema `query` todavia no existe alli. Hasta
que se cree, este servicio se desarrolla contra un PostgreSQL local sembrado con
`sql/001_init.sql` del ETL mas un script de vistas provisional que **no vive en este
repo** (el DDL de las vistas es responsabilidad exclusiva de MIRA-ETL).

Hay una propuesta de esas vistas, mapeada columna a columna contra el `mart.*` real
de hoy, en [`docs/proposed-query-schema.md`](docs/proposed-query-schema.md) — es un
borrador para que alguien lo lleve como PR a MIRA-ETL, no se ejecuta desde aqui.

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

El servicio se conecta a un PostgreSQL estandar mediante `DATABASE_URL`. Hoy ese
PostgreSQL esta alojado en Supabase; **no se usa ninguna funcionalidad propia de
Supabase** (ni PostgREST, ni auth, ni RLS como autorizacion de la aplicacion). El
dia de la migracion cambia el valor de una variable de entorno.

La unica frontera que toca el driver es `src/mira_api/db/`. El resto del servicio
nunca importa `psycopg`.

## Desarrollo

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp .env.example .env      # y completa los valores
.venv/Scripts/uvicorn mira_api.main:app --reload
```

Pruebas y calidad:

```bash
.venv/Scripts/pytest
.venv/Scripts/ruff check src tests
.venv/Scripts/mypy
```

## Estructura

| Ruta | Responsabilidad |
|---|---|
| `src/mira_api/api/` | Endpoints HTTP y esquemas de request/response |
| `src/mira_api/db/` | Pool de conexiones y ejecutor de solo lectura. Unica frontera con el driver |
| `src/mira_api/nlq/` | Resolucion de entidades, generacion y validacion de SQL, pipeline de la consulta |
| `src/mira_api/audit/` | Registro de consultas para auditoria y mejora continua |

## Lo que este repositorio nunca hace

- **Nunca ejecuta DDL.** Todo el esquema, las vistas de consulta y los roles viven
  en `MIRA-ETL/sql/`.
- **Nunca normaliza nombres de entidades.** Esa logica es del ETL; aqui se consumen
  las columnas ya normalizadas.
- **Nunca consulta `mart`, `raw`, `staging` ni `audit` directamente.** Solo el
  esquema `query`, y el rol de base de datos lo hace imposible de violar.

## Licencia

MIT. Ver [LICENSE](LICENSE).
