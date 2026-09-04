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

## 15. El pipeline no va del 1 al 5: el cribado semántico se adelanta

**Contexto.** El diseño original era secuencial: descargar pliegos (3),
analizarlos con LLM (4), alertar (5).

**El problema.** La Decisión 11 estableció que el CPV no discrimina y que la
precisión la aporta el análisis semántico. Pero para separar un concierto de
una corrida de toros **no hace falta el pliego**: basta con título, órgano y
CPV, que ya están en la tabla. Descargar cientos de PDF para después
descartar cinco de cada seis es trabajo tirado.

**Decisión.** El paso 4 se parte en dos y el orden real queda:

```
1 → 2 → 4a (cribado barato) → 3 (descarga) → 4b (extracción) → 5 (alerta)
```

**Motivo.** Reduce el volumen de descarga en un factor de cinco o seis, y con
él el almacenamiento, el tiempo de ejecución y la probabilidad de toparse con
PDF escaneados que exigirían OCR.

**Consecuencia más valiosa que el ahorro.** Con 4a hecho ya hay producto
demostrable **sin tocar un solo PDF**: título, órgano, presupuesto, CPV y
enlace, filtrados semánticamente, son una alerta útil.

**Salvaguarda obligatoria.** El cribado **no puede ser binario**. Si descarta
en binario, lo descartado no vuelve a mirarse nunca y nadie se entera, lo que
rompe la asimetría de la Decisión 11: una llamada al modelo cuesta céntimos y
una oportunidad perdida cuesta un cliente. El cribado tiene tres salidas —
`sí`, `quizás`, `no` — y `quizás` también pasa al usuario.

---

## 16. Los pliegos no se archivan

**Contexto.** El robot corre en una máquina de GitHub que se destruye al
terminar cada ejecución, y la Decisión 5 prohíbe guardar datos en el
repositorio.

**Decisión.** Se descarga, se extrae el texto, se analiza y se descarta el
fichero. Se conserva el resultado del análisis y la URL.

**Motivo.** Los pliegos siguen publicados en el portal oficial, así que
archivarlos duplica algo que ya existe.

**Riesgo asumido conscientemente.** Un pliego puede modificarse o retirarse
durante la licitación. **Se asume que no cambia tras su publicación.**
Mitigación disponible y no aplicada: el feed trae un `DocumentHash` de cada
documento, así que se podría detectar una modificación sin archivar nada.

---

## 17. Un expediente tiene N documentos: hace falta una tabla aparte

**Contexto.** La tabla `licitaciones` tiene una columna `enlace`, lo que
asume un documento por licitación.

**El problema.** Es falso. Un expediente incluye como mínimo pliego de
cláusulas administrativas y pliego de prescripciones técnicas, y
habitualmente anexos y cuadro de características. Medido: **3,6 documentos de
media**.

**Decisión.** Tabla `documentos`, una fila por documento, con clave ajena a
`licitaciones`. Pendiente de construir: los pasos 3 y 4b siguen sin hacerse.

**Consecuencia sobre el estado del pipeline.** Cada documento tendrá su
propio estado de descarga, y una licitación no estará analizada hasta que lo
estén los suyos.

---

## 18. El feed trae plazo y URLs de documentos: el paso 3 deja de ser crítico

**Contexto.** El diseño asumía que la fecha límite y el acceso a los pliegos
exigían descargar y analizar documentos. El paso 3 era el más incierto del
pipeline, estimado entre 3 y 8 horas.

**Lo que dijeron los datos.** Medido en modo diagnóstico sobre licitaciones
reales:

| Dato | Cobertura |
|---|---|
| Fecha límite de presentación | 95,0 % |
| Al menos un documento referenciado | 90,5 % |
| Documentos por licitación | 3,6 de media |

Y las URLs de descarga **funcionan en ventana de incógnito**: son
autocontenidas, sin identificador de sesión ni token temporal.

**Consecuencias.** Se puede emitir una alerta completa sin descargar nada, y
el paso 3 pasa de cuello de botella a trámite.

**Detalles del esquema CODICE.** Cada tipo de documento cuelga de un nodo
distinto: `LegalDocumentReference` (cláusulas administrativas),
`TechnicalDocumentReference` (prescripciones técnicas) y
`AdditionalDocumentReference` (anexos). La dirección vive dentro de
`Attachment > ExternalReference > URI`.

**Detalle del plazo.** Los procedimientos restringidos no publican plazo de
ofertas sino de solicitudes de participación, en un nodo distinto. Buscar
solo el primero habría dejado esos expedientes como "sin fecha".

---

## 19. El feed no da para una alerta: da para un informe

