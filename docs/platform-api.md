# API de gestão da instância

Cada instância do Skeleton expõe uma API somente leitura com sua identidade, aplicativos instalados, resumo por organização, faturas, pagamentos e séries diárias de métricas. A partir dela, uma **instância central** (do mesmo operador) cadastra conexões com as instâncias remotas para que uma app local leia esses dados pela API de serviços. O recurso corresponde ao [epic #707](https://github.com/alaorneto/skeleton/issues/707): identidade ([#708](https://github.com/alaorneto/skeleton/issues/708)), clientes e tokens de plataforma ([#709](https://github.com/alaorneto/skeleton/issues/709)), resumo e métricas ([#710](https://github.com/alaorneto/skeleton/issues/710)), rotas ([#711](https://github.com/alaorneto/skeleton/issues/711)) e conexões remotas ([#712](https://github.com/alaorneto/skeleton/issues/712)).

## Identidade da instância

Cada banco possui um único registro global `InstanceIdentity`, criado após o `migrate` e nunca excluído. Ele guarda o `instance_id` (UUID gerado uma única vez e imutável no modelo e no guard do banco), um nome e uma finalidade.

O administrador da plataforma edita o nome e a finalidade na seção **Identidade** de `/platform/instance/`, com controle de concorrência (CAS). O `instance_id` não é exposto na rota anônima `GET /api/v1/server/`: ele sai apenas pela API de plataforma.

## Clientes e tokens de plataforma

Em `/platform/api-clients/`, o administrador da plataforma (com MFA e autenticação recente) cria **clientes da API da plataforma**. Eles são identidades do tipo `AccountType.PLATFORM_CLIENT`, sem vínculo com organizações, sem senha, sem papel na plataforma e sem permissão de login (assim como as contas de serviço, `is_service_account` vale para ambos).

Para cada cliente, o administrador emite tokens do tipo `kind="platform"` com os seguintes escopos:

| Escopo | Abre |
|---|---|
| `platform.instance:read` | `instance/` |
| `platform.organizations:read` | `organizations/` e `organizations/<id>/` |
| `platform.billing:read` | `billing` nos itens de organização, `invoices/` e `payments/` |
| `platform.billing_contacts:read` | `billing-account/` (dado pessoal) |
| `platform.metrics:read` | `metrics/` |

Os escopos `platform.*` são registrados pelo núcleo e valem exclusivamente em tokens `platform`: outros tipos de token os recusam, e tokens `platform` não aceitam outros escopos. Todo token tem validade obrigatória (com teto definido por `SKELETON_API_TOKEN_MAX_DAYS`) e aceita, opcionalmente, até 20 redes de origem em CIDR. O texto do token é exibido uma única vez, com `Cache-Control: no-store`. Revogar o token ou desativar o cliente surte efeito imediato na requisição seguinte (401).

Na autenticação de um token `platform`, o `ApiTokenMiddleware` aplica quatro verificações:

- a view precisa ser de plataforma (`platform_scope`); caso contrário, responde 403 `token_not_allowed`;
- o endereço de origem do cliente (resolvido pela topologia de proxies configurada) precisa pertencer a uma das redes permitidas; caso contrário, responde 401 `invalid_token` e registra `api_token.rejected` (`reason_code=network`);
- o token precisa conter o escopo exigido pela view; caso contrário, responde 403 `insufficient_scope`;
- os limites de taxa dos tokens valem da mesma forma que para os demais tipos.

Tokens pessoais, de serviço ou de feed enviados a uma view de plataforma recebem 403 `token_not_allowed`.

## Rotas

Todas as rotas vivem sob `/api/v1/platform/`, aceitam apenas `GET`, respondem com `Cache-Control: no-store` e registram o evento `platform_api.read` (com o prefixo do token e o nome da rota, nunca os parâmetros). Uma sessão do administrador da plataforma também tem acesso de leitura para depuração; qualquer outra credencial recebe 404 neutro.

| Rota | Resposta |
|---|---|
| `instance/` | `instance_id`, `name`, `purpose`, `product_name`, `core_version`, `billing_enabled`, `apps` (`app_label`, `app_id`, `version`, `kind`, `availability_mode`, `limits`; `kind` é `app` ou `library`, e bibliotecas first-party trazem `availability_mode: null` e `limits` vazio) e `generated_at` |
| `organizations/` | `{"results", "next_cursor", "generated_at"}`. Filtros aceitos: `updated_since` (ISO 8601 com fuso), `is_active`, `access_state`, `has_overdue`, `cursor` e `page_size` (1 a 200, padrão 100); qualquer outro parâmetro retorna 400 `invalid_filter`. Ordem estável `(updated_at, sysid)`; o cursor é opaco e assinado (cursor adulterado retorna 400 `invalid_cursor`) |
| `organizations/<id>/` | Um item com `id`, `name`, `slug`, `is_active`, `created_at`, `members` (`active`, `limit`, `external`, `service_accounts`), `apps`, `bundle`, `storage` (`used_bytes`, `quota_bytes`, `location`), `billing` (somente com `platform.billing:read`), `refreshed_at` e `updated_at` |
| `organizations/<id>/invoices/` | Faturas (número, situação, moeda, subtotal, desconto, imposto, total, pago, período, emissão, vencimento e pagamento), com filtros `status` e `issued_since` e paginação `page`/`page_size` |
| `organizations/<id>/payments/` | Pagamentos (método, situação, valor, moeda, provedor, criação e confirmação), nunca a referência externa do provedor nem dados do meio de pagamento |
| `organizations/<id>/billing-account/` | Razão social, email de cobrança, documento, país, moeda e estado; cada leitura gera `platform_api.personal_data` com o prefixo do token e o `sysid` da organização, sem os valores |
| `metrics/` | `{"results": [{"day", "code", "currency", "value"}]}`, com filtros `since`, `until` (datas, intervalo de até 400 dias), `code` (repetível; `billing.*` casa por prefixo) e `currency` |

Para consultar faturas, pagamentos e contato de cobrança de uma organização, a view abre `system_context(reason="platform:api", organization_id=...)` em modo somente leitura. Essa razão está em `PLATFORM_CLIENT_REASONS` e só é admitida quando a requisição usa um token com `platform.billing:read` ou `platform.billing_contacts:read` — nunca por sessão comum.

**Sincronização incremental pela central.** Na carga inicial, a instância central pagina `organizations/` usando o cursor. Nas sincronizações seguintes, envia `updated_since` com o maior `updated_at` recebido e busca apenas as organizações que mudaram.

O [OpenAPI](api.md#schema-openapi) inclui as rotas de plataforma (sob a tag "Plataforma", com o escopo exigido na descrição) apenas para quem tem acesso a elas: a sessão do administrador da plataforma ou um token `platform` com o escopo correspondente.

## Resumo e métricas (`skeleton/insights`)

O pacote `skeleton/insights` (label `sk_insights`) consolida os indicadores operacionais da instância em duas tabelas operacionais globais, escritas exclusivamente pelos seus jobs, sem auditoria por linha e fora do gateway, do OpenAPI e da exportação CSV:

- `OrganizationSnapshot`: um registro por organização com nome, identificador, situação, data de criação, membros (pessoas), limite de membros, pessoas externas, contas de serviço, apps em uso, armazenamento (uso, cota e código do local), resumo de cobrança (situação de acesso, assinatura, moeda, plano, MRR, valores em aberto e vencido, vencimento mais antigo, último pagamento, fim do trial e do período), totais do dia (faturado, pago e estornado por moeda) e métricas de apps. O campo `updated_at` só muda quando algum valor muda, funcionando como cursor da API.
- `InstanceMetric`: série diária (UTC) por código e moeda com `organizations.total`, `organizations.active`, `organizations.created`, `members.total`, `apps.<label>.organizations`, `storage.bytes` e, com a cobrança ativa, `billing.mrr`, `billing.invoiced`, `billing.paid`, `billing.refunded`, `billing.open`, `billing.overdue`, `billing.subscriptions.<situação>` e `billing.access.<estado>`, por moeda.

Os agendamentos e consumidores desse subsistema só são registrados quando `SKELETON_INSIGHTS_ENABLED` está ativo (desligado por padrão, lido na partida). Assim, uma instância que não se reporta a uma central não agenda trabalho ocioso; com a opção desligada, os resumos ficam vazios e `organizations/` e `metrics/` não retornam itens.

| Job | Quando | O que faz |
|---|---|---|
| `insights.refresh_organization` | De hora em hora (periódico por organização) e após `membership.*`, `app.*` e eventos de cobrança (no máximo uma vez a cada 5 minutos por organização) | Lê a organização no contexto de sistema dela e regrava o resumo apenas se algum valor mudou |
| `insights.metrics` | De hora em hora (global) | Recalcula as métricas do dia corrente a partir dos resumos |
| `insights.daily` | Às 00:20 UTC (global) | Fecha o dia anterior e remove métricas mais antigas que `SKELETON_INSIGHTS_RETENTION_DAYS` (padrão 800, mínimo 400) |

O MRR soma os itens vivos ativos normalizados por mês (`unit_amount × quantity / interval_count` no ciclo mensal e `/ (12 × interval_count)` no anual, arredondado). Os dados de cobrança vêm de `skeleton/billing/reporting.py`; com a cobrança desligada, o resumo de cobrança fica vazio e nenhuma métrica `billing.*` é gerada.

Apps podem contribuir com métricas próprias registrando um coletor em `ready()` via `skeleton.service_api.insights.register_instance_metric("<app_label>", coletor)`. O coletor recebe o identificador de cada organização, roda no contexto dela e devolve `{"<nome>": inteiro}`, consolidado como `<app_label>.<nome>`. Apenas contagens são permitidas — nunca dados pessoais.

A página `/platform/insights/` (acessível ao administrador da plataforma e a papéis com `inventory.read`) apresenta os totais da instância, MRR, valores em aberto e vencidos por moeda, organizações com pendências vencidas e a distribuição de apps por quantidade de organizações.

## Instância central: conexões remotas

Na instância central, o administrador da plataforma cadastra uma **conexão** por instância remota em `/platform/remote-instances/` (tabela global `RemoteInstanceConnection`). Cada cadastro informa:

- o endereço base (`https://host[:porta]`, sem caminho nem credenciais);
- o token de plataforma emitido na instância remota (cifrado com `REMOTE_INSTANCE_ENCRYPTION_KEYS`, nunca exibido novamente; deixá-lo em branco na edição preserva o valor gravado);
- a única organização autorizada a consultar essa conexão.

A ação **Testar** chama `instance/` na remota e fixa o `instance_id` no primeiro sucesso. A partir desse momento, qualquer resposta com identificador diferente falha com `remote_instance_mismatch`.

O cliente HTTP passa por `skeleton.common.outbound_http` (apenas `https`, sem seguir redirecionamentos e respostas limitadas a 1 MiB) e converte erros 401, 403, 404, 5xx ou corpo inválido em `remote_unauthorized`, `remote_forbidden`, `remote_not_found`, `remote_unavailable` e `remote_invalid_response`, sem expor o corpo da resposta.

Uma app da organização liberada consome as instâncias remotas pelo `RemoteInstanceService` ([API de serviços](service-api.md#catálogo-do-núcleo)), com os métodos `connections`, `instance`, `organizations` (paginado pelo cursor da remota), `organization`, `invoices`, `payments` e `metrics`. Todos devolvem DTOs imutáveis que nunca incluem o token. Por envolver chamadas de rede, o serviço só pode rodar em jobs (`actor=None` no contexto de sistema da organização liberada); chamá-lo durante uma requisição HTTP responde `Unavailable("remote_call_in_request")`. Conexões inativas ou pertencentes a outra organização respondem `NotFound`. Em testes, `skeleton.testing.remote_instances.fake_remote(...)` substitui o transporte por uma remota simulada, sem tráfego de rede.
