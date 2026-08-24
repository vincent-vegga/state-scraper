#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STATE SCRAPER v2 - Pasos 1 y 2
==============================

Vigilancia diaria de licitaciones públicas de nicho (eventos y cultura).

Arquitectura de esta versión:

  PASO 1 · Descarga de los feeds ATOM/RSS oficiales, con reintentos,
           timeouts y paginación acotada.

  PASO 2 · Parseo mediante EXTRACTORES ESPECIALIZADOS por fuente
           (PLACSP y Catalunya tienen esquemas y particularidades
           distintas), filtrado estricto por prefijo de CPV, y
           control de estado contra Supabase.

  SALIDA · Supabase es el único destino. No se escribe ningún fichero
           en el repositorio: ni JSON, ni CSV, ni caché. El repositorio
           contiene código, nunca datos.

Los pasos 3 (descarga de pliegos), 4 (análisis LLM) y 5 (alerta) leerán
de la tabla `licitaciones`, filtrando por `estado_pipeline`.

Uso:
    python lector_atom.py                 # ejecución normal
    python lector_atom.py --diagnostico   # prueba los feeds SIN tocar Supabase

Variables de entorno requeridas (en ejecución normal):
    SUPABASE_URL   -> https://xxxxxxxx.supabase.co
    SUPABASE_KEY   -> clave secreta (sb_secret_... o service_role)

Variables de entorno opcionales (afinado sin editar el código):
    DIAS_ANTIGUEDAD_MAX       -> tope de retroceso en días (por defecto 14)
    DIAS_MARGEN_SOLAPE        -> solape sobre lo ya guardado (por defecto 2)
    MAX_PAGINAS_POR_FEED      -> freno de emergencia (por defecto 25)
    SOLO_CATALUNYA_AGREGADO   -> 'true'/'false' (por defecto true)
    FEEDS_EXTRA_CATALUNYA     -> URLs extra tratadas con el extractor catalán
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Sequence

import requests
from dateutil import parser as parser_fechas
from lxml import etree

# ==============================================================
# 1. ZONA DE CONFIGURACIÓN
#    Es la única parte que necesitarás tocar con el tiempo.
# ==============================================================

# --- Códigos CPV objetivo -------------------------------------
# Se acepta la licitación si CUALQUIERA de sus CPV empieza por
# uno de estos prefijos.
#
# EL SECTOR ES CONFIGURACIÓN, NO CÓDIGO. Estos valores son el
# nicho de partida (cultura y eventos), pero se sobrescriben con
# la variable de entorno CPV_PREFIJOS sin tocar este fichero.
# Cambiar de sector es editar una línea del workflow.
#
# Nicho actual:
#   7995xxxx -> Servicios de organización de eventos, ferias y congresos
#   923xxxxx -> Servicios de entretenimiento (espectáculos, artes escénicas)
#   925xxxxx -> Servicios de bibliotecas, archivos, museos y patrimonio
CPV_PREFIJOS_POR_DEFECTO: tuple[str, ...] = ("7995", "923", "925")


def _leer_prefijos_cpv() -> tuple[str, ...]:
    """Lee los prefijos de la variable de entorno; si no hay, usa los de serie."""
    bruto = os.environ.get("CPV_PREFIJOS", "").strip()
    if bruto:
        prefijos = tuple(p.strip() for p in bruto.split(",") if p.strip())
        if prefijos:
            return prefijos
    return CPV_PREFIJOS_POR_DEFECTO


CPV_PREFIJOS: tuple[str, ...] = _leer_prefijos_cpv()

# --- Fuentes ---------------------------------------------------
# El campo "tipo" decide QUÉ EXTRACTOR se aplica a cada feed.
# Añadir una fuente nueva es añadir una entrada aquí; añadir un
# esquema nuevo es escribir un extractor y registrarlo abajo.
FEEDS: list[dict[str, str]] = [
    {
        "nombre": "PLACSP · Perfiles de contratante (sindicación 643)",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/sindicacion/"
            "sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
        ),
        "tipo": "placsp",
    },
    {
        "nombre": "Generalitat de Catalunya · vía plataformas agregadas (sindicación 1044)",
        "url": (
            "https://contrataciondelsectorpublico.gob.es/sindicacion/"
            "sindicacion_1044/PlataformasAgregadasSinMenores.atom"
        ),
        "tipo": "catalunya",
    },
]

# Señales de procedencia catalana dentro del canal agregado, que
# transporta TODAS las comunidades autónomas, no solo Cataluña.
DOMINIOS_CATALUNYA: tuple[str, ...] = (
    "contractaciopublica.cat",
    "contractaciopublica.gencat.cat",
    "gencat.cat",
    "seu-e.cat",
    "aoc.cat",
)
PISTAS_TEXTO_CATALUNYA: tuple[str, ...] = (
    "generalitat de catalunya",
    "ajuntament",
    "diputació",
    "diputacio de barcelona",
    "consell comarcal",
    "àrea metropolitana de barcelona",
    "area metropolitana de barcelona",
    "consorci",
)

# --- Parámetros de ejecución ----------------------------------
# VENTANA ADAPTATIVA.
# El script no vigila un número fijo de días: pregunta a Supabase cuál
# es la publicación más reciente que ya guardó de cada fuente y retrocede
# solo hasta ahí, con un margen de seguridad. En un día normal son pocas
# páginas; si el robot ha estado caído, retrocede solo lo necesario para
# tapar el hueco. Los feeds tienen profundidades muy distintas (PLACSP
# publica unas 2.400 entradas diarias y Catalunya unas 475), así que cada
# uno encuentra la suya sin configuración manual.
#
# DIAS_ANTIGUEDAD_MAX es el TOPE de retroceso, no el objetivo. Se aplica
# en la primera ejecución de una fuente y como freno tras una caída larga.
DIAS_ANTIGUEDAD_MAX = int(os.environ.get("DIAS_ANTIGUEDAD_MAX", "14"))

# Proporción de una página que debe quedar fuera de ventana para dejar de
# paginar. No se exige el 100 % porque el canal agregado mezcla plataformas
# con retrasos distintos: casi siempre se cuela algún rezagado reciente que
# impide el corte y obliga a descargar decenas de páginas inútiles.
UMBRAL_CORTE_PAGINA = float(os.environ.get("UMBRAL_CORTE_PAGINA", "0.9"))

# Ventana que usa el modo diagnóstico, que no tiene memoria que consultar.
# Corta a propósito: sirve para medir cobertura de campos, no para recolectar.
DIAS_DIAGNOSTICO = int(os.environ.get("DIAS_DIAGNOSTICO", "3"))

# Días de solape sobre la última publicación conocida. Cubre las entradas
# que llegan al feed con retraso o fuera de orden cronológico.
DIAS_MARGEN_SOLAPE = int(os.environ.get("DIAS_MARGEN_SOLAPE", "2"))

# Freno de emergencia: tope de páginas por feed. Con la ventana adaptativa
# no debería alcanzarse en operación normal; si el registro avisa de que
# se ha alcanzado, es señal de que algo va mal (feed desbocado o memoria
# vacía), no de que haya que subir el número sin más.
MAX_PAGINAS_POR_FEED = int(os.environ.get("MAX_PAGINAS_POR_FEED", "25"))
SOLO_CATALUNYA_AGREGADO = os.environ.get("SOLO_CATALUNYA_AGREGADO", "true").lower() != "false"

TABLA_SUPABASE = "licitaciones"

TIMEOUT_SEGUNDOS = 90
REINTENTOS_MAXIMOS = 3
ESPERA_ENTRE_REINTENTOS = 5  # segundos, se duplica en cada intento
TAMANO_LOTE_SUPABASE = 100   # filas por consulta/inserción
USER_AGENT = "StateScraper/2.0 (monitorizacion de licitaciones publicas)"


# ==============================================================
# 2. UTILIDADES GENERALES
# ==============================================================

