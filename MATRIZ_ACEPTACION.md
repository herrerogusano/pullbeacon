# Matriz final de aceptación (MVP)

Este archivo permite al orquestador y al usuario comprobar que el proyecto **funciona**, no solo que se han creado carpetas y planes. Para cada escenario, registrar en `PROGRESS.md`: PASS / FAIL / NOT_RUN / NOT_APPLICABLE, comando/evento utilizado, evidencia y notas. Nunca marcar PASS si no hubo ejecución.

| ID | Escenario | Resultado esperado | Fase |
|---|---|---|---|
| T01 | Instalar y arrancar sin keys | CLI help/config/mock funcionan sin red | 01 |
| T02 | Config inválida o permite rutas peligrosas | Rechazo explícito con error legible | 01 |
| T03 | PR con 2 páginas de archivos y varios hunks | Diff paginado y mapeo de líneas correcto | 02 |
| T04 | Archivo binario y patch ausente | Skip visible, sin línea inventada | 02 |
| T05 | LLM devuelve JSON inválido | Retry limitado o error, no review falsificada | 03 |
| T06 | Finding sobre path o línea inexistente | Rechazo/degradación explícita | 03–04 |
| T07 | PR limpia sin bug | 0 findings y resumen honesto | 03 |
| T08 | Review automática de PR interna | Review sin duplicación para mismo SHA | 04–05 |
| T09 | `@pullbeacon review` tras review existente | Nueva revisión manual | 05 |
| T10 | `@pullbeacon explain PB-001` | Explicación en comentario, sin escritura de código | 04–05 |
| T11 | Comentario emitido en issue o por propio bot | Ignorado, sin LLM ni writes | 05 |
| T12 | Actor sin permiso de escritura | No LLM ni modificación de repo | 05–06 |
| T13 | PR desde fork/Dependabot | Omitida o error de soporte sin secrets | 05–06 |
| T14 | `@pullbeacon apply PB-001` autorizado | Commit de cambio acotado en rama PR | 06 |
| T15 | `@pullbeacon apply` en SHA obsoleto | No commit, informar conflicto | 06 |
| T16 | Propuesta de editar fuera de whitelist | Abort, sin aplicar parcialmente | 06 |
| T17 | Propuesta de IA incluye comando de shell | Tratar como dato, jamás ejecutarlo | 06 |
| T18 | GITHUB_TOKEN crea commit | No afirmar CI/review relanzados sin prueba | 06 |
| T19 | Benchmark mock con respuestas conocidas | Métricas exactas y etiquetadas mock | 07 |
| T20 | Benchmark live sin autorización | No ejecución ni gasto de API | 07 |
| T21 | Instalar Action en segundo repo | Funciona con config/secrets, sin editar el motor | 08 |
| T22 | Logs con errores de GitHub/LLM | No tokens, claves ni diff privado | 01–08 |
| T23 | Rama protegida o GitHub API 403 en apply | No commit y estado explícito | 06 |
| T24 | Repo sin GitHub remoto | Todas las fases locales quedan implementadas y anotadas | 01–08 |

## Escenario de demo para el portfolio

Con autorización para usar repo sandbox, grabar o documentar **con evidencia real**:

1. PR interna introduce un error sencillo de autorización. La Action detecta el error y comenta en línea real.
2. Se escribe `@pullbeacon explain PB-001`, se obtiene explicación sin código modificado.
3. Se escribe `@pullbeacon apply PB-001`; PullBeacon crea un commit acotado a la PR y publica resumen.
4. El desarrollador vuelve a invocar `@pullbeacon review`, comprobando si el bug sigue presente. No confiar en auto-trigger del commit.
5. Mostrar configuración para cambiar de proveedor (sin exponer API key); ejecutar benchmark solo si hubo presupuesto aprobado.
6. Demonstrar usuario no autorizado y fork rechazados.

La demo NO requiere producción, cliente, dominio, servicio backend permanente ni panel web.
