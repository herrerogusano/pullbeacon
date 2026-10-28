# PullBeacon — paquete de implementación

**Objetivo:** construir desde cero una herramienta BYOK y multimodelo para revisar Pull Requests y, previa orden de un desarrollador autorizado, aplicar correcciones en la rama de una PR de GitHub. El nombre **PullBeacon** es provisional hasta comprobar marca/nombre de GitHub App. Idioma de los planes: español. Código, nombres de símbolos, comentarios en código y documentación pública del producto: inglés.

## Cómo usar este paquete

1. Crea un directorio/repositorio Git vacío para PullBeacon.
2. Copia **todos** estos `.md` a la **raíz** del repositorio; no cambies sus nombres.
3. Abre Claude Code o Codex desde esa raíz y pega **`PROMPT_PARA_EL_AGENTE.md`** como primer mensaje (o indica al agente que lo lea íntegramente).
4. El agente ejecutará las fases en orden y preparará el producto, pruebas, documentación y workflows. Tú solo debes intervenir para autorizar recursos externos, proporcionar secretos **en GitHub Secrets o un gestor seguro, nunca en el chat**, o aprobar cambios de alcance/coste.
5. No se debe considerar la fase concluida hasta que pasen sus criterios de aceptación y se haya actualizado `PROGRESS.md` (creado por el agente).

## Orden obligatorio

1. `00_PLAN_MAESTRO.md` — objetivo, alcance, arquitectura y reglas globales.
2. `01_FASE_FUNDACION.md` — proyecto, CLI, configuración, calidad y pruebas.
3. `02_FASE_GITHUB_Y_DIFF.md` — cliente GitHub, lectura y validación de cambios.
4. `03_FASE_LLM_Y_REVIEW_ENGINE.md` — LiteLLM y motor de revisión seguro.
5. `04_FASE_PUBLICAR_REVIEWS.md` — comentarios precisos en GitHub.
6. `05_FASE_GITHUB_ACTIONS_Y_COMANDOS.md` — disparadores y distribución.
7. `06_FASE_APPLY_Y_COMMITS.md` — correcciones controladas y commits.
8. `07_FASE_EVALUACION.md` — pruebas de calidad y coste por modelo.
9. `08_FASE_HARDENING_Y_RELEASE.md` — empaquetado, seguridad y release del MVP.
10. `09_FASE_GITHUB_APP_OPCIONAL.md` — extensión futura, **no bloquea el MVP**.

El archivo `REFERENCIAS_TECNICAS.md` contiene fuentes oficiales consultadas el 28-10-2026. Una referencia externa no sustituye los tests ni la verificación actual de interfaces antes de implementar.

## Qué NO incluye el MVP

SaaS de pago, dashboard, base de datos, multi-tenancy centralizado, infraestructura AWS/Cloudflare, despliegue 24/7, agentes autónomos generales, merges automáticos ni ejecución de código arbitrario de PRs. El MVP funciona mediante GitHub Actions con coste operativo de infraestructura bajo; **el consumo de APIs de LLM sí puede costar dinero**.