**Contexto.** Quedaba la duda de cuánto del contenido del pliego estaba ya
volcado en campos estructurados. La hipótesis del arquitecto era que
`TenderingTerms` vendría "relleno pero incompleto", porque el primer
expediente inspeccionado tenía la solvencia rellena con una remisión: *"Al
menos uno de los medios indicados en el apartado 2.2 de la Cláusula 8ª del
Pliego"*.

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

El expediente que motivó la hipótesis estaba en el 23 % que remite al pliego.
Generalizar desde una muestra de uno fue el error.

**Decisión.** El paso 3 deja de ser incondicional. **La detección de
remisiones pasa de métrica a disparador:** solo se descarga el pliego cuando
el campo de solvencia remite a él. Tres de cada cuatro licitaciones no
necesitan descarga alguna.

**Hallazgo de producto no previsto.** El email del órgano viene en el 92,6 %
de los casos. Convierte la alerta en acción inmediata: no solo "existe esta
oportunidad", sino "y este es el correo de quien la convoca".

---

## 20. El estado del expediente es el filtro más rentable de todos

**Contexto.** El sistema alertaba de contratos "nuevos" que en realidad
eran de abril y se habían formalizado la víspera. La primera hipótesis
—que fuese un problema de fechas— era errónea.

**La causa.** Un expediente vive un ciclo: publicado, en evaluación,
adjudicado, formalizado. Cada cambio lo devuelve al feed. El scraper lo
veía por primera vez y lo daba por nuevo, aunque llegara ya muerto.

**Los datos.** Sobre 455 filas:

| Código | Significado | Filas |
|---|---|---|
| EV | En evaluación, plazo cerrado | 139 |
| RES | Formalizada | 131 |
| ADJ | Adjudicada | 113 |
| PUB | **Publicada, plazo abierto** | **71** |
| PRE | Anuncio previo | 1 |

**El 84 % de lo capturado eran expedientes cerrados.** Un solo filtro por
estado eliminó más ruido que todo el trabajo previo sobre ventanas
temporales y fechas.

**Verificación de `EV`.** No estaba claro si significaba "en plazo" o "en
evaluación", y la diferencia decidía si eran 139 oportunidades o 139
ruidos. Se confirmó por tres vías independientes: la definición del
código, tres expedientes abiertos a mano en el portal, y la propia tabla
—`EV` tenía cero filas con plazo abierto mientras que todas las `PUB` con
plazo lo tenían vigente.

**Decisión de diseño: lista blanca, no lista negra.** La vista filtra por
`estado = 'PUB'` en lugar de excluir los estados muertos conocidos. Si
aparece un código nuevo, queda fuera por defecto en vez de colarse. Se
validó de inmediato: `ANUL` apareció días después sin haberlo previsto.

**Se guarda todo igualmente.** Un contrato formalizado es basura como
alerta pero es inteligencia comercial: dice qué órgano tiene presupuesto
real para este tipo de servicio, cuánto paga y en qué fechas. La tabla es
el archivo; la vista es la selección.

---

## 21. Tres fechas distintas, no una

**El error.** La columna se llamaba `fecha_publicacion` pero guardaba el
`updated` del feed, que es la fecha de última **modificación**.

**Por qué importa.** No basta con saber si el plazo sigue abierto. Si un
contrato lleva tres semanas publicado y te acabas de enterar, la
competencia lleva tres semanas de ventaja para preparar su oferta. Es una
observación del Director de Proyecto que corrigió el criterio del
arquitecto, que había dado por suficiente la fecha límite.

**Decisión.** Tres columnas con tres funciones:

| Campo | Origen | Función |
|---|---|---|
| `fecha_actualizacion` | `updated` del feed | **Interna.** Ordena la paginación |
| `fecha_publicacion` | `IssueDate` de CODICE | Cuánto lleva en la calle |
| `fecha_limite` | `TenderSubmissionDeadlinePeriod` | Si aún se puede ofertar |

**Trampa evitada.** La ventana adaptativa debe seguir apoyándose en
`updated`, porque es el criterio con el que el feed ordena sus páginas.
Cambiarla a `IssueDate` habría roto toda la paginación.

**Un fallo de parseo que casi da un rodeo entero.** CODICE publica las
fechas en el formato `date` de XML Schema, que admite zona horaria sin
hora: `2026-06-09+02:00`. Es legal y correcto, y ni `dateutil` ni la
librería estándar lo interpretan. Sin el arreglo, la fecha de publicación
habría salido vacía casi siempre, se habría concluido que el feed no la
trae, y se habría montado una descarga adicional para obtener un dato que
ya estaba ahí.

