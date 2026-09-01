#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STATE SCRAPER · Generador de la interfaz
========================================

Lee las oportunidades ya cribadas de Supabase y escribe un único fichero
`interfaz.html` con los datos incrustados dentro.

Por qué los datos van incrustados y no se consultan desde el navegador:

  · No hace falta exponer ninguna credencial en un fichero que se abre en
    un navegador.
  · No hay que abrir permisos de lectura pública sobre la base de datos.
  · Se abre con doble clic, sin servidor y sin conexión. Para grabar una
    demostración eso importa: ni esperas de carga ni fallos de red a
    mitad del vídeo.

El fichero resultante NO se sube al repositorio: contiene datos, y el
repositorio guarda código. Se recoge como artefacto descargable de la
ejecución de GitHub Actions.

Uso:
    python generar_interfaz.py
    python generar_interfaz.py --salida otra_ruta.html
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

# ==============================================================
# 1. CONFIGURACIÓN
# ==============================================================

VISTA = "oportunidades"
SALIDA_POR_DEFECTO = "interfaz.html"

# Provincia a partir de los dos primeros dígitos del código postal.
# Es el mapeo oficial y no cambia; por eso va en el código y no en la
# base de datos.
PROVINCIAS: dict[str, str] = {
    "01": "Álava", "02": "Albacete", "03": "Alicante", "04": "Almería",
    "05": "Ávila", "06": "Badajoz", "07": "Illes Balears", "08": "Barcelona",
    "09": "Burgos", "10": "Cáceres", "11": "Cádiz", "12": "Castellón",
    "13": "Ciudad Real", "14": "Córdoba", "15": "A Coruña", "16": "Cuenca",
    "17": "Girona", "18": "Granada", "19": "Guadalajara", "20": "Gipuzkoa",
    "21": "Huelva", "22": "Huesca", "23": "Jaén", "24": "León",
    "25": "Lleida", "26": "La Rioja", "27": "Lugo", "28": "Madrid",
    "29": "Málaga", "30": "Murcia", "31": "Navarra", "32": "Ourense",
    "33": "Asturias", "34": "Palencia", "35": "Las Palmas",
    "36": "Pontevedra", "37": "Salamanca", "38": "S. C. de Tenerife",
    "39": "Cantabria", "40": "Segovia", "41": "Sevilla", "42": "Soria",
    "43": "Tarragona", "44": "Teruel", "45": "Toledo", "46": "Valencia",
    "47": "Valladolid", "48": "Bizkaia", "49": "Zamora", "50": "Zaragoza",
    "51": "Ceuta", "52": "Melilla",
}


def configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


# ==============================================================
# 2. LECTURA DE DATOS
# ==============================================================

