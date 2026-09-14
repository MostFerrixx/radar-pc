#!/usr/bin/env python3
"""
Recolector del Radar iPhone: conviene comprarlo en Chile o en EE.UU.

Corre en GitHub Actions una vez al dia y deja iphone/latest.json. No decide
nada: solo junta cuatro cosas y anota si cada fuente respondio o no.

  1. Apple EE.UU.  precio de lista del iPhone Pro Max, sin impuesto de venta.
  2. Apple Chile   precio de lista del mismo equipo, con IVA incluido.
  3. SoloTodo      el precio mas barato de verdad en el retail chileno.
  4. mindicador    el dolar observado del Banco Central.

El juicio (veredicto, tablero, avisos) lo hace la tarea diaria de Claude, que
solo lee este archivo.

Uso:  python3 colector_iphone.py   -> escribe iphone/latest.json
                                      y iphone/historial/AAAA-MM-DD.json
"""
import json, re, os, sys, time, datetime, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor

API = "https://publicapi.solotodo.com"
CAT_CELULARES = 6
TIMEOUT = 45
UA = {"User-Agent": "Mozilla/5.0 (radar-iphone/1.0; github actions; uso personal, 1 vez al dia)",
      "Accept-Language": "es-CL,es;q=0.9,en;q=0.8"}

# Modelos que sigue el radar. El Pro y el Pro Max comparten la misma pagina de
# Apple, asi que hay que distinguirlos: "apple_familia" es el campo familyType
# del catalogo (iphone18promax), y "pulgadas" es el respaldo.
MODELOS = [
    {
        "id": "17-pro-max",
        "nombre": "iPhone 17 Pro Max",
        "apple_slug": "iphone-17-pro",
        "apple_familia": "iphone17promax",
        "pulgadas": "6.9",
        "descontinuado_apple": True,   # Apple lo saco el 9-sep-2026
        "solotodo_busqueda": "iphone 17 pro max",
        "incluye": ["17 pro max"],
        "capacidades": ["256 GB", "512 GB", "1 TB"],
    },
    {
        "id": "18-pro-max",
        "nombre": "iPhone 18 Pro Max",
        "apple_slug": "iphone-18-pro",
        "apple_familia": "iphone18promax",
        "pulgadas": "6.9",
        "descontinuado_apple": False,
        "solotodo_busqueda": "iphone 18 pro max",
        "incluye": ["18 pro max"],
        "capacidades": ["256 GB", "512 GB", "1 TB"],
    },
]

# Martin acepta nuevo y open box, pero NO reacondicionado. Estas palabras en el
# nombre del producto lo sacan de la comparacion.
VETO_CONDICION = ["reacondicion", "refurbish", "renewed", "seminuevo", "semi nuevo",
                  "usado", "reparado", "grado a", "grado b", "openbox"]

# Tiendas cuyo precio NO es el de un equipo nuevo suelto:
#  - operadoras: publican el precio SUBSIDIADO CON PLAN. En la corrida del
#    14-sep-2026 eso metio un "17 Pro Max 256 GB" a $899.900 en Claro y uno de
#    512 GB a $129.990 en Movistar One.
#  - reacondicionadores: Reuse es la marca de reacondicionados de Falabella y
#    BackOnline vende "iPhone reacondicionados". Martin acepta nuevo y open box,
#    reacondicionado no, y el nombre del producto no siempre lo dice: hay que
#    mirar la tienda.
VETO_TIENDA = ["claro", "movistar", "entel", "wom",
               "reuse", "backonline"]

# Piso y techo por capacidad, en pesos. Un Pro Max nuevo no baja de estos
# valores: lo que quede abajo es precio con plan, un accesorio o un error.
PISO_CLP = {"256 GB": 900000, "512 GB": 1000000, "1 TB": 1200000}
TECHO_CLP = 4000000


# ---------- red ---------------------------------------------------------------
def bajar(url, intentos=3, json_=True):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                crudo = r.read().decode("utf-8", "replace")
            return json.loads(crudo) if json_ else crudo
        except Exception as e:
            ultimo = e
            time.sleep(3 * (i + 1))
    raise RuntimeError("%s -> %s" % (url, ultimo))


