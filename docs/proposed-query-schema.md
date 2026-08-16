# Propuesta: esquema `query` para MIRA-ETL

**Estado: SQL ya escrito y pusheado a MIRA-ETL, pendiente de aplicar contra la
base real y de PR.** Este documento vive en MIRA-API porque aqui se detecto el
vacio, pero **el DDL vive en `MIRA-ETL/sql/`**, nunca aqui (ver la regla en el
[README](../README.md)). Nada de este archivo se ejecuta automaticamente.

El SQL de esta propuesta ya se escribio como archivo real y se subio a
MIRA-ETL en la rama `feat/query-layer`
(`sql/003_query_layer.sql` + `docs/query_layer_access.md`, 2026-08-15):
**https://github.com/mira-observatory/MIRA-ETL/pull/new/feat/query-layer**.
Falta que alguien con acceso a la Supabase real corra `mira-etl init-db` (crea
las vistas y los indices, no toca ninguna tabla existente) y despues siga los
pasos de `docs/query_layer_access.md` para crear el rol `mira_query` con una
contrasena real -- ese paso se dejo fuera del SQL a proposito, para que ningun
secreto quede commiteado.

## Por que existe este documento

Verificado contra `mira-observatory/MIRA-ETL` en el commit `3f5b87a` (historial
completo, 37 commits): el esquema `query` que MIRA-API necesita consultar **no
existe**. Tampoco existen las vistas, el rol de solo lectura, los indices de
trigrama, ni una tabla de diccionario semantico. Lo unico que hay hoy es:

- Esquemas `raw`, `staging`, `mart`, `audit` (`sql/001_init.sql`).
- Acceso publico via RLS directamente sobre `mart.*` a los roles `anon` /
  `authenticated` de Supabase (`sql/002_public_read_access.sql`) — exactamente lo
  que MIRA-API tiene prohibido usar.

Este documento propone como cerrar esa brecha, mapeado columna a columna contra el
`mart.*` real (no contra el contrato aspiracional del plan de arquitectura), y deja
explicitos los puntos donde el esquema actual no alcanza para cumplir el contrato
tal cual esta escrito.

## El hallazgo que cambia el diseño: los proveedores ya no son 1:1

