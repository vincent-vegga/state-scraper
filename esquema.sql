-- ============================================================
-- STATE SCRAPER · Esquema completo de la base de datos
-- ============================================================
--
-- Fuente de verdad del esquema. Ejecutar este fichero entero en el
-- SQL Editor de Supabase reconstruye la estructura desde cero.
--
-- Es idempotente: se puede ejecutar sobre una base ya montada sin
-- borrar ni alterar los datos existentes.
--
-- Incorpora las migraciones de fechas, estado del expediente y cribado.
-- Última actualización: agosto de 2026 (pasos 1, 2 y 4a operativos)
-- ============================================================


-- ------------------------------------------------------------
-- 1. TABLA PRINCIPAL
--
-- Cumple tres funciones a la vez:
--   · MEMORIA: qué licitaciones se han visto ya, para no repetir alertas.
--   · COLA DE TRABAJO: qué falta por procesar, vía `estado_pipeline`.
--   · ARCHIVO: se guarda TODO, incluido lo ya adjudicado. El ruido de hoy
--     es inteligencia de mercado mañana (quién contrata, cuánto paga, en
--     qué fechas). El filtrado se hace al mirar, no al guardar.
-- ------------------------------------------------------------

create table if not exists public.licitaciones (
    -- Identificador ATOM de la entrada, o el enlace si aquel falta.
    id_licitacion     text primary key,

    -- Canal del que procede (nombre completo del feed).
    fuente            text not null,

    -- 'Estado' o 'Catalunya'. Permite filtrar por procedencia.
    origen            text,

    -- Referencia interna que le da el órgano de contratación.
    expediente        text,

    titulo            text not null,
    organo            text,
    enlace            text,

    -- CP de 5 dígitos. Prioridad: lugar de ejecución > dirección del órgano.
    -- Es TEXT y no numérico a propósito: como número, 08017 perdería el cero.
    codigo_postal     text,

    presupuesto       numeric,
    moneda            text default 'EUR',

    -- Todos los CPV de la licitación, no solo el que activó el filtro.
    cpvs              jsonb not null default '[]'::jsonb,

    -- Estado del expediente en CODICE. EL FILTRO MÁS IMPORTANTE:
    --   PUB  Publicada, plazo abierto -> lo único vivo
    --   EV   En evaluación, plazo ya cerrado
    --   ADJ  Adjudicada
    --   RES  Resuelta / formalizada
    --   ANUL Anulada
    --   PRE  Anuncio previo
    estado_licitacion text,
    -- Etiqueta oficial en español (atributo `name` de CODICE). El feed
    -- ATOM no siempre la trae; el documento CallForTenders sí.
    estado_nombre     text,

    -- TRES FECHAS DISTINTAS, y confundirlas fue un error real:
    --   actualizacion -> INTERNA. `updated` del feed. Ordena la paginación
    --                    y gobierna la ventana adaptativa. No mostrar.
    --   publicacion   -> `IssueDate` de CODICE. Cuánto lleva en la calle,
    --                    que mide la ventaja que lleva la competencia.
    --   limite        -> Plazo de presentación. Si aún se puede ofertar.
    fecha_actualizacion timestamptz,
    fecha_publicacion   timestamptz,
    fecha_limite        timestamptz,

    -- Gobierna el avance por el pipeline.
    estado_pipeline   text not null default 'pendiente_analisis',

    -- Cuándo la vio el scraper (distinto de cuándo se publicó).
    fecha_deteccion   timestamptz not null default now(),

    -- --- Paso 4a: cribado semántico ---
    -- 'si' | 'quizas' | 'no'. Nunca binario: un "no" equivocado es una
    -- oportunidad perdida en silencio, el único error que hace daño.
    cribado_veredicto text,
    cribado_motivo    text,
    cribado_fecha     timestamptz,
    -- Trazabilidad. Sin esto no se pueden comparar iteraciones del prompt.
    cribado_version   text,
    cribado_modelo    text
);


-- ------------------------------------------------------------
-- 2. ÍNDICES
-- ------------------------------------------------------------

create index if not exists idx_licitaciones_deteccion
    on public.licitaciones (fecha_deteccion desc);

create index if not exists idx_licitaciones_pipeline
    on public.licitaciones (estado_pipeline);

create index if not exists idx_licitaciones_origen
    on public.licitaciones (origen);

create index if not exists idx_licitaciones_codigo_postal
    on public.licitaciones (codigo_postal);

create index if not exists idx_licitaciones_estado_licitacion
    on public.licitaciones (estado_licitacion);

create index if not exists idx_licitaciones_fecha_limite
    on public.licitaciones (fecha_limite);

create index if not exists idx_licitaciones_cribado
    on public.licitaciones (cribado_veredicto);

-- Soporta el marcador de la ventana adaptativa: "última publicación
-- guardada de esta fuente".
create index if not exists idx_licitaciones_fuente_actualizacion
    on public.licitaciones (fuente, fecha_actualizacion desc);


-- ------------------------------------------------------------
-- 3. DOCUMENTACIÓN EMBEBIDA
-- Visible desde el panel de Supabase al inspeccionar la tabla.
-- ------------------------------------------------------------

comment on table public.licitaciones is
    'Fuente única de verdad del State Scraper. Sin ficheros de datos en Git.';

comment on column public.licitaciones.codigo_postal is
    'CP de 5 dígitos. Prioridad: lugar de ejecución > dirección del órgano.';
comment on column public.licitaciones.origen is
    'Comunidad o plataforma de procedencia (ej. Catalunya, Estado).';
