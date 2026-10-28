# 00 — PLAN MAESTRO: PullBeacon

**Estado:** especificación aprobada para implementación, sujeta a validación técnica previa. **Fecha:** 2026-10-28. **Idioma del producto:** inglés; planes internos en español.

## 1. Problema y propuesta de valor

Equipos que utilizan GitHub quieren analizar PRs y aplicar correcciones sin atarse a una suscripción personal o a un único modelo. PullBeacon será una herramienta **open-source, BYOK, multimodelo, basada inicialmente en GitHub Actions** que haga revisiones explicables y cambios controlados bajo orden humana.

### Casos de uso MVP

- Abrir/actualizar una PR interna → revisión automática (si se habilita).
- Comentar `@pullbeacon review` → revisión nueva, aunque el SHA no haya cambiado.
- Comentar `@pullbeacon explain PB-001` → explicación del hallazgo sin modificar código.
- Comentar `@pullbeacon apply` → aplicar **solo los hallazgos válidos y reparables de la última revisión de PullBeacon sobre el SHA actual**, creando un único commit (o respuesta explícita si no hay cambios seguros).
- Comentar `@pullbeacon apply PB-001` → aplicar únicamente el hallazgo indicado.
- Configurar proveedor/modelo, límites de contexto y política de archivos en `.pullbeacon.yml`.

El texto `@pullbeacon` se detecta con un parser en los comentarios de la PR; **no equivale** a crear una cuenta de usuario o GitHub App y el autocompletado de menciones no se garantiza. La identidad propia y las menciones nativas, si son viables, se investigarán en la fase 09.

### Diferenciación real

- Selección de modelo sin bloqueo de proveedor y credenciales propiedad del cliente.
- Validación determinista de findings antes de publicarlos: paths, líneas, SHA, diff y deduplicación.
- Aplicación segura de correcciones sin permitir comandos ni merges autónomos.
- Benchmarks reproducibles (detección vs falsos positivos, coste, latencia), no afirmaciones subjetivas de que un modelo es mejor.

## 2. Arquitectura

```
GitHub events (pull_request_target / issue_comment)
       |
       v
Trusted GitHub Action -> Event guard (permissions / same-repo / dedupe)
       |
       v
GitHub API reader -> PR metadata + patches + bounded context
       |
       v
Diff validator -> ReviewEngine -> LLM Provider abstraction (LiteLLM SDK)
       |                            |
       |                     Anthropic / OpenAI / Gemini
       v
Validated ReviewReport -> GitHub Reviews API (inline comments + summary)
       |
       +-- on authorized 'apply' --> Safe edit planner -> Git Data API
                                                       -> atomic commit on PR branch
                                                       -> explicit status/re-review option
```

**No ejecutar checkout ni código de la rama PR en workflows privilegiados**; revisar mediante GitHub REST API. Para `apply`, construir árboles/commits con la Git Data API tras validar contenido y SHA, en lugar de hacer checkout del código no confiable con secretos activos.

## 3. Stack y límites

- Python 3.12+, paquete `src/pullbeacon/`; `uv` para dependencia/lock, `pyproject.toml` PEP 621.
- `pydantic` para schemas; `httpx` cliente GitHub REST; `litellm` **SDK Python** para proveedores (NO proxy LiteLLM ni base de datos); `PyYAML` o alternativa segura para YAML.
- `pytest`, `respx`, `ruff`, `mypy` (o pyright, justificar), `coverage`.
- CLI (`python -m pullbeacon`) con `review`, `explain`, `apply`, `validate-config` y `eval`; `--dry-run` cuando aplique.
- GitHub Actions con permisos mínimos por workflow, timeout, concurrency, sin secrets para eventos de forks. Dependencias y actions fijadas por versiones/SHAs de confianza en release.
- Sin base de datos. Estado de revisión mínimo en reviews/comentarios de GitHub, SHA y marcadores de bot verificables, nunca un almacenamiento externo obligatorio.
- Licencia MIT por defecto; el agente confirma viabilidad de nombre y conflictos evidentes antes de publicar como proyecto público.

## 4. Configuración propuesta

Ejemplo válido de `.pullbeacon.yml` (el esquema exacto se congelará con tests):

```yaml
version: 1
automatic_review: true
model:
  provider: anthropic
  name: anthropic/<modelo-real-configurado>
limits:
  max_changed_files: 40
  max_changed_lines: 1500
  max_context_chars: 120000
  max_output_tokens: 4096
  max_estimated_cost_usd: 1.00
policy:
  include: ["**/*.py", "**/*.ts", "**/*.tsx", "**/*.js"]
  exclude: ["**/*.lock", "**/generated/**", "**/vendor/**"]
  apply_enabled: true
  allowed_apply_paths: ["src/**", "tests/**"]
  min_apply_confidence: 0.80
```

**Importante:** `name` de ejemplo es un PLACEHOLDER, no un ID de modelo utilizable; los ejemplos públicos deberán usar un modelo disponible, verificado por agente y **sin fijarlo en el código**. El gasto real puede diferir de lo estimado; los límites de coste son preventivos y por corrida, **no garantía de tope contable**.

