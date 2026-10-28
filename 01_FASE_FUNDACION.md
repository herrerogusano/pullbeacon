# FASE 01 — Fundación, contrato y experiencia local

**Objetivo:** proyecto Python mantenible, configurable, testeable y ejecutable localmente, sin consumo de API ni permisos de GitHub. **Bloquea:** fase 02.

## Implementar

1. Auditar la carpeta actual: si hay código previo, conservarlo y adaptar. Si es nuevo, iniciar `git`, `pyproject.toml`, `uv.lock`, `.gitignore`, `.python-version`, `.env.example`, `LICENSE` MIT, `README.md` y `SECURITY.md` inicial.
2. Python 3.12 como objetivo base. Seleccionar versiones estables compatibles de `httpx`, `pydantic`, `litellm` SDK, `PyYAML` seguro, `typer` (o `argparse` estándar si basta), `pytest`, `respx`, `ruff`, `mypy`. Usar lock reproducible; explicar cualquier cambio de stack.
3. Estructura recomendada:

```
src/pullbeacon/
  __init__.py
  __main__.py
  cli.py
  config.py
  domain/models.py
  domain/errors.py
  core/diff.py
  core/review.py
  core/apply.py
  adapters/github.py
  adapters/llm.py
  orchestration/commands.py
  orchestration/events.py
  evaluation/
tests/unit/
tests/integration/
tests/fixtures/
examples/
.github/workflows/
```

   Pueden faltar módulos hasta sus fases. No crear capas vacías sin utilidad.
4. Crear `PullBeaconConfig` con esquema versionado para `.pullbeacon.yml`, valores seguros, validación de paths glob, límites positivos, estrategia para config inexistente e indicador `automatic_review`; **separar config confiable de base de datos no confiables de PR**.
5. Entorno: `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` (solo habilitados cuando se necesitan). Validar que no se imprimen ni se guardan. Un proveedor no configurado da error legible sin hacer llamadas de pago.
6. CLI inicial: `python -m pullbeacon --help`, `validate-config --file .pullbeacon.yml`, `review --fixture ... --dry-run`. Modo fixture puramente local devolverá un resultado determinista simulado hasta fase 03.
7. Modelos Pydantic iniciales definidos en plan maestro. Serialización JSON determinista, validación de severidades (`critical/high/medium/low`), líneas y SHA, `RunResult` con estados normalizados. Añadir logging estructurado sin código/secretos y manejador de errores.
8. CI propia básica (`.github/workflows/ci.yml`): lint, typecheck y tests; permisos read-only, no secrets ni llamadas API LLM. Pin de actions a versiones verificadas; al release, SHA si política exige.
9. `PROGRESS.md` con decisiones, tests ejecutados y próximas tareas; actualizado en cada fase.

## Tests obligatorios

- Config por defecto y custom, claves desconocidas (reject), YAML inválido, glob peligroso, límites extremos, provider inválido.
- Los tokens no aparecen ni en excepciones ni en logs.
- Entrypoint `--help` sin API ni red; serialización/deserialización JSON.
- Lint + typecheck + `pytest` en máquina local; si herramientas no disponibles, registrar el bloqueo exacto.

## Comandos de verificación sugeridos

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m pullbeacon --help
uv run python -m pullbeacon validate-config --file examples/.pullbeacon.yml
```

**DONE:** nuevo usuario puede clonar el repo, instalar dependencias y ejecutar CLI/tests localmente sin una API key, sin llamadas a GitHub y sin errores. Commit independiente, actualización de `PROGRESS.md`.

**No hacer:** dashboard, autenticación web, base de datos, despliegue, generación de código a partir de IA o subsistemas RAG.