def num(x):
    try:
        return int(round(float(x)))
    except Exception:
        return None


def id_desde_url(u):
    m = re.search(r"/(\d+)/?$", str(u or ""))
    return int(m.group(1)) if m else None


# ---------- 1 y 2: precios de lista de Apple ----------------------------------
def _objeto_json_desde(texto, inicio):
    """Devuelve el objeto JSON que empieza en la llave de posicion 'inicio',
    contando llaves y respetando comillas. Apple mete su catalogo en un
    javascript, asi que no sirve un regex a secas."""
    prof, en_texto, escape = 0, False, False
    for i in range(inicio, len(texto)):
        c = texto[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            en_texto = not en_texto
            continue
        if en_texto:
            continue
        if c == "{":
            prof += 1
        elif c == "}":
            prof -= 1
            if prof == 0:
                return texto[inicio:i + 1]
    return None


def _json_tras_clave(texto, clave):
    """El objeto JSON que sigue a 'clave' en la pagina. Apple envuelve su
    catalogo en un objeto JAVASCRIPT (window.PRODUCT_SELECTION_BOOTSTRAP = {
    productSelectionData: {...} }) cuya llave externa NO lleva comillas, asi que
    json.loads se cae si uno arranca ahi. Hay que entrar un nivel, al valor de
    productSelectionData, que si es JSON valido. Ese fue el bug de la primera
    version: el parseo bueno se caia siempre y quedaba el plan B, que invento
    precios de 395 y 907 dolares."""
    p = texto.find(clave)
    if p < 0:
        return None
    inicio = texto.find("{", p)
    if inicio < 0:
        return None
    crudo = _objeto_json_desde(texto, inicio)
    if not crudo:
        return None
    try:
        return json.loads(crudo)
    except Exception:
        return None


def _cap_apple(t):
    """'256gb' -> '256 GB' ; '1tb' -> '1 TB'."""
    m = re.fullmatch(r"(\d+)\s*(gb|tb)", str(t or "").strip(), re.I)
    return "%s %s" % (m.group(1), m.group(2).upper()) if m else None


def apple_desde_catalogo(html, familia, digitos_pantalla):
    """Camino bueno, verificado el 14-sep-2026 en apple.com y apple.com/cl.

    El catalogo trae dos piezas separadas que hay que cruzar:
      - fichas de producto: {partNumber, dimensionCapacity: "512gb",
        dimensionScreensize: "6_9inch", familyType: "iphone18promax",
        fullPrice: "<clave>"}   <- fullPrice es una CLAVE, no un monto
      - mapa "prices": {"<clave>": {currentPrice: {raw_amount: "1499.00"}, ...}}

    En EE.UU. la misma capacidad aparece con precio de operadora y desbloqueado;
    solo sirve el desbloqueado. En Chile no hay variantes de operadora.
    Devuelve {capacidad: precio} o None."""
    datos = _json_tras_clave(html, "productSelectionData:")
    if datos is None:
        return None

    precios_map, fichas = {}, []

    def recorrer(o):
        if isinstance(o, dict):
            if not precios_map and isinstance(o.get("prices"), dict):
                precios_map.update(o["prices"])
            if isinstance(o.get("dimensionCapacity"), str) and isinstance(o.get("fullPrice"), str):
                fichas.append(o)
            for v in o.values():
                recorrer(v)
        elif isinstance(o, list):
            for v in o:
                recorrer(v)

    recorrer(datos)
    if not precios_map or not fichas:
        return None

    def monto(clave):
        e = precios_map.get(clave)
        if not isinstance(e, dict):
            return None
        cp = e.get("currentPrice")
        if isinstance(cp, dict) and cp.get("raw_amount"):
            v = num(cp["raw_amount"])
            if v:
                return v
        return num(e.get("amountBeforeTradeIn"))

    # 1) quedarse con las fichas del modelo pedido (Pro y Pro Max comparten pagina)
    mias = []
    for f in fichas:
        fam = ("%s %s" % (f.get("familyType") or "", f.get("productLocatorFamily") or "")).lower()
        pant = re.sub(r"\D", "", str(f.get("dimensionScreensize") or ""))
        if familia and familia.lower() in fam:
            mias.append(f)
        elif not familia and digitos_pantalla and pant == digitos_pantalla:
            mias.append(f)
    if not mias:
        return None

    # 2) si hay variantes desbloqueadas, las de operadora no cuentan
    desbloq = [f for f in mias if "unlocked" in f["fullPrice"].lower()]
    if desbloq:
        mias = desbloq

    precios = {}
    for f in mias:
        cap = _cap_apple(f.get("dimensionCapacity"))
        v = monto(f["fullPrice"])
        if not cap or not v:
            continue
        precios[cap] = min(precios.get(cap, v), v)
    return precios or None


# Rangos plausibles de precio segun el pais, para no confundir un precio con
# una capacidad, un numero de cuotas o un codigo. Apple siempre imprime los
# montos con signo peso o dolar, asi que el respaldo se ancla en el simbolo.
RANGO = {"": (200, 6000), "cl/": (200000, 6000000)}


def apple_por_cercania(html, pulgadas, region):
    """Plan B, solo si el catalogo no aparece: para cada capacidad, el monto que
    aparece mas veces cerca de ella. Es tosco y ya se equivoco una vez, asi que
    el metodo queda anotado en la salida para que la tarea de Claude desconfie."""
    trozo = html
    if pulgadas:
        marcas = [m.start() for m in re.finditer(
            pulgadas.replace(".", r"[.,]") + r"\s*(-?\s*inch|pulgadas)", html, re.I)]
        if marcas:
            trozo = html[marcas[0]: marcas[-1] + 20000]

    bajo, alto = RANGO.get(region, (200, 6000000))
    precios = {}
    for cap in ["256 GB", "512 GB", "1 TB", "2 TB"]:
        n, u = cap.split()
        patron = re.compile(r"%s\s*%s\b" % (n, u), re.I)
        votos = {}
        for m in patron.finditer(trozo):
            # una ventana corta y un solo voto por aparicion: el precio va
            # pegado a su capacidad, y una ventana ancha se come la de al lado
            ini, fin = max(0, m.start() - 120), m.end() + 260
            ventana = trozo[ini:fin]
            ancla = m.start() - ini
            cerca = None
            # solo montos con simbolo de moneda: descarta capacidades sueltas,
            # numeros de cuotas y codigos de producto
            for cand in re.finditer(r"\$\s*(\d[\d.,]*)", ventana):
                bruto = cand.group(1).rstrip(".,")
                # un numero pegado a GB/TB no es un precio
                if re.match(r"\s*(gb|tb)\b", ventana[cand.end():cand.end() + 6], re.I):
                    continue
                v = _monto(bruto)
                if not v or not (bajo <= v <= alto):
                    continue
                d = abs(cand.start() - ancla)
                if cerca is None or d < cerca[0]:
                    cerca = (d, v)
            if cerca:
                votos[cerca[1]] = votos.get(cerca[1], 0) + 1
        if votos:
            # el monto mas repetido; a empate, el mas bajo (el precio de lista
            # aparece mas que las cuotas, que varian)
            precios[cap] = sorted(votos.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return precios or None


def _monto(s):
    """'1,299.00' -> 1299 ; '1.699.990' -> 1699990."""
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):        # chileno
        return num(s.replace(".", ""))
    if re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d{1,2})?", s):  # gringo
        return num(s.replace(",", ""))
    if re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return num(s)
    return None


