# Registro de decisiones

Por qué el sistema es como es. Cada entrada recoge el contexto, la decisión
y lo que costó — incluidas las que hubo que rectificar.

---

## 1. Leer feeds oficiales, no hacer scraping de HTML

**Contexto.** Hay dos formas de obtener licitaciones públicas: raspar las
páginas web de los portales o consumir los canales de sindicación que las
administraciones publican por obligación legal.

**Decisión.** Feeds ATOM oficiales.

**Motivo.** El scraping de HTML es frágil (se rompe con cada rediseño),
lento, propenso a bloqueos por detección de bots y jurídicamente
incómodo. Los canales de sindicación existen precisamente para el consumo
automatizado, son estables y su uso es inequívoco.

---

## 2. lxml con `local-name()`, no BeautifulSoup ni namespaces declarados

**Contexto.** Los feeds usan CODICE, un dialecto XML basado en UBL con
espacios de nombres largos y versionados (`cac:`, `cbc:`,
`cac-place-ext:`).

**Decisión.** lxml, y localizar las etiquetas por *nombre local* mediante
XPath, ignorando el namespace por completo.

**Motivo.** Declarar un mapa de namespaces obliga a actualizarlo cada vez
que Hacienda publica una versión nueva del esquema; hasta entonces el
parser devuelve cero resultados sin dar error. BeautifulSoup, por su
parte, no maneja bien la sensibilidad a mayúsculas del XML, y
`ItemClassificationCode` es *case-sensitive*.

**Consecuencia.** El parser sobrevive a cambios de versión del esquema.

---

## 3. Cataluña vía canal agregado, con filtro de procedencia

**Contexto.** La plataforma catalana no expone un canal ATOM público
documentado, pero sindica de forma bidireccional con el Estado.

**Decisión.** Consumir el canal de plataformas agregadas y filtrar por
procedencia catalana (dominio del enlace, nombre del órgano).

**Motivo.** Es más robusto que depender de un endpoint no documentado que
cambia con cada rediseño del portal.

**Consecuencia no evidente.** Ese canal transporta **todas** las
comunidades autónomas. Sin el filtro geográfico, "Estado + Cataluña" se
habría convertido en "España entera, dos veces".

---

## 4. Un extractor por fuente

**Contexto.** Tentación de escribir una única función de extracción para
todo.

**Decisión.** `extraer_placsp()` y `extraer_catalunya()` separadas, con un
registro que asigna el extractor según el tipo de feed.

**Motivo.** Las diferencias son reales, no cosméticas: el canal agregado
llega a menudo con el bloque estructurado incompleto y hay que leer del
texto libre; la plataforma catalana publica también en RSS plano, con
`<item>` en lugar de `<entry>`; y solo una de las dos necesita filtro
geográfico.

**Verificación.** Con una entrada de Girona sin importe en el XML, el
extractor genérico devuelve `null` y el catalán rescata 88.000,50 € del
resumen. Añadir una plataforma nueva es escribir una función y una línea
en el registro.

---

## 5. Supabase como fuente única, cero ficheros en el repositorio

**Contexto.** La primera versión escribía un JSON y lo subía al
repositorio con un commit automático.

**Decisión.** Todo a base de datos. Ningún dato en Git.

**Motivo.** Un repositorio que acumula commits de datos se vuelve
ilegible, y el histórico de código queda enterrado bajo ruido.

**Efecto colateral positivo.** El robot dejó de necesitar permiso de
escritura sobre el repositorio: menos superficie de riesgo.

**Contrapartida asumida.** Se pierde la trazabilidad temporal que daba
gratis el `git log`. Para un sistema que vigila contratación pública puede
llegar a importar acreditar cuándo se tuvo conocimiento de algo; la
solución sería una tabla de eventos de solo inserción.

---

## 6. El sector es configuración, no código

**Contexto.** El nicho de cultura es un banco de pruebas; el objetivo es
que el sistema sirva para cualquier sector.

**Decisión.** Los prefijos CPV se leen de una variable de entorno.

**Motivo.** Cambiar de cultura a sanidad o a obra pública debe ser editar
una línea de configuración, no tocar Python. El día que haya usuarios con
intereses distintos, esa misma configuración pasa a ser una columna de la
tabla de suscripciones sin reescribir nada.

---

## 7. Prioridad del lugar de ejecución sobre la dirección del órgano

**Contexto.** Los feeds pueden traer dos códigos postales: dónde se presta
el servicio y dónde tiene su sede quien contrata.