def configurar_logging() -> None:
    """Deja trazas legibles en la pestaña Actions de GitHub."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def dividir_en_lotes(elementos: Sequence[Any], tamano: int) -> Iterator[Sequence[Any]]:
    """Trocea una lista larga en bloques manejables para la base de datos."""
    for inicio in range(0, len(elementos), tamano):
        yield elementos[inicio:inicio + tamano]


def texto_limpio(valor: str | None) -> str:
    """Normaliza el texto de un nodo XML (quita saltos de línea y espacios sobrantes)."""
    if not valor:
        return ""
    return " ".join(valor.split())


def a_numero(valor: str | None) -> float | None:
    """Convierte un importe del feed a número. Devuelve None si no se puede."""
    if not valor:
        return None
    limpio = re.sub(r"[^\d,.\-]", "", valor.strip())
    if not limpio:
        return None
    # Algunos publicadores usan la coma como separador decimal.
    if "," in limpio and "." not in limpio:
        limpio = limpio.replace(",", ".")
    elif "," in limpio and "." in limpio:
        limpio = limpio.replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


# CODICE publica las fechas en el formato `date` de XML Schema, que admite
# zona horaria SIN hora: "2026-06-09+02:00". Es legal y correcto, pero ni
# dateutil ni la librería estándar lo interpretan, así que hay que insertar
# la hora cero antes del desfase para poder leerlo.
_FECHA_CON_ZONA_SIN_HORA = re.compile(
    r"^(\d{4}-\d{2}-\d{2})([+-]\d{2}:\d{2}|Z)(\s+.*)?$"
)


def a_fecha(valor: str | None) -> datetime | None:
    """
    Interpreta una fecha del feed y la devuelve siempre con zona horaria.

    Tolera los tres formatos que aparecen en datos reales:
      · ISO completo de ATOM     -> 2026-08-20T09:00:00+02:00
      · RSS del canal catalán    -> Mon, 17 Aug 2026 08:00:00 +0200
      · `date` con zona de CODICE-> 2026-06-09+02:00   (sin hora)
    """
    if not valor:
        return None

    texto = valor.strip()

    # "2026-06-09+02:00" -> "2026-06-09T00:00:00+02:00"
    # Si detrás viene una hora suelta (IssueDate + IssueTime concatenados),
    # se descarta el desfase y se deja que se interprete la hora real.
    coincidencia = _FECHA_CON_ZONA_SIN_HORA.match(texto)
    if coincidencia:
        dia, zona, resto = coincidencia.groups()
        texto = f"{dia}T{resto.strip()}{zona}" if resto else f"{dia}T00:00:00{zona}"

    try:
        fecha = parser_fechas.parse(texto)
    except (ValueError, OverflowError, TypeError):
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha


# ==============================================================
# 3. CAPA XML · Acceso resistente a los namespaces de CODICE
#
#    Los feeds de PLACSP declaran espacios de nombres largos y
#    cambiantes (cac, cbc, cac-place-ext, at, ...). Buscar por
#    NOMBRE LOCAL de etiqueta hace que el parser siga funcionando
#    aunque el Ministerio publique una versión nueva del esquema.
# ==============================================================

def buscar_todos(nodo: etree._Element, etiqueta: str) -> list[etree._Element]:
    """Descendientes con ese nombre de etiqueta, ignorando el namespace."""
    return nodo.xpath(f".//*[local-name()='{etiqueta}']")


def buscar_hijos(nodo: etree._Element, etiqueta: str) -> list[etree._Element]:
    """Igual que `buscar_todos`, pero solo entre los hijos directos."""
    return nodo.xpath(f"./*[local-name()='{etiqueta}']")


def primer_texto(nodo: etree._Element, etiqueta: str, solo_hijos: bool = False) -> str:
    """Texto del primer nodo que coincida, o cadena vacía."""
    encontrados = buscar_hijos(nodo, etiqueta) if solo_hijos else buscar_todos(nodo, etiqueta)
    for elemento in encontrados:
        valor = texto_limpio(elemento.text)
        if valor:
            return valor
    return ""


def parsear_xml(contenido: bytes) -> etree._Element | None:
    """
    Convierte los bytes descargados en un árbol XML.

    `recover=True` permite aprovechar un documento con un carácter
    inválido o una etiqueta mal cerrada, en vez de perder la jornada.
    `resolve_entities=False` evita la clase de ataque XXE, relevante
    porque consumimos XML de terceros.
    """
    try:
        analizador = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
        raiz = etree.fromstring(contenido, parser=analizador)
        return raiz if raiz is not None else None
    except etree.XMLSyntaxError as error:
        logging.error("XML irrecuperable: %s", error)
        return None


def campos_del_resumen(entrada: etree._Element) -> dict[str, str]:
    """
    Trocea el texto libre de <summary>/<description> en pares clave-valor.

    PLACSP y las plataformas agregadas escriben ahí cosas como:
        "Id licitación: 12/2026; Órgano de Contratación: Ayuntamiento de X;
         Importe: 45000.00 EUR; Estado: EV"

    Es la red de seguridad cuando el XML estructurado viene vacío, algo
    frecuente en anuncios previos y en entradas replicadas desde una
    plataforma autonómica.
    """
    bruto = primer_texto(entrada, "summary", solo_hijos=True) \
        or primer_texto(entrada, "description", solo_hijos=True)
    campos: dict[str, str] = {}
    for fragmento in bruto.split(";"):
        if ":" not in fragmento:
            continue
        clave, _, valor = fragmento.partition(":")
        clave_normalizada = texto_limpio(clave).lower()
        if clave_normalizada:
            campos[clave_normalizada] = texto_limpio(valor)
    return campos


def extraer_cpvs(entrada: etree._Element) -> list[str]:
    """
    Extrae los CPV de una entrada probando tres estrategias en cascada.

    1. `ItemClassificationCode`: la etiqueta estándar de CODICE.
    2. Cualquier etiqueta cuyo nombre contenga "cpv" (publicadores atípicos).
    3. Números de 8 dígitos precedidos de la palabra "CPV" en texto libre.
    """
    encontrados: list[str] = []

    for nodo in buscar_todos(entrada, "ItemClassificationCode"):
        codigo = texto_limpio(nodo.text)
        if codigo:
            encontrados.append(codigo)

    if not encontrados:
        for nodo in entrada.xpath(
            ".//*[contains(translate(local-name(), 'CPV', 'cpv'), 'cpv')]"
        ):
            codigo = texto_limpio(nodo.text)
            if codigo:
                encontrados.append(codigo)

    if not encontrados:
        texto_completo = " ".join(entrada.itertext())
        encontrados.extend(
            re.findall(r"CPV[^0-9]{0,12}(\d{8})", texto_completo, re.IGNORECASE)
        )

    unicos: list[str] = []
    for codigo in encontrados:
        normalizado = "".join(c for c in codigo if c.isdigit())
        if normalizado and normalizado not in unicos:
            unicos.append(normalizado)
    return unicos


def extraer_presupuesto(entrada: etree._Element) -> float | None:
    """
    Localiza el importe más representativo.

    Preferencia: valor estimado del contrato > importe total > importe
    sin impuestos. None si todos vienen vacíos (habitual en anuncios previos).
    """
    for etiqueta in ("EstimatedOverallContractAmount", "TotalAmount", "TaxExclusiveAmount"):
        importe = a_numero(primer_texto(entrada, etiqueta))
        if importe is not None:
            return importe
    return None


def extraer_enlace(entrada: etree._Element) -> str:
    """URL pública del expediente."""
    for nodo in buscar_hijos(entrada, "link"):
        href = nodo.get("href")
        if href:
            return href.strip()
    # RSS clásico: el enlace es el texto del nodo, no un atributo.
    enlace_rss = primer_texto(entrada, "link", solo_hijos=True)
    if enlace_rss.startswith("http"):
        return enlace_rss
    identificador = primer_texto(entrada, "id", solo_hijos=True)
    return identificador if identificador.startswith("http") else ""


def normalizar_codigo_postal(valor: str | None) -> str | None:
    """
    Devuelve un CP español válido de 5 dígitos, o None.

    Dos saneados que importan en datos reales:
      · Algunos publicadores exportan el CP como número y pierden el cero
        inicial: '8017' es en realidad '08017' (Barcelona). Se rellena.
      · Se valida que los dos primeros dígitos estén entre 01 y 52, que es
        el rango de provincias. Así se descartan códigos extranjeros o
        cifras que no eran un CP (referencias, importes, años).
    """
    if not valor:
        return None
    digitos = "".join(c for c in valor if c.isdigit())
    if len(digitos) == 4:
        digitos = "0" + digitos
    if len(digitos) != 5:
        return None
    if not 1 <= int(digitos[:2]) <= 52:
        return None
    return digitos


def extraer_codigo_postal(entrada: etree._Element) -> str | None:
    """
    Localiza el código postal de la licitación, por orden de fiabilidad.

    1. LUGAR DE EJECUCIÓN (<cac:RealizedLocation>). Es el dato correcto:
       dónde se presta el servicio.
    2. Cualquier <cbc:PostalZone> de la entrada. En la práctica suele ser
       la dirección del órgano de contratación. Para contratos locales
       coincide con el lugar de ejecución; para un organismo central que
       licita en otra provincia, NO coincide. Se acepta como aproximación
       consciente, no como equivalente.
    3. Texto libre: cinco dígitos precedidos de 'CP' o 'código postal'.
    """
    for localizacion in buscar_todos(entrada, "RealizedLocation"):
        codigo = normalizar_codigo_postal(primer_texto(localizacion, "PostalZone"))
        if codigo:
            return codigo

    for nodo in buscar_todos(entrada, "PostalZone"):
        codigo = normalizar_codigo_postal(texto_limpio(nodo.text))
        if codigo:
            return codigo

    texto_completo = " ".join(entrada.itertext())
    coincidencia = re.search(
        r"(?:c\.?p\.?|c[óo]digo\s+postal)[^0-9]{0,10}(\d{4,5})",
        texto_completo,
        re.IGNORECASE,
    )
    if coincidencia:
        return normalizar_codigo_postal(coincidencia.group(1))

    return None


def extraer_fecha_publicacion(entrada: etree._Element) -> str | None:
    """
    Fecha REAL de publicación del anuncio, distinta de su última modificación.

    Un expediente publicado en julio que recibe cualquier cambio en agosto
    (se sube un documento, cambia de estado, se publica la adjudicación)
    reaparece en el feed con un `updated` de hoy. Para el script es nuevo;
    para el usuario lleva un mes en la calle. Y eso importa: si algo lleva
    tres semanas publicado y te acabas de enterar, la competencia te lleva
    tres semanas de ventaja para preparar su oferta.

    Se busca `IssueDate` de CODICE, excluyendo expresamente las que cuelgan
    de referencias a documentos: cada PDF tiene su propia fecha de emisión
    y no es la del anuncio. Si no viene, se usa el `published` de ATOM.
    """
    nodos = entrada.xpath(
        ".//*[local-name()='IssueDate']"
        "[not(ancestor::*[contains(local-name(), 'DocumentReference')])]"
    )
    for nodo in nodos:
        fecha_texto = texto_limpio(nodo.text)
        if not fecha_texto:
            continue
        padre = nodo.getparent()
        hora = primer_texto(padre, "IssueTime", solo_hijos=True) if padre is not None else ""
        momento = a_fecha(f"{fecha_texto} {hora}".strip()) or a_fecha(fecha_texto)
        if momento:
            return momento.isoformat()

    momento = a_fecha(primer_texto(entrada, "published", solo_hijos=True))
    return momento.isoformat() if momento else None


def extraer_fecha_limite(entrada: etree._Element) -> str | None:
    """
    Fecha límite de presentación de ofertas, si el feed la trae.

    En CODICE vive dentro de <cac:TenderingProcess>, repartida entre una
    fecha y una hora en nodos separados que hay que recomponer.

    Orden de búsqueda:
      1. Plazo de presentación de OFERTAS (procedimiento abierto).
      2. Plazo de presentación de SOLICITUDES DE PARTICIPACIÓN, que es el
         que aplica en procedimientos restringidos y de licitación con
         negociación, donde la primera fase no es una oferta.
      3. Cualquier nodo cuyo nombre contenga "Deadline", por si un
         publicador usa una variante del esquema.

    Se devuelve en ISO 8601 para que PostgreSQL la interprete sin ayuda.
    """
    def _componer(bloque: etree._Element) -> str | None:
        fecha_texto = primer_texto(bloque, "EndDate")
        if not fecha_texto:
            return None
        hora_texto = primer_texto(bloque, "EndTime")
        combinada = f"{fecha_texto} {hora_texto}".strip() if hora_texto else fecha_texto
        momento = a_fecha(combinada) or a_fecha(fecha_texto)
        return momento.isoformat() if momento else None

    for etiqueta in ("TenderSubmissionDeadlinePeriod",
                     "ParticipationRequestReceptionPeriod"):
        for bloque in buscar_todos(entrada, etiqueta):
            resultado = _componer(bloque)
            if resultado:
                return resultado

    for bloque in entrada.xpath(
        ".//*[contains(translate(local-name(), 'DEADLIN', 'deadlin'), 'deadlin')]"
    ):
        resultado = _componer(bloque)
        if resultado:
            return resultado

    return None


# Tipos de documento en CODICE, con el nombre que usaremos internamente.
# Un expediente referencia varios: al menos pliego administrativo y técnico.
TIPOS_DOCUMENTO: dict[str, str] = {
    "LegalDocumentReference":      "pliego_administrativo",
    "TechnicalDocumentReference":  "pliego_tecnico",
    "AdditionalDocumentReference": "adicional",
}


def extension_de(nombre: str) -> str:
    """
    Extensión del fichero a partir de su nombre, o 'desconocida'.

    Permite saber el formato de un documento SIN descargarlo: el feed trae
    el nombre original con el que el órgano lo subió.
    """
    if "." not in nombre:
        return "desconocida"
    posible = nombre.rsplit(".", 1)[-1].strip().lower()
    return posible if 1 < len(posible) <= 5 and posible.isalnum() else "desconocida"


def extraer_documentos(entrada: etree._Element) -> list[dict[str, str]]:
    """
    Documentos que el feed referencia para esta licitación.

    En CODICE cada referencia cuelga de un nodo distinto según su
    naturaleza jurídica, y la dirección vive dentro de
    <cac:Attachment><cac:ExternalReference><cbc:URI>.

    De cada uno se recoge:
      · tipo        -> código oficial (DOC_PCAP, DOC_PPT...) si viene; si no,
                       se deduce del nodo del que cuelga.
      · nombre      -> nombre original del fichero, con su extensión.
      · extension   -> el formato, conocido ANTES de descargar nada.
      · url         -> dirección directa, sin sesión ni token temporal.
      · hash        -> huella del contenido. Permite detectar que un pliego
                       ha sido modificado sin necesidad de archivarlo.
    """
    documentos: list[dict[str, str]] = []
    vistas: set[str] = set()

    for etiqueta, tipo_del_nodo in TIPOS_DOCUMENTO.items():
        for bloque in buscar_todos(entrada, etiqueta):
            url = primer_texto(bloque, "URI")
            if not url or url in vistas:
                continue
            vistas.add(url)
            nombre = primer_texto(bloque, "ID")
            documentos.append({
                "tipo": primer_texto(bloque, "DocumentTypeCode") or tipo_del_nodo,
                "tipo_del_nodo": tipo_del_nodo,
                "nombre": nombre,
                "extension": extension_de(nombre),
                "url": url,
                "hash": primer_texto(bloque, "DocumentHash"),
            })

    return documentos


# Un campo de solvencia relleno con "ver la cláusula 8ª del pliego" está
# formalmente cumplimentado pero es inservible: remite al PDF. Detectarlo
# permite medir cuánto del contenido útil vive realmente fuera del feed.
PATRON_REMISION = re.compile(
    r"(cl[áa]usula|apartado|pliego|\bpcap\b|\bppt\b|anexo|v[ée]ase|ver\s+el|"
    r"seg[úu]n\s+lo|conforme\s+a\s+lo|indicad[oa]s?\s+en)",
    re.IGNORECASE,
)


def extraer_condiciones(entrada: etree._Element) -> dict[str, Any]:
    """
    Condiciones de la licitación que CODICE publica en forma estructurada.

    Distingue deliberadamente entre dos cosas que se confunden:

      · CRITERIOS DE ADJUDICACIÓN: cómo te van a puntuar. Vienen con
        ponderación numérica y clasificados en objetivos (OBJ) o de
        juicio de valor (SUBJ). Suelen estar completos.

      · REQUISITOS DE SOLVENCIA: qué necesitas acreditar para poder
        presentarte. El campo existe, pero los órganos lo rellenan a
        menudo con una remisión al pliego en PDF. Se marca cuáles son
        remisiones para poder medir cuánto contenido útil falta.
    """
    criterios: list[dict[str, Any]] = []
    for bloque in buscar_todos(entrada, "AwardingCriteria"):
        descripcion = primer_texto(bloque, "Description")
        if not descripcion:
            continue
        criterios.append({
            "descripcion": descripcion,
            "tipo": primer_texto(bloque, "AwardingCriteriaTypeCode"),
            "peso": a_numero(primer_texto(bloque, "WeightNumeric")),
        })

    solvencia: list[dict[str, Any]] = []
    for etiqueta in ("TechnicalEvaluationCriteria",
                     "FinancialEvaluationCriteria",
                     "SpecificTendererRequirement"):
        for bloque in buscar_todos(entrada, etiqueta):
            descripcion = primer_texto(bloque, "Description")
            if not descripcion:
                continue
            solvencia.append({
                "clase": etiqueta,
                "descripcion": descripcion,
                "es_remision": bool(PATRON_REMISION.search(descripcion)),
            })

    garantia = None
    for bloque in buscar_todos(entrada, "RequiredFinancialGuarantee"):
        garantia = a_numero(primer_texto(bloque, "AmountRate"))
        if garantia is not None:
            break

    return {
        "criterios_adjudicacion": criterios,
        "solvencia": solvencia,
        "garantia_pct": garantia,
        "email_contacto": primer_texto(entrada, "ElectronicMail"),
        "telefono_contacto": primer_texto(entrada, "Telephone"),
    }


# ==============================================================
# 4. EXTRACTORES ESPECIALIZADOS POR FUENTE
#
#    Cada fuente tiene su función. Comparten la capa XML de arriba,
#    pero cada una conoce las particularidades de SU esquema. Cuando
#    una plataforma cambie su formato, se toca un solo extractor y
#    la otra fuente sigue operativa.
# ==============================================================

def extraer_placsp(entrada: etree._Element, fuente: str) -> dict[str, Any] | None:
    """
    Extractor para la sindicación 643 (perfiles de contratante del Estado).

    Esquema: CODICE puro. Los datos vienen bien estructurados dentro de
    <cac-place-ext:ContractFolderStatus>, así que se lee directamente del
    XML y solo se recurre al texto libre para el órgano de contratación.
    """
    identificador = primer_texto(entrada, "id", solo_hijos=True)
    enlace = extraer_enlace(entrada)
    id_licitacion = identificador or enlace
    if not id_licitacion:
        return None

    resumen = campos_del_resumen(entrada)

    # El órgano vive en cac:Party > cac:PartyName > cbc:Name.
    organo = ""
    for nodo_parte in buscar_todos(entrada, "PartyName"):
        organo = primer_texto(nodo_parte, "Name")
        if organo:
            break
    if not organo:
        organo = resumen.get("órgano de contratación") or resumen.get("organo de contratacion", "")

    return {
        "id_licitacion": id_licitacion,
        "fuente": fuente,
        "origen": "Estado",
        "expediente": primer_texto(entrada, "ContractFolderID") or resumen.get("id licitación", ""),
        "titulo": primer_texto(entrada, "title", solo_hijos=True)
                  or primer_texto(entrada, "Name")
                  or "(sin título)",
        "organo": organo or "(órgano no informado)",
        "enlace": enlace,
        "codigo_postal": extraer_codigo_postal(entrada),
        "fecha_limite": extraer_fecha_limite(entrada),
        "documentos": extraer_documentos(entrada),
        **extraer_condiciones(entrada),
        "presupuesto": extraer_presupuesto(entrada),
        "cpvs": extraer_cpvs(entrada),
        "estado_licitacion": primer_texto(entrada, "ContractFolderStatusCode"),
        # Interna: gobierna la paginación, porque es el orden del feed.
        "fecha_actualizacion": primer_texto(entrada, "updated", solo_hijos=True)
                               or primer_texto(entrada, "published", solo_hijos=True),
        # Para el usuario: cuánto tiempo lleva el anuncio en la calle.
        "fecha_publicacion": extraer_fecha_publicacion(entrada),
    }


def es_de_catalunya(enlace: str, organo: str, texto_entrada: str) -> bool:
    """
    Decide si una entrada del canal agregado procede de Cataluña.

    El canal de plataformas agregadas replica TODAS las comunidades
    autónomas. Sin este filtro, "PLACSP + Generalitat" se convertiría
    en "España entera, dos veces".

    Se comprueba, por orden de fiabilidad: el dominio del enlace, el
    nombre del órgano y, por último, cualquier mención a un dominio
    catalán en el cuerpo de la entrada.
    """
    referencia = f"{enlace} {texto_entrada}".lower()
    if any(dominio in referencia for dominio in DOMINIOS_CATALUNYA):
        return True

    organo_normalizado = organo.lower()
    if "catalunya" in organo_normalizado or "cataluña" in organo_normalizado:
        return True
    return any(pista in organo_normalizado for pista in PISTAS_TEXTO_CATALUNYA) and (
        "barcelona" in organo_normalizado
        or "girona" in organo_normalizado
        or "lleida" in organo_normalizado
        or "tarragona" in organo_normalizado
    )


def extraer_catalunya(entrada: etree._Element, fuente: str) -> dict[str, Any] | None:
    """
    Extractor para la Plataforma de Serveis de Contractació Pública.

    Tres diferencias reales frente al extractor de PLACSP:

    1. FORMATO DUAL. La plataforma catalana se publica tanto en CODICE
       (canal agregado) como en RSS plano (canales del perfil de
       contratante). Aquí se detecta cuál ha llegado y se lee en
       consecuencia: <entry>/<title> en ATOM, <item>/<title> en RSS.

    2. CAMPOS EN TEXTO LIBRE. En las entradas replicadas desde una
       plataforma autonómica el bloque CODICE llega a menudo incompleto
       (sin órgano, sin importe). Se recurre al resumen como fuente
       primaria, no como último recurso.

    3. FILTRO GEOGRÁFICO. El canal agregado trae todas las comunidades;
       se descarta lo que no sea catalán salvo que se desactive con
       SOLO_CATALUNYA_AGREGADO=false.
    """
    identificador = primer_texto(entrada, "id", solo_hijos=True) \
        or primer_texto(entrada, "guid", solo_hijos=True)
    enlace = extraer_enlace(entrada)
    id_licitacion = identificador or enlace
    if not id_licitacion:
        return None

    resumen = campos_del_resumen(entrada)

    # El órgano: primero el texto libre (más fiable aquí), luego el XML.
    organo = resumen.get("órgano de contratación") or resumen.get("organo de contratacion", "")
    if not organo:
        for nodo_parte in buscar_todos(entrada, "PartyName"):
            organo = primer_texto(nodo_parte, "Name")
            if organo:
                break

    texto_entrada = " ".join(entrada.itertext())

    if SOLO_CATALUNYA_AGREGADO and not es_de_catalunya(enlace, organo, texto_entrada):
        return None

    # El importe: XML estructurado y, si viene vacío, el resumen.
    presupuesto = extraer_presupuesto(entrada)
    if presupuesto is None:
        presupuesto = a_numero(resumen.get("importe") or resumen.get("import", ""))

    return {
        "id_licitacion": id_licitacion,
        "fuente": fuente,
        "origen": "Catalunya",
        "expediente": primer_texto(entrada, "ContractFolderID")
                      or resumen.get("id licitación", "")
                      or resumen.get("expedient", ""),
        "titulo": primer_texto(entrada, "title", solo_hijos=True)
                  or primer_texto(entrada, "Name")
                  or "(sin título)",
        "organo": organo or "(órgano no informado)",
        "enlace": enlace,
        "codigo_postal": extraer_codigo_postal(entrada),
        "fecha_limite": extraer_fecha_limite(entrada),
        "documentos": extraer_documentos(entrada),
        **extraer_condiciones(entrada),
        "presupuesto": presupuesto,
        "cpvs": extraer_cpvs(entrada),
        "estado_licitacion": primer_texto(entrada, "ContractFolderStatusCode")
                             or resumen.get("estado", ""),
        "fecha_actualizacion": primer_texto(entrada, "updated", solo_hijos=True)
                               or primer_texto(entrada, "published", solo_hijos=True)
                               or primer_texto(entrada, "pubDate", solo_hijos=True),
        "fecha_publicacion": extraer_fecha_publicacion(entrada),
    }


# Registro de extractores. Añadir una plataforma nueva (Euskadi,
# Andalucía...) es escribir su función y añadir una línea aquí.
EXTRACTORES: dict[str, Callable[[etree._Element, str], dict[str, Any] | None]] = {
    "placsp": extraer_placsp,
    "catalunya": extraer_catalunya,
}


# ==============================================================
# 5. PASO 1 · DESCARGA Y RECORRIDO DE LOS FEEDS
# ==============================================================

def crear_sesion_http() -> requests.Session:
    """Sesión HTTP reutilizable con cabeceras educadas."""
    sesion = requests.Session()
    sesion.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    })
    return sesion


def descargar_con_reintentos(sesion: requests.Session, url: str) -> bytes | None:
    """
    Descarga una URL tolerando cortes de red y caídas puntuales del servidor.

    Devuelve None si tras todos los intentos falla. Nunca lanza excepción:
    un feed caído no debe tumbar la ejecución completa.
    """
    espera = ESPERA_ENTRE_REINTENTOS
    for intento in range(1, REINTENTOS_MAXIMOS + 1):
        try:
            respuesta = sesion.get(url, timeout=TIMEOUT_SEGUNDOS)
            respuesta.raise_for_status()
            return respuesta.content
        except requests.exceptions.RequestException as error:
            logging.warning("Intento %d/%d fallido al descargar %s -> %s",
                            intento, REINTENTOS_MAXIMOS, url, error)
            if intento < REINTENTOS_MAXIMOS:
                time.sleep(espera)
                espera *= 2
    logging.error("Descarga descartada tras %d intentos: %s", REINTENTOS_MAXIMOS, url)
    return None


def cumple_filtro_cpv(cpvs: list[str]) -> bool:
    """True si algún CPV de la licitación empieza por un prefijo objetivo."""
    return any(codigo.startswith(CPV_PREFIJOS) for codigo in cpvs)


def es_reciente(fecha_texto: str, limite: datetime) -> bool:
    """
    True si la publicación entra en la ventana temporal vigilada.

    Si la fecha es ilegible se devuelve True: preferimos revisar de más
    a perder una licitación por un formato de fecha inesperado.
    """
    fecha = a_fecha(fecha_texto)
    if fecha is None:
        return True
    return fecha >= limite


def localizar_entradas(raiz: etree._Element) -> list[etree._Element]:
    """Devuelve las entradas del documento, sea ATOM (<entry>) o RSS (<item>)."""
    entradas = buscar_todos(raiz, "entry")
    return entradas if entradas else buscar_todos(raiz, "item")


def recorrer_feed(
    sesion: requests.Session,
    feed: dict[str, str],
    limite_temporal: datetime,
) -> list[dict[str, Any]]:
    """
    Descarga un feed, sigue su paginación y devuelve las licitaciones que
    superan el filtro CPV hasta la fecha `limite_temporal`.

    Los feeds encadenan páginas hacia atrás con <link rel="next">. Quien
    debe detener el recorrido es la fecha, no el tope de páginas: ese tope
    es solo un freno de emergencia y su activación se avisa como problema.
    """
    nombre = feed["nombre"]
    extractor = EXTRACTORES.get(feed.get("tipo", "placsp"), extraer_placsp)
    url_actual: str | None = feed["url"]

    resultados: list[dict[str, Any]] = []
    total_entradas = 0
    ventana_agotada = False       # ¿paramos por fecha (bien) o por tope (mal)?

    # Dos métricas distintas, y confundirlas induce a error:
    #   · leída   -> la entrada más antigua que se llegó a mirar, aunque se
    #                descartara por vieja. Sirve para saber si se alcanzó
    #                el límite temporal pedido.
    #   · cubierta-> la más antigua que entró DENTRO de la ventana. Es la
    #                profundidad real de vigilancia.
    fecha_mas_antigua_leida: datetime | None = None
    fecha_mas_antigua_cubierta: datetime | None = None

    for numero_pagina in range(1, MAX_PAGINAS_POR_FEED + 1):
        if not url_actual:
            break

        logging.info("[%s] Descargando página %d...", nombre, numero_pagina)
        contenido = descargar_con_reintentos(sesion, url_actual)
        if contenido is None:
            break

        raiz = parsear_xml(contenido)
        if raiz is None:
            break

        entradas = localizar_entradas(raiz)
        total_entradas += len(entradas)
        fuera_de_ventana = 0

        for entrada in entradas:
            # Entradas marcadas como borradas por el publicador: se ignoran.
            if buscar_hijos(entrada, "deleted-entry"):
                continue

            try:
                datos = extractor(entrada, nombre)
            except Exception as error:
                logging.warning("[%s] Entrada descartada por error de parseo: %s",
                                nombre, error)
                continue

            if datos is None:
                continue

            fecha_entrada = a_fecha(datos["fecha_actualizacion"])
            if fecha_entrada and (fecha_mas_antigua_leida is None
                                  or fecha_entrada < fecha_mas_antigua_leida):
                fecha_mas_antigua_leida = fecha_entrada

            if not es_reciente(datos["fecha_actualizacion"], limite_temporal):
                fuera_de_ventana += 1
                continue

            # A partir de aquí la entrada está DENTRO de la ventana: es lo
            # que cuenta como profundidad realmente cubierta.
            if fecha_entrada and (fecha_mas_antigua_cubierta is None
                                  or fecha_entrada < fecha_mas_antigua_cubierta):
                fecha_mas_antigua_cubierta = fecha_entrada

            if cumple_filtro_cpv(datos["cpvs"]):
                resultados.append(datos)

        # Si la MAYORÍA de la página queda fuera de la ventana, las siguientes
        # son aún más antiguas: dejamos de paginar. Exigir el 100 % hacía que
        # un solo rezagado reciente impidiera el corte indefinidamente.
        if entradas and fuera_de_ventana >= len(entradas) * UMBRAL_CORTE_PAGINA:
            logging.info(
                "[%s] Página %d: %d de %d entradas fuera de ventana (>= %.0f %%). Fin.",
                nombre, numero_pagina, fuera_de_ventana, len(entradas),
                UMBRAL_CORTE_PAGINA * 100,
            )
            ventana_agotada = True
            break

        enlaces_siguientes = raiz.xpath("./*[local-name()='link'][@rel='next']/@href")
        url_actual = enlaces_siguientes[0] if enlaces_siguientes else None

    ahora = datetime.now(timezone.utc)
    if fecha_mas_antigua_cubierta is not None:
        dias = (ahora - fecha_mas_antigua_cubierta).days
        logging.info("[%s] Ventana cubierta hasta %s (%d días), %d páginas.",
                     nombre, fecha_mas_antigua_cubierta.date(), dias, numero_pagina)
    if fecha_mas_antigua_leida is not None:
        logging.info("[%s] Entrada más antigua leída: %s (descartada por vieja "
                     "si es anterior a %s).",
                     nombre, fecha_mas_antigua_leida.date(), limite_temporal.date())

    # Solo hay hueco si NO se llegó a leer nada anterior al límite pedido.
    # Sin esta comprobación, un feed que retrocede de más disparaba la alarma.
    alcanzo_el_limite = (fecha_mas_antigua_leida is not None
                         and fecha_mas_antigua_leida <= limite_temporal)

    if not ventana_agotada and url_actual and not alcanzo_el_limite:
        logging.warning(
            "[%s] FRENO DE EMERGENCIA: alcanzadas las %d páginas sin llegar al "
            "punto donde termina lo ya guardado. Puede haber un hueco sin "
            "vigilar. Revisa si la memoria de Supabase está vacía o si el feed "
            "ha aumentado mucho su volumen.",
            nombre, MAX_PAGINAS_POR_FEED,
        )
    elif not ventana_agotada and not url_actual:
        logging.info("[%s] El feed se ha agotado antes del límite temporal.", nombre)

    logging.info("[%s] %d entradas revisadas -> %d coinciden con los CPV objetivo.",
                 nombre, total_entradas, len(resultados))
    return resultados


def construir_lista_fuentes() -> list[dict[str, str]]:
    """Devuelve las fuentes configuradas más las añadidas por variable de entorno."""
    fuentes = list(FEEDS)
    extras = os.environ.get("FEEDS_EXTRA_CATALUNYA", "").strip()
    if extras:
        for indice, url in enumerate(u.strip() for u in extras.split(",") if u.strip()):
            fuentes.append({
                "nombre": f"Catalunya · feed extra {indice + 1}",
                "url": url,
                "tipo": "catalunya",
            })
    return fuentes


def calcular_limite_temporal(cliente, feed: dict[str, str]) -> datetime:
    """
    Decide hasta dónde retroceder en un feed concreto.

    Regla: hasta la publicación más reciente que ya tenemos guardada de
    esa fuente, menos un margen de solape. Si no hay nada guardado (primera
    ejecución) o si el hueco es enorme (caída larga), se aplica el tope de
    DIAS_ANTIGUEDAD_MAX para que el trabajo siga siendo acotado.
    """
    ahora = datetime.now(timezone.utc)
    tope = ahora - timedelta(days=DIAS_ANTIGUEDAD_MAX)

    if cliente is None:
        # Modo diagnóstico: no hay memoria que consultar. Se usa una ventana
        # corta y propia, porque retroceder el tope completo tardaría diez
        # minutos y dispararía el aviso de freno sin que haya problema real.
        limite_diagnostico = ahora - timedelta(days=DIAS_DIAGNOSTICO)
        logging.info("[%s] Diagnóstico: ventana corta de %d días.",
                     feed["nombre"], DIAS_DIAGNOSTICO)
        return limite_diagnostico

    ultima = fecha_ultima_guardada(cliente, feed["nombre"])
    if ultima is None:
        logging.info("[%s] Sin histórico previo: se retrocede el máximo (%d días).",
                     feed["nombre"], DIAS_ANTIGUEDAD_MAX)
        return tope

    limite = ultima - timedelta(days=DIAS_MARGEN_SOLAPE)
    if limite < tope:
        logging.warning(
            "[%s] El hueco desde la última ejecución supera %d días. Se recorta "
            "al tope: puede quedar sin vigilar lo publicado antes de %s.",
            feed["nombre"], DIAS_ANTIGUEDAD_MAX, tope.date(),
        )
        return tope

    logging.info("[%s] Ventana adaptativa: hasta %s (última guardada %s).",
                 feed["nombre"], limite.date(), ultima.date())
    return limite


def procesar_fuentes(cliente, diagnostico: bool) -> tuple[list[dict[str, Any]], int]:
    """
    Recorre las fuentes y, salvo en diagnóstico, GUARDA AL TERMINAR CADA UNA.

    Guardar por fuente y no al final evita perder todo el trabajo si la
    ejecución se corta a mitad: lo recolectado del primer feed ya está a
    salvo en Supabase cuando empieza el segundo.
    """
    sesion = crear_sesion_http()
    ya_vistas: set[str] = set()
    acumuladas: list[dict[str, Any]] = []
    fuentes_fallidas = 0

    for feed in construir_lista_fuentes():
        nombre = feed["nombre"]
        try:
            limite = calcular_limite_temporal(cliente, feed)
            encontradas = recorrer_feed(sesion, feed, limite)

            # Si la misma licitación aparece en dos feeds, gana la primera.
            unicas = [x for x in encontradas if x["id_licitacion"] not in ya_vistas]
            ya_vistas.update(x["id_licitacion"] for x in unicas)

            if not unicas:
                logging.info("[%s] Sin coincidencias nuevas en esta ejecución.", nombre)

            if diagnostico:
                acumuladas.extend(unicas)
                continue

            nuevas = filtrar_ya_procesadas(cliente, unicas)
            if nuevas:
                guardar_licitaciones(cliente, nuevas)
            acumuladas.extend(nuevas)

        except Exception as error:
            fuentes_fallidas += 1
            logging.error("[%s] Fuente descartada por error inesperado: %s",
                          nombre, error)

    return acumuladas, fuentes_fallidas


# ==============================================================
# 6. PASO 2 · PERSISTENCIA Y CONTROL DE ESTADO EN SUPABASE
#
#    Supabase es el ÚNICO destino de los datos. No se escribe
#    ningún fichero en el repositorio.
# ==============================================================

def obtener_cliente_supabase():
    """
    Crea el cliente de Supabase a partir de las variables de entorno.

    Si faltan credenciales se aborta con un mensaje explícito: es preferible
    un fallo ruidoso a un robot que aparenta funcionar y no guarda nada.
    """
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "").strip()
    clave = os.environ.get("SUPABASE_KEY", "").strip()

    # Saneado defensivo de la URL. El cliente de Supabase añade por su
    # cuenta el sufijo /rest/v1, así que la URL debe ser la raíz limpia
    # del proyecto. Si llega con una barra final o con el sufijo ya
    # incluido (es fácil copiar del panel la dirección equivocada), la
    # ruta resultante queda duplicada y el servidor responde PGRST125,
    # "Invalid path specified in request URL".
    url = url.rstrip("/")
    for sufijo in ("/rest/v1", "/rest"):
        if url.endswith(sufijo):
            logging.warning(
                "SUPABASE_URL incluía el sufijo '%s'. Se ignora: debe ser la "
                "URL raíz del proyecto (https://xxxxx.supabase.co).", sufijo
            )
            url = url[: -len(sufijo)].rstrip("/")

    if not url or not clave:
        logging.error(
            "Faltan credenciales. Revisa que los secrets SUPABASE_URL y SUPABASE_KEY "
            "estén creados en Settings > Secrets and variables > Actions."
        )
        sys.exit(1)

    try:
        cliente = create_client(url, clave)
        # Prueba de conexión temprana: mejor fallar aquí que a mitad de proceso.
        cliente.table(TABLA_SUPABASE).select("id_licitacion").limit(1).execute()
        return cliente
    except Exception as error:
        logging.error("No se pudo conectar con Supabase (o la tabla '%s' no existe): %s",
                      TABLA_SUPABASE, error)
        sys.exit(1)


def fecha_ultima_guardada(cliente, fuente: str) -> datetime | None:
    """
    Fecha de publicación más reciente que ya está en Supabase para esa fuente.

    Es el marcador que hace adaptativa la ventana. Ante cualquier problema
    devuelve None, y el llamante retrocede el máximo: fallar hacia el lado
    de mirar de más nunca produce alertas duplicadas, porque el control de
    estado las filtra después.
    """
    try:
        respuesta = (
            cliente.table(TABLA_SUPABASE)
            .select("fecha_actualizacion")
            .eq("fuente", fuente)
            .not_.is_("fecha_actualizacion", "null")
            .order("fecha_actualizacion", desc=True)
            .limit(1)
            .execute()
        )
        filas = respuesta.data or []
        if not filas:
            return None
        return a_fecha(filas[0].get("fecha_actualizacion"))
    except Exception as error:
        logging.warning("No se pudo leer el marcador temporal de '%s': %s", fuente, error)
        return None


def filtrar_ya_procesadas(cliente, candidatas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Consulta la memoria y devuelve solo las licitaciones nunca vistas.

    Se pregunta por lotes: una consulta con miles de identificadores en una
    sola petición superaría el límite de longitud de la URL.
    """
    if not candidatas:
        return []

    identificadores = [c["id_licitacion"] for c in candidatas]
    ya_conocidas: set[str] = set()

    for lote in dividir_en_lotes(identificadores, TAMANO_LOTE_SUPABASE):
        try:
            respuesta = (
                cliente.table(TABLA_SUPABASE)
                .select("id_licitacion")
                .in_("id_licitacion", list(lote))
                .execute()
            )
            for fila in (respuesta.data or []):
                ya_conocidas.add(fila["id_licitacion"])
        except Exception as error:
            # Ante la duda, no reportamos: mejor omitir una alerta hoy que
            # inundar de duplicados. El lote se reintentará mañana.
            logging.error("Consulta a Supabase fallida, lote omitido: %s", error)
            ya_conocidas.update(lote)

    nuevas = [c for c in candidatas if c["id_licitacion"] not in ya_conocidas]
    logging.info("Control de estado: %d candidatas, %d ya conocidas, %d nuevas.",
                 len(candidatas), len(candidatas) - len(nuevas), len(nuevas))
    return nuevas