def precios_apple(slug, pulgadas, region, familia=None, descontinuado=False):
    """region: '' para EE.UU., 'cl/' para Chile.
    Un modelo descontinuado ya no tiene pagina: un solo intento, sin reintentos."""
    url = "https://www.apple.com/%sshop/buy-iphone/%s" % (region, slug)
    salida = {"ok": False, "metodo": None, "precios": {}, "error": None, "url": url}
    try:
        html = bajar(url, intentos=1 if descontinuado else 3, json_=False)
    except Exception as e:
        salida["error"] = ("descontinuado, la pagina ya no existe: %s" if descontinuado
                           else "no respondio: %s") % e
        return salida
    dig = re.sub(r"\D", "", pulgadas or "")
    # Un modelo descontinuado NO puede usar el plan B. La pagina del 17 Pro Max
    # sigue respondiendo (Apple la dejo viva) pero ya no trae su catalogo, asi
    # que el plan B se ponia a adivinar y devolvia 395 y 907 dolares como si
    # fueran precios. Mejor decir "no esta" que inventar.
    caminos = [("catalogo", lambda h: apple_desde_catalogo(h, familia, dig))]
    if not descontinuado:
        caminos.append(("cercania", lambda h: apple_por_cercania(h, pulgadas, region)))
    for metodo, fn in caminos:
        try:
            p = fn(html)
        except Exception as e:
            print("aviso: %s %s fallo: %s" % (slug, metodo, e), file=sys.stderr)
            p = None
        if p:
            salida.update({"ok": True, "metodo": metodo, "precios": p})
            return salida
    salida["error"] = (("descontinuado: la pagina responde (%d bytes) pero ya no "
                        "trae el catalogo de este modelo") if descontinuado
                       else "pagina descargada (%d bytes) pero no encontre precios") % len(html)
    return salida


