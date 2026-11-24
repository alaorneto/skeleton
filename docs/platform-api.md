# API de gestão da instância

Cada instância exporta, por uma API somente leitura, a identidade, as apps, um resumo por organização, as faturas e os pagamentos de uma organização e séries diárias de métricas. Uma **instância central**, do mesmo dono, cadastra conexões com as demais e uma app dela as lê pela API de serviços. É o [epic #707](https://github.com/alaorneto/skeleton/issues/707): identidade ([#708](https://github.com/alaorneto/skeleton/issues/708)), clientes e tokens de plataforma ([#709](https://github.com/alaorneto/skeleton/issues/709)), resumo e métricas ([#710](https://github.com/alaorneto/skeleton/issues/710)), rotas ([#711](https://github.com/alaorneto/skeleton/issues/711)) e conexões remotas ([#712](https://github.com/alaorneto/skeleton/issues/712)).

## Identidade da instância

Cada base tem uma única linha `InstanceIdentity` (tabela global), criada depois do `migrate` e nunca excluída, com um `instance_id` (UUID gerado uma vez e imutável no modelo e no guard do banco), um nome e uma finalidade. O administrador da plataforma edita nome e finalidade na seção "Identidade" de `/platform/instance/`, com CAS. O identificador não sai pelo anônimo `GET /api/v1/server/`; só pela API de plataforma.

## Clientes e tokens de plataforma

Em `/platform/api-clients/`, o administrador da plataforma (MFA, autenticação recente) cria **clientes da API da plataforma**: identidades `AccountType.PLATFORM_CLIENT` sem organização, sem senha, sem papel na plataforma e que nunca fazem login (como as contas de serviço, `is_service_account` vale para as duas). Para cada cliente, emite tokens `kind="platform"` com:

| Escopo | Abre |
|---|---|
| `platform.instance:read` | `instance/` |
| `platform.organizations:read` | `organizations/` e `organizations/<id>/` |
| `platform.billing:read` | `billing` nos itens de organização, `invoices/` e `payments/` |
| `platform.billing_contacts:read` | `billing-account/` (dado pessoal) |
| `platform.metrics:read` | `metrics/` |

Os escopos `platform.*` são registrados pelo núcleo e só valem em tokens `platform` (os outros tipos os recusam, e um token `platform` não aceita outro escopo). Cada token tem validade (teto `SKELETON_API_TOKEN_MAX_DAYS`) e, opcionalmente, até 20 redes de origem em CIDR; o texto aparece uma vez, com `Cache-Control: no-store`. Revogar ou desativar o cliente vale na requisição seguinte (401).

O `ApiTokenMiddleware` confere, para um token `platform`: a view precisa ser de plataforma (`platform_scope`), senão 403 `token_not_allowed`; o endereço do cliente (pela topologia de proxies configurada) precisa estar numa das redes, senão 401 `invalid_token` com o evento `api_token.rejected` (`reason_code=network`); e o escopo da view precisa estar no token, senão 403 `insufficient_scope`. Um token pessoal, de serviço ou de feed numa view de plataforma recebe 403 `token_not_allowed`. Os limites de taxa dos tokens valem iguais.

## Rotas

Todas em `/api/v1/platform/`, só `GET`, `Cache-Control: no-store`, com o evento `platform_api.read` (prefixo do token e nome da rota, nunca os parâmetros). Uma sessão do administrador da plataforma também as lê, para depuração; qualquer outra credencial recebe o 404 neutro.

| Rota | Resposta |
|---|---|
| `instance/` | `instance_id`, `name`, `purpose`, `product_name`, `core_version`, `billing_enabled`, `apps` (`app_label`, `app_id`, `version`, `kind`, `availability_mode`, `limits`; `kind` é `app` ou `library`, e uma biblioteca first-party, #792, vem com `availability_mode: null` e `limits` vazio), `generated_at` |
| `organizations/` | `{"results", "next_cursor", "generated_at"}`. Filtros `updated_since` (ISO 8601 com fuso), `is_active`, `access_state`, `has_overdue`, `cursor`, `page_size` (1 a 200, padrão 100); outro parâmetro é 400 `invalid_filter`. Ordem estável `(updated_at, sysid)`; o cursor é opaco e assinado (um cursor alterado é 400 `invalid_cursor`) |
| `organizations/<id>/` | Um item: `id`, `name`, `slug`, `is_active`, `created_at`, `members` (`active`, `limit`, `external`, `service_accounts`), `apps`, `bundle`, `storage` (`used_bytes`, `quota_bytes`, `location`), `billing` (só com `platform.billing:read`), `refreshed_at`, `updated_at` |
| `organizations/<id>/invoices/` | Faturas (número, situação, moeda, subtotal, desconto, imposto, total, pago, período, emissão, vencimento, pagamento), filtros `status` e `issued_since`, páginas `page`/`page_size` |
| `organizations/<id>/payments/` | Pagamentos (método, situação, valor, moeda, provedor, criado, confirmado); nunca a referência do provedor nem dados do meio de pagamento |
| `organizations/<id>/billing-account/` | Razão social, email de cobrança, documento, país, moeda e estado; cada leitura gera `platform_api.personal_data` com o prefixo do token e o sysid da organização, sem os valores |
| `metrics/` | `{"results": [{"day", "code", "currency", "value"}]}`, filtros `since`, `until` (datas; até 400 dias), `code` (repetível; `billing.*` casa um prefixo), `currency` |

Para ler faturas, pagamentos e o contato de uma organização, a view abre `system_context(reason="platform:api", organization_id=...)` somente leitura. A razão está em `PLATFORM_CLIENT_REASONS` e só é admitida quando a requisição roda com um token que tem `platform.billing:read` ou `platform.billing_contacts:read`, nunca por sessão comum.

**Sincronização da central.** A primeira chamada pagina `organizations/` pelo cursor; as seguintes passam `updated_since` igual ao maior `updated_at` recebido e trazem só as organizações que mudaram.

O [OpenAPI](api.md#schema-openapi) lista as rotas de plataforma, com a tag "Plataforma" e o escopo exigido na descrição, só para quem as alcança (a sessão do administrador da plataforma ou um token `platform` com o escopo).

## Resumo e métricas (`skeleton/insights`)

O pacote `skeleton/insights` (label `sk_insights`) mantém duas tabelas operacionais globais, escritas só pelos jobs dele, sem auditoria por linha e fora do gateway, do OpenAPI e da exportação CSV:

- `OrganizationSnapshot`, uma linha por organização: nome, identificador, situação, criação, membros (pessoas), limite de membros, pessoas externas, contas de serviço, apps em uso, armazenamento (uso, cota, código do local), o resumo da cobrança (situação de acesso, assinatura, moeda, plano, MRR, em aberto, vencido, vencimento mais antigo, último pagamento, fim do trial e do período), os totais do dia (faturado, pago, estornado por moeda) e as métricas de app. `updated_at` só muda quando algum valor muda: é o cursor da API.
- `InstanceMetric`, valor por dia (UTC), código e moeda: `organizations.total`, `organizations.active`, `organizations.created`, `members.total`, `apps.<label>.organizations`, `storage.bytes`, e com a cobrança ligada `billing.mrr`, `billing.invoiced`, `billing.paid`, `billing.refunded`, `billing.open`, `billing.overdue`, `billing.subscriptions.<situação>` e `billing.access.<estado>`, por moeda.

Os agendamentos e os consumidores só existem com `SKELETON_INSIGHTS_ENABLED` (desligado por padrão, lido na partida): uma instância que não reporta a uma central não agenda nada. Sem ele, o resumo fica vazio e `organizations/` e `metrics/` não trazem nada.

| Job | Quando | O que faz |
|---|---|---|
| `insights.refresh_organization` | De hora em hora (periódico por organização) e depois de `membership.*`, `app.*` e dos eventos de cobrança (no máximo uma vez a cada 5 minutos por organização) | Lê a organização no contexto de sistema dela e regrava a linha só se algo mudou |
| `insights.metrics` | De hora em hora (global) | Reescreve as métricas do dia corrente a partir dos resumos |
| `insights.daily` | 00:20 UTC (global) | Fecha o dia anterior e apaga as métricas mais antigas que `SKELETON_INSIGHTS_RETENTION_DAYS` (padrão 800, mínimo 400) |

O MRR soma os itens vivos ativos normalizados por mês (`unit_amount × quantity / interval_count` no mensal e `/ (12 × interval_count)` no anual, arredondado). Os números de cobrança vêm de `skeleton/billing/reporting.py`; com a cobrança desligada, o resumo de cobrança fica vazio e não há métrica `billing.*`.

Uma app soma métricas próprias com `skeleton.service_api.insights.register_instance_metric("<app_label>", coletor)` no `ready()`: o coletor recebe o id de cada organização, roda no contexto dela e devolve `{"<nome>": inteiro}`, somado como `<app_label>.<nome>`. Só contagens, nunca dado pessoal.

A página `/platform/insights/` (administrador da plataforma e papéis com `inventory.read`) mostra os totais, o MRR, o em aberto e o vencido por moeda, as organizações com valores vencidos e as apps por número de organizações.

## Instância central: conexões remotas

Em `/platform/remote-instances/`, o administrador da plataforma da central cadastra uma **conexão** por instância remota (`RemoteInstanceConnection`, tabela global): o endereço base (`https://host[:porta]`, sem caminho nem credencial), o token de plataforma emitido na remota (cifrado com `REMOTE_INSTANCE_ENCRYPTION_KEYS`, nunca exibido; vazio numa edição mantém o gravado) e a única organização liberada para lê-la. "Testar" chama `instance/` da remota e fixa o `instance_id` no primeiro sucesso; a partir daí, uma resposta com outro identificador falha com `remote_instance_mismatch`.

O cliente HTTP passa por `skeleton.common.outbound_http` (só `https`, sem seguir redirecionamento, respostas até 1 MiB) e transforma respostas 401, 403, 404, 5xx ou corpo inválido em `remote_unauthorized`, `remote_forbidden`, `remote_not_found`, `remote_unavailable` e `remote_invalid_response`, sem eco do corpo.

Uma app da organização liberada lê as remotas por `RemoteInstanceService` ([API de serviços](service-api.md#catálogo-do-núcleo)): `connections`, `instance`, `organizations` (paginado pelo cursor da remota), `organization`, `invoices`, `payments` e `metrics`, com DTOs imutáveis que nunca trazem o token. Como cada método faz uma chamada de rede, ele só roda em jobs (`actor=None` no contexto de sistema da organização liberada); numa requisição HTTP responde `Unavailable("remote_call_in_request")`. Uma conexão de outra organização ou inativa é `NotFound`. O kit `skeleton.testing.remote_instances.fake_remote(...)` substitui o transporte por uma remota falsa, sem rede.
