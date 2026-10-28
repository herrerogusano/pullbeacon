# Aprobaciones, credenciales y coste

## Lo que el agente puede hacer sin pedir permisos adicionales

- Crear código, docs, scripts, tests y workflows locales.
- Ejecutar mocks y validar schemas, linters, typecheck, pytest.
- Preparar `action.yml`, fixture de PR y documentación para instalarla.
- Detectar restricciones técnicas y dejar alternativas justificadas.

## Lo que requiere acción humana explícita

1. **Crear o elegir repositorio remoto en GitHub** y permitir publicación o pushes. El agente no debe crear repos públicos/privados ni cambiar visibilidad sin autorización.
2. **Proporcionar proveedor/API key** desde consola del proveedor, añadirla como `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` o `GEMINI_API_KEY` en GitHub Actions Secrets. Nunca pegar el valor en mensajes ni commitearlo. Para una primera demo basta **una** API key y un modelo; el soporte multimodelo se verifica con mocks para los otros dos.
3. **Autorizar llamadas de pago** para smoke test (presupuesto recomendado de prueba: máximo objetivo 2 USD; la cantidad es una propuesta, no un cargo ni garantía de tope duro). El agente debe proponer cuántas llamadas y estimación antes de consumir.
4. **Habilitar GitHub Actions** y/o configuración de protección de ramas del repo sandbox.
5. **Autorizar tag/release público**, y por separado cualquier futura GitHub App o despliegue en nube.

## Coste y seguridad

- Sin servicios 24/7 en MVP: no hay compromiso de coste fijo de servidor por diseño.
- GitHub Actions tiene sus propias políticas/cuotas de minutos (dependen de cuenta, repositorio y runners): no prometer gratuidad universal.
- Las APIs LLM son facturadas por uso de proveedor. Los presupuestos de PullBeacon son estimaciones por input/output y previenen llamadas, pero no sustituyen controles de facturación del proveedor.
- El código enviado a una API externa podría estar sujeto a políticas del proveedor y contratos de empresa. Antes de probar repos de trabajo reales, confirmar autorización y restricciones de privacidad.
- Si no se obtiene permiso para ningún coste, terminar el proyecto con mocks e instrucciones de validación remota. **No afirmar que se ha demostrado calidad real del modelo**.

## Primer setup manual (tras implementar)

1. En el repo sandbox: Settings → Secrets and variables → Actions → New repository secret → añadir el secreto del proveedor elegido.
2. Copiar el ejemplo `examples/.pullbeacon.yml` como `.pullbeacon.yml` en la rama default, cambiar a un ID de modelo real y accesible.
3. Copiar el workflow desde `examples/pullbeacon.yml` a `.github/workflows/pullbeacon.yml` de la rama default, referenciando versión/commit confiable real de PullBeacon.
4. Abrir PR desde una rama **del mismo repo** y observar el workflow.
5. Comentar `@pullbeacon review`, después `@pullbeacon explain PB-001` y `@pullbeacon apply PB-001` cuando exista un finding aplicable.

Si la organización no permite `pull_request_target` o el bot no puede escribir en branches, **no elevar privilegios ni usar PAT automáticamente**: documentar configuración y pedir aprobación de una alternativa segura.
