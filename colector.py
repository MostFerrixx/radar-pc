#!/usr/bin/env python3
"""
Recolector de precios para el Radar de Precios PC (Chile).

Corre en GitHub Actions todos los dias. Lee items.json, consulta la API publica
de SoloTodo y deja en latest.json el producto mas barato que cumple las reglas
de cada item, mas los candidatos que descarto y por que. No decide nada mas:
el juicio (alertas, objetivos, tablero) lo hace la tarea diaria de Claude,
que solo tiene que leer este archivo.

Uso:  python3 colector.py            -> escribe latest.json y historial/AAAA-MM-DD.json
"""
import json, re, sys, os, time, datetime, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

API = "https://publicapi.solotodo.com"
TIMEOUT = 40
UA = {"User-Agent": "radar-precios-pc/2.0 (github actions; uso personal, 1 vez al dia)"}


def get(url, intentos=3):
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # red, 5xx, JSON roto: reintenta con pausa
            ultimo = e
            time.sleep(3 * (i + 1))
    raise RuntimeError("%s -> %s" % (url, ultimo))


def num(x):
    try:
        return int(round(float(x)))
    except Exception:
        return None


# ---------- catalogos auxiliares ----------------------------------------------
def id_desde_url(u):
    m = re.search(r"/(\d+)/?$", str(u or ""))
    return int(m.group(1)) if m else None


def mapa_tiendas():
    m = {}
    try:
        for s in get(API + "/stores/"):
            i = s.get("id") or id_desde_url(s.get("url"))
            if i is not None:
                m[int(i)] = s.get("name") or str(i)
    except Exception as e:
        print("aviso: no pude leer /stores/:", e, file=sys.stderr)
    return m


def ids_monedas():
    clp = usd = None
    try:
        for c in get(API + "/currencies/"):
            iso = (c.get("iso_code") or "").upper()
            i = c.get("id") or id_desde_url(c.get("url"))
            if iso == "CLP":
                clp = int(i)
            elif iso == "USD":
                usd = int(i)
    except Exception as e:
        print("aviso: no pude leer /currencies/:", e, file=sys.stderr)
    return clp, usd


def precios_meta(meta, clp, usd):
    """Devuelve (precio_clp, precio_usd) de un bloque metadata de browse.
    Si el catalogo de monedas no ayuda, usa la magnitud: CLP es ~900 veces USD."""
    pc = pu = None
    vals = []
    # SoloTodo tambien deja el equivalente en USD como campo directo (offer_price_usd)
    for k, v in (meta or {}).items():
        if "usd" in str(k).lower() and "offer" in str(k).lower():
            try:
                pu = float(v) or None
            except Exception:
                pass
    for p in meta.get("prices_per_currency", []):
        cid = p.get("currency_id") or id_desde_url(p.get("currency"))
        try:
            v = float(p.get("offer_price") or 0)
        except Exception:
            v = 0
        if not v:
            continue
        vals.append(v)
        if cid is not None and cid == clp:
            pc = v
        elif cid is not None and cid == usd:
            pu = v
    if vals and (pc is None or pu is None):
        vals.sort()
        if pc is None:
            pc = vals[-1]
        if pu is None and len(vals) > 1 and vals[0] * 100 < vals[-1]:
            pu = vals[0]
    return (num(pc) if pc else None), pu


# ---------- reglas por item ---------------------------------------------------
def motivo_descarte(nombre, item, specs=None):
    """None si el producto cumple; si no, el texto de por que se descarta.
    'incluye' se busca en el nombre Y en la ficha tecnica (specs): por ejemplo
    la certificacion 80 PLUS de una fuente no viene en el nombre, viene en la
    ficha. Marcas y exclusiones se miran solo en el nombre."""
    n = (nombre or "").lower()
    ficha = n
    if specs:
        try:
            ficha = n + " " + json.dumps(specs, ensure_ascii=False).lower()
        except Exception:
            pass
    for t in item.get("incluye", []):
        if t.lower() not in ficha:
            return "no dice '%s'" % t
    for t in item.get("excluye", []):
        if t.lower() in n:
            return "dice '%s'" % t
    for t in item.get("marcas_veto", []):
        if t.lower() in n:
            return "marca vetada (%s)" % t
    marcas = item.get("marcas_ok")
    if marcas and not any(m.lower() in n for m in marcas):
        return "marca fuera de la lista confiable"
    return None


# ---------- tiendas con stock de un producto ----------------------------------
def ofertas_de(pid, tiendas):
    d = get(API + "/products/available_entities/?ids=%s" % pid)
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
            ofertas.append({
                "precio": v,
                "tienda": tiendas.get(int(sid), "tienda %s" % sid) if sid is not None else "?",
                "url": e.get("external_url"),
            })
    ofertas.sort(key=lambda o: o["precio"])
    return ofertas