**Decisión.** Buscar primero en el bloque de lugar de ejecución; recurrir
a la dirección del órgano solo si el primero no existe.

**Motivo.** En cultura casi siempre coinciden —un ayuntamiento contrata un
espectáculo para su propio municipio—, pero al extrapolar dejan de
hacerlo: un ministerio con sede en Madrid puede licitar una obra en
Almería. La prioridad correcta hoy no cambia nada y mañana evita un fallo
silencioso.

**Detalles que aparecen en datos reales.** Algunos órganos exportan el
código postal como número y pierden el cero inicial: `8017` es en realidad
`08017`. Y hay que validar que los dos primeros dígitos correspondan a una
provincia existente, o acaban en la columna años y referencias de
expediente.

---

## 8. Ventana adaptativa, no fija

**Contexto.** La primera versión miraba "los últimos 7 días" con un tope
de 3 páginas por feed.

**El problema.** Medido: cubría **2 días y 6 horas**, no 7. El tope de
páginas cortaba el recorrido antes de que la ventana temporal llegara a
actuar. El sistema creía vigilar una semana y vigilaba dos días, sin
avisar de nada.

**Primer intento, fallido.** Subir el tope a 10 páginas. Los números lo
desmintieron: PLACSP publica unas 2.400 entradas diarias, así que 14 días
exigirían unas 70 páginas y un cuarto de hora de descarga cada mañana,
redescargando cada día lo ya procesado.

**Decisión final.** El script pregunta a la base de datos cuál es la
publicación más reciente que ya guardó de cada fuente y retrocede solo
hasta ahí, con dos días de solape. Día normal: pocas páginas. Tras una
caída de tres días: retrocede cinco, solo. Tope de 14 días como freno.

**Consecuencia.** Cada feed encuentra su propia profundidad sin
configuración manual, lo cual importa porque las fuentes tienen volúmenes
que difieren en un factor de cinco.

---

## 9. Guardar al terminar cada fuente

**Contexto.** El script descargaba todo y escribía en base de datos al
final.

**El problema.** Una ejecución cortada a mitad —por tiempo agotado o por
un feed colgado— perdía el trabajo entero.

**Decisión.** Guardar al terminar cada fuente.

**Consecuencia.** La recolecta es reanudable por tramos. Ante una tarea
larga, es preferible ejecutarla dos veces con alcance reducido que una vez
con alcance completo y riesgo de perderlo todo.

---

## 10. Fallar ruidosamente

**Principio transversal.** Un scraper que devuelve cero resultados en
silencio es indistinguible de uno que funciona en un mercado tranquilo.

**Aplicación.** El script informa de la profundidad temporal alcanzada por
cada feed, avisa cuando el freno de emergencia se activa, mide la
cobertura de cada campo en modo diagnóstico y comprueba al final de cada
ejecución que no ha dejado ficheros en disco.

**Lección aprendida.** La instrumentación también miente si se mide lo que
no toca. El indicador de profundidad informaba de la entrada más antigua
*leída*, no de la más antigua *aceptada*, y llevó a interpretar que había
datos que no existían. Un indicador que engaña es peor que no tener
ninguno.

**Corrección aplicada.** Ahora se informan dos métricas separadas:

- `Ventana cubierta hasta` — la entrada más antigua que entró **dentro** de
  la ventana. Es la profundidad real de vigilancia.
- `Entrada más antigua leída` — lo que se llegó a mirar aunque se
  descartara por vieja. Sirve para saber si se alcanzó el límite pedido.

Y la alarma de hueco solo salta si **nunca** se leyó nada anterior al
límite. Sin esa condición, un feed que retrocedía de más disparaba un
falso positivo — que fue exactamente lo que ocurrió y lo que indujo el
error de interpretación.

---

## 11. El CPV no discrimina: el LLM es estructural

**Contexto.** La hipótesis inicial era que afinando bien los prefijos CPV
se obtendría un filtro suficientemente preciso.

**Lo que dijeron los datos.** En la misma muestra conviven:

| Contrato | CPV |
|---|---|
| Concierto de un artista en fiestas patronales | `92312000` |
| Corrida de toros mixta | `92312000` |
| Representación de toreros | `92312250` |
| Festejo taurino | `79952100` |

**Los toros y los conciertos comparten exactamente los mismos códigos.**
`92312250` es "servicios prestados por artistas individuales": lo usa un
cantautor y lo usa un apoderado taurino. No existe ningún prefijo, por
fino que se hile, que los separe.

**Decisión.** No estrechar los prefijos. El CPV es una red de arrastre
deliberadamente amplia; la precisión la aporta el análisis semántico del
paso 4.