**Cobertura real: 26,8 %.** El feed transporta `IssueDate` en solo uno de
cada cuatro casos. Predicción del arquitecto: más del 90 %. Segunda
estimación optimista consecutiva sobre cobertura de campos.

---

## 22. El cribado semántico: tres iteraciones medidas

**Diseño.** Tres salidas —`si`, `quizas`, `no`— y nunca dos. Un `no`
equivocado es una oportunidad perdida en silencio, y es el único error de
los tres que hace daño.

**v1.** Dejaba pasar el 70 %, contra el 33 % de la clasificación manual.
El `quizas` funcionaba como cajón de sastre. Fallos concretos: clasificó
una gala deportiva como `si` razonando que era "un evento cultural", y
dejó pasar la participación institucional en una feria educativa.

**v2.** Reencuadre del criterio y exclusiones nuevas. Evaluada contra 20
casos clasificados a mano: **16 aciertos de 20, y cero falsos negativos.**
Los cuatro fallos iban todos en la misma dirección — `quizas` donde
correspondía `no`.

**v3.** La regla que resolvió los cuatro de golpe la formuló el Director
de Proyecto: *¿podría mi cliente ser el **contratista principal** de este
contrato?* No basta con que el contrato contenga actividad cultural; hay
que preguntarse quién ejecutaría la mayor parte del encargo. Si es un
monitor, un educador, un docente, un guía o un proveedor de bienes, no
vale por muchas actividades culturales que incluya.

**Resultado sobre 124 licitaciones vivas:**

| Veredicto | Filas |
|---|---|
| sí | 16 |
| quizás | 28 |
| no | 80 |

Auditados 20-30 rechazos a mano: ningún falso negativo.

**El embudo completo:** 926 capturadas → 124 vivas → **44 relevantes.**

**Trazabilidad.** Cada veredicto se guarda con la versión del prompt y el
modelo. Sin eso, comparar iteraciones es imposible y no se sabe de qué
versión viene cada resultado.

**Coste.** Céntimos por cada centenar de clasificaciones con un modelo
pequeño. La asimetría económica es la que gobierna todo el diseño: una
llamada cuesta céntimos, una oportunidad perdida cuesta un cliente.

---

## 23. El fallo peligroso no es el que da error

**Tres incidentes, el mismo patrón.** En un sistema desatendido, lo que
hace daño no es el fallo ruidoso: es el que devuelve un resumen
tranquilizador.

1. **La ventana que decía siete días y cubría dos.** El tope de páginas
   cortaba antes de que la ventana temporal actuara. Sin error, sin aviso.
2. **El indicador que medía lo que no tocaba.** Informaba de la entrada
   más antigua *leída*, no de la más antigua *aceptada*. Indujo a dar por
   buenos datos que no existían.
3. **El guardado que no guardaba.** El cribador clasificó 124
   licitaciones, imprimió un reparto perfecto y terminó en verde con cero
   filas escritas.

**La causa técnica del tercero, que merece registrarse.** Se usaba
`upsert` enviando solo las columnas a modificar, suponiendo que
PostgreSQL, al encontrar la fila, actualizaría solo esas. Pero PostgreSQL
**comprueba las restricciones `NOT NULL` sobre la fila propuesta antes de
detectar el conflicto**, así que un envío sin `fuente` ni `titulo` se
rechaza aunque la fila exista. La misma función de refresco de estados
del lector arrastraba el bug y llevaba dos días sin escribir nada.

**Correcciones.** `UPDATE` explícito en lugar de `upsert` parcial, y la
ejecución **falla con error** si se clasifica y no se guarda nada.

**Cómo se encontraron los tres.** Porque un número no cuadraba con otro y
alguien preguntó. No hay sustituto para eso.

**Un corolario sobre los informes.** El resumen del cribado listaba solo
los `si` y `quizas`, con un tope de 40 filas. Los `no` —los únicos que
importa auditar— quedaban siempre fuera. **No se puede auditar lo que no
se muestra**, y el informe que oculta los rechazos es de la misma familia
que el resumen tranquilizador.

---

## 24. La interfaz lleva los datos dentro, no los consulta

**Contexto.** La demostración necesita una web que enseñe las
oportunidades filtradas.

**Alternativa descartada.** Una página que consultara Supabase desde el
navegador. Habría exigido exponer una clave en un fichero público, abrir
permisos de lectura sobre la base de datos y depender de la red en cada
carga.

**Decisión.** `generar_interfaz.py` produce un HTML único con los datos
incrustados. Sin servidor, sin credenciales en el cliente, sin conexión.