El plan de arquitectura de MIRA-API asume que "un proceso con varios adjudicatarios
pierde informacion" como limitacion conocida. **Eso ya no es cierto en el codigo
actual de MIRA-ETL**: desde el commit `ab1bed2` (*"Enhance supplier relationship
handling with composite primary key and support for multiple suppliers in
records"*), `mart.procurement_supplier_details` tiene clave primaria compuesta
`(process_id, supplier_id)` y soporta N proveedores por proceso. El lado comprador
(`mart.procurement_buyer_details`) sigue siendo 1:1.

Esto crea una tension real con el contrato de `query.v_process` del plan, que pide
`supplier_id` / `supplier_name` como columnas planas de una vista a nivel de
proceso. Si se hace un `JOIN` directo contra `procurement_supplier_details`, un
proceso con 3 proveedores se convierte en 3 filas en `v_process` — un fanout que
nadie pidio y que no es lo mismo que el `grain` de linea de item de Costa Rica (son
dos multiplicidades distintas, y mezclarlas en una sola columna `grain` las
confunde).

**Decidido (aprobado por el dueno del producto, 2026-08-15):**

1. `query.v_process` mantiene grano de proceso. La columna `supplier_id` /
   `supplier_name` se puebla solo cuando el proceso tiene exactamente un
   proveedor; si tiene mas de uno, quedan en `NULL` y se agrega
   `supplier_count > 1` a la fila para que sea detectable.
2. Se agrega una sexta vista, `query.v_process_suppliers` (`process_id,
   supplier_id`), para responder correctamente preguntas como "cuantos contratos
   gano X" sin necesitar el fanout en `v_process`.
3. Ya se agrego `query.v_process_suppliers` a la lista blanca de
   `nlq/validator.py` (`ALLOWED_RELATIONS`) y el codigo de advertencia
   `MULTIPLE_SUPPLIERS_PER_PROCESS` a `api/schemas.py`. El SQL de esta vista
   sigue pendiente de que MIRA-ETL cree el esquema `query` (ver mas abajo).

## Vacios en `mart.*` que bloquean el contrato tal cual esta escrito

| Falta | Donde | Por que importa |
|---|---|---|
| Columna `grain` | `mart.procurement_record_core` | No existe ningun campo que distinga `PROCESS` de `LINE_ITEM`. Derivarlo con `case when country_code = 'CR' then ...` es un atajo fragil: el dia que otro pais publique a nivel de linea, la vista miente en silencio. Deberia ser una columna real que el conector de cada pais setea explicitamente. **Sigue abierto** — mi recomendacion es agregar la columna real ahora (es barato) en vez de esperar a que un segundo pais la necesite; no bloquea nada mientras tanto porque hoy solo Costa Rica usa grano de linea y eso ya se sabe por convencion. |
| ~~Nombre original de proveedor/comprador~~ | `mart.suppliers.name_normalised`, `mart.buyers.name_normalised` | **Resuelto (2026-08-15):** se usa `name_normalised` como `display_name` sin problema, no se requiere un campo adicional. |
| Cobertura por pais/periodo consultable | `audit.etl_runs` no tiene periodo estructurado mas alla del texto libre `period` | Es suficiente para `data_version` (`max(finished_at) where status='SUCCESS'`), pero para que `query.v_coverage` reporte "que paises y periodos estan realmente cargados" con precision, conviene confirmar el formato de `period` (hoy es `text`, no un rango tipado) antes de escribir la vista. **Sigue abierto.** |

El de `grain` no bloquea escribir la vista — bloquea escribir la version honesta
que el plan de arquitectura pide. Lo dejo explicito para no proponer una vista que
parezca completa pero mienta en un caso de borde.

## Propuesta de DDL (para revision, destino final: `MIRA-ETL/sql/003_query_layer.sql`)

Asume que se resuelve el vacio de `grain` agregando la columna a
`procurement_record_core` primero (migracion previa, no incluida aqui porque toca
datos existentes y la debe escribir quien conoce el estado real de cada conector).

```sql
-- 003_query_layer.sql (borrador)

create extension if not exists pg_trgm;

create schema if not exists query;

-- --------------------------------------------------------------------------
-- v_process: grano de proceso. Ver seccion "el hallazgo que cambia el diseno"
-- para la decision pendiente sobre supplier_id/supplier_name.
-- --------------------------------------------------------------------------
create or replace view query.v_process as
select
    core.process_id,
    core.country_code,
    core.source_system,
    core.source_url,
    core.grain,                          -- ver "Vacios": columna aun no existe
    core.data_quality_status,
    core.missing_fields,
    core.extracted_at,

    proc.process_number,
    proc.title,
    proc.description,
    proc.procurement_method,
    proc.process_status,
    proc.source_status,
    proc.publication_date,
    proc.closing_date,
    proc.award_date,
    proc.estimated_amount,
    proc.awarded_amount,
    proc.currency_code,
    date_trunc('month', proc.award_date)::date as award_month,

    case when buyer_fanout.buyer_count = 1 then buyer_single.buyer_id end
        as buyer_id,
    case when buyer_fanout.buyer_count = 1 then buyer_single.name_normalised end
        as buyer_name,                       -- decidido: name_normalised sirve como display_name
    case when buyer_fanout.buyer_count = 1 then buyer_single.name_normalised end
        as buyer_name_normalised,
    case when buyer_fanout.buyer_count = 1 then buyer_single.buyer_tax_id end
        as buyer_tax_id,
    coalesce(buyer_fanout.buyer_count, 0) as buyer_count,

    case when supplier_fanout.supplier_count = 1 then supplier_single.supplier_id end
        as supplier_id,
    case when supplier_fanout.supplier_count = 1 then supplier_single.name_normalised end
        as supplier_name,
    case when supplier_fanout.supplier_count = 1 then supplier_single.name_normalised end
        as supplier_name_normalised,
    case when supplier_fanout.supplier_count = 1 then supplier_single.supplier_tax_id end
        as supplier_tax_id,
    case when supplier_fanout.supplier_count = 1 then supplier_single.supplier_type end
        as supplier_type,
    coalesce(supplier_fanout.supplier_count, 0) as supplier_count,

    item.item_description,
    item.category_source,
    item.category_normalised
from mart.procurement_record_core core
join mart.procurement_process_details proc using (process_id)
left join mart.procurement_item_details item using (process_id)
left join (
    select process_id, count(*) as buyer_count
    from mart.procurement_buyer_details
    group by process_id
) buyer_fanout using (process_id)
left join lateral (
    select b.*
    from mart.procurement_buyer_details bd
    join mart.buyers b on b.buyer_id = bd.buyer_id
    where bd.process_id = core.process_id
    limit 1
) buyer_single on buyer_fanout.buyer_count = 1
left join (
    select process_id, count(*) as supplier_count
    from mart.procurement_supplier_details
    group by process_id
) supplier_fanout using (process_id)
left join lateral (
    select s.*
    from mart.procurement_supplier_details sd
    join mart.suppliers s on s.supplier_id = sd.supplier_id
    where sd.process_id = core.process_id
    limit 1
) supplier_single on supplier_fanout.supplier_count = 1;

-- Segunda y tercera vista: relacion completa proceso <-> comprador/proveedor,
-- sin aplanar. Necesarias para no perder informacion cuando buyer_count > 1 o
-- supplier_count > 1 en v_process. mart.procurement_buyer_details paso a ser
-- 1-a-muchos el 2026-08-15 (MIRA-ETL commit d483009), mismo patron que ya
-- tenia procurement_supplier_details.
create or replace view query.v_process_buyers as
select
    bd.process_id,
    b.buyer_id,
    b.name_normalised as buyer_name,
    b.buyer_tax_id
from mart.procurement_buyer_details bd
join mart.buyers b on b.buyer_id = bd.buyer_id;

create or replace view query.v_process_suppliers as
select
    sd.process_id,
    s.supplier_id,
    s.name_normalised as supplier_name,
    s.supplier_tax_id,
    s.supplier_type
from mart.procurement_supplier_details sd
join mart.suppliers s on s.supplier_id = sd.supplier_id;

-- --------------------------------------------------------------------------
-- v_buyers / v_suppliers: un candidato por entidad, con su conteo REAL. Nunca
-- fusionar aqui -- eso es exactamente lo que el caso Karro/Carro prohibe.
-- --------------------------------------------------------------------------
create or replace view query.v_buyers as
select
    b.buyer_id as entity_id,
    b.country_code,
    b.name_normalised as display_name,          -- decidido: name_normalised sirve como display_name
    b.name_normalised,
    b.buyer_tax_id as tax_id,
    count(bd.process_id) as record_count
from mart.buyers b
left join mart.procurement_buyer_details bd on bd.buyer_id = b.buyer_id
group by b.buyer_id, b.country_code, b.name_normalised, b.buyer_tax_id;

create or replace view query.v_suppliers as
select
    s.supplier_id as entity_id,
    s.country_code,
    s.name_normalised as display_name,          -- decidido: name_normalised sirve como display_name
    s.name_normalised,
    s.supplier_tax_id as tax_id,
    s.supplier_type,
    count(sd.process_id) as record_count
from mart.suppliers s
left join mart.procurement_supplier_details sd on sd.supplier_id = s.supplier_id
group by s.supplier_id, s.country_code, s.name_normalised, s.supplier_tax_id, s.supplier_type;

-- --------------------------------------------------------------------------
-- Indices de trigrama. Sin esto, T1.3 (resolucion de entidades en <150ms) no
-- es alcanzable sobre la dimension completa.
-- --------------------------------------------------------------------------
create index if not exists idx_buyers_name_trgm
    on mart.buyers using gin (name_normalised gin_trgm_ops);

create index if not exists idx_suppliers_name_trgm
    on mart.suppliers using gin (name_normalised gin_trgm_ops);

-- --------------------------------------------------------------------------
-- v_duplicate_hints: pares de nombres parecidos dentro del mismo pais, con
-- senales de por que podrian ser el mismo actor mal escrito dos veces. Nunca
-- se usa para fusionar -- solo para advertir.
-- --------------------------------------------------------------------------
create or replace view query.v_duplicate_hints as
select
    'supplier'::text as entity_type,
    a.supplier_id as entity_id_a,
    b.supplier_id as entity_id_b,
    a.country_code,
    a.name_normalised as name_a,
    b.name_normalised as name_b,
    similarity(a.name_normalised, b.name_normalised) as name_similarity,
    (a.supplier_tax_id is null or b.supplier_tax_id is null) as missing_tax_id
from mart.suppliers a
join mart.suppliers b
    on a.country_code = b.country_code
    and a.supplier_id < b.supplier_id
    and a.name_normalised % b.name_normalised   -- usa el indice GIN de arriba
where similarity(a.name_normalised, b.name_normalised) >= 0.5
union all
select
    'buyer'::text,
    a.buyer_id,
    b.buyer_id,
    a.country_code,
    a.name_normalised,
    b.name_normalised,
    similarity(a.name_normalised, b.name_normalised),
    (a.buyer_tax_id is null or b.buyer_tax_id is null)
from mart.buyers a
join mart.buyers b
    on a.country_code = b.country_code
    and a.buyer_id < b.buyer_id
    and a.name_normalised % b.name_normalised
where similarity(a.name_normalised, b.name_normalised) >= 0.5;

-- --------------------------------------------------------------------------
-- v_coverage: que pais/periodo esta realmente cargado, para distinguir "cero
-- porque no hubo" de "cero porque no tenemos el dato".
-- --------------------------------------------------------------------------
create or replace view query.v_coverage as
select
    r.source as country_code,
    r.period,
    r.status,
    r.finished_at as loaded_at,
    rc.table_name,
    rc.row_count
from audit.etl_runs r
left join audit.etl_row_counts rc on rc.run_id = r.id
where r.status = 'SUCCESS';

-- --------------------------------------------------------------------------
-- Rol de solo lectura. Deliberadamente NO hereda de anon/authenticated: si
-- heredara, tendria acceso a mart.* via las policies de 002_public_read_access.sql
-- y la lista blanca del validador de MIRA-API dejaria de ser la unica frontera.
-- --------------------------------------------------------------------------
create role mira_query login password '<definir en el gestor de secretos>' noinherit;
grant usage on schema query to mira_query;
grant select on all tables in schema query to mira_query;
alter default privileges in schema query grant select on tables to mira_query;
```

## Estrategia de actualizacion: tabla, no vista materializada

`mart.web_country_stats` ya establece el patron que usa MIRA-ETL para datos
derivados que no pueden calcularse en cada request: una tabla plana que el pipeline
actualiza con `Database.refresh_web_country_stats()` despues de cargar cada pais
(`src/mira_etl/db.py:266`). Para consistencia con ese patron ya existente en el
codigo (y porque el proyecto no usa vistas materializadas nativas en ningun otro
lado), recomiendo que `v_buyers`, `v_suppliers` y `v_duplicate_hints` sigan el mismo
camino si el `similarity()` en tiempo real resulta demasiado lento sobre la
dimension completa: convertirlas en tablas (`query.buyers_summary`, etc.)
refrescadas por un metodo `Database.refresh_query_layer()` analogo, en vez de
`MATERIALIZED VIEW` + `REFRESH` manual. `v_process` y `v_process_suppliers` se
quedan como vistas simples -- son lectura directa, no agregacion cara.

Esto es una recomendacion de consistencia de estilo, no un bloqueante: la version
con `CREATE VIEW` de arriba es correcta y mas simple para arrancar; se puede migrar
a tabla + refresh despues si el `EXPLAIN` en produccion lo pide.

## Que sigue pendiente

1. ~~Aprobar la fila-por-proveedor en `v_process` vs `v_process_suppliers`~~ —
   **decidido** 2026-08-15: la segunda opcion. Ya implementado en el validador y
   en el contrato de respuesta de MIRA-API.
2. ~~Nombre "original" de proveedor/comprador~~ — **resuelto** 2026-08-15:
   `name_normalised` se usa como `display_name`, no hace falta un campo nuevo.
3. La columna `grain` en `mart.procurement_record_core` sigue sin existir. No
   bloquea nada hoy (solo Costa Rica usa grano de linea), pero recomiendo
   agregarla como columna real antes de que un segundo pais la necesite, en vez
   de inferirla por `country_code` dentro de la vista.
4. Decidir el formato de `audit.etl_runs.period` si se quiere que `v_coverage`
   reporte rangos de fecha en vez de la etiqueta de texto libre actual.
5. ~~Coordinar con MIRA-ETL el cambio analogo en `mart.procurement_buyer_details`~~
   -- **resuelto** 2026-08-15: MIRA-ETL vacio la base y la recreo desde cero con
   el esquema nuevo (commit `d483009`, `feat: Support multiple buyers in
   procurement records and update related logic and tests`, en `main`).
   Verificado corriendo su suite completa (32 tests, incluye
   `test_buyer_relationship_has_composite_primary_key` y los casos de
   `buyer_records_for`): todos pasan. Como consecuencia, `v_process` y
   `v_process_suppliers` de este documento ya se actualizaron para tratar
   comprador igual que proveedor (fanout + `query.v_process_buyers`), y el
   validador y el `Warning` de MIRA-API ya incluyen la vista y el codigo nuevo.
