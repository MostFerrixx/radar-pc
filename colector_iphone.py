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

# Modelos que sigue el radar. "apple" es el slug de la pagina de compra de
# Apple; varios modelos pueden compartirla (el Pro y el Pro Max viven juntos),
# por eso hace falta el tamano de pantalla para distinguirlos.
MODELOS = [
    {
        "id": "17-pro-max",
        "nombre": "iPhone 17 Pro Max",
        "apple_slug": "iphone-17-pro",
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


def _recorrer(o):
    """Todos los diccionarios anidados de una estructura, sin importar la forma."""
    if isinstance(o, dict):
        yield o
        for v in o.values():
            for x in _recorrer(v):
                yield x
    elif isinstance(o, list):
        for v in o:
            for x in _recorrer(v):
                yield x


def _norm_cap(t):
    """'256gb', '256 GB', '1tb' -> '256 GB' / '1 TB'."""
    m = re.search(r"(\d+)\s*(gb|tb)", str(t or ""), re.I)
    return "%s %s" % (m.group(1), m.group(2).upper()) if m else None


def apple_desde_bootstrap(html, pulgadas):
    """Camino bueno: el catalogo que Apple deja embebido en la pagina.
    Devuelve {capacidad: precio} y None si no encuentra la estructura."""
    pos = html.find("PRODUCT_SELECTION_BOOTSTRAP")
    if pos < 0:
        return None
    llave = html.find("{", pos)
    if llave < 0:
        return None
    crudo = _objeto_json_desde(html, llave)
    if not crudo:
        return None
    try:
        datos = json.loads(crudo)
    except Exception:
        return None

    precios = {}
    for d in _recorrer(datos):
        dims = d.get("dimensions") if isinstance(d.get("dimensions"), dict) else d
        cap = _norm_cap(dims.get("dimensionCapacity") or dims.get("capacity"))
        if not cap:
            continue
        pantalla = str(dims.get("dimensionScreensize") or dims.get("screenSize") or "")
        # el Pro y el Pro Max comparten pagina: separa por tamano de pantalla
        if pulgadas and pulgadas.replace(".", "") not in pantalla.replace(".", "").replace(",", ""):
            continue
        p = d.get("price") if isinstance(d.get("price"), dict) else {}
        val = p.get("fullPrice") or p.get("sellingPrice") or d.get("fullPrice") or d.get("sellingPrice")
        val = num(val)
        if val and val > 100:
            precios.setdefault(cap, val)
            precios[cap] = min(precios[cap], val)
    return precios or None


# Rangos plausibles de precio segun el pais, para no confundir un precio con
# una capacidad, un numero de cuotas o un codigo. Apple siempre imprime los
# montos con signo peso o dolar, asi que el respaldo se ancla en el simbolo.
RANGO = {"": (200, 6000), "cl/": (200000, 6000000)}


def apple_por_cercania(html, pulgadas, region):
    """Plan B: para cada capacidad, quedarse con el monto que aparece mas veces
    cerca de ella. Menos fino que el catalogo embebido, pero aguanta que Apple
    cambie la forma del javascript. Mira solo el trozo de pagina que habla del
    tamano de pantalla pedido, cuando esa marca existe."""
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


def precios_apple(slug, pulgadas, region):
    """region: '' para EE.UU., 'cl/' para Chile."""
    url = "https://www.apple.com/%sshop/buy-iphone/%s" % (region, slug)
    salida = {"ok": False, "metodo": None, "precios": {}, "error": None, "url": url}
    try:
        html = bajar(url, json_=False)
    except Exception as e:
        salida["error"] = "no respondio: %s" % e
        return salida
    for metodo, fn in (("bootstrap", lambda h, p_: apple_desde_bootstrap(h, p_)),
                       ("cercania", lambda h, p_: apple_por_cercania(h, p_, region))):
        try:
            p = fn(html, pulgadas)
        except Exception as e:
            print("aviso: %s %s fallo: %s" % (slug, metodo, e), file=sys.stderr)
            p = None
        if p:
            salida.update({"ok": True, "metodo": metodo, "precios": p})
            return salida
    salida["error"] = "pagina descargada (%d bytes) pero no encontre precios" % len(html)
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
        if ofs:
            salida["por_capacidad"][cap] = {
                "producto": c["nombre"], "precio": ofs[0]["precio"],
                "tienda": ofs[0]["tienda"], "url": ofs[0]["url"], "n_tiendas": len(ofs)}
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

    def apple(slug, pulgadas, region):
        k = (slug, region)
        if k not in cache_apple:
            cache_apple[k] = precios_apple(slug, pulgadas, region)
        return cache_apple[k]

    with ThreadPoolExecutor(max_workers=3) as ex:
        tareas = {m["id"]: ex.submit(solotodo_modelo, m, tiendas, clp, usd) for m in MODELOS}
        for m in MODELOS:
            us = apple(m["apple_slug"], m["pulgadas"], "")
            cl = apple(m["apple_slug"], m["pulgadas"], "cl/")
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
