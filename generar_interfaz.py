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
import re
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


# Comunidad autónoma a la que pertenece cada provincia. Permite buscar
# "Andalucía" y añadir sus ocho provincias de una vez, en lugar de ocho
# pulsaciones. Va aquí y no en la base de datos por el mismo motivo que
# el mapeo de provincias: es una tabla fija que no cambia.
COMUNIDADES: dict[str, list[str]] = {
    "Andalucía": ["Almería", "Cádiz", "Córdoba", "Granada", "Huelva", "Jaén",
                  "Málaga", "Sevilla"],
    "Aragón": ["Huesca", "Teruel", "Zaragoza"],
    "Asturias": ["Asturias"],
    "Cantabria": ["Cantabria"],
    "Castilla-La Mancha": ["Albacete", "Ciudad Real", "Cuenca", "Guadalajara",
                           "Toledo"],
    "Castilla y León": ["Ávila", "Burgos", "León", "Palencia", "Salamanca",
                        "Segovia", "Soria", "Valladolid", "Zamora"],
    "Cataluña": ["Barcelona", "Girona", "Lleida", "Tarragona"],
    "Ceuta": ["Ceuta"],
    "Comunidad de Madrid": ["Madrid"],
    "Comunidad Valenciana": ["Alicante", "Castellón", "Valencia"],
    "Extremadura": ["Badajoz", "Cáceres"],
    "Galicia": ["A Coruña", "Lugo", "Ourense", "Pontevedra"],
    "Illes Balears": ["Illes Balears"],
    "Canarias": ["Las Palmas", "S. C. de Tenerife"],
    "La Rioja": ["La Rioja"],
    "Melilla": ["Melilla"],
    "Navarra": ["Navarra"],
    "País Vasco": ["Álava", "Bizkaia", "Gipuzkoa"],
    "Región de Murcia": ["Murcia"],
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
            # Necesario para separar las novedades. Es la fecha en que el
            # robot la vio por primera vez, no la de publicación.
            "deteccion": fila.get("fecha_deteccion"),
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
  --alto:42px;   /* alto común de los tres controles de la barra */
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
/* Las columnas siguen separadas por la rejilla, pero sin marca visual:
   los números ya están alineados y las líneas sobraban. */
.marcador{
  display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  margin-top:46px;background:transparent;
}
.marcador div{background:var(--fondo);padding:20px 20px 20px 0}
.marcador span{
  display:block;font-size:1.75rem;font-weight:600;line-height:1.1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums;
  transition:opacity .18s ease;
}
/* Al filtrar, los números parpadean brevemente. Sin ese aviso el ojo no
   distingue si la lista se ha filtrado o si nunca cambió. */
.marcador.cambiando span{opacity:.25}
.marcador small{display:block;margin-top:6px;color:var(--tenue);font-size:.82rem}

/* ---------- Pestañas ---------- */
.pestanas{
  display:flex;gap:26px;margin-top:44px;
  border-bottom:1px solid var(--linea);
}
.pestana{
  background:none;border:0;color:var(--tenue);font:inherit;font-size:.98rem;
  font-weight:500;padding:0 0 13px;cursor:pointer;position:relative;
  transition:color .15s;
}
.pestana:hover{color:var(--texto)}
.pestana[aria-selected="true"]{color:var(--texto)}
/* La línea inferior marca la activa. Es más sobrio que un fondo y no
   añade otra caja a una página que ya tiene bastantes. */
.pestana[aria-selected="true"]::after{
  content:'';position:absolute;left:0;right:0;bottom:-1px;height:2px;
  background:var(--texto);
}
.pestana .cuantas{
  margin-left:7px;font-size:.82rem;color:var(--tenue);font-weight:400;
}

/* ---------- Filtros ---------- */
.filtros{
  display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start;
  padding:20px 0;position:sticky;top:0;background:var(--fondo);
  border-bottom:1px solid var(--linea);z-index:10;
}
/* Selector de provincias: campo con etiquetas y desplegable buscable.
   Sustituye a una fila de 50 botones, que era ruido visual: la vista se
   resbalaba por encima y el filtro pasaba desapercibido. */
/* `align-self` lo mantiene a la altura de los otros controles: la barra
   se alinea arriba para que el campo pueda crecer con las etiquetas sin
   descolocar al resto, y eso le quitaba el alto que heredaba antes. */
.selector{position:relative;flex:1 1 260px;min-width:0;align-self:stretch}
.campo{
  display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  background:var(--superficie);border:1px solid var(--linea);
  border-radius:3px;padding:6px 10px;min-height:var(--alto);cursor:text;
  transition:border-color .15s;
}
/* Al enfocar se refuerza el borde que YA existe, en lugar de dibujar un
   segundo contorno encima. Dos marcos concéntricos ensucian el campo. */
.campo:focus-within{border-color:var(--linea-fuerte)}
.campo input:focus{outline:0}
/* Contorno en vez de relleno: un fondo claro sobre un tema oscuro
   descuadra la página entera. */
.etiqueta{
  display:inline-flex;align-items:center;gap:7px;
  background:none;color:var(--texto);
  border:1px solid var(--linea-fuerte);
  border-radius:100px;padding:3px 5px 3px 12px;font-size:.83rem;font-weight:500;
}
.etiqueta button{
  background:none;border:0;color:var(--tenue);cursor:pointer;
  font-size:1.05rem;line-height:1;padding:0 4px;transition:color .15s;
}
.etiqueta button:hover{color:var(--texto)}
/* Al lado de las etiquetas, no debajo: el cursor debe quedar donde el
   usuario espera seguir escribiendo. `min-width` le garantiza un ancho
   utilizable, y cuando ya no cabe salta de línea él solo en vez de irse
   encogiendo con cada provincia añadida. */
.campo input{
  flex:1 1 130px;min-width:130px;background:none;border:0;color:var(--texto);
  font:inherit;font-size:.94rem;padding:4px 0;outline:0;
}
.campo input::placeholder{color:var(--tenue)}

.desplegable{
  position:absolute;top:calc(100% + 5px);left:0;right:0;z-index:30;
  background:var(--superficie);border:1px solid var(--linea-fuerte);
  border-radius:3px;max-height:290px;overflow-y:auto;padding:5px;
  box-shadow:0 12px 32px rgba(0,0,0,.45);
}
.desplegable[hidden]{display:none}
/* Alto fijo: el texto secundario de las comunidades ("3 provincias")
   hacía sus filas más altas que las de provincia, y la lista quedaba
   irregular al recorrerla. */
.opcion{
  display:flex;align-items:center;gap:11px;width:100%;min-height:36px;
  background:none;border:0;color:var(--texto);font:inherit;font-size:.92rem;
  padding:0 9px;cursor:pointer;text-align:left;border-radius:2px;
}
.opcion:hover,.opcion.destacada{background:rgba(233,232,229,.07)}
/* La casilla es un cuadro dibujado con CSS, no un input: así el botón
   entero es una sola zona pulsable en lugar de dos. */
.casilla{
  width:15px;height:15px;flex:0 0 15px;border:1px solid var(--linea-fuerte);
  border-radius:2px;position:relative;
}
.opcion[aria-selected="true"] .casilla{background:var(--texto);border-color:var(--texto)}
.opcion[aria-selected="true"] .casilla::after{
  content:'';position:absolute;left:4px;top:1px;width:4px;height:8px;
  border:solid var(--fondo);border-width:0 2px 2px 0;transform:rotate(43deg);
}
.opcion .cuantas{margin-left:auto;color:var(--tenue);font-size:.84rem}
/* Las comunidades se distinguen de las provincias por la sangría y el
   peso, no por un separador: siguen siendo la misma lista y se recorren
   con las mismas flechas. */
.opcion.raiz{font-weight:600}
.opcion.hija{padding-left:24px;font-weight:400}
.opcion .abarca{color:var(--tenue);font-weight:400;font-size:.84rem}
.sinresultado{padding:14px 10px;color:var(--tenue);font-size:.9rem}

/* Los tres controles de la barra comparten alto exacto: 42px. Sin
   fijarlo, el relleno y el borde de cada tipo de elemento dan alturas
   que difieren en unos píxeles y la fila queda desalineada. */
select,input[type=search]{
  font:inherit;font-size:.94rem;color:var(--texto);line-height:1.2;
  background:var(--superficie);border:1px solid var(--linea);
  border-radius:3px;padding:0 12px;height:var(--alto);
  transition:border-color .15s;
}
/* Los tres reparten el ancho a partes iguales y saltan de línea a la vez.
   Con anchos distintos, la barra parecía tres controles sueltos en lugar
   de una fila. */
select{flex:1 1 260px;min-width:0}
input[type=search]{flex:1 1 260px;min-width:0}
input[type=search]:focus{border-color:var(--linea-fuerte);outline:0}
select:focus-visible,.fila:focus-visible{
  outline:2px solid var(--linea-fuerte);outline-offset:2px;
}

/* ---------- Lista ---------- */
#lista{transition:opacity .16s ease}
#lista.cambiando{opacity:0}

.fila{
  display:grid;grid-template-columns:76px 1fr auto;gap:26px;
  animation:entrar .3s ease both;
}
/* La entrada escalonada hace legible el resultado de un filtro: se ve que
   la lista se ha vuelto a componer, no que estaba así desde el principio. */
@keyframes entrar{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){
  .fila{animation:none}
  #lista,.marcador span{transition:none}
}
.fila{
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

<div class="pestanas" role="tablist">
  <button class="pestana" role="tab" id="p-hoy" aria-selected="false"
          aria-controls="lista">Contratos de hoy<span class="cuantas" id="c-hoy"></span></button>
  <button class="pestana" role="tab" id="p-todo" aria-selected="true"
          aria-controls="lista">Todos los abiertos<span class="cuantas" id="c-todo"></span></button>
</div>

<div class="filtros">
  <select id="f-veredicto" aria-label="Filtrar por viabilidad">
    <option value="">Cualquier viabilidad</option>
    <option value="si">Para mí</option>
    <option value="quizas">Puede ser para mí</option>
  </select>
  <input type="search" id="f-texto" placeholder="Buscar por título u órgano">

  <div class="selector">
    <div class="campo" id="campo">
      <input type="text" id="buscador" autocomplete="off"
             role="combobox" aria-expanded="false" aria-controls="desplegable"
             aria-label="Filtrar por provincia" placeholder="Toda España">
    </div>
    <div class="desplegable" id="desplegable" role="listbox"
         aria-multiselectable="true" hidden></div>
  </div>
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

function marcador(lista){
  // El marcador describe LO QUE SE ESTÁ VIENDO, no el total. Filtrar por
  // una provincia debe responder "cuánto hay aquí", que es la pregunta
  // que se está haciendo quien filtra.
  const provincias = [...new Set(lista.map(o => o.provincia).filter(Boolean))];
  const total = lista.reduce((s,o) => s + (o.presupuesto || 0), 0);

  const n = lista.length;
  document.getElementById('titular').textContent = pestana === 'hoy'
    ? (n === 0 ? 'Hoy no hay contratos nuevos para ti'
               : `Hoy hay ${n} ${n === 1 ? 'contrato nuevo' : 'contratos nuevos'} para ti`)
    : `Hay ${n} ${n === 1 ? 'contrato abierto' : 'contratos abiertos'} que podrían interesarte`;

  document.getElementById('m-total').textContent = lista.length;
  document.getElementById('m-prov').textContent = provincias.length;
  document.getElementById('m-importe').textContent =
    new Intl.NumberFormat('es-ES',{notation:'compact',style:'currency',currency:'EUR',maximumFractionDigits:1}).format(total);
  document.getElementById('m-urge').textContent =
    lista.filter(o => o.dias != null && o.dias >= 0 && o.dias <= 7).length;
}

// Conjunto de provincias activas. Vacío significa "toda España", no
// "ninguna": es lo que espera quien todavía no ha elegido nada.
const elegidas = new Set();

// Se considera novedad lo detectado en la última pasada del robot, no lo
// detectado "hoy" según el calendario. Si el robot se cae un día, al
// volver detecta lo acumulado y todo eso entra como novedad: así no se
// pierde ninguna, aunque el rótulo "hoy" abarque a veces más de un día.
// Es una decisión deliberada: el usuario no tiene por qué saber cuándo
// falló el sistema, solo qué hay nuevo para él.
const ULTIMA_PASADA = DATOS.reduce((max,o) =>
  o.deteccion && o.deteccion > max ? o.deteccion : max, '');
const MARGEN_NOVEDAD = 6 * 60 * 60 * 1000;   // 6 h de holgura
const esNovedad = o => o.deteccion &&
  (new Date(ULTIMA_PASADA) - new Date(o.deteccion)) < MARGEN_NOVEDAD;

let pestana = 'todo';

function filtrar(){
  const ver = document.getElementById('f-veredicto').value;
  const txt = document.getElementById('f-texto').value.toLowerCase().trim();
  // El filtro de la pestaña se aplica junto a los demás: al cambiar de
  // pestaña se conservan la provincia y la viabilidad elegidas.
  return DATOS.filter(o =>
    (pestana === 'todo' || esNovedad(o)) &&
    (elegidas.size === 0 || elegidas.has(o.provincia)) &&
    (!ver || o.veredicto === ver) &&
    (!txt || (o.titulo + ' ' + o.organo).toLowerCase().includes(txt)));
}

function pintar(){
  const vistos = filtrar();
  marcador(vistos);

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
    : (pestana === 'hoy' && !elegidas.size && !document.getElementById('f-veredicto').value
        && !document.getElementById('f-texto').value.trim()
        ? `<p class="vacio">No hay novedades.<br>
           Vuelve mañana: el robot revisa los anuncios cada madrugada.</p>`
        : `<p class="vacio">Ningún contrato encaja con estos filtros.<br>
           Prueba a quitar la provincia o a ampliar la viabilidad.</p>`);

  document.querySelectorAll('.fila').forEach(f => f.onclick = () => {
    const d = document.getElementById('d' + f.dataset.i);
    f.setAttribute('aria-expanded', d.classList.toggle('abierto'));
  });
}

// La lista de provincias del desplegable sí sale del total: si solo
// ofreciera las de la selección actual, al filtrar por una provincia
// desaparecerían todas las demás y no habría forma de cambiar.
// Repinta con una transición breve. El desvanecido no es decorativo:
// avisa de que la lista se ha vuelto a componer. Sin él, un filtro que
// devuelve resultados parecidos parece no haber hecho nada.
let temporizador;
function repintar(){
  const lista = document.getElementById('lista');
  const marca = document.querySelector('.marcador');
  lista.classList.add('cambiando');
  marca.classList.add('cambiando');
  clearTimeout(temporizador);
  temporizador = setTimeout(() => {
    pintar();
    lista.classList.remove('cambiando');
    marca.classList.remove('cambiando');
  }, 160);
}

const provincias = [...new Set(DATOS.map(o => o.provincia).filter(Boolean))]
  .sort((a,b) => a.localeCompare(b,'es'));

// El dato real sustituye a la promesa: no "filtrados entre miles", sino
// cuántos se han leído y cuántos se han valorado para llegar hasta aquí.
const sector = 'Contratos de las administraciones españolas para música, artes escénicas, producción y servicios técnicos de espectáculo.';
document.getElementById('entradilla').textContent = STATS.leidos
  ? `${sector} De ${num(STATS.leidos)} anuncios leídos, ${num(STATS.valorados || 0)} se han valorado uno a uno.`
  : sector;

// Cuántas oportunidades hay por provincia: se enseña en cada opción para
// poder elegir con criterio en vez de a ciegas.
const porProvincia = {};
DATOS.forEach(o => { if(o.provincia) porProvincia[o.provincia] = (porProvincia[o.provincia]||0)+1; });

const campo = document.getElementById('campo');
const buscador = document.getElementById('buscador');
const desplegable = document.getElementById('desplegable');
let destacada = -1;   // opción resaltada con el teclado

// Quita tildes para que "avila" encuentre "Ávila" y "coruna" encuentre
// "A Coruña". Sin esto hay que escribir con el acento exacto.
const plano = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();

// Comunidades que tienen al menos una oportunidad. Ofrecer "Aragón"
// cuando no hay nada en Aragón sería una promesa vacía.
const COMUNIDADES = Object.entries(PAQUETE.comunidades || {})
  .map(([nombre, provs]) => ({
    nombre,
    provs: provs.filter(p => provincias.includes(p)),  // con oportunidades
    total: provs.length,                               // que tiene la comunidad
  }))
  .filter(c => c.provs.length > 0)
  .sort((a,b) => a.nombre.localeCompare(b.nombre,'es'));

// Una comunidad está marcada cuando lo están TODAS sus provincias con
// oportunidades. Así el estado del selector no puede contradecirse.
const completa = c => c.provs.every(p => elegidas.has(p));

function visibles(){
  const q = plano(buscador.value.trim());
  const lista = [];

  COMUNIDADES.forEach(c => {
    // Una comunidad aparece si coincide su nombre o el de alguna de sus
    // provincias: buscar "Sevilla" debe ofrecer también "Andalucía".
    const coincideCom = !q || plano(c.nombre).includes(q);
    const hijas = c.provs.filter(p => coincideCom || plano(p).includes(q));
    if(!hijas.length) return;

    // La cabecera solo se colapsa cuando la comunidad es UNIPROVINCIAL de
    // verdad: ahí comunidad y provincia son la misma cosa y repetirla
    // sería redundante. Si Aragón solo tiene contratos en Zaragoza, la
    // cabecera se mantiene: la comunidad existe y el usuario la busca por
    // su nombre.
    if(c.total === 1){
      lista.push({tipo:'provincia', nivel:'raiz', clave:c.provs[0]});
      return;
    }

    lista.push({tipo:'comunidad', nivel:'raiz', clave:c.nombre, dato:c});
    hijas.forEach(p => lista.push({tipo:'provincia', nivel:'hija', clave:p}));
  });

  return lista;
}

// Marcar o desmarcar un elemento de la lista, sea provincia o comunidad.
function alternar(item){
  if(item.tipo === 'comunidad'){
    const todas = completa(item.dato);
    item.dato.provs.forEach(p => todas ? elegidas.delete(p) : elegidas.add(p));
  } else {
    elegidas.has(item.clave) ? elegidas.delete(item.clave) : elegidas.add(item.clave);
  }
}

function pintarEtiquetas(){
  // Las etiquetas van ANTES del campo de escritura, así que el cursor
  // queda siempre a la derecha de la última: se ve que se puede seguir
  // escribiendo aunque ya haya provincias elegidas.
  campo.querySelectorAll('.etiqueta').forEach(e => e.remove());
  [...elegidas].forEach(p => {
    const et = document.createElement('span');
    et.className = 'etiqueta';
    et.innerHTML = `${p}<button type="button" aria-label="Quitar ${p}">×</button>`;
    et.querySelector('button').onclick = ev => {
      ev.stopPropagation();
      elegidas.delete(p);
      pintarEtiquetas(); pintarOpciones(); repintar();
    };
    campo.insertBefore(et, buscador);
  });
  buscador.placeholder = elegidas.size ? '' : 'Toda España';
}

function pintarOpciones(){
  const lista = visibles();
  if(!lista.length){
    desplegable.innerHTML = '<p class="sinresultado">Ninguna provincia con ese nombre.</p>';
    return;
  }
  desplegable.innerHTML = lista.map((item,i) => {
    const esCom = item.tipo === 'comunidad';
    const marcada = esCom ? completa(item.dato) : elegidas.has(item.clave);
    const cuenta = esCom
      ? item.dato.provs.reduce((s,p) => s + (porProvincia[p]||0), 0)
      : (porProvincia[item.clave] || 0);
    const n = esCom ? item.dato.provs.length : 0;
    const extra = esCom
      ? `<span class="abarca">${n} ${n === 1 ? 'provincia' : 'provincias'}</span>`
      : '';
    // La sangría la decide el NIVEL, no el tipo: una provincia que va sola
    // en su comunidad se muestra a la izquierda del todo, para que no
    // parezca que cuelga de la comunidad anterior.
    return `<button type="button" role="option" data-i="${i}"
              class="opcion ${item.nivel === 'raiz' ? 'raiz' : 'hija'} ${i === destacada ? 'destacada' : ''}"
              aria-selected="${marcada}">
      <span class="casilla"></span>${item.clave} ${extra}
      <span class="cuantas">${cuenta}</span>
    </button>`;
  }).join('');

  desplegable.querySelectorAll('.opcion').forEach(b => b.onclick = () => {
    alternar(lista[+b.dataset.i]);
    // El desplegable NO se cierra: elegir varias es el caso normal, y
    // cerrarlo obligaría a reabrirlo por cada una.
    buscador.value = '';
    pintarEtiquetas(); pintarOpciones(); repintar();
    buscador.focus();
  });
}

function abrir(){
  desplegable.hidden = false;
  buscador.setAttribute('aria-expanded','true');
  pintarOpciones();
}
function cerrar(){
  desplegable.hidden = true;
  buscador.setAttribute('aria-expanded','false');
  destacada = -1;
}

campo.onclick = () => { buscador.focus(); abrir(); };
buscador.onfocus = abrir;
buscador.oninput = () => { destacada = -1; abrir(); };

buscador.onkeydown = ev => {
  const lista = visibles();
  if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
    ev.preventDefault();
    if(desplegable.hidden) abrir();
    destacada = ev.key === 'ArrowDown'
      ? Math.min(destacada + 1, lista.length - 1)
      : Math.max(destacada - 1, 0);
    pintarOpciones();
    desplegable.querySelector('.destacada')?.scrollIntoView({block:'nearest'});
  } else if(ev.key === 'Enter'){
    ev.preventDefault();
    // Con una sola coincidencia, Enter la elige aunque no se haya bajado
    // con las flechas: escribir "giro" y pulsar Enter debe funcionar.
    const item = destacada >= 0 ? lista[destacada] : (lista.length === 1 ? lista[0] : null);
    if(item){
      alternar(item);
      buscador.value = ''; destacada = -1;
      pintarEtiquetas(); pintarOpciones(); repintar();
    }
  } else if(ev.key === 'Backspace' && !buscador.value && elegidas.size){
    // Borrar con el campo vacío quita la última etiqueta, como en
    // cualquier campo de destinatarios de correo.
    const ultima = [...elegidas].pop();
    elegidas.delete(ultima);
    pintarEtiquetas(); pintarOpciones(); repintar();
  } else if(ev.key === 'Escape'){
    cerrar(); buscador.blur();
  }
};

document.addEventListener('click', ev => {
  if(!ev.target.closest('.selector')) cerrar();
});

pintarEtiquetas();

// Los contadores de las pestañas ignoran los filtros: dicen cuánto hay en
// total en cada una, no cuánto queda tras filtrar. Si no, cambiar de
// pestaña sería un salto a ciegas.
document.getElementById('c-hoy').textContent = DATOS.filter(esNovedad).length;
document.getElementById('c-todo').textContent = DATOS.length;

function cambiarPestana(cual){
  pestana = cual;
  document.getElementById('p-hoy').setAttribute('aria-selected', cual === 'hoy');
  document.getElementById('p-todo').setAttribute('aria-selected', cual === 'todo');
  repintar();
}
document.getElementById('p-hoy').onclick = () => cambiarPestana('hoy');
document.getElementById('p-todo').onclick = () => cambiarPestana('todo');

['f-veredicto','f-texto'].forEach(id =>
  document.getElementById(id).addEventListener('input', repintar));

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
    paquete = {
        "oportunidades": oportunidades,
        "stats": stats or {},
        "comunidades": COMUNIDADES,
    }
    datos = json.dumps(paquete, ensure_ascii=False).replace("</", "<\\/")
    ahora = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y a las %H:%M")

    html = PLANTILLA.replace("__DATOS__", datos).replace("__ACTUALIZADO__", ahora)

    # Un CSS con una llave sin cerrar no da error: el navegador descarta
    # en silencio todo lo que viene después y la página aparece sin
    # diseño. Como los estilos se editan por sustitución de texto, es un
    # fallo fácil de introducir y difícil de ver, así que se comprueba.
    for etiqueta, patron in (("CSS", r"<style>(.*?)</style>"),
                             ("JavaScript", r"<script>(.*?)</script>")):
        for bloque in re.findall(patron, html, re.S):
            abiertas, cerradas = bloque.count("{"), bloque.count("}")
            if abiertas != cerradas:
                raise ValueError(
                    f"{etiqueta} descuadrado: {abiertas} llaves abiertas y "
                    f"{cerradas} cerradas. La página se vería sin estilos."
                )

    if "__DATOS__" in html or "__ACTUALIZADO__" in html:
        raise ValueError("Han quedado marcadores sin sustituir en la plantilla.")

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
