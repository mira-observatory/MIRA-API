# Esquema `query` de MIRA-ETL

**Estado: ya existe y esta aplicado en produccion**, verificado el 2026-08-15
contra la Supabase real (`mart.processes` tiene 7,992 filas cargadas). El DDL
vive exclusivamente en `MIRA-ETL/sql/`, nunca en este repo (ver la regla en el
[README](../README.md)). Este documento describe lo que hay, no propone nada
que MIRA-API deba escribir.

## Modelo de datos: proceso -> adjudicacion -> proveedor/item

`mart` se reestructuro el 2026-08-15 (MIRA-ETL PR #1,
`feature/part-a-query-schema`, mergeado a `main`). Ya no es "un proceso tiene
un comprador y un proveedor": ahora un proceso puede tener varias
**adjudicaciones** (`mart.awards`), y cada adjudicacion tiene sus propios
proveedores e items. **El monto adjudicado vive en la adjudicacion, no en el
proceso** -- `estimated_amount` si esta en `v_process` (es del proceso, antes
de adjudicar), pero `awarded_amount` solo existe en `v_awards`.

Consecuencia directa para generar SQL: **"cuanto se gasto" nunca es una
columna de `v_process`**, siempre requiere
`v_process -> v_awards -> v_award_suppliers` (y `v_award_items` si se filtra
por tipo de bien/servicio).

Esto tambien resolvio, sin que MIRA-API tuviera que hacer nada, el problema de
`grain` que este documento documentaba como pendiente: Costa Rica ya no
duplica la fila de proceso por cada linea de compra (`sql/relational_awards_csv.py`,
antes `transform_cr.py`) -- cada linea es su propio `item`/`award` bajo el
mismo `process_id`. `mart.processes` no tiene columna `grain` porque ya no
hace falta: un proceso es un proceso para los cuatro paises.

## Las 8 vistas (`sql/002_indexes_and_views.sql` en MIRA-ETL)

Normalizadas, casi 1:1 con `mart.*`. Nada de arrays, nada de columnas que se
ponen en `NULL` cuando hay mas de un comprador/proveedor -- todo join
explicito, para que la resolucion de "hay varios" quede en la consulta que
genera el modelo, no escondida en la vista:

| Vista | Refleja | Uso tipico |
|---|---|---|
| `query.v_process` | `mart.processes` | Datos del procedimiento: titulo, estado, fechas, `estimated_amount` |
| `query.v_buyers` | `mart.buyers` | Entidad compradora (dimension) |
| `query.v_suppliers` | `mart.suppliers` | Entidad proveedora (dimension) |
| `query.v_process_buyers` | `mart.process_buyers` | Que comprador(es) tiene un proceso (1 a muchos) |
| `query.v_items` | `mart.items` | Bien/servicio contratado, una fila por linea |
| `query.v_awards` | `mart.awards` | Cada adjudicacion: fecha, `awarded_amount`, `currency_code` |
| `query.v_award_items` | `mart.award_items` | Que item(s) cubre una adjudicacion |
| `query.v_award_suppliers` | `mart.award_suppliers` | Que proveedor(es) gano una adjudicacion (1 a muchos) |

`nlq/validator.py` (`ALLOWED_RELATIONS`) ya tiene exactamente estas 8.

### `name_normalised` ya sirve como nombre para mostrar

Cambio importante en el mismo rediseno: `mart.suppliers.name_normalised` y
`mart.buyers.name_normalised` **ya no** se normalizan a mayusculas/sin
acentos/sin sufijos legales. Solo se normaliza la representacion Unicode y se
colapsan espacios -- conservan mayusculas, acentos y puntuacion tal como los
publico la fuente. Es decir, `name_normalised` es ahora literalmente la
"grafia original" que pedia el contrato de `EntityCandidate.display_name`, sin
necesidad de un campo aparte.

La contraparte: la deduplicacion agresiva que antes hacia el ETL al cargar
("Constructora S.A." y "CONSTRUCTORA SA" colapsaban al mismo `supplier_id`) ya
no ocurre. Dos grafías distintas de la misma entidad producen dos filas de
dimension distintas. Eso empuja la comparacion difusa (case/accent-insensitive)
a tiempo de consulta, en la resolucion de entidades de MIRA-API (Hito 1) --
comparar contra `lower(unaccent(name_normalised))`, nunca asumir que la
columna ya viene normalizada para matching.

## Que NO se construye: `query.v_duplicate_hints`

Decision de producto (2026-08-15): no se senala cuando dos entidades con
nombre parecido podrian ser la misma ("Karro y Limon S.A." vs "Carro y Limon
S.A."). Nombres parecidos pueden ser entidades distintas a proposito, y no se
quiere que el sistema sugiera lo contrario. `EntityCandidate` ya no tiene
`similar_to`, y `Warning` ya no tiene `POSSIBLE_DUPLICATE_ENTITY`.

Esto **no** elimina la necesidad de indices de trigrama (ver mas abajo): la
busqueda difusa para *encontrar* la entidad que el usuario escribio
(`match_method: NAME_FUZZY`) sigue haciendo falta. Lo que no se hace es
comparar entidades ya guardadas entre si para advertir sobre posibles
duplicados.

## Lo que todavia falta en MIRA-ETL

| Falta | Por que importa | Bloquea |
|---|---|---|
| `query.v_coverage` | Sin ella, MIRA-API no puede distinguir "cero real" de "cero porque el ETL no cargo ese pais/periodo" -- publicaria un cero como si fuera un hecho verificado cuando en realidad es un vacio de datos. Se construiria sobre `audit.etl_runs` / `audit.etl_row_counts`. | Cualquier respuesta agregada honesta sobre un pais/periodo con pocos datos |
| Indices de trigrama sobre `mart.suppliers.name_normalised` / `mart.buyers.name_normalised` | Sin indice, la busqueda difusa escanea toda la tabla de dimension calculando similitud fila por fila -- funciona pero se vuelve lenta al crecer los datos. Necesitan construirse sobre una expresion (`lower(unaccent(name_normalised))`), no la columna cruda, para que "KARRO" y "Karro" comparen igual. | Que T1.3 (resolucion de entidades) responda rapido |

Ninguna de las dos bloquea empezar a construir Hito 1 -- solo su rendimiento /
completitud final. Se puede escribir la logica de resolucion de entidades
ahora mismo contra las vistas que ya existen.

## Roles

`mira_query` y `mira_logger` ya existen en produccion, correctamente
delimitados (`mira_query`: `LOGIN NOINHERIT`, solo `USAGE`/`SELECT` sobre
`query.*`, sin nada en `mart`/`raw`/`staging`/`audit`; `mira_logger`: solo
`analytics.*`). Documentado y verificado en
`MIRA-ETL/docs/database_security.md`. Las contrasenas se fijan manualmente por
ambiente, nunca se commitean.
