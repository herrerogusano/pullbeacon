# FASE 02 — Cliente GitHub, lectura de PRs y mapa exacto del diff

**Objetivo:** obtener un snapshot consistente de una PR y mapear con precisión archivos y líneas comentables. **Depende de:** 01.

## Implementar

1. `GitHubClient` basado en `httpx` e inyección de transporte: autenticación por `GITHUB_TOKEN`, cabeceras correctas/API version verificada, timeouts, paginación, reintentos acotados para 429/5xx con `Retry-After`, mensajes seguros, error de rate limit y control de permisos.
2. Métodos: `get_pull_request`, `list_pull_files` paginado, `get_file_content_at_sha` solo para paths permitidos y con límite de bytes, `get_collaborator_permission`, `list_reviews`, `get_review`, `list_review_comments`, `create_review` (puede activarse después) y los métodos mínimos de comentarios. Documentar scopes de tokens.
3. `PullRequestSnapshot` inmutable anclado al **head SHA real**, no a `GITHUB_SHA` del workflow (en `issue_comment` corresponde a la default branch). Guardar owner/base_repo/head_repo, número, head ref y SHA, base SHA, estado PR, autor, draft.
4. Obtener `changed files` de REST; paginar y abortar de forma segura si el número excede límites. La API puede cortar a 3000 archivos: reportar truncamiento, nunca decir que se revisó toda la PR.
5. Parser de diff unificado de los campos `patch`: mapear cada línea candidata (`file path`, `new line`, `old line`, `side` RIGHT/LEFT, `hunk`), incluidas líneas de contexto. No confundir offset en patch con línea real del archivo. Detectar renames, additions, deletions, CRLF, `\ No newline at end of file`, múltiples hunks, archivos vacíos, parches ausentes y nombres de ruta poco habituales.
6. Filtro de ámbito: `policy.include/exclude`, paths de control (secretos, `.env`, keyfiles), archivos generados o binarios, límites en archivos, líneas y contexto. No confiar en el nombre de archivo de la PR como input ejecutable. Evitar descargas desde URLs arbitrarias: construir endpoints del API de GitHub autorizado.
7. Generar **representación acotada de diff** para LLM y mapa de líneas para validar findings; calcular fingerprints estables a partir de ruta+hunk+regla+contenido relevante, no del orden de salida de modelo.
8. Contexto opcional: obtener solo ficheros complementarios **a partir de una allowlist del repositorio base**, limitados por bytes; NO acceso arbitrario del modelo a otros repositorios, URLs o filesystem del runner.

## Tests obligatorios

- PR con 2 páginas de archivos; paginación con `Link` y límites; archivos >3000: error/partial explícito.
- Un patch con 2 hunks, rename y líneas de ambos sides.
- Binary/no-patch/large-file: skip con reason, no hallazgo inline ficticio.
- `get_file_content_at_sha` verifica que el SHA usado es exacto y longitud bajo el límite.
- `GET collaborator permission`: write/admin permitidos; read/none, 403/404 denegados.
- Snapshot consistente aunque el evento tenga `GITHUB_SHA` de default branch.
- Inputs con cadenas que parecen shell o instrucciones de prompt son datos inertes.

## Demo local

`uv run python -m pullbeacon review --fixture tests/fixtures/pr_two_hunks.json --dry-run` debe imprimir JSON con archivos/ubicaciones (sin llamadas reales), e indicar los skips con razones.

**DONE:** `GitHubClient` completamente simulable, snapshots por SHA, líneas del diff precisas y tests de paginación, permisos y casos raros. Actualizar `PROGRESS.md` y crear commit.
