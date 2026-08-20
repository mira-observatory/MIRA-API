"""Verificacion del servicio: contrato con la base y preguntas de referencia.

Dos piezas con costos muy distintos, a proposito:

- `contract`: comprueba que la base quedo en un estado que la API puede usar.
  No llama al modelo, no cuesta nada, tarda segundos. Es lo que hay que correr
  despues de una recarga del ETL.
- `cases` + `runner`: preguntas de referencia contra el pipeline completo. Cada
  una es una llamada real a Claude con costo real, asi que nunca corre sola en
  las pruebas: se invoca a mano.
"""
