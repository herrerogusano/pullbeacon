# FASE 07 — Benchmark de reviewers y comparación de modelos

**Objetivo:** evaluar de forma reproducible detección, falsos positivos, coste y latencia para mostrar valor técnico real de IA aplicada a engineering. **Depende de:** 03–06.

## Dataset inicial obligatorio

Crear `evaluation/cases/` con 12–20 fixtures de PRs sintéticas, pequeñas y legales de compartir, etiquetadas manualmente; todas con código original escrito para el benchmark y errores verificables, sin copiar código privado:

- Python: autorización omitida en endpoint, validación insuficiente, recurso abierto sin cerrar, N+1 en consulta, race condition sencilla.
- TypeScript/JavaScript: tipo opcional no validado, async mal esperado, input no sanitizado, comportamiento incorrecto de zona horaria.
- Tests: cambio funcional sin cobertura donde es crítico, mocks que no prueban comportamiento real.
- Cambios sin bugs: refactor correcto, cambio de nombres, documentación inocua, test mejorado (sirven para falsos positivos).
- Falso contexto/inyección: comentario dentro de PR que pide al reviewer saltarse normas.

Cada caso debe tener `case_id`, diff fixture, `ground_truth` de bugs (categoría, path/líneas, rationale), expected non-findings, tamaño/idioma/criticidad. Crear también tests para correcciones aplicadas con expected changes, no solo detección.

## Implementar

1. `eval` CLI: `uv run python -m pullbeacon eval --dataset evaluation/cases --mock-llm --output evaluation/results/mock.json` y `--provider live --models ...` solo con autorización de gasto.
2. Métricas: recall de issues conocidos, precision de findings (matching manual/heurístico claramente documentado), falsos positivos por PR limpia, findings sin línea válida, número de hallazgos duplicados, latencia, tokens, coste por review y coste por bug real detectado.
3. Separar `mock` vs `live`: resultados mock se etiquetan como contract/infra tests, **NO como calidad real de los modelos**. Los benchmarks de modelos reales requieren API calls autorizadas, dataset fijo, misma configuración de contextos y modelos/versiones anotados.
4. Los findings de modelos varían: no realizar matching solo por coincidencia literal de línea; usar criterios/plantilla de evaluación reproducibles y una revisión manual de desacuerdos. No auto-calificar 'correcto' con el mismo modelo revisor sin señalizar sesgo.
5. Repetibilidad: seeds cuando se soporten, temperatura controlada cuando el modelo lo permita, fecha y provider/model ID exacto, versión de prompt, commit hash de PullBeacon, hashes de fixtures y configuración, uso/cost fuente.
6. Comparador Markdown y JSON/CSV con tablas por modelo; no hace falta frontend/dashboard. Redactar 5–8 líneas sobre trade-offs calidad/latencia/coste y limitaciones estadísticas.
7. Regla de seguridad: por defecto la CI NO ejecuta benchmarking live. Cualquier job live requiere `workflow_dispatch` controlado y autorización; límites presupuestarios.

## Tests y aceptación

- Todos los casos cargan y tienen labels válidos; no hay ground truth vacío accidental en casos positivos.
- Evaluación de respuesta simulada conocida calcula métricas exactas con tests numéricos.
- PR limpia con findings simulados suma falso positivo; issue correctamente detectado suma TP; error no detectado suma FN.
- Runner sin API keys funciona y genera un reporte mock.
- No inventar resultados comparativos en `README.md`: usar `N/A` o `not run` hasta benchmark real.

**DONE:** dataset inicial, CLI de evaluación, métricas correctas y plantilla de reporte. Resultados live solo si autorizado, actualizando PROGRESS y commit.
