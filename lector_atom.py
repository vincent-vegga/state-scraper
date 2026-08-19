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
    DIAS_ANTIGUEDAD_MAX       -> ventana temporal en días (por defecto 7)
    MAX_PAGINAS_POR_FEED      -> páginas a recorrer por feed (por defecto 3)
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
# uno de estos prefijos:
#   7995xxxx -> Servicios de organización de eventos, ferias y congresos
#   923xxxxx -> Servicios de entretenimiento (espectáculos, artes escénicas)
#   925xxxxx -> Servicios de bibliotecas, archivos, museos y patrimonio
CPV_PREFIJOS: tuple[str, ...] = ("7995", "923", "925")

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
DIAS_ANTIGUEDAD_MAX = int(os.environ.get("DIAS_ANTIGUEDAD_MAX", "7"))
MAX_PAGINAS_POR_FEED = int(os.environ.get("MAX_PAGINAS_POR_FEED", "3"))
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


def a_fecha(valor: str | None) -> datetime | None:
    """Interpreta una fecha del feed y la devuelve siempre con zona horaria."""
    if not valor:
        return None
    try:
        fecha = parser_fechas.parse(valor.strip())
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
        "presupuesto": extraer_presupuesto(entrada),
        "cpvs": extraer_cpvs(entrada),
        "estado_licitacion": primer_texto(entrada, "ContractFolderStatusCode"),
        "fecha_publicacion": primer_texto(entrada, "updated", solo_hijos=True)
                             or primer_texto(entrada, "published", solo_hijos=True),
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
        "presupuesto": presupuesto,
        "cpvs": extraer_cpvs(entrada),
        "estado_licitacion": primer_texto(entrada, "ContractFolderStatusCode")
                             or resumen.get("estado", ""),
        "fecha_publicacion": primer_texto(entrada, "updated", solo_hijos=True)
                             or primer_texto(entrada, "published", solo_hijos=True)
                             or primer_texto(entrada, "pubDate", solo_hijos=True),
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


def recorrer_feed(sesion: requests.Session, feed: dict[str, str]) -> list[dict[str, Any]]:
    """
    Descarga un feed, sigue su paginación y devuelve las licitaciones que
    superan el filtro CPV dentro de la ventana temporal.

    Los feeds de PLACSP encadenan páginas hacia atrás con <link rel="next">.
    MAX_PAGINAS_POR_FEED evita descargar el histórico completo cada mañana.
    """
    nombre = feed["nombre"]
    extractor = EXTRACTORES.get(feed.get("tipo", "placsp"), extraer_placsp)
    url_actual: str | None = feed["url"]
    limite_temporal = datetime.now(timezone.utc) - timedelta(days=DIAS_ANTIGUEDAD_MAX)

    resultados: list[dict[str, Any]] = []
    total_entradas = 0

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

            if not es_reciente(datos["fecha_publicacion"], limite_temporal):
                fuera_de_ventana += 1
                continue

            if cumple_filtro_cpv(datos["cpvs"]):
                resultados.append(datos)

        # Si la página entera queda fuera de la ventana temporal, las
        # siguientes son aún más antiguas: dejamos de paginar.
        if entradas and fuera_de_ventana == len(entradas):
            logging.info("[%s] Página %d ya fuera de la ventana temporal. Fin.",
                         nombre, numero_pagina)
            break

        enlaces_siguientes = raiz.xpath("./*[local-name()='link'][@rel='next']/@href")
        url_actual = enlaces_siguientes[0] if enlaces_siguientes else None

    logging.info("[%s] %d entradas revisadas -> %d coinciden con los CPV objetivo.",
                 nombre, total_entradas, len(resultados))
    return resultados