# ---------- 3: SoloTodo -------------------------------------------------------
def mapa_tiendas():
    m = {}
    try:
        for s in bajar(API + "/stores/"):
            i = s.get("id") or id_desde_url(s.get("url"))
            if i is not None:
                m[int(i)] = s.get("name") or str(i)
    except Exception as e:
        print("aviso: no pude leer /stores/:", e, file=sys.stderr)
    return m


def ids_monedas():
    clp = usd = None
    try:
        for c in bajar(API + "/currencies/"):
            iso = (c.get("iso_code") or "").upper()
            i = c.get("id") or id_desde_url(c.get("url"))
            if iso == "CLP":
                clp = int(i)
            elif iso == "USD":
                usd = int(i)
    except Exception:
        pass
    return clp, usd


def ofertas_de(pid, tiendas):
    d = bajar(API + "/products/available_entities/?ids=%s" % pid)
    ofertas = []
    for res in d.get("results", []):
        for e in res.get("entities", []):
            ar = e.get("active_registry") or {}
            if not ar.get("is_available"):
                continue
            v = num(ar.get("offer_price"))
            if v is None:
                continue
            sid = e.get("store_id") or id_desde_url(e.get("store"))
            ofertas.append({"precio": v,
                            "tienda": tiendas.get(int(sid), "tienda %s" % sid) if sid is not None else "?",
                            "url": e.get("external_url")})
    ofertas.sort(key=lambda o: o["precio"])
    return ofertas


def precio_clp_meta(meta, clp, usd):
    """El precio en pesos de un bloque metadata de browse. Si el catalogo de
    monedas no ayuda, se queda con el valor grande: CLP es ~900 veces USD."""
    vals, pc = [], None
    for p in (meta or {}).get("prices_per_currency", []):
        cid = p.get("currency_id") or id_desde_url(p.get("currency"))
        try:
            v = float(p.get("offer_price") or 0)
        except Exception:
            v = 0
        if not v:
            continue
        vals.append(v)
        if cid is not None and clp is not None and cid == clp:
            pc = v
    if pc is None and vals:
        pc = max(vals)
    return num(pc) if pc else None


