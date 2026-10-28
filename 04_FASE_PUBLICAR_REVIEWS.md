# FASE 04 — Publicación de reviews con evidencia en GitHub

**Objetivo:** publicar un review útil, con hallazgos inline en líneas reales, resumen y trazabilidad. **Depende de:** 03.

## Implementar

1. Adaptador `GitHubReviewPublisher` con `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` o endpoints equivalentes verificados. Usar `comments` con `path`, `line`, `side` y commit SHA correcto; **no depender del deprecated `position`**.
2. Formato por finding: severidad, ID estable `PB-001`, categoría, por qué importa, evidencia mínima, sugerencia concreta sin asegurar corrección si es tentativa. Nunca pedir `APPROVE` ni `REQUEST_CHANGES` automáticos en MVP; publicar como `COMMENT`.
3. `ReviewReport.summary`: cambios analizados, riesgos, hallazgos agrupados por severidad, skips (con cantidad y motivo), proveedor/modelo, SHA, coste/uso (real/simulado/unknown), URL y comandos disponibles. Truncar para límites de GitHub.
4. Guardar metadatos mínimos recuperables para `apply` sin DB: en el cuerpo del review un **marcador de versión**, SHA y una representación canónica y acotada de findings validada; identificar más adelante la review por `review.id` y autor bot verificado. No confiar en comentarios anónimos que copien un marcador; comprobar autor y SHA. No guardar códigos privados íntegros en marcadores.
5. Antes de publicar, recuperar SHA actual y abortar o marcar obsoleto si cambió desde el análisis. Si los endpoints rechazaran comentarios inline (422), no hacer 'éxito' falso: degradar explícitamente el comentario fallido a resumen con `path:line` y `reason=not_posted_inline`, usando revisión sin inline o un issue comment. Documentar reglas.
6. Idempotencia: automática para el mismo `head_sha + config_hash + prompt_version + mode`, detectar y no duplicar reviews. Revisión manual `@pullbeacon review` **sí** permite revisar otra vez; evitar duplicar si GitHub reentrega el mismo `comment.id`/`delivery`.
7. Soportar `explain`: recuperar finding ID de la review más reciente del bot para el SHA actual, ofrecer explicación más detallada a través del mismo motor LLM (o respuesta determinista con evidencia si modelo no disponible), publicar como **comentario normal**, no otro review; sin edición ni side effects en código.
8. Log seguro de links, IDs, estados y errores. Reducir rate limit secundario (un único review con múltiples comentarios cuando sea posible).

## Tests obligatorios

- Create review con `line` y `side` correctos, no `position`.
- Head SHA obsoleto: no publicar hallazgos como actuales.
- GitHub responde 422 por línea inválida: fallback explícito, ningún resultado reporta inline si no lo fue.
- Dos eventos automáticos mismo SHA: una revisión; dos comandos manuales distintos: dos revisiones; delivery repetido: idempotente.
- Marker adulterado, bot author distinto, review de otro SHA: no elegibles para apply.
- API 403/429/5xx, error controlado.

**DONE:** con mocks se generan payloads GitHub reales y, en repo autorizado, los hallazgos aparecen sobre líneas correctas (si hay API/permisos). Los tests no requieren GitHub real. Actualizar PROGRESS + commit.
