# FASE 08 — Seguridad, usabilidad, publicación y release candidate del MVP

**Objetivo:** que otra persona pueda instalar PullBeacon en su repo sin asistencia del autor, con limitaciones honestas. **Depende de:** 01–07.

## Implementar

1. End-to-end en pipeline de test: fixture GitHub event -> auth -> snapshot -> fake LLM -> validación -> GitHub Review API -> comentario `@pullbeacon apply` -> commit por Git Data API -> respuesta. Aislar mocks para GitHub y LLM; ninguna dependencia obligatoria de internet.
2. Lint, types, coverage razonable en dominios críticos: permisos, rutas, SHA, límites, diff mapping, apply; documentar cobertura por módulo. `ruff`, `mypy`, `pytest`, `pip-audit`/`uv audit` si disponible, y herramientas de secreto (gitleaks o alternativa) en CI cuando viable. No cambiar al azar dependencias vulnerables sin verificar compatibilidad.
3. Leer permisos de cada job/workflow; confirmar tokens mínimamente necesarios, faltas de checkout PR, pinning de dependencias/third-party actions, validación de entorno, timeouts, `concurrency` por PR, tratamiento de race conditions y logs seguros.
4. README orientado a usuario real: qué hace, alternativas (Claude Code Action oficial/PR-Agent), por qué usarlo, comandos exactos, esquema de `.pullbeacon.yml`, instalación Action, secrets, permisos, repos internos vs forks, coste estimado, privacidad (código enviado al LLM del proveedor elegido), troubleshooting y demo con screenshots **solo si son reales, no inventados**.
5. `docs/architecture.md`: diagrama Mermaid, flujo de eventos, frontera de confianza, adaptadores LLM, review, apply y rechazo seguro. `docs/adr/` con ADR-001 trusted action, ADR-002 BYOK, ADR-003 commit Git Data API, ADR-004 without DB y por qué.
6. `SECURITY.md`: threat model concreto, responsable divulgación, manejo de clave comprometida, secretos, inyección de prompts, gestión de forks y protección de PRs. Documentar que **self-hosted/Actions no significa privacidad local** si se envía código a APIs de terceros.
7. `examples/.pullbeacon.yml` y `examples/pullbeacon.yml` deben ser copy-paste y estar validados contra el schema y `action.yml`; versionar la referencia de la Action y mostrar qué reemplazar por `<OWNER>/<REPO>@<COMMIT_SHA>` sin inventar owner.
8. Construir paquete Python con metadatos correctos, CLI, `action.yml` testado; dejar tag `v0.1.0` **solo tras autorización** para publicar. No prometer registro PyPI si no hace falta: instalación desde Action/version trusted es suficiente.
9. Documentar límites conocidos: coste aproximado, ausencia de dashboard, sin forks/Dependabot, menciones @ como texto, `apply` sin ejecución de CI automática al escribir con GITHUB_TOKEN, modelos compatibles según SDK, diffs grandes/patches sin línea.
10. Preparar checklist real de aceptación externa en repo sandbox autorizado: auto review, manual review, explain, apply, corrección commit, rechazo de actor sin permiso, PR grande, fork, SHA stale, error LLM, cero secretos en logs.
11. Actualizar `PROGRESS.md` con resultado exacto `DONE_LOCAL`, `DONE_REMOTE` o `BLOCKED_BY_AUTH` por fase, evidencias (comandos o enlaces reales) y pendiente de GitHub App.

## Definition of done final MVP

- `uv run ruff check .`, format check, typecheck, `uv run pytest`, CLI mock y `uv build` pasan; workflow CI pasa si hay repo remoto autorizado.
- El review no inventa líneas y el apply no toca un fork, una rama obsoleta ni archivos ajenos a scope.
- API keys en secretos y sin impresión; se conoce coste aproximado por corrida o marca desconocido si no es calculable.
- Un ingeniero ajeno puede seguir README y conectar su propio proveedor sin cambiar código.
- Todos los comandos documentados tienen tests y ninguna limitación queda oculta.
- Se entrega resumen de decisiones/arquitectura, qué está probado realmente, y guía de siguientes pasos (no promesas de trabajo futuro asíncrono).

**DONE:** release candidate del MVP y stop. **No comenzar Fase 09 sin instrucción humana.**
