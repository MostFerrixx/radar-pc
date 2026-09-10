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

## Radar PS5 jailbreak

En el mismo repositorio y en la misma corrida diaria, pero aparte: `colector_ps5.py`
mira si aparecio algo real para la PS5 en firmware 13.x. Corre con
`continue-on-error`, asi que si falla los precios se guardan igual.

- Fuentes: la API de GitHub (repos y releases de Gezine y EchoStretch, releases de
  Y2JB / BD-JB5 / P2JB-Y2JB-Porting, y una busqueda de repos de PS5 tocados hace
  poco) mas la pagina de Wololo. **psdevwiki quedo fuera**: esta detras de un muro
  anti-bots que le responde 403 a cualquier robot.
- `ps5/snap-*.txt` es la copia del dia anterior; `ps5/latest.json` trae las lineas
  NUEVAS respecto a esa copia, que es lo unico que hay que leer. Una fuente caida
  no actualiza su copia, para que al dia siguiente no parezca que todo es nuevo.
- Lo lee la tarea **semanal** de Claude "Radar PS5 Jailbreak (13.40)", los lunes,
  que decide si amerita avisar. El robot recolecta; el juicio lo hace Claude.