def obtener_cliente():
    """Conexión a Supabase, con el mismo saneado de URL que el resto."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    for sufijo in ("/rest/v1", "/rest"):
        if url.endswith(sufijo):
            url = url[: -len(sufijo)].rstrip("/")
    clave = os.environ.get("SUPABASE_KEY", "").strip()

    if not url or not clave:
        logging.error("Faltan SUPABASE_URL o SUPABASE_KEY.")
        sys.exit(1)
    try:
        return create_client(url, clave)
    except Exception as error:
        logging.error("No se pudo conectar con Supabase: %s", error)
        sys.exit(1)


def dias_hasta(fecha_iso: str | None) -> int | None:
    """Días que quedan hasta el plazo. None si no hay fecha publicada."""
    if not fecha_iso:
        return None
    try:
        limite = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if limite.tzinfo is None:
        limite = limite.replace(tzinfo=timezone.utc)
    return (limite - datetime.now(timezone.utc)).days


def contar(cliente, filtro=None) -> int | None:
    """
    Cuenta filas sin traérselas. Devuelve None si la consulta falla: un
    dato de cabecera no puede tumbar la generación de la interfaz.
    """
    try:
        consulta = cliente.table("licitaciones").select("id_licitacion", count="exact")
        if filtro:
            consulta = filtro(consulta)
        return consulta.limit(1).execute().count
    except Exception as error:
        logging.warning("No se pudo contar (%s): %s", filtro, error)
        return None


def estadisticas(cliente) -> dict[str, int | None]:
    """
    El embudo real, para poder enseñarlo en lugar de afirmarlo.

    "Filtrados automáticamente entre miles de anuncios" es una promesa.
    "Ha leído 926 y valorado 124" es un hecho comprobable.
    """
    return {
        "leidos": contar(cliente),
        "valorados": contar(
            cliente, lambda c: c.not_.is_("cribado_veredicto", "null")
        ),
    }


def preparar(filas: list[dict]) -> list[dict]:
    """
    Convierte las filas de Supabase en lo que necesita la interfaz.

    Aquí se deriva la provincia y los días restantes: son cálculos, no
    datos, y por eso no se guardan en la base.
    """
    preparadas = []
    for fila in filas:
        # Se rellena el cero inicial por si alguna fila antigua se guardó
        # sin normalizar: '8001' es Barcelona, y sin el cero se quedaría
        # sin provincia y fuera del filtro, en silencio.
        cp = "".join(c for c in (fila.get("codigo_postal") or "") if c.isdigit())
        if len(cp) == 4:
            cp = "0" + cp
        cpvs = fila.get("cpvs") or []
        if isinstance(cpvs, str):
            try:
                cpvs = json.loads(cpvs)
            except json.JSONDecodeError:
                cpvs = []

        preparadas.append({
            "titulo": fila.get("titulo") or "(sin título)",
            "organo": fila.get("organo") or "",
            "provincia": PROVINCIAS.get(cp[:2], "") if len(cp) >= 2 else "",
            "presupuesto": fila.get("presupuesto"),
            "limite": fila.get("fecha_limite"),
            "dias": dias_hasta(fila.get("fecha_limite")),
            "publicacion": fila.get("fecha_publicacion"),
            "veredicto": fila.get("cribado_veredicto") or "",
            "motivo": fila.get("cribado_motivo") or "",
            "cpvs": [str(c) for c in cpvs][:6],
            "enlace": fila.get("enlace") or "",
            "origen": fila.get("origen") or "",
        })

    # Lo que vence antes, primero. Sin plazo publicado, al final: no se
    # puede afirmar que estén vencidas, pero tampoco meten prisa.
    preparadas.sort(key=lambda x: (x["dias"] is None, x["dias"] if x["dias"] is not None else 0))
    return preparadas


# ==============================================================
# 3. LA INTERFAZ
# ==============================================================

PLANTILLA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contratos públicos para el espectáculo en vivo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
/* Sin color decorativo: la jerarquía la llevan el peso y las líneas.
   El color solo aparece donde significa algo — plazo que aprieta,
   viabilidad alta. */
:root{
  --fondo:#17171A;
  --superficie:#1F1F23;
  --texto:#E9E8E5;
  --tenue:#8B8B92;
  --linea:rgba(233,232,229,.11);
  --linea-fuerte:rgba(233,232,229,.28);
  --urge:#D98A72;
  --viable:#8FB89A;
}
*{box-sizing:border-box}
body{
  margin:0;background:var(--fondo);color:var(--texto);
  font-family:'IBM Plex Sans',system-ui,sans-serif;
  font-size:16px;line-height:1.6;font-weight:400;
  -webkit-font-smoothing:antialiased;
}
.envoltorio{max-width:1000px;margin:0 auto;padding:0 28px 96px}

/* ---------- Cabecera ---------- */
header{padding:80px 0 44px}
h1{
  font-weight:600;font-size:clamp(1.9rem,4.2vw,2.9rem);
  line-height:1.2;letter-spacing:-.02em;margin:0 0 20px;max-width:20ch;
}
.entradilla{max-width:62ch;color:var(--tenue);margin:0;font-size:1rem;line-height:1.65}
.marcador{
  display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  margin-top:46px;background:var(--linea);
  border-top:1px solid var(--linea);border-bottom:1px solid var(--linea);
}
.marcador div{background:var(--fondo);padding:20px 20px 20px 0}
.marcador span{
  display:block;font-size:1.75rem;font-weight:600;line-height:1.1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums;
}
.marcador small{display:block;margin-top:6px;color:var(--tenue);font-size:.82rem}

/* ---------- Filtros ---------- */
.filtros{
  display:flex;gap:12px;flex-wrap:wrap;align-items:center;
  padding:20px 0;position:sticky;top:0;background:var(--fondo);
  border-bottom:1px solid var(--linea);z-index:10;
}
select,input[type=search]{
  font:inherit;font-size:.94rem;color:var(--texto);
  background:var(--superficie);border:1px solid var(--linea);
  border-radius:3px;padding:9px 12px;min-width:200px;
}
input[type=search]{flex:1;min-width:240px}
select:focus-visible,input:focus-visible,.fila:focus-visible{
  outline:2px solid var(--linea-fuerte);outline-offset:2px;
}
.recuento{color:var(--tenue);font-size:.88rem;margin-left:auto}

/* ---------- Lista ---------- */
.fila{
  display:grid;grid-template-columns:76px 1fr auto;gap:26px;
  align-items:start;padding:24px 0;border-bottom:1px solid var(--linea);
  cursor:pointer;width:100%;text-align:left;background:none;
  border-left:0;border-right:0;border-top:0;color:inherit;font:inherit;
}
.fila:hover .titulo{text-decoration:underline;text-underline-offset:3px}

.fecha{
  text-align:left;padding-top:2px;
  border-top:1px solid var(--linea-fuerte);
}
.fecha .dia{
  display:block;font-size:1.45rem;font-weight:600;line-height:1.15;
  padding-top:8px;font-variant-numeric:tabular-nums;
}
.fecha .cuenta{display:block;margin-top:2px;font-size:.78rem;color:var(--tenue)}
.fecha.apura{border-top-color:var(--urge)}
.fecha.apura .cuenta{color:var(--urge)}
.fecha.sinplazo .dia{font-size:.9rem;font-weight:400;color:var(--tenue)}

.titulo{font-weight:500;margin:0 0 5px;font-size:1rem;line-height:1.45;max-width:62ch}
.meta{color:var(--tenue);font-size:.88rem;margin:0}
.derecha{text-align:right;white-space:nowrap}
.importe{font-weight:600;font-size:1rem;font-variant-numeric:tabular-nums}
.sello{display:block;margin-top:5px;font-size:.78rem;color:var(--tenue)}
.sello.si{color:var(--viable)}

/* ---------- Detalle ---------- */
.detalle{display:none;padding:2px 0 32px 102px;border-bottom:1px solid var(--linea)}
.detalle.abierto{display:block}
.detalle dl{display:grid;grid-template-columns:150px 1fr;gap:11px 24px;margin:0 0 24px}
.detalle dt{color:var(--tenue);font-size:.88rem}
.detalle dd{margin:0;font-size:.94rem}
.cita{
  border-left:1px solid var(--linea-fuerte);padding-left:18px;
  margin:0 0 24px;max-width:66ch;color:var(--texto);
}
.boton{
  display:inline-block;border:1px solid var(--linea-fuerte);color:var(--texto);
  padding:9px 18px;border-radius:3px;font-weight:500;
  text-decoration:none;font-size:.92rem;
}
.boton:hover{background:var(--superficie)}

.vacio{padding:72px 0;text-align:center;color:var(--tenue)}
footer{padding-top:36px;color:var(--tenue);font-size:.84rem;max-width:70ch}

@media(max-width:720px){
  .fila{grid-template-columns:66px 1fr;gap:18px}
  .derecha{grid-column:2;text-align:left}
  .detalle{padding-left:0}
  .detalle dl{grid-template-columns:1fr}
  .marcador{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<div class="envoltorio">

<header>
  <h1 id="titular">—</h1>
  <p class="entradilla" id="entradilla">—</p>
  <div class="marcador">
    <div><span id="m-total">—</span><small>oportunidades</small></div>
    <div><span id="m-prov">—</span><small>provincias</small></div>
    <div><span id="m-importe">—</span><small>en juego</small></div>
    <div><span id="m-urge">—</span><small>vencen esta semana</small></div>
  </div>
</header>

<div class="filtros">
  <select id="f-provincia" aria-label="Filtrar por provincia"></select>
  <select id="f-veredicto" aria-label="Filtrar por viabilidad">
    <option value="">Cualquier viabilidad</option>
    <option value="si">Para mí</option>
    <option value="quizas">Puede ser para mí</option>
  </select>
  <input type="search" id="f-texto" placeholder="Buscar por título u órgano">
  <span class="recuento" id="recuento"></span>
</div>

<main id="lista"></main>

<footer id="pie"></footer>
</div>

<script id="datos" type="application/json">__DATOS__</script>
<script>
const PAQUETE = JSON.parse(document.getElementById('datos').textContent);
const DATOS = PAQUETE.oportunidades;
const STATS = PAQUETE.stats || {};
const MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];

const num = n => new Intl.NumberFormat('es-ES').format(n);
const euros = n => n == null ? 'sin importe'
  : new Intl.NumberFormat('es-ES',{style:'currency',currency:'EUR',maximumFractionDigits:0}).format(n);
const fechaLarga = s => s ? new Date(s).toLocaleDateString('es-ES',
  {day:'numeric',month:'long',year:'numeric'}) : '—';

function bloqueFecha(o){
  if(o.dias == null) return `<div class="fecha sinplazo"><span class="dia">Sin plazo publicado</span></div>`;
  const d = new Date(o.limite);
  const cuenta = o.dias < 0 ? 'vencido' : o.dias === 0 ? 'vence hoy' :
                 o.dias === 1 ? 'queda 1 día' : `quedan ${o.dias} días`;
  return `<div class="fecha ${o.dias <= 7 ? 'apura' : ''}">
    <span class="dia">${d.getDate()} ${MESES[d.getMonth()]}</span>
    <span class="cuenta">${cuenta}</span></div>`;
}

function detalle(o, i){
  return `<section class="detalle" id="d${i}">
    ${o.motivo ? `<p class="cita">${o.motivo}</p>` : ''}
    <dl>
      <dt>Órgano</dt><dd>${o.organo || '—'}</dd>
      <dt>Provincia</dt><dd>${o.provincia || 'no informada'}</dd>
      <dt>Presupuesto</dt><dd>${euros(o.presupuesto)}</dd>
      <dt>Plazo</dt><dd>${fechaLarga(o.limite)}</dd>
      <dt>Publicado</dt><dd>${fechaLarga(o.publicacion)}</dd>
      <dt>Códigos CPV</dt><dd>${o.cpvs.length ? o.cpvs.join(', ') : '—'}</dd>
    </dl>
    ${o.enlace ? `<a class="boton" href="${o.enlace}" target="_blank" rel="noopener">Ver el expediente</a>` : ''}
  </section>`;
}

function pintar(){
  const prov = document.getElementById('f-provincia').value;
  const ver  = document.getElementById('f-veredicto').value;
  const txt  = document.getElementById('f-texto').value.toLowerCase().trim();

  const vistos = DATOS.filter(o =>
    (!prov || o.provincia === prov) &&
    (!ver  || o.veredicto === ver) &&
    (!txt  || (o.titulo + ' ' + o.organo).toLowerCase().includes(txt)));

  document.getElementById('recuento').textContent =
    vistos.length === DATOS.length ? '' : `${vistos.length} de ${DATOS.length}`;

  document.getElementById('lista').innerHTML = vistos.length ? vistos.map((o,i) => `
    <button class="fila" aria-expanded="false" aria-controls="d${i}" data-i="${i}">
      ${bloqueFecha(o)}
      <div>
        <p class="titulo">${o.titulo}</p>
        <p class="meta">${[o.organo, o.provincia].filter(Boolean).join(' · ')}</p>
      </div>
      <div class="derecha">
        <div class="importe">${euros(o.presupuesto)}</div>
        <span class="sello ${o.veredicto}">${o.veredicto === 'si' ? 'para mí' : 'puede ser para mí'}</span>
      </div>
    </button>${detalle(o,i)}`).join('')
    : `<p class="vacio">Ningún contrato encaja con estos filtros.<br>
       Prueba a quitar la provincia o a ampliar la viabilidad.</p>`;

  document.querySelectorAll('.fila').forEach(f => f.onclick = () => {
    const d = document.getElementById('d' + f.dataset.i);
    f.setAttribute('aria-expanded', d.classList.toggle('abierto'));
  });
}

const provincias = [...new Set(DATOS.map(o => o.provincia).filter(Boolean))]
  .sort((a,b) => a.localeCompare(b,'es'));
const total = DATOS.reduce((s,o) => s + (o.presupuesto || 0), 0);

document.getElementById('titular').textContent =
  `Hay ${DATOS.length} ${DATOS.length === 1 ? 'contrato nuevo' : 'contratos nuevos'} que podrían interesarte`;

// El dato real sustituye a la promesa: no "filtrados entre miles", sino
// cuántos se han leído y cuántos se han valorado para llegar hasta aquí.
const sector = 'Contratos de las administraciones españolas para música, artes escénicas, producción y servicios técnicos de espectáculo.';
document.getElementById('entradilla').textContent = STATS.leidos
  ? `${sector} De ${num(STATS.leidos)} anuncios leídos, ${num(STATS.valorados || 0)} se han valorado uno a uno.`
  : sector;

document.getElementById('m-total').textContent = DATOS.length;
document.getElementById('m-prov').textContent = provincias.length;
document.getElementById('m-importe').textContent =
  new Intl.NumberFormat('es-ES',{notation:'compact',style:'currency',currency:'EUR',maximumFractionDigits:1}).format(total);
document.getElementById('m-urge').textContent =
  DATOS.filter(o => o.dias != null && o.dias >= 0 && o.dias <= 7).length;

document.getElementById('f-provincia').innerHTML =
  '<option value="">Toda España</option>' + provincias.map(p => `<option>${p}</option>`).join('');

['f-provincia','f-veredicto','f-texto'].forEach(id =>
  document.getElementById(id).addEventListener('input', pintar));

document.getElementById('pie').textContent =
  'Datos de la Plataforma de Contratación del Sector Público y de las plataformas autonómicas agregadas. Actualizado el __ACTUALIZADO__.';

pintar();
</script>
</body>
</html>
"""


