-- Corrige el CHECK de `outcome` en analytics.query_log y analytics.query_attempt.
--
-- El problema: MIRA-API agrego REJECTED_SQL_COUNTRY_SCOPE a su taxonomia (la
-- consulta genero SQL sin el filtro de country_code, o filtrando un pais fuera
-- del pedido), pero el CHECK en la base sigue con los 16 valores originales.
-- El INSERT de auditoria falla contra el constraint, y como esa escritura es
-- deliberadamente fire-and-forget, falla EN SILENCIO: el usuario recibe su
-- respuesta normal y el rechazo nunca queda registrado.
--
-- Lo que se pierde es justo lo que hay que vigilar: REJECTED_SQL_COUNTRY_SCOPE
-- esta en MUST_BE_ZERO (src/mira_api/audit/outcomes.py), la lista de codigos
-- que deben ser cero en operacion normal y que disparan alerta. Hoy siempre
-- se ve como cero, este roto o no.
--
-- Verificado contra produccion el 2026-08-19: ambos constraints aceptan 16 de
-- los 17 valores de la taxonomia.
--
-- Es DDL sobre datos en produccion. No borra ni reescribe filas: solo cambia
-- la regla de validacion para aceptar un valor mas. Las filas existentes ya
-- cumplen el constraint nuevo (es un superconjunto del viejo), asi que el
-- ALTER no puede fallar por datos preexistentes.
--
-- Correr con un administrador de PostgreSQL, en una transaccion.

begin;

alter table analytics.query_log
    drop constraint if exists query_log_outcome_check;

alter table analytics.query_log
    add constraint query_log_outcome_check check (outcome in (
        'OK',
        'OK_ZERO_ROWS',
        'OK_DEGRADED_NARRATIVE',
        'OUT_OF_SCOPE',
        'REJECTED_ENTITY_NOT_FOUND',
        'REJECTED_ENTITY_AMBIGUOUS',
        'REJECTED_SQL_PARSE',
        'REJECTED_SQL_NOT_SELECT',
        'REJECTED_SQL_RELATION',
        'REJECTED_SQL_FUNCTION',
        'REJECTED_SQL_COST',
        'REJECTED_SQL_COUNTRY_SCOPE',
        'FAILED_DB_TIMEOUT',
        'FAILED_DB_ERROR',
        'FAILED_LLM_ERROR',
        'THROTTLED_QUOTA',
        'THROTTLED_BUDGET'
    ));

alter table analytics.query_attempt
    drop constraint if exists query_attempt_outcome_check;

alter table analytics.query_attempt
    add constraint query_attempt_outcome_check check (outcome in (
        'OK',
        'OK_ZERO_ROWS',
        'OK_DEGRADED_NARRATIVE',
        'OUT_OF_SCOPE',
        'REJECTED_ENTITY_NOT_FOUND',
        'REJECTED_ENTITY_AMBIGUOUS',
        'REJECTED_SQL_PARSE',
        'REJECTED_SQL_NOT_SELECT',
        'REJECTED_SQL_RELATION',
        'REJECTED_SQL_FUNCTION',
        'REJECTED_SQL_COST',
        'REJECTED_SQL_COUNTRY_SCOPE',
        'FAILED_DB_TIMEOUT',
        'FAILED_DB_ERROR',
        'FAILED_LLM_ERROR',
        'THROTTLED_QUOTA',
        'THROTTLED_BUDGET'
    ));

commit;

-- Comprobacion despues de correrlo: debe devolver 17 en las dos filas.
--
-- select rel.relname,
--        (length(pg_get_constraintdef(con.oid))
--         - length(replace(pg_get_constraintdef(con.oid), ''',''', ''))) / 2 + 1 as valores
-- from pg_constraint con
-- join pg_class rel on rel.oid = con.conrelid
-- join pg_namespace n on n.oid = rel.relnamespace
-- where n.nspname = 'analytics'
--   and rel.relname in ('query_log', 'query_attempt')
--   and con.conname like '%outcome%';
