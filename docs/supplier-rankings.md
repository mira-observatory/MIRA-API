# Rankings de proveedores

La pregunta general por el proveedor que mas dinero ha ganado usa el acumulado
adjudicado del historial cargado. Una pregunta explicita por "una adjudicacion"
usa el monto individual. Nunca se presentan adjudicaciones como pagos comprobados.

`query.v_supplier_award_totals`, mantenida por MIRA-ETL, ofrece acumulados exactos
por pais, proveedor y moneda. El SQL generado no suma dinero libremente: lee
estos valores ya calculados. Sin moneda solicitada devuelve el lider de cada
moneda mediante `DISTINCT ON (currency_code)`, sin recortar a un ganador global.
Con moneda explicita puede devolver un solo lider. Se bloquean las nuevas
sumas, conversiones y JOIN sobre este resumen para evitar duplicaciones.

El resumen cubre todo el historial al corte `refreshed_at`. Acumulados de un
periodo o de estados excluidos no estan soportados: se devuelve OUT_OF_SCOPE
antes que sustituirlos por el total historico de adjudicaciones validas.

Una adjudicacion compartida se asocia una sola vez a cada proveedor; no existe
desglose de participacion individual. La respuesta debe aclararlo cuando
`shared_award_count` sea mayor que cero. No se pueden sumar los totales de varios
proveedores para obtener gasto del Estado.

Desplegar primero la tabla, vista, indices, permisos, diccionario y ETL descritos
en MIRA-ETL/docs/digitalocean.md. Inicializar los acumulados antes de activar la
API: no deben presentarse ceros por una tabla de resumen todavia vacia.
