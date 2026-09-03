#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STATE SCRAPER · Paso 5 — Alerta diaria
======================================

Envía por correo las oportunidades que el robot ha detectado en su última
pasada. Cierra el circuito: leer, filtrar, cribar y avisar.

Dos decisiones que gobiernan este paso:

  · SOLO SI HAY NOVEDADES. Un correo diario que a veces dice "hoy no hay
    nada" acaba en la papelera sin abrir, y con él los días en que sí
    había algo. El silencio también informa.

  · NOVEDAD ES LO DE LA ÚLTIMA PASADA, no lo de hoy según el calendario.
    Si el robot se cae un día, al volver detecta lo acumulado y todo eso
    se envía. Así no se pierde ninguna oportunidad por una avería. Es la
    misma definición que usa la web, para que nadie reciba por correo
    algo que ya vio marcado como nuevo en la página.

Uso:
    python alertador.py                # envía si hay novedades
    python alertador.py --simulacro    # muestra el correo, NO lo envía

Variables de entorno:
    SUPABASE_URL, SUPABASE_KEY   -> obligatorias
    RESEND_API_KEY               -> obligatoria (salvo en simulacro)
    DESTINATARIOS_ALERTA         -> correos separados por comas
    REMITENTE_ALERTA             -> por defecto onboarding@resend.dev
    URL_INTERFAZ                 -> enlace a la web que se incluye
    HORAS_NOVEDAD                -> ventana de novedad (por defecto 6)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ==============================================================
# 1. CONFIGURACIÓN
# ==============================================================

VISTA = "oportunidades"
API_RESEND = "https://api.resend.com/emails"

REMITENTE = os.environ.get("REMITENTE_ALERTA", "onboarding@resend.dev")
URL_INTERFAZ = os.environ.get(
    "URL_INTERFAZ", "https://vincent-vegga.github.io/state-scraper/"
)
# Holgura sobre la última detección. Una pasada tarda minutos, no horas,
# pero el margen absorbe ejecuciones que se solapen o se retrasen.
HORAS_NOVEDAD = int(os.environ.get("HORAS_NOVEDAD", "6"))

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
# 2. LECTURA
# ==============================================================

def obtener_cliente():
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


# Los plazos españoles se publican en hora peninsular. Asumir UTC cuando
# falta la zona desplaza un vencimiento de las 23:59 al día siguiente.
try:
    from zoneinfo import ZoneInfo
    ZONA_ESPANA = ZoneInfo("Europe/Madrid")
except Exception:
    ZONA_ESPANA = timezone.utc


def a_fecha(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=ZONA_ESPANA)


def novedades(cliente) -> list[dict]:
    """
    Oportunidades detectadas en la última pasada del robot.

    El corte se toma respecto a la detección MÁS RECIENTE que hay en la
    tabla, no respecto al reloj. Así el resultado es el mismo aunque el
    correo se envíe con retraso, y coincide con lo que enseña la web.
    """
    try:
        filas = (cliente.table(VISTA).select("*").execute().data) or []
    except Exception as error:
        logging.error("No se pudo leer la vista '%s': %s", VISTA, error)
        sys.exit(1)

    detecciones = [a_fecha(f.get("fecha_deteccion")) for f in filas]
    detecciones = [d for d in detecciones if d]
    if not detecciones:
        return []

    corte = max(detecciones) - timedelta(hours=HORAS_NOVEDAD)
    recientes = [f for f in filas
                 if (d := a_fecha(f.get("fecha_deteccion"))) and d >= corte]

    # Lo que vence antes, primero: es el orden en que hay que actuar.
    recientes.sort(key=lambda f: f.get("fecha_limite") or "9999")
    return recientes


# ==============================================================
# 3. EL CORREO
# ==============================================================

def euros(valor) -> str:
    if valor is None:
        return "sin importe publicado"
    entero = f"{float(valor):,.0f}".replace(",", ".")
    return f"{entero} €"


def dias_restantes(limite: str | None) -> str:
    """Días restantes y hora exacta de cierre, en hora peninsular."""
    fecha = a_fecha(limite)
    if not fecha:
        return "sin plazo publicado"
    local = fecha.astimezone(ZONA_ESPANA)
    dias = (fecha - datetime.now(timezone.utc)).days
    cuando = local.strftime("%d/%m a las %H:%M")
    if dias < 0:
        return f"vencido ({cuando})"
    if dias == 0:
        return f"vence hoy, {local.strftime('%H:%M')}"
    plazo = "queda 1 día" if dias == 1 else f"quedan {dias} días"
    return f"{plazo} · hasta el {cuando}"


# Algunos órganos, sobre todo catalanes, ponen como título el encabezado
# entero del pliego: un párrafo con el objeto, los fines y hasta los
# objetivos del contrato. En una lista se lee mal; en un correo, peor.
_ARRANQUES = (
    "l'objecte del present contracte és la prestació del servei de",
    "l'objecte del present contracte és la prestació de",
    "l'objecte del present contracte és el",
    "l'objecte del present contracte és la",
    "l'objecte d'aquest contracte el constitueix el",
    "l'objecte d'aquest contracte és",
    "el present contracte té per objecte la prestació de",
    "el present contracte té per objecte",
    "es objeto del presente contrato la prestación del servicio de",
    "es objeto del presente contrato la",
    "es objeto del presente contrato el",
    "el objeto del presente contrato es la prestación de",
    "el objeto del presente contrato es",
    "constituye el objeto del presente contrato la",
    "constituye el objeto del presente contrato",
)
LARGO_MAXIMO = 130


