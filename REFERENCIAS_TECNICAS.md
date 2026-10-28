# Referencias técnicas verificadas — 2026-10-28

Fuentes oficiales. Antes de implementar, el agente debe revalidar URLs, API contracts y campos de configuración; la documentación evoluciona.

## GitHub

- Workflow events, `issue_comment`, `pull_request_target`: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Riesgos de `pull_request_target`: https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target
- Injection en Actions: https://docs.github.com/en/actions/concepts/security/script-injections
- `GITHUB_TOKEN` y por qué sus pushes no desencadenan normalmente workflows: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
- Obtener permisos reales de un usuario: https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user
- Listar archivos de PR, paginación, límite API de 3000: https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files
- Review API (comentarios por línea y resumen): https://docs.github.com/en/rest/pulls/reviews
- Review comments, `line`/`side` preferidos frente a `position`: https://docs.github.com/en/rest/pulls/comments
- Git database API (blobs, trees, commits, refs): https://docs.github.com/en/rest/git
- Composite actions: https://docs.github.com/en/actions/tutorials/create-actions/create-a-composite-action
- Seguridad de token / permisos: https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication

## Proveedores

- LiteLLM SDK/documentación: https://docs.litellm.ai/
- LiteLLM costes y limitaciones: https://docs.litellm.ai/docs/completion/token_usage
- Claude Code Action oficial (soporta API key y alternativas): https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md

## Observaciones fundamentadas

- `issue_comment` se dispara para PRs e issues; distinguir con `issue.pull_request`, y el workflow debe estar en la rama por defecto.
- La API de comentarios de review recomienda `line` y `side` (no nueva implementación con `position` deprecado).
- El acceso real `write` puede consultarse vía repos collaborator permission endpoint; no confiar en un simple texto del comentario.
- GitHub devuelve patches que pueden faltar (binarios, archivos grandes). No inventar localización de findings.
- La API no envía claves a PRs fork de la misma manera que a ramas internas; construir explícitamente el MVP para PRs internas.