def guardar_licitaciones(cliente, nuevas: list[dict[str, Any]]) -> int:
    """
    Inserta la FILA COMPLETA de cada licitación nueva.

    `upsert` con `ignore_duplicates`: si dos ejecuciones se solapan, la
    segunda no revienta con un error de clave primaria.

    Las filas nacen con estado_pipeline='pendiente_analisis'. Esa columna
    convierte la tabla en la cola de trabajo de los pasos 3, 4 y 5.
    """
    if not nuevas:
        return 0

    filas = [
        {
            "id_licitacion": item["id_licitacion"],
            "fuente": item["fuente"],
            "origen": item["origen"],
            "expediente": item["expediente"] or None,
            "titulo": item["titulo"],
            "organo": item["organo"],
            "enlace": item["enlace"] or None,
            "codigo_postal": item["codigo_postal"],
            "presupuesto": item["presupuesto"],
            "cpvs": item["cpvs"],
            "estado_licitacion": item["estado_licitacion"] or None,
            # Se normaliza a ISO: el feed catalán puede traerla en formato
            # RSS ("Mon, 17 Aug 2026 08:00:00 +0200"), que PostgreSQL no
            # interpreta, y sin fecha el marcador adaptativo no funciona.
            "fecha_actualizacion": (
                fecha_iso.isoformat()
                if (fecha_iso := a_fecha(item["fecha_actualizacion"])) else None
            ),
            "fecha_publicacion": item.get("fecha_publicacion"),
            "fecha_limite": item.get("fecha_limite"),
            "estado_pipeline": "pendiente_analisis",
        }
        for item in nuevas
    ]

    guardadas = 0
    for lote in dividir_en_lotes(filas, TAMANO_LOTE_SUPABASE):
        try:
            cliente.table(TABLA_SUPABASE).upsert(
                list(lote),
                on_conflict="id_licitacion",
                ignore_duplicates=True,
            ).execute()
            guardadas += len(lote)
        except Exception as error:
            logging.error("Inserción fallida en un lote de %d filas: %s", len(lote), error)

    logging.info("Guardadas %d licitaciones en Supabase.", guardadas)
    return guardadas