La configuración de un repo PR nunca puede elevar permisos, desbloquear forks, ampliar reglas del workflow base ni revelar secrets. La configuración de seguridad/coste se lee de **rama base confiable**, no de la PR sin revisión humana.

## 5. Contratos principales

- `PullRequestSnapshot`: owner, repo, number, base_sha, head_sha, head_repo, draft, archivos, diff, timestamp.
- `ChangedFile`: path, status, previous_path?, patch?, additions, deletions, size?, changed_line_map.
- `ReviewFinding`: stable_id, severity, category, title, explanation, impact, evidence, path, line, side, fix_suggestion?, confidence, can_apply.
- `ReviewReport`: pr/head_sha/model, findings[], summary, skipped_files[], usage, estimated_cost_usd?, warnings[].
- `ApplyProposal`: original_head_sha, finding_ids[], edits[] (path, original_content_hash, new_content or strictly validated patch), explanation.
- `RunResult`: status (completed/skipped/partial/failed), reason, event_id, reviewed_head_sha, review_url?, commit_sha?, cost.

Pydantic, JSON schema explícito, validar input de LLM antes de publicar. Los findings solo pueden señalar rutas admitidas y líneas realmente presentes en el diff (o degradarse a comentario general sin atribuir línea falsa).

## 6. Modelo de amenazas y garantías

1. Inputs no confiables: diff, PR title/body, archivos, comentarios y salida del LLM. Deben tratarse **como datos**. No interpolarlos en shell; no ejecutar instrucciones del LLM.
2. `issue_comment` se origina en la rama por defecto. Guardar `event.comment.id`, `sender.login`, `issue.pull_request`, PR actual y permission real del actor. `review/explain/apply` manuales requieren `write` o superior (o política de autorizados definida en base protegida). Bloquear bots propios.
3. El MVP solamente opera sobre PRs cuyo `head.repo.full_name == base.repo.full_name`; forks y Dependabot se omiten explicando la limitación, sin consumir tokens. Esto evita prometer acceso a secrets en forks.
4. Eventos automáticos: usar `pull_request_target` **sin checkout, sin ejecutar código de PR**, cargar herramienta desde versión confiable y configuración de base. Si la organización bloquea el evento, documentar alternativa antes de cambiar el diseño.
5. El modelo no tiene herramientas de shell, red arbitraria, acceso a secrets, permisos de merge ni autorización para ampliar su scope. Implementar edición de archivos por API con límites y whitelist.
6. Antes de crear un commit: revalidar permisos del comentarista, SHA actual, rama del mismo repo y reglas de cambios. Crear commit hijo del SHA esperado y actualizar ref con `force=false`; abortar ante carrera, branch protection o respuesta inválida. Sin force push, sin merge.
7. Secretos solo en env protegidos/Actions Secrets, nunca logs. Evitar imprimir prompts/diffs/código confidencial y minimizar contenido enviado al proveedor externo.
8. Limitar gasto, reintentos (máximo 2 ante errores transitorios), ejecución por PR, input size, tamaño de comentarios, rate limits y concurrencia.
9. `GITHUB_TOKEN` usado para push normalmente no dispara workflows nuevos: el resultado de `apply` **no se declarará validado por CI** sin confirmación independiente. Una re-revisión debe orquestarse explícitamente en el mismo run, o requerir `@pullbeacon review` y quedar claro en UX. Por defecto MVP: publicar enlace y sugerir `@pullbeacon review`, sin afirmar ejecución de CI.

## 7. Fases y dependencias

| Fase | Entrega verificable | Depende de |
|---|---|---|
| 01 | Paquete y CLI local verde | — |
| 02 | GitHub reader + diff mapping | 01 |
| 03 | review engine + mock y proveedores | 02 |
| 04 | reviews publicadas con líneas verificadas | 03 |
| 05 | workflows y comandos en PRs | 04 |
| 06 | apply autorizado, commit sin checkout | 05 |
| 07 | evaluación y comparación reproducible | 03–06 |
| 08 | Action instalable, documentación y release candidate | 01–07 |
| 09 opcional | GitHub App independiente | decisión posterior |

## 8. Criterios globales de DONE

- Tests y chequeos locales verdes; cero claves expuestas; revisión y `apply` simulados con fixtures y API mock.
- En repo de prueba autorizado: review automático y manual, hallazgo inline en línea válida, explicación, `apply` con commit, denegación sin permisos, rechazo de PR fork, manejo de conflicto y nueva revisión manual.
- Usuarios pueden instalar en otro repo siguiendo `README.md` sin editar el código del motor, solo config/workflow/secrets.
- Registro de uso/coste estimado donde el proveedor suministre datos; presupuestos defensivos.
- Documentación clara de limitaciones, permisos, coste, privacidad y no CI automático tras apply.

## 9. Intervención humana permitida

Crear/autenticar repositorios, GitHub Secrets, API keys, habilitar Actions, probar facturación con límite bajo, decidir exposición pública e instalar GitHub App más adelante. **No detener trabajo local por estas autorizaciones**.