**Motivo económico.** Una llamada a un modelo cuesta céntimos. Una
oportunidad no vista cuesta un cliente.

**Consecuencia inesperada.** El circuito taurino es un nicho por derecho
propio, alcanzable con los mismos datos y el mismo pipeline.

---

## 12. Cultura como banco de pruebas, no como mercado

**Decisión.** Mantener cultura y eventos como nicho de desarrollo pese a
no ser el sector más atractivo comercialmente.

**Motivo.** Es un dominio *sucio*: etiquetado inconsistente, títulos
ambiguos, mezcla de contratos grandes y pequeños. Un sector homogéneo
—limpieza de edificios, mantenimiento— haría que el pipeline pareciera
funcionar de maravilla y el susto llegaría al extrapolar.

**Principio.** Si el sistema aguanta el caso difícil, los fáciles vienen
solos.


---

## 13. Corte de paginación por mayoría, no por unanimidad

**Contexto.** El recorrido de un feed se detenía cuando una página entera
quedaba fuera de la ventana temporal.

**El problema.** El canal agregado mezcla plataformas autonómicas con
retrasos de publicación distintos, así que casi siempre se cuela algún
rezagado reciente en páginas por lo demás antiguas. Un solo rezagado
impedía el corte, y el feed descargaba 25 páginas para quedarse con 3 días
de datos.

**Decisión.** Cortar cuando una proporción configurable de la página
(`UMBRAL_CORTE_PAGINA`, por defecto 0,9) quede fuera de ventana.

**Estado: calibración pendiente.** Con 0,9 el canal estatal corta
limpiamente —llegó a una página con 500 de 500 entradas fuera—, pero el
agregado sigue agotando páginas: su desorden está repartido de forma
homogénea, no concentrado al final, así que bajar el umbral probablemente
no bastaría.

**Alternativa anotada.** Cambiar el criterio: en lugar de mirar qué
proporción de la página está fuera de ventana, mirar **la entrada más
reciente de la página**. Si ni la más nueva entra en ventana, todas las
siguientes serán más viejas y el corte es seguro. No depende de
proporciones y resiste mejor el desorden.

**Por qué se deja así.** No hay pérdida de datos: la ventana queda
cubierta y no salta ninguna alarma legítima. El coste es de unos dos
minutos de descarga diaria en un proceso desatendido. No justifica más
tiempo con tres pasos del pipeline sin construir.

---

## 14. La puntualidad del cron no está garantizada

**Contexto.** El robot se programa con el planificador de GitHub Actions.

**Lo observado.** Las ejecuciones automáticas no se disparan de forma
fiable. Las manuales funcionan sin problema, luego la configuración,
las credenciales y la ubicación del fichero son correctas.

**Causa.** GitHub documenta que, en momentos de carga alta, los cron se
ejecutan con retraso o **se descartan sin aviso ni reintento**. Además,
cada modificación de la programación reinicia el registro en el
planificador, lo que invalida las pruebas hechas a los pocos minutos de
editar.

**Mitigaciones aplicadas.** Programar en un minuto no redondo, para
esquivar la avalancha de tareas en punto.

**Consecuencia para el producto.** Si la propuesta de valor incluye
"alerta diaria a una hora fija", el planificador de GitHub no la sostiene.
No es cuestión de configurarlo mejor. Alternativas para cuando llegue el
momento: `pg_cron` en Supabase disparando el workflow por API, o un
servicio de cron externo.

**Por qué no urge.** La ventana adaptativa hace que una ejecución perdida
se recupere sola en la siguiente. El sistema tolera la impuntualidad; lo
que no toleraría es prometerla.

---

## 18. El feed trae plazo y URLs de documentos: el paso 3 deja de ser crítico

**Contexto.** El diseño asumía que la fecha límite de presentación y el
acceso a los pliegos exigían descargar y analizar documentos. El paso 3
era el más incierto del pipeline, estimado entre 3 y 8 horas.

**Lo que dijeron los datos.** Medido en modo diagnóstico sobre licitaciones
reales:

| Dato | Cobertura |
|---|---|
| Fecha límite de presentación | 95,0 % |
| Al menos un documento referenciado | 90,5 % |
| Documentos por licitación | 3,6 de media |

Y las URLs de descarga **funcionan en ventana de incógnito**: son
autocontenidas, sin identificador de sesión ni token temporal.

**Consecuencias.**

1. **Se puede emitir una alerta completa sin descargar nada.** Título,
   órgano, presupuesto, CPV, código postal, enlace y plazo bastan para que
   un profesional decida si le interesa. El MVP se cierra con
   `1 → 2 → 4a → 5`.