# ==============================================================
# 7. INFORME DE LA EJECUCIÓN
#    Sustituye al antiguo nuevas_licitaciones.json: el resumen
#    legible se publica en la propia pantalla de GitHub Actions.
# ==============================================================

def formatear_importe(valor: float | None) -> str:
    """Formatea un importe en notación española (1.234.567,89 EUR)."""
    if valor is None:
        return "no informado"
    return f"{valor:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".") + " EUR"


def informar_cobertura(licitaciones: list[dict[str, Any]]) -> None:
    """
    Mide qué porcentaje de las licitaciones trae cada dato clave.

    Sirve para decidir con números, y no por intuición, si un campo es
    utilizable como filtro de producto. Un campo presente en el 30 % de
    los casos no sostiene una funcionalidad de cara al cliente.
    """
    total = len(licitaciones)
    if not total:
        logging.info("Sin licitaciones que medir.")
        return

    logging.info("--- COBERTURA DE DATOS (sobre %d licitaciones) ---", total)
    medidas = (
        ("Código postal", sum(1 for x in licitaciones if x["codigo_postal"])),
        ("Presupuesto", sum(1 for x in licitaciones if x["presupuesto"] is not None)),
        ("Órgano", sum(1 for x in licitaciones if not x["organo"].startswith("("))),
        ("Expediente", sum(1 for x in licitaciones if x["expediente"])),
        ("Fecha límite", sum(1 for x in licitaciones if x.get("fecha_limite"))),
        ("Fecha publicación real", sum(1 for x in licitaciones
                                       if x.get("fecha_publicacion"))),
    )
    for etiqueta, presentes in medidas:
        logging.info("  %-16s %4d/%-4d (%5.1f %%)",
                     etiqueta, presentes, total, 100 * presentes / total)

    # Reparto del volumen entre los prefijos CPV configurados: responde a
    # "¿cuánto de mi ruido viene de cada familia?" sin salir del registro.
    logging.info("--- VOLUMEN POR PREFIJO CPV ---")
    for prefijo in CPV_PREFIJOS:
        n = sum(1 for x in licitaciones
                if any(c.startswith(prefijo) for c in x["cpvs"]))
        logging.info("  %-10s %4d  (%5.1f %%)", prefijo, n, 100 * n / total)

    # Documentos referenciados: decide si el paso 3 necesita navegar HTML.
    con_documentos = sum(1 for x in licitaciones if x.get("documentos"))
    logging.info("--- DOCUMENTOS REFERENCIADOS EN EL FEED ---")
    logging.info("  Licitaciones con al menos un documento: %d/%d (%.1f %%)",
                 con_documentos, total, 100 * con_documentos / total)
    if con_documentos:
        todos = [d for x in licitaciones for d in x.get("documentos", [])]

        logging.info("  Media de documentos por licitación: %.1f",
                     len(todos) / con_documentos)

        logging.info("  Por tipo:")
        for tipo, n in Counter(d["tipo"] for d in todos).most_common(10):
            logging.info("    %-28s %4d", tipo, n)

        # Lo decisivo para el paso 4b: qué formatos habrá que saber leer.
        logging.info("  Por formato de fichero:")
        for ext, n in Counter(d["extension"] for d in todos).most_common(10):
            logging.info("    %-28s %4d  (%5.1f %%)", ext, n, 100 * n / len(todos))

        con_hash = sum(1 for d in todos if d["hash"])
        logging.info("  Con huella de contenido: %d/%d (%.1f %%)",
                     con_hash, len(todos), 100 * con_hash / len(todos))

        for x in licitaciones:
            if x.get("documentos"):
                logging.info("  EJEMPLO · %s", x["titulo"][:70])
                for d in x["documentos"][:4]:
                    logging.info("    [%s] %s", d["tipo"], d["nombre"][:70])
                break

    # ¿Cuánto del pliego vive ya, estructurado, dentro del feed?
    logging.info("--- CONDICIONES ESTRUCTURADAS EN EL FEED ---")
    con_criterios = [x for x in licitaciones if x.get("criterios_adjudicacion")]
    logging.info("  Con criterios de adjudicación: %d/%d (%.1f %%)",
                 len(con_criterios), total, 100 * len(con_criterios) / total)
    if con_criterios:
        n_crit = sum(len(x["criterios_adjudicacion"]) for x in con_criterios)
        con_peso = sum(1 for x in con_criterios
                       for c in x["criterios_adjudicacion"] if c["peso"] is not None)
        logging.info("    Media de criterios: %.1f | con ponderación: %d/%d (%.1f %%)",
                     n_crit / len(con_criterios), con_peso, n_crit,
                     100 * con_peso / n_crit)
        suman_cien = sum(
            1 for x in con_criterios
            if abs(sum(c["peso"] or 0 for c in x["criterios_adjudicacion"]) - 100) < 0.5
        )
        logging.info("    Ponderaciones que suman 100: %d/%d (%.1f %%)",
                     suman_cien, len(con_criterios), 100 * suman_cien / len(con_criterios))

    con_solvencia = [x for x in licitaciones if x.get("solvencia")]
    logging.info("  Con requisitos de solvencia: %d/%d (%.1f %%)",
                 len(con_solvencia), total, 100 * len(con_solvencia) / total)
    if con_solvencia:
        campos = [s for x in con_solvencia for s in x["solvencia"]]
        remisiones = sum(1 for s in campos if s["es_remision"])
        logging.info("    LA CIFRA CLAVE · campos que solo remiten al pliego: "
                     "%d/%d (%.1f %%)", remisiones, len(campos),
                     100 * remisiones / len(campos))
        for s in campos:
            if not s["es_remision"]:
                logging.info("    Ejemplo CON contenido: %s", s["descripcion"][:100])
                break
        for s in campos:
            if s["es_remision"]:
                logging.info("    Ejemplo de REMISIÓN:    %s", s["descripcion"][:100])
                break

    logging.info("  Con garantía definitiva: %d/%d | Con email de contacto: %d/%d",
                 sum(1 for x in licitaciones if x.get("garantia_pct") is not None), total,
                 sum(1 for x in licitaciones if x.get("email_contacto")), total)

    # ¿Cuántas de las "nuevas" son reediciones de anuncios antiguos?
    desfases = []
    for x in licitaciones:
        pub = a_fecha(x.get("fecha_publicacion"))
        act = a_fecha(x.get("fecha_actualizacion"))
        if pub and act:
            desfases.append((act - pub).days)
    if desfases:
        desfases.sort()
        reediciones = sum(1 for d in desfases if d > 7)
        logging.info("--- ANTIGÜEDAD REAL DE LO DETECTADO ---")
        logging.info("  Días entre publicación y última actualización: "
                     "mediana %d | máximo %d",
                     desfases[len(desfases) // 2], desfases[-1])
        logging.info("  Publicadas hace más de 7 días (reediciones): %d/%d (%.1f %%)",
                     reediciones, len(desfases), 100 * reediciones / len(desfases))

    provincias = Counter(
        x["codigo_postal"][:2] for x in licitaciones if x["codigo_postal"]
    )
    if provincias:
        reparto = ", ".join(f"{p}: {n}" for p, n in provincias.most_common(12))
        logging.info("--- PROVINCIAS MÁS FRECUENTES (2 primeros dígitos del CP) ---")
        logging.info("  %s", reparto)


def mostrar_resumen(licitaciones: list[dict[str, Any]]) -> None:
    """Imprime un resumen legible en el registro de la ejecución."""
    if not licitaciones:
        logging.info("Sin novedades en esta ejecución.")
        return

    logging.info("--- NUEVAS LICITACIONES DETECTADAS ---")
    for item in licitaciones:
        logging.info("  · [%s] %s", item["origen"], item["titulo"][:100])
        limite = item.get("fecha_limite")
        logging.info("    Órgano: %s | CP: %s | Presupuesto: %s | Límite: %s",
                     item["organo"][:60],
                     item["codigo_postal"] or "s/d",
                     formatear_importe(item["presupuesto"]),
                     limite[:16].replace("T", " ") if limite else "s/d")
        logging.info("    CPV: %s", ", ".join(item["cpvs"][:5]))


def publicar_informe_actions(licitaciones: list[dict[str, Any]]) -> None:
    """
    Escribe una tabla en el resumen visual del workflow y expone los
    contadores para pasos posteriores. No genera ningún fichero en el
    repositorio: GITHUB_STEP_SUMMARY vive fuera del árbol de trabajo.
    """
    ruta_salida = os.environ.get("GITHUB_OUTPUT")
    if ruta_salida:
        try:
            with open(ruta_salida, "a", encoding="utf-8") as fichero:
                fichero.write(f"hay_nuevas={'true' if licitaciones else 'false'}\n")
                fichero.write(f"total_nuevas={len(licitaciones)}\n")
        except OSError as error:
            logging.warning("No se pudo escribir la salida de Actions: %s", error)

    ruta_resumen = os.environ.get("GITHUB_STEP_SUMMARY")
    if not ruta_resumen:
        return

    try:
        with open(ruta_resumen, "a", encoding="utf-8") as fichero:
            fichero.write(f"## Licitaciones nuevas: {len(licitaciones)}\n\n")
            if not licitaciones:
                fichero.write("_Sin novedades. Los datos históricos están en Supabase._\n")
                return
            fichero.write("| Origen | CP | Título | Órgano | Presupuesto | CPV |\n")
            fichero.write("|---|---|---|---|---|---|\n")
            for item in licitaciones[:50]:
                titulo = item["titulo"][:90].replace("|", "/")
                organo = item["organo"][:50].replace("|", "/")
                enlace = item["enlace"]
                celda_titulo = f"[{titulo}]({enlace})" if enlace else titulo
                fichero.write(
                    f"| {item['origen']} | {item['codigo_postal'] or '—'} | "
                    f"{celda_titulo} | {organo} | "
                    f"{formatear_importe(item['presupuesto'])} | "
                    f"{', '.join(item['cpvs'][:3])} |\n"
                )
            if len(licitaciones) > 50:
                fichero.write(f"\n_...y {len(licitaciones) - 50} más. Consulta Supabase._\n")
    except OSError as error:
        logging.warning("No se pudo escribir el informe de Actions: %s", error)


# ==============================================================
# 8. ORQUESTACIÓN
# ==============================================================

def main() -> int:
    argumentos = argparse.ArgumentParser(
        description="State Scraper v2 · Pasos 1 y 2 (lectura de feeds y control de estado)."
    )
    argumentos.add_argument(
        "--diagnostico",
        action="store_true",
        help="Prueba los feeds y el filtro CPV sin conectar con Supabase ni guardar nada.",
    )
    opciones = argumentos.parse_args()

    configurar_logging()
    logging.info("=" * 62)
    logging.info("STATE SCRAPER v2 · Pasos 1 y 2")
    logging.info("Filtro CPV: %s", ", ".join(CPV_PREFIJOS))
    logging.info("Ventana adaptativa | Tope: %d días | Solape: %d días | "
                 "Freno: %d páginas",
                 DIAS_ANTIGUEDAD_MAX, DIAS_MARGEN_SOLAPE, MAX_PAGINAS_POR_FEED)
    logging.info("Filtro geográfico en canal agregado: %s",
                 "solo Catalunya" if SOLO_CATALUNYA_AGREGADO else "desactivado")
    logging.info("=" * 62)

    # La conexión se abre ANTES de descargar nada: el marcador temporal de
    # Supabase es lo que decide cuánto hay que retroceder en cada feed.
    cliente = None if opciones.diagnostico else obtener_cliente_supabase()

    resultados, fuentes_fallidas = procesar_fuentes(cliente, opciones.diagnostico)

    if fuentes_fallidas and fuentes_fallidas >= len(FEEDS):
        logging.error("Todas las fuentes han fallado. Revisa las URLs de los feeds.")
        return 1

    if opciones.diagnostico:
        logging.info("MODO DIAGNÓSTICO: %d licitaciones coincidirían con el filtro.",
                     len(resultados))
        informar_cobertura(resultados)
        mostrar_resumen(resultados[:15])
        logging.info("No se ha consultado ni modificado Supabase.")
        return 0

    mostrar_resumen(resultados)
    publicar_informe_actions(resultados)
    logging.info("Ejecución completada: %d licitaciones nuevas guardadas.",
                 len(resultados))
    return 0


if __name__ == "__main__":
    sys.exit(main())