def solotodo_modelo(modelo, tiendas, clp, usd):
    """Para cada capacidad del modelo, el producto mas barato con stock que
    cumple las reglas. Devuelve {capacidad: dato} y la lista de descartados."""
    url = ("%s/categories/%d/browse/?search=%s&ordering=offer_price_usd&page_size=60"
           % (API, CAT_CELULARES, urllib.parse.quote(modelo["solotodo_busqueda"])))
    salida = {"ok": False, "error": None, "por_capacidad": {}, "descartados": [], "url": url}
    try:
        d = bajar(url)
    except Exception as e:
        salida["error"] = "no respondio: %s" % e
        return salida
    salida["ok"] = True

    vistos = {}
    for res in d.get("results", []):
        for pe in res.get("product_entries", []):
            p = pe.get("product") or {}
            nombre = p.get("name") or ""
            n = nombre.lower()
            precio = precio_clp_meta(pe.get("metadata") or {}, clp, usd)
            if precio is None:
                continue
            # la busqueda de SoloTodo es difusa: devuelve 15 y 16 Pro Max tambien
            if not all(t in n for t in modelo["incluye"]):
                salida["descartados"].append({"nombre": nombre, "precio": precio,
                                              "motivo": "no es %s" % modelo["nombre"]})
                continue
            veto = next((v for v in VETO_CONDICION if v in n), None)
            if veto:
                salida["descartados"].append({"nombre": nombre, "precio": precio,
                                              "motivo": "condicion vetada (%s)" % veto})
                continue
            cap = next((c for c in modelo["capacidades"]
                        if re.search(r"\b%s\s*%s\b" % tuple(c.split()), n, re.I)), None)
            if not cap:
                salida["descartados"].append({"nombre": nombre, "precio": precio,
                                              "motivo": "capacidad fuera del radar"})
                continue
            actual = vistos.get(cap)
            if actual is None or precio < actual["precio_lista"]:
                vistos[cap] = {"product_id": p.get("id"), "nombre": nombre, "precio_lista": precio}

    # el precio de browse es referencial; el de verdad sale de las tiendas con stock
    for cap, c in vistos.items():
        try:
            ofs = ofertas_de(c["product_id"], tiendas)
        except Exception as e:
            ofs = []
            salida["descartados"].append({"nombre": c["nombre"], "motivo": "sin tiendas: %s" % e})

        # Filtrar oferta por oferta antes de elegir la mas barata: una operadora
        # o un precio bajo el piso contaminan el resultado si se cuelan primero.
        buenas = []
        for o in ofs:
            t = (o.get("tienda") or "").lower()
            veto = next((v for v in VETO_TIENDA if v in t), None)
            if veto:
                salida["descartados"].append({
                    "nombre": c["nombre"], "precio": o["precio"], "tienda": o["tienda"],
                    "motivo": "tienda vetada (%s): no vende el equipo nuevo suelto" % veto})
                continue
            piso = PISO_CLP.get(cap, 900000)
            if o["precio"] < piso:
                salida["descartados"].append({
                    "nombre": c["nombre"], "precio": o["precio"], "tienda": o["tienda"],
                    "motivo": "bajo el piso de %s para %s: no es el equipo solo" % (piso, cap)})
                continue
            if o["precio"] > TECHO_CLP:
                salida["descartados"].append({
                    "nombre": c["nombre"], "precio": o["precio"], "tienda": o["tienda"],
                    "motivo": "sobre el techo de %s" % TECHO_CLP})
                continue
            buenas.append(o)

        if buenas:
            salida["por_capacidad"][cap] = {
                "producto": c["nombre"], "precio": buenas[0]["precio"],
                "tienda": buenas[0]["tienda"], "url": buenas[0]["url"],
                "n_tiendas": len(buenas)}
        elif ofs:
            salida["descartados"].append({"nombre": c["nombre"], "precio": ofs[0]["precio"],
                                          "motivo": "todas las ofertas quedaron fuera por tienda o por precio"})
        else:
            salida["descartados"].append({"nombre": c["nombre"], "precio": c["precio_lista"],
                                          "motivo": "en lista pero ninguna tienda con stock"})
    return salida


# ---------- 4: dolar ----------------------------------------------------------
def dolar_observado():
    s = {"ok": False, "valor": None, "fecha": None, "fuente": "mindicador.cl", "error": None}
    try:
        d = bajar("https://mindicador.cl/api/dolar")
        serie = d.get("serie") or []
        if serie:
            s.update({"ok": True, "valor": round(float(serie[0]["valor"]), 2),
                      "fecha": str(serie[0].get("fecha", ""))[:10]})
            return s
        s["error"] = "serie vacia"
    except Exception as e:
        s["error"] = "no respondio: %s" % e
    return s


