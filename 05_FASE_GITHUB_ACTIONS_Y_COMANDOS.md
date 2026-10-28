# FASE 05 — Workflows, comandos @pullbeacon y uso desde otros repositorios

**Objetivo:** review automático y review/explain a través de comentarios, funcionando en GitHub Actions con API keys de empresa. **Depende de:** 04.

## Diseño decidido

- Soportar eventos `pull_request_target` (`opened`, `synchronize`, `reopened`, opcional `ready_for_review`) y `issue_comment` (`created`). El primero se utiliza **solo como orquestación desde default branch confiable**, SIN checkout/build/test/carga/ejecución de código de PR. `issue_comment` lee la PR explícitamente por REST, no usa `github.sha` como head.
- En el MVP: **solo PRs que se originan en el mismo repo**, no forks/Dependabot. Evita usar secrets/privilegios en aportes externos. Si otra política GitHub invalida `pull_request_target`, documentar restricción y buscar diseño equivalente sin degradar seguridad.
- Comandos en comentarios generales de la PR (no review comments para MVP): `@pullbeacon review`, `@pullbeacon explain PB-001`, `@pullbeacon apply` / `@pullbeacon apply PB-001` (apply se activa en Fase 06). Parser robusto con límites, case-insensitive si se documenta, comando reconocido de forma explícita no por coincidencia parcial (ignorar código citado, bloques fenced y menciones dentro de links si se puede analizar con seguridad).
- No afirmar autocompletado @. La mención puede ser texto plano interpretado por PullBeacon.

## Implementar

1. `EventRouter` que lee `GITHUB_EVENT_PATH`/JSON con Pydantic; valida `action`, PR number, repository identity y `sender`. Ignorar otros eventos, comments editados, comentarios en issues y bots propios.
2. `AuthorizationService`: antes de gastar en LLM, pedir a GitHub el permiso efectivo del actor y permitir solo `write`/`admin`/`maintain` (esquema compatible con roles personalizados mapeados a write cuando API lo confirme). No confiar únicamente en `author_association`; denegar siempre ante fallo de verificación. Para auto: política de repositorio en default branch y branch same-repo, allowlist de autores si aplica.
3. Guardar `comment.id`/delivery + SHA para idempotencia de comandos; usar marker/rest API y `concurrency` de Actions por PR. Revalidar SHA tras entrar en la sección crítica para `apply`.
4. Crear el workflow consumidor de ejemplo `examples/pullbeacon.yml` con `pull_request_target` + `issue_comment` + `workflow_dispatch` (opcional), `if` temprano y `permissions` restrictivos. `contents: read` para review y `pull-requests: write` para publicar; operaciones `apply` con `contents: write` en workflow separado/escenario de permisos mínimo. Si se usa workflow unificado, documentar y probar explícitamente el permiso adicional y garantizar autorización antes de cualquier escritura. Prefiero **separar review y apply en diferentes jobs/workflows** si simplifica separación de privilegios.
5. Construir `action.yml` como acción composite (o Docker action solo si hace falta) para instalar/ejecutar el paquete del repositorio **referenciado por versión confiable**; no hacer `checkout` del PR para instalar la herramienta. La instalación de dependencias debe ser reproducible y no interpretar inputs como shell. Pin de referencia de action recomendada por SHA en README.
6. Inputs: `config-file`, `automatic-review`, `dry-run`/`mock-llm`, `model` opcional bajo límites de config base. Las API keys se inyectan vía `secrets` por proveedor elegido y nunca se exponen en argumentos/logs. No utilizar el OAuth de una suscripción personal para un producto empresarial.
7. Timeouts y concurrency per PR; serializar publicación/apply, tolerar evento simultáneo synchronize/comment; no crear un loop por comentarios propios.
8. Mensajes amigables en GitHub para errores de configuración, permiso, fork no soportado, PR enorme, modelo caído y presupuesto agotado; evitar spam (rate limit y sin secret leakage).
9. Integración local con fixtures de payloads para `pull_request_target`, `issue_comment`, `workflow_dispatch`, actores sin permiso, forks, bots y evento duplicado. No requieren GitHub remoto.
10. Añadir guía de instalación desde otro repo usando versión/tag/commit real después de publicar. En el repo propio, workflow de dogfooding lee Action y configuración desde default branch, no del head PR.

## Tests obligatorios

- Comando exacto, aliases permitidos, texto citado/regex ambiguo, `@pullbeacon` sin verbo → ayuda sin llamada LLM.
- `issue_comment` de issue normal → skip. PR fork → skip sin secretos/LLM. Dependabot → skip en MVP.
- Usuarios `read`, `none`, no autenticados → denegados antes de API LLM; `write` → autorizados.
- Comentario enviado por bot o por el propio bot → ignorado; eventos repetidos no duplican coste.
- `pull_request_target` no hace checkout, `npm install`, `pip install` desde la PR, `pytest` de código PR, `eval`, ni ejecuta shell con PR payload.
- `workflow_dispatch` opcional: solo actores autorizados.
- Contrato de Action: help, validation y dry-run; secretos no salen al log.

## Validación externa condicionada a autorización

Crear/usar repo de prueba `pullbeacon-playground` (nombre disponible a confirmar), activar Actions y añadir **una** API key como secreto con control de coste. Abrir PR interna que incluya bug conocido; comprobar review en la PR; comentar `@pullbeacon review`; comprobar segunda revisión y denegación a actor no autorizado. Si no hay acceso remoto, dejar un checklist ejecutable e indicar `NOT_VERIFIED_REMOTELY`.

**DONE:** test local CI en verde y workflows válidos; smoke test remoto si el usuario lo habilita. Actualizar PROGRESS, commit.