def leer_todos_los_feeds() -> tuple[list[dict[str, Any]], int]:
    """
    Recorre todas las fuentes y devuelve las coincidencias deduplicadas,
    junto al número de fuentes que fallaron por completo.
    """
    fuentes = list(FEEDS)

    # Permite probar feeds catalanes adicionales sin editar el código.
    extras = os.environ.get("FEEDS_EXTRA_CATALUNYA", "").strip()
    if extras:
        for indice, url in enumerate(u.strip() for u in extras.split(",") if u.strip()):
            fuentes.append({
                "nombre": f"Catalunya · feed extra {indice + 1}",
                "url": url,
                "tipo": "catalunya",
            })

    sesion = crear_sesion_http()
    por_identificador: dict[str, dict[str, Any]] = {}
    feeds_fallidos = 0

    for feed in fuentes:
        try:
            encontradas = recorrer_feed(sesion, feed)
            if not encontradas:
                # Cero resultados es legítimo con un nicho estrecho.
                logging.info("[%s] Sin coincidencias en esta ejecución.", feed["nombre"])
            for licitacion in encontradas:
                # Si la misma licitación aparece en dos feeds, gana la primera.
                por_identificador.setdefault(licitacion["id_licitacion"], licitacion)
        except Exception as error:
            feeds_fallidos += 1
            logging.error("[%s] Fuente descartada por error inesperado: %s",
                          feed["nombre"], error)

    return list(por_identificador.values()), feeds_fallidos


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
            "presupuesto": item["presupuesto"],
            "cpvs": item["cpvs"],
            "estado_licitacion": item["estado_licitacion"] or None,
            "fecha_publicacion": item["fecha_publicacion"] or None,
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


def mostrar_resumen(licitaciones: list[dict[str, Any]]) -> None:
    """Imprime un resumen legible en el registro de la ejecución."""
    if not licitaciones:
        logging.info("Sin novedades en esta ejecución.")
        return

    logging.info("--- NUEVAS LICITACIONES DETECTADAS ---")
    for item in licitaciones:
        logging.info("  · [%s] %s", item["origen"], item["titulo"][:100])
        logging.info("    Órgano: %s | Presupuesto: %s | CPV: %s",
                     item["organo"][:60],
                     formatear_importe(item["presupuesto"]),
                     ", ".join(item["cpvs"][:5]))


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
            fichero.write("| Origen | Título | Órgano | Presupuesto | CPV |\n")
            fichero.write("|---|---|---|---|---|\n")
            for item in licitaciones[:50]:
                titulo = item["titulo"][:90].replace("|", "/")
                organo = item["organo"][:50].replace("|", "/")
                enlace = item["enlace"]
                celda_titulo = f"[{titulo}]({enlace})" if enlace else titulo
                fichero.write(
                    f"| {item['origen']} | {celda_titulo} | {organo} | "
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
    logging.info("Filtro CPV: %s | Ventana: %d días | Páginas por feed: %d",
                 ", ".join(CPV_PREFIJOS), DIAS_ANTIGUEDAD_MAX, MAX_PAGINAS_POR_FEED)
    logging.info("Filtro geográfico en canal agregado: %s",
                 "solo Catalunya" if SOLO_CATALUNYA_AGREGADO else "desactivado")
    logging.info("=" * 62)

    # --- PASO 1 ---
    candidatas, feeds_fallidos = leer_todos_los_feeds()

    if feeds_fallidos and feeds_fallidos >= len(FEEDS):
        logging.error("Todas las fuentes han fallado. Revisa las URLs de los feeds.")
        return 1

    if opciones.diagnostico:
        logging.info("MODO DIAGNÓSTICO: %d licitaciones coincidirían con el filtro.",
                     len(candidatas))
        mostrar_resumen(candidatas[:15])
        logging.info("No se ha consultado ni modificado Supabase.")
        return 0

    # --- PASO 2 ---
    cliente = obtener_cliente_supabase()
    nuevas = filtrar_ya_procesadas(cliente, candidatas)

    mostrar_resumen(nuevas)
    if nuevas:
        guardar_licitaciones(cliente, nuevas)

    publicar_informe_actions(nuevas)
    logging.info("Ejecución completada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