def dolar_desde_solotodo(clp, usd):
    """Respaldo: SoloTodo publica cada precio en pesos y en dolares. El cuociente
    entre ambos es el tipo de cambio que usa, que sirve de referencia."""
    try:
        d = bajar("%s/categories/%d/browse/?search=iphone&page_size=20" % (API, CAT_CELULARES))
    except Exception:
        return None
    razones = []
    for res in d.get("results", []):
        for pe in res.get("product_entries", []):
            meta = pe.get("metadata") or {}
            pc = precio_clp_meta(meta, clp, usd)
            pu = None
            for k, v in meta.items():
                if "usd" in str(k).lower() and "offer" in str(k).lower():
                    try:
                        pu = float(v) or None
                    except Exception:
                        pass
            if pc and pu and pu > 1:
                razones.append(pc / pu)
    if not razones:
        return None
    razones.sort()
    return round(razones[len(razones) // 2], 2)


# ---------- armado ------------------------------------------------------------
def main():
    aqui = os.path.dirname(os.path.abspath(__file__))
    destino = os.path.join(aqui, "iphone")
    os.makedirs(os.path.join(destino, "historial"), exist_ok=True)
    hoy = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    tiendas = mapa_tiendas()
    clp, usd = ids_monedas()

    out = {
        "fecha": hoy,
        "generado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "para": "Radar iPhone: conviene comprarlo aca o alla",
        "sales_tax_supuesto": 0.07,
        "sales_tax_nota": "Florida, el estado donde suele comprar. Es un supuesto fijo, no un dato consultado.",
        "dolar": None,
        "fuentes": {},
        "modelos": {},
    }

    # dolar, con respaldo
    dol = dolar_observado()
    if not dol["ok"]:
        alt = dolar_desde_solotodo(clp, usd)
        if alt:
            dol = {"ok": True, "valor": alt, "fecha": hoy, "fuente": "SoloTodo (respaldo)",
                   "error": dol["error"]}
    out["dolar"] = dol

    # Apple: una descarga por pagina, compartida entre modelos que usan el mismo slug
    cache_apple = {}

    def apple(m, region):
        k = (m["apple_slug"], region)
        if k not in cache_apple:
            cache_apple[k] = precios_apple(m["apple_slug"], m["pulgadas"], region,
                                           m.get("apple_familia"),
                                           m.get("descontinuado_apple", False))
        return cache_apple[k]

    with ThreadPoolExecutor(max_workers=3) as ex:
        tareas = {m["id"]: ex.submit(solotodo_modelo, m, tiendas, clp, usd) for m in MODELOS}
        for m in MODELOS:
            us = apple(m, "")
            cl = apple(m, "cl/")
            st = tareas[m["id"]].result()

            caps = {}
            for cap in m["capacidades"]:
                caps[cap] = {
                    "usa_apple": us["precios"].get(cap),
                    "chile_apple": cl["precios"].get(cap),
                    "chile_retail": st["por_capacidad"].get(cap),
                }
            out["modelos"][m["id"]] = {
                "nombre": m["nombre"],
                "descontinuado_apple": m["descontinuado_apple"],
                "capacidades": caps,
                "fuentes": {"apple_us": {k: v for k, v in us.items() if k != "precios"},
                            "apple_cl": {k: v for k, v in cl.items() if k != "precios"},
                            "solotodo": {"ok": st["ok"], "error": st["error"], "url": st["url"],
                                         "n_descartados": len(st["descartados"])}},
                "descartados": st["descartados"][:12],
            }

    # resumen de salud, para que la tarea de Claude sepa de que puede fiarse
    ok = 0
    total = 0
    for mid, m in out["modelos"].items():
        for f in m["fuentes"].values():
            total += 1
            ok += 1 if f.get("ok") else 0
    out["fuentes"] = {"consultas_ok": ok, "consultas_total": total,
                      "dolar_ok": bool(dol.get("ok"))}

    with open(os.path.join(destino, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(os.path.join(destino, "historial", hoy + ".json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("%s  fuentes %d/%d  dolar %s (%s)" % (hoy, ok, total,
          dol.get("valor"), dol.get("fuente")))
    for mid, m in out["modelos"].items():
        print(" ", m["nombre"])
        for cap, c in m["capacidades"].items():
            r = c["chile_retail"]
            print("    %-7s  US Apple %-9s  CL Apple %-11s  CL retail %s" % (
                cap,
                ("$" + format(c["usa_apple"], ",")) if c["usa_apple"] else "--",
                ("$" + format(c["chile_apple"], ",").replace(",", ".")) if c["chile_apple"] else "--",
                ("$" + format(r["precio"], ",").replace(",", ".") + "  " + r["tienda"]) if r else "--"))


if __name__ == "__main__":
    main()