**Motivo adicional, específico del encargo.** La demostración se graba en
vídeo. Con los datos dentro no hay esperas de carga ni fallos de red a
mitad de toma.

**Consecuencia.** La web enseña la foto de la última ejecución, no el
estado en vivo. Para un producto con usuarios haría falta lo contrario;
para un MVP que se actualiza cada mañana, la diferencia es irrelevante.

**Detalle de diseño.** La provincia se calcula desde los dos primeros
dígitos del código postal en el momento de generar. Es un cálculo, no un
dato: guardarlo en la tabla sería duplicar información existente.

---

## 25. Publicar en Pages desde artefacto, no desde rama

**Contexto.** Publicar en GitHub Pages parecía obligar a incumplir la
Decisión 5 —"el repositorio contiene código y nada más"—, porque lo
habitual es servir el sitio desde una rama `gh-pages` con el HTML dentro.

**Decisión.** Usar el despliegue oficial **desde artefacto**. El HTML se
sube como artefacto de la ejecución y Pages lo sirve directamente.

**Consecuencia.** Los datos no se guardan en **ninguna** rama, ni siquiera
en una de publicación. La Decisión 5 se mantiene intacta en lugar de
matizarse. La primera propuesta del arquitecto —rama `gh-pages`— era
peor y se descartó al comprobar que existía el mecanismo oficial.

**Separación de permisos.** El despliegue va en un job aparte, con permisos
de escritura sobre Pages. El job que descarga XML de terceros y ejecuta
código sobre él conserva solo permisos de lectura. Es la misma lógica que
llevó a quitar el permiso de escritura del repositorio en la Decisión 5.

**El repositorio pasa a ser público.** Pages en el plan gratuito lo exige.
Se verificó antes de hacerlo que el historial de commits no contuviera
credenciales; las claves viven en los *secrets*, que siguen siendo
privados. Los datos publicados son de contratación pública y ya estaban
abiertos.

---

## 26. El dato real en lugar de la promesa

**Contexto.** La entradilla de la web decía "filtrados automáticamente
entre miles de anuncios".

**Objeción del Director de Proyecto.** Eso es una afirmación. Mejor un
número comprobable.

**Decisión.** La interfaz consulta cuántos anuncios se han leído y cuántos
se han valorado, y lo enseña: *"De N anuncios leídos, M se han valorado uno
a uno"*.

**Por qué importa más de lo que parece.** El embudo es el argumento
central del producto: 926 capturadas, 124 vivas, 44 relevantes. Enseñar la
cifra convierte la promesa de marketing en la demostración del trabajo.

**Léxico.** "Encaje" se sustituyó por "viabilidad", y las etiquetas del
filtro pasaron a estar escritas desde el punto de vista del usuario —"Para
mí", "Puede ser para mí"— en lugar del sistema.

---

## 27. Los plazos van en hora peninsular, no en UTC

**El fallo.** La web mostraba un plazo que vencía el 17 de septiembre a las
23:59:59 como si fuera el día 18.

**La causa.** CODICE publica a veces la hora sin indicar la zona. El
conversor asumía UTC cuando faltaba, así que unas 23:59:59 españolas se
guardaban como 23:59:59 UTC, que en España son **las 01:59 del día
siguiente**. El desfase de dos horas caía justo por encima de medianoche.

**Por qué importa más que otros fallos de este registro.** Los demás
producían ruido o trabajo perdido. Este podía hacer que alguien preparase
una oferta para un día en que el plazo ya estaba cerrado.

**Decisión.** Sin zona explícita se asume `Europe/Madrid`, que es donde
publica la Administración española y que gestiona sola el cambio de hora.
Y la interfaz **fuerza la hora peninsular al mostrar**, en lugar de usar la
del navegador: un visitante desde otro huso veía otra fecha.

**Añadido.** El detalle de cada oportunidad muestra ahora la hora de cierre,
no solo el día. Presentarse a las 23:59 o a las 09:00 no es lo mismo, y el
dato estaba en el expediente sin usarse.

---

## 28. La alerta solo se envía si hay algo que contar

**Decisión.** El correo diario no se envía los días sin novedades.

**Motivo.** Un correo que a veces dice "hoy no hay nada" enseña a ignorar al
remitente, y con él los días en que sí importa. El silencio también informa.

**Qué es una novedad.** Lo detectado en la última pasada del robot, no lo
detectado hoy según el calendario. Si el robot se cae un día, al volver
detecta lo acumulado y todo eso se envía: así una avería no hace perder
ninguna oportunidad. Es la misma definición que usa la pestaña de la web,
para que nadie reciba por correo algo que ya vio marcado como nuevo.