comment on column public.licitaciones.estado_pipeline is
    'pendiente_analisis -> pdf_descargado -> analizado -> alertado';
comment on column public.licitaciones.fecha_actualizacion is
    'INTERNA: `updated` del feed. Gobierna la paginación. No mostrar.';
comment on column public.licitaciones.fecha_publicacion is
    'Fecha real de publicación (IssueDate). Cuánto lleva en la calle.';
comment on column public.licitaciones.fecha_limite is
    'Límite de presentación de ofertas o de solicitudes de participación.';
comment on column public.licitaciones.estado_nombre is
    'Etiqueta oficial del estado en español (atributo name de CODICE).';
comment on column public.licitaciones.cribado_veredicto is
    'si / quizas / no. "quizas" NO se descarta: llega igual al usuario.';
comment on column public.licitaciones.cribado_version is
    'Versión del prompt. Sin esto no se pueden comparar iteraciones.';


-- ------------------------------------------------------------
-- 4. VISTAS
--
-- Nota: CREATE OR REPLACE VIEW solo admite AÑADIR columnas al final,
-- porque compara la definición por posición. Para reordenar o insertar
-- una columna en medio hay que hacer DROP y volver a crearla.
-- ------------------------------------------------------------

-- 4.1 · Lo vivo y sin cribar: la cola de trabajo del paso 4a.
drop view if exists public.licitaciones_por_cribar;

create view public.licitaciones_por_cribar
with (security_invoker = true) as
select id_licitacion, titulo, organo, origen, codigo_postal,
       presupuesto, cpvs, enlace, fecha_limite
from public.licitaciones
where cribado_veredicto is null
  and coalesce(estado_licitacion, '') = 'PUB'
  and (fecha_limite is null or fecha_limite >= now())
order by fecha_deteccion desc;


-- 4.2 · Todo lo vivo, cribado o no.
--
-- Se usa LISTA BLANCA ('PUB') y no lista negra a propósito: si mañana
-- aparece un código de estado nuevo, queda fuera por defecto en lugar de
-- colarse. Ocurrió: ANUL apareció sin haberlo previsto.
drop view if exists public.licitaciones_pendientes;

create view public.licitaciones_pendientes
with (security_invoker = true) as
select id_licitacion, titulo, organo, origen, codigo_postal,
       presupuesto, cpvs, enlace, estado_licitacion, estado_nombre,
       fecha_publicacion, fecha_limite, fecha_actualizacion, fecha_deteccion
from public.licitaciones
where estado_pipeline = 'pendiente_analisis'
  and coalesce(estado_licitacion, '') = 'PUB'
  -- Lo vencido fuera, pero NO lo que no publica plazo: ese hueco hay que
  -- revisarlo a mano, no darlo por caducado.
  and (fecha_limite is null or fecha_limite >= now())
order by coalesce(fecha_limite, fecha_deteccion);


-- 4.3 · Lo que se le enseñaría al usuario final.
--
-- Incluye 'quizas' deliberadamente: perder una oportunidad cuesta un
-- cliente, mostrar una de más cuesta un vistazo.
drop view if exists public.oportunidades;

create view public.oportunidades
with (security_invoker = true) as
select id_licitacion, titulo, organo, origen, codigo_postal,
       presupuesto, cpvs, enlace,
       fecha_limite, fecha_publicacion,
       cribado_veredicto, cribado_motivo
from public.licitaciones
where cribado_veredicto in ('si', 'quizas')
  and coalesce(estado_licitacion, '') = 'PUB'
  and (fecha_limite is null or fecha_limite >= now())
order by cribado_veredicto, coalesce(fecha_limite, fecha_deteccion);


-- ------------------------------------------------------------
-- 5. SEGURIDAD
--
-- RLS activado y sin políticas: nada es accesible con las claves
-- públicas del proyecto. Los scripts usan la clave secreta de servidor,
-- que ignora RLS por diseño y nunca sale del almacén de secretos de
-- GitHub.
-- ------------------------------------------------------------

alter table public.licitaciones enable row level security;


-- ============================================================
-- CONSULTAS ÚTILES DE OPERACIÓN
-- (no forman parte del esquema; copiar y pegar cuando hagan falta)
-- ============================================================

-- El embudo completo, de lo capturado a lo relevante:
--
--   select
--     (select count(*) from public.licitaciones)          as capturado,
--     (select count(*) from public.licitaciones
--       where estado_licitacion = 'PUB')                  as vivo,
--     (select count(*) from public.oportunidades)         as relevante;

-- Reparto de estados del expediente:
--
--   select estado_licitacion, count(*) from public.licitaciones
--   group by 1 order by 2 desc;

-- Reparto de veredictos por versión de prompt:
--
--   select cribado_version, cribado_veredicto, count(*)
--   from public.licitaciones where cribado_veredicto is not null
--   group by 1, 2 order by 1, 2;

-- Auditar los rechazos, que es el único error que hace daño:
--
--   select titulo, cribado_motivo, enlace from public.licitaciones
--   where cribado_veredicto = 'no' order by random() limit 20;

-- Reclasificar tras cambiar el prompt: devuelve a la cola lo cribado con
-- una versión concreta.
--
--   update public.licitaciones
--   set cribado_veredicto = null, cribado_motivo = null,
--       cribado_fecha = null, cribado_version = null, cribado_modelo = null
--   where cribado_version = 'v3';

-- Cobertura de campos:
--
--   select count(*) as total,
--          count(codigo_postal)     as con_cp,
--          count(fecha_limite)      as con_plazo,
--          count(fecha_publicacion) as con_fecha_real
--   from public.licitaciones;
