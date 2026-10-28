# FASE 09 (OPCIONAL, NO MVP) — GitHub App empresarial e identidad propia

**Estado:** no implementar hasta que el MVP haya sido validado y el usuario confirme pasar a producto instalable tipo GitHub App.

## Motivación y alcance

La GitHub Action resuelve el MVP. La GitHub App puede ofrecer instalación en una organización, identidad de bot, permisos por instalación, webhooks y estado propio; a cambio requiere un endpoint de webhook seguro, ejecución de trabajos y gestión de claves/instalaciones. No asumir que la mención/autocompletado `@pullbeacon` sea idéntica a una cuenta de usuario; probar en un sandbox con permisos/identidad reales.

## Investigación obligatoria antes de decidir infraestructura

1. Revisar capacidades actuales de GitHub Apps: permissions, webhooks `pull_request`, `issue_comment`, reviews, instalación, tokens de corta duración, menciones y rate limits.
2. Evaluar 2 alternativas por coste/operación/seguridad: app self-hostable en AWS Lambda + API Gateway + cola (SQS/EventBridge), o Cloudflare Workers + Queues. Ninguna se despliega automáticamente. Presentar matriz breve de coste fijo, coste por evento, secrets, residencia de datos y mantenimiento.
3. Definir dónde almacenaría cada empresa sus API keys: por defecto **en infraestructura controlada por ella**, no en un servicio central sin un diseño explícito de seguridad/KMS. No guardar secretos plaintext en DB. Valorar uso de OIDC/secret managers.
4. Definir tenencia/instalaciones, scopes mínimos, verificación HMAC de webhook, protección replay, idempotencia de delivery, cola/reintentos y eliminación de datos.
5. Confirmar necesidad real de DB; de ser necesario, almacenar solamente metadatos de ejecuciones, nada de diffs en bruto a largo plazo.

## Implementación tras decisión

- Reutilizar core, `GitHubClient`, validadores y `ReviewEngine` del MVP.
- Añadir `AppWebhookAdapter` y `InstallationTokenProvider` con tests de firma, expirar token y rate limit.
- Integración e2e en una organización sandbox, revocación/desinstalación, varios repos y modelos.
- No introducir cambios incompatibles en `action.yml`; dejar Action como opción para equipos que no quieran un servicio central.

**DONE opcional:** GitHub App instalada y verificada realmente, sin elevar permisos injustificados, coste y privacidad documentados, despliegue reproducible y flujo `@` investigado con evidencias.