def acortar(titulo: str) -> str:
    """
    Deja el título en algo legible sin perder de qué va el contrato.

    Primero quita el preámbulo jurídico ("l'objecte del present contracte
    és..."), que no aporta nada y se repite igual en todos. Después corta
    por la primera frase, y si aún es largo, por palabra entera.
    """
    limpio = " ".join((titulo or "").split())
    bajo = limpio.lower()
    # Se prueban de más largo a más corto, y el prefijo debe terminar en
    # límite de palabra: si no, "la prestació de" recortaba dentro de "la
    # prestació dels serveis" y se comía una letra.
    for arranque in sorted(_ARRANQUES, key=len, reverse=True):
        if not bajo.startswith(arranque):
            continue
        siguiente = limpio[len(arranque):len(arranque) + 1]
        if siguiente and siguiente not in " :,;":
            continue
        limpio = limpio[len(arranque):].lstrip(" :,;")
        if limpio:
            limpio = limpio[0].upper() + limpio[1:]
        break

    if len(limpio) <= LARGO_MAXIMO:
        return limpio

    # Cortar por el final de la primera frase, si cae en un sitio razonable.
    punto = limpio.find(". ")
    if 40 <= punto <= LARGO_MAXIMO:
        return limpio[:punto + 1]

    corte = limpio[:LARGO_MAXIMO].rsplit(" ", 1)[0]
    return corte.rstrip(" ,;:.") + "…"


def provincia_de(codigo_postal: str | None) -> str:
    cp = "".join(c for c in (codigo_postal or "") if c.isdigit())
    if len(cp) == 4:
        cp = "0" + cp
    return PROVINCIAS.get(cp[:2], "") if len(cp) >= 2 else ""


def componer(items: list[dict]) -> tuple[str, str, str]:
    """
    Devuelve (asunto, cuerpo HTML, cuerpo en texto plano).

    Se envían las dos versiones: hay clientes de correo que no muestran
    HTML, y un mensaje que llega en blanco es peor que no llegar.
    """
    n = len(items)
    asunto = (f"{n} contrato nuevo para ti" if n == 1
              else f"{n} contratos nuevos para ti")

    filas_html, filas_texto = [], []
    for it in items:
        # Se escapa UNA sola vez, al insertar en la plantilla. Escapar el
        # órgano aquí y el contexto después convertía "X & Y" en
        # "X &amp;amp; Y" en el correo.
        titulo = acortar(it.get("titulo") or "") or "(sin título)"
        organo = it.get("organo") or ""
        prov = provincia_de(it.get("codigo_postal"))
        plazo = dias_restantes(it.get("fecha_limite"))
        importe = euros(it.get("presupuesto"))
        enlace = it.get("enlace") or URL_INTERFAZ
        contexto = " · ".join(x for x in (organo, prov) if x)

        filas_html.append(f"""
        <tr><td style="padding:20px 0;border-bottom:1px solid #E4E2DD;">
          <a href="{html.escape(enlace, quote=True)}"
             style="color:#17171A;font-size:16px;font-weight:600;
                    text-decoration:none;line-height:1.45;">{html.escape(titulo)}</a>
          <div style="color:#6E6E75;font-size:14px;margin-top:6px;">{html.escape(contexto)}</div>
          <div style="color:#17171A;font-size:14px;margin-top:8px;">
            <strong>{importe}</strong>
            <span style="color:#6E6E75;">· {plazo}</span>
          </div>
        </td></tr>""")

        # La versión en texto NO se escapa: no es HTML.
        filas_texto.append(
            f"- {titulo}\n"
            f"  {contexto}\n"
            f"  {importe} · {plazo}\n"
            f"  {enlace}\n"
        )

    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es"><body style="margin:0;padding:0;background:#F5F4F1;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F5F4F1;padding:32px 16px;">
<tr><td align="center">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="max-width:560px;background:#FFFFFF;border-radius:4px;padding:36px 32px;
                font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
    <tr><td>
      <h1 style="margin:0 0 8px;font-size:22px;font-weight:600;color:#17171A;line-height:1.3;">
        {asunto}
      </h1>
      <p style="margin:0 0 8px;color:#6E6E75;font-size:15px;line-height:1.6;">
        Contratos públicos de música, artes escénicas, producción y servicios
        técnicos de espectáculo, detectados esta madrugada.
      </p>
    </td></tr>
    <tr><td><table width="100%" cellpadding="0" cellspacing="0">{''.join(filas_html)}</table></td></tr>
    <tr><td style="padding-top:28px;">
      <a href="{html.escape(URL_INTERFAZ)}"
         style="display:inline-block;background:#17171A;color:#FFFFFF;
                padding:12px 22px;border-radius:3px;text-decoration:none;
                font-size:15px;font-weight:500;">Ver todos los contratos abiertos</a>
    </td></tr>
    <tr><td style="padding-top:26px;color:#8B8B92;font-size:12px;line-height:1.6;">
      Datos de la Plataforma de Contratación del Sector Público y de las
      plataformas autonómicas agregadas. Solo recibes este correo los días
      que hay novedades.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""

    cuerpo_texto = (
        f"{asunto}\n\n"
        "Contratos públicos de música, artes escénicas, producción y servicios\n"
        "técnicos de espectáculo, detectados esta madrugada.\n\n"
        + "\n".join(filas_texto)
        + f"\nVer todos los contratos abiertos: {URL_INTERFAZ}\n"
    )

    return asunto, cuerpo_html, cuerpo_texto