def escribir(oportunidades: list[dict], ruta: str,
             stats: dict | None = None) -> None:
    """Inyecta los datos en la plantilla y escribe el fichero."""
    # `</script>` dentro del JSON cerraría la etiqueta antes de tiempo.
    paquete = {"oportunidades": oportunidades, "stats": stats or {}}
    datos = json.dumps(paquete, ensure_ascii=False).replace("</", "<\\/")
    ahora = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y a las %H:%M")

    html = PLANTILLA.replace("__DATOS__", datos).replace("__ACTUALIZADO__", ahora)
    with open(ruta, "w", encoding="utf-8") as fichero:
        fichero.write(html)


# ==============================================================
# 4. ORQUESTACIÓN
# ==============================================================

def main() -> int:
    argumentos = argparse.ArgumentParser(
        description="Genera la interfaz de demostración a partir de Supabase."
    )
    argumentos.add_argument("--salida", default=SALIDA_POR_DEFECTO,
                            help="Ruta del fichero HTML a escribir.")
    opciones = argumentos.parse_args()

    configurar_logging()
    cliente = obtener_cliente()

    try:
        respuesta = cliente.table(VISTA).select("*").execute()
        filas = respuesta.data or []
    except Exception as error:
        logging.error("No se pudo leer la vista '%s': %s", VISTA, error)
        return 1

    if not filas:
        logging.warning("La vista '%s' no ha devuelto ninguna fila. "
                        "Se generará una interfaz vacía.", VISTA)

    oportunidades = preparar(filas)
    stats = estadisticas(cliente)
    escribir(oportunidades, opciones.salida, stats)
    logging.info("  Embudo: %s leídos -> %s valorados -> %d relevantes",
                 stats.get("leidos"), stats.get("valorados"), len(oportunidades))

    con_provincia = sum(1 for o in oportunidades if o["provincia"])
    con_plazo = sum(1 for o in oportunidades if o["dias"] is not None)
    logging.info("Escrito %s con %d oportunidades.", opciones.salida, len(oportunidades))
    logging.info("  Con provincia: %d/%d | Con plazo: %d/%d",
                 con_provincia, len(oportunidades), con_plazo, len(oportunidades))
    return 0


if __name__ == "__main__":
    sys.exit(main())
