# FASE 06 — @pullbeacon apply: correcciones reales con garantías de seguridad

**Objetivo:** al recibir una orden **explícita de una persona autorizada**, aplicar cambios acotados en la misma rama de la PR mediante GitHub Git Data API y generar un commit. **Depende de:** 05.

## Alcance exacto

- `@pullbeacon apply`: tomar los findings `can_apply=true` de la **última review de PullBeacon que corresponda al SHA actual**, solo si pasan validaciones y caben en límites; si hay dudas, no editar y mostrar por qué.
- `@pullbeacon apply PB-001`: limitar a ese hallazgo de la review vigente.
- Nada de merges, pushes forzados, PRs de forks, cambios en rutas no autorizadas, cambios a secrets, CI ejecutado con permisos privilegiados, comandos shell solicitados por el modelo ni ejecución autónoma de instrucciones de PR.
- Un único commit por comando. El body del commit menciona `PullBeacon`, PR, IDs de findings y el SHA de origen; no incluye secretos ni texto de código privado.

## Diseño de seguridad/implementación

1. Antes de siquiera llamar al LLM: evento `issue_comment`, no issue normal, comando exacto, actor `write/admin`, PR abierta, mismo repo, branch permitida, repo/paths permisibles en config **de default branch**, head SHA reciente, validación de idempotencia por `comment.id`.
2. Localizar review más reciente publicada **por identidad de bot verificada** con marker versionado y mismo head SHA. Leer solo los IDs canónicos y descripciones de findings validados, **sin confiar en un marker de tercero**. Si no existe review válida, responder `Run @pullbeacon review first`.
3. `ApplyPlanner` hace propuesta tipada, únicamente para archivos actuales y rutas permitidas. Obtener contenido **a SHA exacto** por GitHub REST; hashear contenido original (`sha256`) y verificar. No permitir creación/borrado de archivos en MVP; symlinks/submodules/binarios y archivos grandes se excluyen.
4. Aceptar ediciones solo de `path` en PR actual y `policy.allowed_apply_paths`, con topes de tamaño, de líneas modificadas y número de archivos. Comparar `new_content` vs `original_content` con diff determinista; exigir que los hunks de edición estén localizados en áreas de findings (con ventana de contexto configurable y pequeña, p. ej. 10 líneas); rechazar reformateo masivo o cambios desproporcionados. No atribuir verificación semántica a un simple hash.
5. Rechazar placeholders, cambios que borran todo el archivo sin razón, modificaciones sospechosas de CI/credenciales/seguridad; cualquier ambigüedad → skip/abort, nunca aplicar parcialmente sin declararlo. Evitar ediciones destructivas, CRLF inesperado o cambios de encoding. Validar que no hay conflictos entre findings sobre misma región.
6. Para commit **sin checkout del código PR**: leer `GET git/commits/{head_sha}`/tree de origen, `POST git/trees` con `base_tree` y entradas de contenido editado conservando modos de archivos ordinarios, `POST git/commits` con padre `head_sha`, actualizar `refs/heads/<branch>` con `force: false`. **Verificar HEAD justo antes y después**; si cambió, abortar, nunca force. No permitir ramas protegidas ni nombres de refs controlados como shell.
7. Distinguir `plan` de `commit`: un `--dry-run` muestra solo archivos/estadísticas/motivos de skip, sin contenido sensible ni coste simulado como real; un flag de ejecución real requiere `apply_enabled`, usuario autorizado y evento válido. Crear indicador `apply_dry_run` para pruebas en GitHub si conviene.
8. `GITHUB_TOKEN` usado para crear commit no dispara normalmente nuevos workflows (GitHub docs). **No afirmar que CI o review se relanzaron**. Tras el commit, mostrar sha/link y cómo hacer `@pullbeacon review` o bien ejecutar **re-revisión explícita dentro del mismo job** si se implementa y valida con presupuesto restante. Predeterminado MVP: orden manual posterior para simplicidad y control de gasto.
9. Publicar comentario resultado: IDs abordados, archivos modificados, commit SHA, skipped con motivos, coste aproximado, pruebas NO ejecutadas. En fallos 409/422/403 no publicar 'aplicado' y no atribuir commit si no existe.
10. Desacoplar autor del commit de la disponibilidad futura de GitHub App. No asumir que `github-actions[bot]` ofrece menciones de autocompletado `@pullbeacon`.

## Pruebas obligatorias

- Sin permisos `write` → ninguna llamada LLM, ningún write API.
- Misma PR pero SHA movido entre propuesta y commit → abort, ref no mutado.
- Fork → abort. Rama protegida/API 403 → informar sin mutar. Bot comment spoofed → no trust.
- Hallazgo de review vieja, id desconocido, duplicate comment event, hash de contenido no coincide, archivo fuera del diff, path traversal, symlink, cambios desproporcionados → abort.
- Árbol Git con dos archivos editados y uno sin cambios preserva otros archivos y modos. `force=false` verificado con fake transport.
- Escribir commit con una edición sencilla, luego leer rama y comprobar contenido exacto en sandbox API de tests. Si hay autorización, smoke test en PR real + desactivar repo de prueba después.
- Nunca se crea merge ni se ejecuta el código de la PR; verificado con tests de llamadas/adaptadores.

## DONE

`@pullbeacon apply` y `@pullbeacon apply PB-001` pueden crear commit real (cuando estén las credenciales), pero **fallan cerrados** sin permisos, con PR cambiada o con propuesta inválida. Reportan siempre la verdad sobre validación/CI. `PROGRESS.md` y commit.
