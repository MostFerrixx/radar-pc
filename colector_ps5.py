#!/usr/bin/env python3
"""
Radar PS5 jailbreak.

Corre en GitHub Actions junto al radar de precios, pero es un script aparte y no
comparte nada con el: si este falla, los precios no se enteran.

Uso:  python3 colector_ps5.py     -> escribe ps5/latest.json, ps5/historial/ y
                                     ps5/snap-*.txt (la copia de referencia)
"""
import json, re, os, time, datetime, urllib.request
import html as html_lib

TIMEOUT = 40

# =============================================================================
# Radar PS5 jailbreak
# =============================================================================
# Vive en el mismo repositorio y en la misma corrida diaria que el radar de
# precios, pero es completamente independiente: main() lo llama al final dentro
# de un try/except, asi que si esto falla los precios ya quedaron escritos.
#
# Que hace: baja el texto de dos fuentes confiables del scene, lo compara con la
# copia del dia anterior guardada en el repositorio y deja en ps5/latest.json las
# lineas NUEVAS mas las que hablan de firmware 13.x. No juzga nada: la tarea
# semanal de Claude lee ese archivo y decide si hay novedad real.
#
# El motivo de todo esto: WebFetch dentro de una tarea programada de Claude pide
# aprobacion por dominio en cada corrida, asi que las corridas desatendidas se
# quedaban sin fuentes. Un runner de GitHub Actions si tiene internet abierto, y
# Claude puede leer raw.githubusercontent.com sin pedir permiso.

PS5_FUENTES = [
    {
        "id": "psdevwiki",
        "titulo": "PS Dev Wiki - Vulnerabilities",
        # action=raw devuelve el wikitext limpio: mismo contenido que la tabla de
        # la pagina, pero sin HTML y sin cambiar de forma cuando cambia el tema.
        "url": "https://www.psdevwiki.com/ps5/index.php?title=Vulnerabilities&action=raw",
        "ver_en": "https://www.psdevwiki.com/ps5/Vulnerabilities",
        "formato": "texto",
    },
    {
        "id": "wololo",
        "titulo": "Wololo - PS5 Jailbreak and Custom Firmware",
        "url": "https://wololo.net/ps5-jailbreak-and-custom-firmware/",
        "ver_en": "https://wololo.net/ps5-jailbreak-and-custom-firmware/",
        "formato": "html",
    },
]

# Un exploit solo le sirve a Martin si es de KERNEL y llega a 13.x. Estas son las
# palabras que hacen que una linea valga la pena mirar.
PS5_CLAVES_KERNEL = [
    "kernel", "jailbreak", "downgrade", "cfw", "etahen", "kstuff", "payload",
    "umtx", "kqueue", "ipv6", "lapse", "bootrom", "sandbox escape", "arbitrary rw",
]
PS5_FW_RE = re.compile(r"\b1[3-9]\.\d{2}\b")          # 13.00 y superiores
PS5_FW_TODOS_RE = re.compile(r"\b\d{1,2}\.\d{2}\b")   # cualquier firmware