**Consecuencia asumida.** Tras una caída, el rótulo "hoy" abarca más de un
día. Es deliberado: el usuario no tiene por qué saber cuándo falló el
sistema, solo qué hay nuevo para él. Cada fila lleva su plazo, que es el
dato que decide.

**Envío.** Se manda en HTML y en texto plano a la vez. Hay clientes que no
muestran HTML, y un correo en blanco es peor que ninguno.

---

## 29. Encadenar cosas independientes las hace fallar juntas

**Qué pasó.** El envío del correo fue rechazado por el proveedor. Como el
paso estaba dentro del mismo *job* que publicaba la web, el fallo tumbó el
*job* entero y **la web no se actualizó**. El scraper había funcionado, el
cribado había funcionado y la interfaz se había generado bien.

**El coste real.** Horas viendo datos viejos en la web sin entender por qué,
y persiguiendo un fallo de fechas que ya estaba corregido.

**Decisión.** Publicar y alertar son *jobs* separados que dependen del
scraper pero no entre sí. Si el correo falla, la web se publica igual.

**Principio.** Dos resultados que pueden fallar por motivos distintos no
deben compartir destino. Encadenarlos hace que el más frágil decida por
todos.

**Nota.** El fallo se detectó porque la ejecución terminó en rojo, que es la
corrección introducida en la Decisión 23. Un envío fallido que acabara en
verde habría dejado la web desactualizada durante días sin aviso.

---

## 30. Solo se enseña lo que se puede respaldar

**El problema.** La web mostraba como abiertas licitaciones ya adjudicadas.
El estado guardado solo se actualiza cuando el expediente reaparece en el
feed, y no todos reaparecen. Y había filas sin plazo, guardadas antes de que
ese campo se extrajera.

**La pregunta que ordena la solución.** ¿Qué evidencia hay de que una
licitación sigue viva?

| Evidencia | Decisión |
|---|---|
| Plazo publicado y no vencido | Se muestra |
| Sin plazo, pero vista en el feed hace poco | Se muestra |
| Sin plazo y sin verse en 14 días | Se oculta |

**Por qué la tercera fila es lo correcto.** No saber si algo sigue abierto no
es lo mismo que suponer que sí. Una alerta que no sabe si la oportunidad
existe no es una alerta.

**Cómo se limpia.** No hay tarea de limpieza: la vista se recalcula en cada
consulta y lo obsoleto desaparece solo según pasa el tiempo.

**Resultado medido.** De 53 candidatas, 34 se muestran y **las 34 tienen
plazo verificable**. Se ocultan 19 que no podían respaldarse.

**Lo que sigue sin cubrir.** Un expediente que se adjudique y no vuelva a
aparecer en el feed conservaría su plazo futuro y seguiría mostrándose. Con
ciclos de quince días, que es lo normal en el sector, el expediente se mueve
varias veces dentro de la ventana y el refresco lo alcanza. Es una
probabilidad baja, no una imposibilidad.

---

## 31. Recortar el preámbulo jurídico de los títulos

**Contexto.** Algunos órganos, sobre todo catalanes, ponen como título el
encabezado entero del pliego: *"L'objecte del present contracte és la
prestació del servei de..."* seguido de un párrafo con fines y objetivos.

**Decisión.** Se recorta el preámbulo —idéntico en todos y sin capacidad de
distinguir un contrato de otro— y se corta por la primera frase o por
palabra entera. El título original se conserva y se muestra completo al
desplegar la oportunidad.

**Trampa encontrada al implementarlo.** El prefijo debe terminar en límite de
palabra. Sin esa comprobación, *"la prestació de"* recortaba dentro de *"la
prestació dels serveis"* y se comía una letra.

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
| Estrechar el prefijo CPV `925` a museos y patrimonio | Arrastra destrucción documental: ~15 % de llamadas desperdiciadas |
| Recortar el encabezado del pliego en títulos catalanes | Algunos títulos son la boilerplate del PCAP, sin señal para el cribado |
| Refrescar el estado de las filas antiguas | Solo se refresca lo que reaparece en la ventana adaptativa |
| Plazo ausente en lo detectado antes del 24 de agosto | Se resuelve solo según vencen esos expedientes |
| Fecha real de publicación al 26,8 % | Exigiría descargar el CallForTenders de cada expediente |
| Verificación de un expediente concreto sin esperar al feed | 3-4 h. Cierra el último hueco de estados obsoletos |
| Histórico de cambios de estado | Hoy se sobrescribe. Impide saber cuándo se adjudicó algo |
| Registro de usuarios y suscripciones | 40-60 h. Convierte la herramienta en producto |