# ---------- un item -----------------------------------------------------------
def procesar(item, tiendas, clp, usd):
    iid = item["id"]
    salida = {"nombre": item.get("nombre"), "elegido": None, "candidatos": [], "descartados": []}
    try:
        d = get(item["url"])
    except Exception as e:
        salida["error"] = "no respondio: %s" % e
        return iid, salida, None

    dolar_muestra = None
    cands = []
    if item.get("fuente") == "entities":
        for res in d.get("results", []):
            p = res.get("product") or {}
            try:
                ofs = ofertas_de(p.get("id"), tiendas)
            except Exception as e:
                salida["error"] = "sin tiendas: %s" % e
                ofs = []
            if ofs:
                cands.append({"product_id": p.get("id"), "nombre": p.get("name"),
                              "precio": ofs[0]["precio"], "tienda": ofs[0]["tienda"],
                              "url": ofs[0]["url"], "n_tiendas": len(ofs)})
            else:
                salida["descartados"].append({"nombre": p.get("name"), "motivo": "ninguna tienda con stock"})
    else:
        vistos = []
        for res in d.get("results", []):
            for pe in res.get("product_entries", []):
                p = pe.get("product") or {}
                pc, pu = precios_meta(pe.get("metadata") or {}, clp, usd)
                if pc is None:
                    continue
                if dolar_muestra is None and pu:
                    dolar_muestra = round(pc / pu, 2)
                por_que = motivo_descarte(p.get("name"), item, p.get("specs"))
                if por_que:
                    salida["descartados"].append({"nombre": p.get("name"), "precio": pc, "motivo": por_que})
                    continue
                vistos.append({"product_id": p.get("id"), "nombre": p.get("name"), "precio_lista": pc})
        vistos.sort(key=lambda c: c["precio_lista"])
        for c in vistos[:4]:  # precio real por tienda solo de los 4 mas baratos
            try:
                ofs = ofertas_de(c["product_id"], tiendas)
            except Exception as e:
                ofs = []
            if ofs:
                c.update({"precio": ofs[0]["precio"], "tienda": ofs[0]["tienda"],
                          "url": ofs[0]["url"], "n_tiendas": len(ofs)})
                cands.append(c)
            else:
                salida["descartados"].append({"nombre": c["nombre"], "precio": c["precio_lista"],
                                              "motivo": "en lista pero ninguna tienda con stock"})
    cands.sort(key=lambda c: c["precio"])
    # "cantidad": el item se compra N veces (por ejemplo dos modulos sueltos de
    # 32 GB para llegar a 64 GB). El precio que sale es el total de las N unidades.
    q = int(item.get("cantidad") or 1)
    if q > 1:
        for c in cands:
            c["cantidad"] = q
            c["precio_unitario"] = c["precio"]
            c["precio"] = c["precio"] * q
            if "precio_lista" in c:
                c["precio_lista"] = c["precio_lista"] * q
            c["nombre"] = "%d x %s" % (q, c["nombre"])
        salida["cantidad"] = q
    salida["candidatos"] = cands[:4]
    salida["elegido"] = cands[0] if cands else None
    if not cands and "error" not in salida:
        salida["error"] = "ningun producto cumple las reglas" if not item.get("pendiente") else None
        if salida["error"] is None:
            del salida["error"]
    return iid, salida, dolar_muestra


def main():
    aqui = os.path.dirname(os.path.abspath(__file__))
    items = json.load(open(os.path.join(aqui, "items.json"), encoding="utf-8"))
    tiendas = mapa_tiendas()
    clp, usd = ids_monedas()
    hoy = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = {
        "fecha": hoy,
        "generado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "fuente": "SoloTodo publicapi, via GitHub Actions",
        "dolar_referencia": None,
        "consultas_total": len(items),
        "consultas_ok": 0,
        "errores": {},
        "resultados": {},
    }
    dolares = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for iid, r, dol in ex.map(lambda it: procesar(it, tiendas, clp, usd), items):
            out["resultados"][iid] = r
            if r.get("error"):
                out["errores"][iid] = r["error"]
            else:
                out["consultas_ok"] += 1
            if dol:
                dolares.append(dol)
    if dolares:
        dolares.sort()
        out["dolar_referencia"] = dolares[len(dolares) // 2]  # mediana, por si una fila viene rara

    with open(os.path.join(aqui, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.makedirs(os.path.join(aqui, "historial"), exist_ok=True)
    with open(os.path.join(aqui, "historial", hoy + ".json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("%s: %d de %d consultas OK, dolar %s" % (hoy, out["consultas_ok"], out["consultas_total"], out["dolar_referencia"]))
    for iid, r in out["resultados"].items():
        e = r.get("elegido")
        print("  %-18s %s" % (iid, ("$%s  %s  (%s)" % (format(e["precio"], ",").replace(",", "."), e["nombre"], e["tienda"])) if e else ("-- " + str(r.get("error") or "sin producto (pendiente)"))))


if __name__ == "__main__":
    main()