def ps5_bajar(url):
    """Baja una URL como texto. Sin reintentos agresivos: si hoy falla, la tarea
    de Claude lo ve marcado como error y usa la copia anterior."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "radar-ps5/1.0 (github actions; uso personal, 1 vez al dia)",
        "Accept": "text/html,text/plain,*/*",
    })
    ultimo = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                bruto = r.read()
            return bruto.decode("utf-8", errors="replace")
        except Exception as e:
            ultimo = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(str(ultimo))


def ps5_a_texto(contenido, formato):
    """Deja el contenido como lineas de texto plano, estables de un dia a otro."""
    t = contenido
    if formato == "html":
        t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", t)
        t = re.sub(r"(?is)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        t = html_lib.unescape(t)
    else:
        t = html_lib.unescape(t)
    lineas = []
    for l in t.splitlines():
        l = re.sub(r"[ \t\xa0]+", " ", l).strip()
        # el wikitext usa | y ! para separar celdas: se vuelven espacios para que
        # cada fila de la tabla quede como una sola linea legible
        if l in ("", "|", "|-", "|}", "{|"):
            continue
        if len(l) > 400:
            l = l[:400] + " ..."
        lineas.append(l)
    return lineas


def ps5_interesante(linea):
    b = linea.lower()
    return bool(PS5_FW_RE.search(linea)) and any(k in b for k in PS5_CLAVES_KERNEL)


def ps5_pista_kernel(lineas):
    """Firmware mas alto que aparece en una linea que habla de kernel. Es una
    pista, no un veredicto: Claude la confirma leyendo las lineas."""
    mejor = None
    for l in lineas:
        if "kernel" not in l.lower():
            continue
        for fw in PS5_FW_TODOS_RE.findall(l):
            try:
                v = float(fw)
            except Exception:
                continue
            if mejor is None or v > mejor[0]:
                mejor = (v, fw, l[:300])
    if not mejor:
        return None
    return {"firmware": mejor[1], "linea": mejor[2]}


def recolectar_ps5(aqui):
    dir_ps5 = os.path.join(aqui, "ps5")
    os.makedirs(os.path.join(dir_ps5, "historial"), exist_ok=True)
    hoy = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = {
        "fecha": hoy,
        "generado_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "para": "tarea semanal 'Radar PS5 Jailbreak (13.40)' de Claude",
        "linea_base": {
            "firmware_del_usuario": "13.40",
            "ultimo_firmware_conocido": "13.60 (24-jul-2026)",
            "kernel_publico_hasta": "12.40 / 12.70 (P2JB parchado en 13.00)",
            "userland_que_sirve_en_13_40": ["LuaC0re", "Y2JB", "WebKit/Slopkit", "BD-JB5 (hasta 13.42)"],
        },
        "fuentes_ok": 0,
        "fuentes_total": len(PS5_FUENTES),
        "hay_cambios": False,
        "fuentes": {},
    }
    for f in PS5_FUENTES:
        fid = f["id"]
        r = {"titulo": f["titulo"], "ver_en": f["ver_en"], "ok": False}
        snap = os.path.join(dir_ps5, "snap-%s.txt" % fid)
        try:
            lineas = ps5_a_texto(ps5_bajar(f["url"]), f["formato"])
            if len(lineas) < 5:
                raise RuntimeError("respondio pero casi vacio (%d lineas)" % len(lineas))
            previas = []
            primera_vez = not os.path.exists(snap)
            if not primera_vez:
                previas = open(snap, encoding="utf-8").read().splitlines()
            set_prev = set(previas)
            set_hoy = set(lineas)
            nuevas = [l for l in lineas if l not in set_prev]
            quitadas = [l for l in previas if l not in set_hoy]
            r.update({
                "ok": True,
                "primera_vez": primera_vez,
                "lineas": len(lineas),
                "cambio": (not primera_vez) and bool(nuevas or quitadas),
                # lo que Claude tiene que leer: lo que aparecio hoy y no estaba ayer
                "lineas_nuevas": nuevas[:60],
                "lineas_quitadas": quitadas[:30],
                # y, por si el diff viene vacio, el estado actual de lo que importa
                "menciones_13x_o_superior": [l for l in lineas if PS5_FW_RE.search(l)][:50],
                "candidatos_kernel_13x": [l for l in lineas if ps5_interesante(l)][:30],
                "pista_firmware_kernel_mas_alto": ps5_pista_kernel(lineas),
            })
            if r["cambio"]:
                out["hay_cambios"] = True
            out["fuentes_ok"] += 1
            with open(snap, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lineas))
        except Exception as e:
            r["error"] = str(e)[:300]
            # la copia anterior NO se toca: asi un dia caido no borra la referencia
        out["fuentes"][fid] = r

    for destino in (os.path.join(dir_ps5, "latest.json"),
                    os.path.join(dir_ps5, "historial", hoy + ".json")):
        with open(destino, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    print("ps5: %d de %d fuentes OK, cambios=%s" % (out["fuentes_ok"], out["fuentes_total"], out["hay_cambios"]))
    for fid, r in out["fuentes"].items():
        if r.get("ok"):
            print("  %-10s %d lineas, %d nuevas, %d candidatos kernel 13.x"
                  % (fid, r["lineas"], len(r["lineas_nuevas"]), len(r["candidatos_kernel_13x"])))
        else:
            print("  %-10s ERROR %s" % (fid, r.get("error")))
    return out


if __name__ == "__main__":
    recolectar_ps5(os.path.dirname(os.path.abspath(__file__)))
