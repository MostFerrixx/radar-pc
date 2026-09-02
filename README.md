# Radar de precios PC

Recolector diario de precios de componentes de PC en Chile, para el tablero
personal de Martín. Corre solo, en GitHub Actions, todos los días a las 11:00 UTC.

- `items.json` — qué buscar: para cada componente, la consulta a SoloTodo y las
  reglas (qué tiene que decir el nombre, qué no, qué marcas valen).
- `colector.py` — el script. Consulta la API pública de SoloTodo y elige el
  producto más barato que cumple las reglas de cada item.
- `latest.json` — el resultado de hoy. Lo lee la tarea diaria de Claude.
- `historial/` — una copia por día, como respaldo.

Para agregar o cambiar un componente se edita `items.json`; el tablero y las
alertas viven aparte, en la tarea de Claude.