2. **El paso 3 pasa de cuello de botella a trámite.** No hay que navegar
   HTML ni sortear detección de bots: la URL viene en el feed y se
   descarga directamente. Queda como mejora, no como requisito.
3. **Confirma la Decisión 17.** Con 3,6 documentos de media por
   licitación, la tabla `documentos` es obligatoria.

**Detalles del esquema CODICE.** Cada tipo de documento cuelga de un nodo
distinto según su naturaleza jurídica: `LegalDocumentReference` (pliego de
cláusulas administrativas), `TechnicalDocumentReference` (prescripciones
técnicas) y `AdditionalDocumentReference` (anexos y cuadros). La dirección
vive dentro de `Attachment > ExternalReference > URI`.

**Detalle del plazo.** Los procedimientos restringidos y de licitación con
negociación no publican plazo de ofertas sino de solicitudes de
participación, en un nodo distinto. Buscar solo el primero habría dejado
esos expedientes como "sin fecha".

**Pendiente de confirmar.** En qué formato responde el servlet de descarga
(PDF, HTML o XML). Se resolverá registrando el tipo de contenido al
descargar, no suponiéndolo. Si sirve HTML o XML, el paso 4b se ahorra toda
la extracción de PDF.

---

## 19. El feed no da para una alerta: da para un informe

**Contexto.** Tras confirmar que el plazo venía en el feed, quedaba la duda
de cuánto del contenido del pliego estaba ya volcado en campos
estructurados. La hipótesis del arquitecto era que `TenderingTerms` vendría
"relleno pero incompleto", porque el primer expediente inspeccionado tenía
la solvencia rellena con una remisión: *"Al menos uno de los medios
indicados en el apartado 2.2 de la Cláusula 8ª del Pliego"*.

**Los datos desmintieron la hipótesis.** Medido sobre 312 licitaciones:

| Dato | Cobertura |
|---|---|
| Email de contacto del órgano | 92,6 % |
| Con criterios de adjudicación | 86,5 % |
| Criterios con ponderación numérica | **100 %** (1.227/1.227) |
| Ponderaciones que suman 100 | 85,2 % |
| Con requisitos de solvencia | 83,7 % |
| Campos de solvencia **con contenido real** | **76,8 %** |
| Garantía definitiva | 40,1 % |

El expediente que motivó la hipótesis estaba en el 23 % que remite al
pliego. Generalizar desde una muestra de uno fue el error.

**Decisión.** El paso 3 deja de ser incondicional. **La detección de
remisiones pasa de métrica a disparador:** solo se descarga el pliego
cuando el campo de solvencia dice "véase la cláusula tal". Tres de cada
cuatro licitaciones no necesitan descarga alguna.

```
4a (cribado) → 5 (alerta completa)
       └── ¿la solvencia es una remisión? ──sí──→ 3 → 4b
```

**Consecuencia de volumen.** El paso 3 pasa de procesar unas 60
licitaciones diarias a unas 14, y son aquellas en las que aporta algo.

**Hallazgo de producto no previsto.** El email del órgano de contratación
viene en el 92,6 % de los casos. Convierte la alerta en acción inmediata:
no solo "existe esta oportunidad", sino "y este es el correo de quien la
convoca". No estaba en ningún plan.

**Matiz pendiente de medir.** El 76,8 % es porcentaje de *campos*, no de
licitaciones. Una licitación con cuatro campos donde uno es remisión sigue
teniendo un hueco. Para dimensionar el disparador del paso 3 hará falta la
cifra por licitación, que será más alta.

**Aviso metodológico.** Una remisión detectada por patrón de texto no es
prueba de que falte contenido, ni su ausencia garantiza que el campo sea
útil. El detector es una heurística calibrada contra seis ejemplos, no un
clasificador validado.

---

## Deuda técnica anotada

Cosas conocidas que se decidió no hacer, y por qué.

| Asunto | Motivo de aplazamiento |
|---|---|
| Calibrar el corte de paginación del canal agregado | Cuesta ~2 min de descarga diaria. Sin pérdida de datos |
| Recuperación de histórico vía ZIP mensuales | Más útil después del paso 4, con el filtro definitivo decidido |
| Tabla de eventos de solo inserción para trazabilidad | No hay requisito legal activo todavía |
| Integrar el canal de contratos menores | Decisión de producto pendiente, no técnica |
| Cron externo para puntualidad garantizada | Esperando a confirmar si el de GitHub basta |
