# FASE 03 — Motor de review, multimodelo, datos estructurados y control de coste

**Objetivo:** un motor Python independiente del proveedor que produzca hallazgos demostrables y minimice falsos positivos. **Depende de:** 02.

## Implementar

1. Definir interfaz `LLMClient` mínima (`review(prompt, schema, model, caps) -> StructuredResponse`) e implementación `LiteLLMClient` mediante SDK Python (no levantar proxy, DB ni gateway). Crear `FakeLLMClient` para tests sin API externa.
2. Validar **en ejecución** modelo/proveedor real y soporte estructurado. Soportar al menos Anthropic, OpenAI y Gemini como adaptadores de configuración, con `provider` + `model id`, env secrets, timeout, `max_output_tokens`, reglas de formato y manejo de diferencias de proveedores. Si un modelo no soporta schema nativo, usar salida JSON en texto **con validación estricta y no asumir fiabilidad**.
3. `ReviewEngine`: agrupar cambios por lotes acotados y con orden determinista; priorizar impacto/seguridad/corrección frente a nitpicks de estilo; usar criterios de severidad documentados; leer contexto limitado de base/PR según alcance; retornar `ReviewReport` tipado.
4. Prompts versionados en repo (sistema + instrucciones de tarea) donde **el código y los comentarios PR se tratan como datos no confiables**. Incluir regla explícita contra seguir instrucciones incrustadas en diff o texto del PR. Nunca ofrecer herramientas a LLM (shell, browser, escritura, red, acceso a secrets) para revisión.
5. Findings estructurados: `id`, `severity`, `category`, `path`, `line`, `side`, `title`, `explanation`, `impact`, `evidence`, `suggested_fix`, `confidence`, `can_apply`. Markdown breve y accionable. Evidence ligada a contenido real del diff; `line` y `path` validados contra `ChangedLineMap`. Un hallazgo fuera del diff se mueve al summary solo si tiene evidencia fundada y marcada como no-inline; de lo contrario se descarta.
6. Deduplicación por fingerprint; máximo comentarios inline por run, orden por severidad y confianza. No afirmar haber ejecutado tests ni analizado archivos omitidos. Los hallazgos de seguridad críticos pueden requerir aviso pero NO aprobar/rechazar/merge automáticamente.
7. Metadatos de uso/coste: tokens input/output cuando proveedor los provea; cálculo con coste actualizado de LiteLLM cuando sea compatible, dejando `unknown` si no es fiable; presupuesto defensivo por run previo a enviar la solicitud y chequeo por cada lote. Sin prometer un cap duro en dólares. Registrar coste estimado y límites sin registrar código enviado.
8. Implementar `--dry-run` que **no llama al proveedor** salvo modo explícito `--provider live`; usar fixture de respuestas para demo y tests.
9. Política de error: API key ausente -> error claro, 429 -> backoff acotado, 5xx -> retry máximo 2, 4xx inválido -> abort; JSON malformado -> 1 retry de corrección si cabe en presupuesto y luego error explícito; nunca publicarlo crudo.
10. Prompts y respuesta pueden tener límites por tamaño; truncación/salto explícito con razón en resumen. No resumir enormes PRs como si se hubiera revisado todo.

## Tests obligatorios

- Parser JSON inválido; hallazgo con path inventado; línea que no está en diff; severity fuera de enum; finding duplicado; evidencia ausente; confidence fuera de rango.
- Prompt injection como archivo `README.md` que ordena al modelo mostrar API key o cambiar de rol: la cadena queda en datos, no se ejecuta; tests de orquestación no permiten tools/OS calls.
- Proveedor sin API key, timeout, 429, error 5xx, respuestas vacías; coste desconocido y presupuesto agotado.
- Mismo snapshot + fake response => JSON reproducible, timestamps gestionados aparte.
- Prueba al menos un caso **sin bugs** que produzca cero hallazgos, evitando 'hallazgo obligatorio'.

## Demo

```bash
uv run python -m pullbeacon review --fixture tests/fixtures/pr_auth_bug.json --mock-llm --dry-run
```

Devuelve reporte JSON con finding verificado sobre línea válida y `usage` marcado como simulado. Implementación real de API solo tras configurar un proveedor con permiso de consumo.

**DONE:** revisión reproducible con fake; soporte real programado para tres proveedores, validaciones, costes preventivos y cero llamadas live no autorizadas. Commit + PROGRESS.