# ==============================================================
# 4. ENVÍO
# ==============================================================

def enviar(asunto: str, cuerpo_html: str, cuerpo_texto: str,
           destinatarios: list[str]) -> bool:
    """
    Envía por Resend. Se usa urllib y no una librería nueva: es una sola
    petición HTTP y no compensa otra dependencia que mantener.
    """
    clave = os.environ.get("RESEND_API_KEY", "").strip()
    if not clave:
        logging.error("Falta RESEND_API_KEY en los secrets del repositorio.")
        return False

    cuerpo = json.dumps({
        "from": REMITENTE,
        "to": destinatarios,
        "subject": asunto,
        "html": cuerpo_html,
        "text": cuerpo_texto,
    }).encode("utf-8")

    peticion = urllib.request.Request(
        API_RESEND, data=cuerpo, method="POST",
        headers={
            "Authorization": f"Bearer {clave}",
            "Content-Type": "application/json",
            # Sin User-Agent, la petición sale identificándose como
            # urllib y el cortafuegos que hay delante de la API la
            # rechaza con un 403 antes de que llegue a Resend. El
            # intento ni siquiera aparece en su panel.
            "User-Agent": "StateScraper/1.0 (alertas de contratacion publica)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(peticion, timeout=30) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8"))
            logging.info("Correo aceptado por Resend (id %s).",
                         datos.get("id", "desconocido"))
            return True
    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", "replace")[:600]
        logging.error("Resend rechazó el envío (HTTP %s): %s", error.code, detalle)
        if error.code == 403:
            logging.error(
                "Un 403 suele ser una de estas tres: la clave no tiene "
                "permiso de envío; el destinatario no es la dirección con "
                "la que te registraste (obligatorio sin dominio propio); o "
                "la petición fue bloqueada antes de llegar a la API."
            )
        elif error.code == 422:
            logging.error("Un 422 apunta al remitente o al destinatario.")
        return False
    except Exception as error:
        logging.error("No se pudo enviar el correo: %s", error)
        return False


# ==============================================================
# 5. ORQUESTACIÓN
# ==============================================================

def main() -> int:
    argumentos = argparse.ArgumentParser(
        description="State Scraper · Paso 5, alerta diaria por correo."
    )
    argumentos.add_argument("--simulacro", action="store_true",
                            help="Muestra el correo por pantalla y NO lo envía.")
    opciones = argumentos.parse_args()

    configurar_logging()
    logging.info("=" * 62)
    logging.info("ALERTA DIARIA%s", "  ·  SIMULACRO" if opciones.simulacro else "")
    logging.info("=" * 62)

    cliente = obtener_cliente()
    items = novedades(cliente)

    if not items:
        # Silencio deliberado: un correo que dice "hoy no hay nada" enseña
        # a ignorar el remitente, y con él los días que sí importan.
        logging.info("Sin novedades. No se envía ningún correo.")
        return 0

    logging.info("Novedades a enviar: %d", len(items))
    asunto, cuerpo_html, cuerpo_texto = componer(items)

    if opciones.simulacro:
        logging.info("Asunto: %s", asunto)
        logging.info("--- versión en texto ---\n%s", cuerpo_texto)
        logging.info("SIMULACRO: no se ha enviado nada.")
        return 0

    destinatarios = [d.strip() for d in
                     os.environ.get("DESTINATARIOS_ALERTA", "").split(",")
                     if d.strip()]
    if not destinatarios:
        logging.error("Falta DESTINATARIOS_ALERTA. Sin destino no hay alerta.")
        return 1

    # Se enmascara el destinatario: el registro de Actions es público en
    # un repositorio público, pero hace falta ver si la dirección es la
    # que se espera.
    visibles = [d[:2] + "***@" + d.split("@")[-1] if "@" in d else "???"
                for d in destinatarios]
    logging.info("Enviando desde %s a: %s", REMITENTE, ", ".join(visibles))

    if not enviar(asunto, cuerpo_html, cuerpo_texto, destinatarios):
        # Fallar en rojo: un envío fallido que termina en verde es
        # indistinguible de un día sin novedades.
        return 1

    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if ruta:
        try:
            with open(ruta, "a", encoding="utf-8") as fichero:
                fichero.write(f"\n## Alerta enviada\n\n**{len(items)}** "
                              f"novedades a {len(destinatarios)} destinatario(s).\n")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
