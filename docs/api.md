# API REST

A API REST do Skeleton utiliza o prefixo `/api/v1/` e compartilha a mesma identidade das páginas HTML, autenticada por sessão ou por [token de API](#tokens-de-api). No endereço próprio de uma organização (`<slug>.<domínio base>`, [#482](https://github.com/alaorneto/skeleton/issues/482)), os caminhos permanecem os mesmos — inclusive `/api/v1/o/<uuid>/…` —, mas apenas a organização daquele host responde; requisições para `/api/v1/o/<outra>/…` retornam 404.

Para autenticar por sessão, faça login pelo allauth: o cookie `sessionid` (`skeleton_session` quando o [domínio base](authentication-sso.md#sessão-entre-o-domínio-principal-e-os-endereços-de-organização) está configurado; o nome vem de `SESSION_COOKIE_NAME`) identifica a sessão mantida no servidor. A API aceita apenas sessão (`SessionAuthentication`) e, nas views habilitadas, [tokens de API](#autenticação-por-token); não há autenticação Basic nem JWT. Clientes nativos (aplicativos desktop) obtêm um access token via [provedor OIDC do núcleo](authentication-sso.md#provedor-oidc-para-clientes-nativos-603) com login no navegador do sistema e PKCE, usando-o como Bearer ([access tokens OIDC](#access-tokens-oidc-de-clientes-nativos)). A autenticação apenas identifica a pessoa: cada operação continua exigindo vínculo ativo, organização válida e permissões correspondentes (veja [autorização e contexto](concepts.md#autorização-e-contexto)).

Esta API destina-se exclusivamente a clientes HTTP. Código de outra app no mesmo processo não deve chamá-la: utilize a [API de serviços](service-api.md) da app ou do núcleo, que retorna DTOs e participa da transação do chamador.

Métodos que alteram estado (`POST`, `PATCH`, `PUT` e `DELETE`) autenticados por sessão exigem o cookie de sessão **e** proteção CSRF. Requisições autenticadas por [token de API](#autenticação-por-token) não utilizam cookie nem CSRF. Em clientes como `curl`, envie o cookie `csrftoken` junto ao header `X-CSRFToken`: o cookie de sessão sozinho não basta. Requisições anônimas à API recebem 403 do DRF, enquanto páginas HTML redirecionam para o login. Em produção, use sempre HTTPS e cookies seguros; tokens do IdP não substituem essa sessão.

## Recursos

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `openapi.json` | GET | [Schema OpenAPI 3.1](#schema-openapi) do que o ator alcança; aceita sessão ou token |
| `server/` | GET | Anônimo: produto, versão do core, versões da API e issuer OIDC ([descoberta](#descoberta-para-clientes-nativos)) |
| `me/` | GET, PATCH | Própria identidade e perfil; ETag composto |
| `organizations/` | GET | Apenas organizações com vínculo ativo do ator |
| `me/join-requests/`, `me/join-requests/<id>/withdraw/` | GET, POST; POST com `If-Match` | Próprios pedidos de ingresso em todas as organizações. `POST {organization_id, message?}` cria (201) ou devolve o pedido aberto; organização que não aceita pedidos retorna 404 neutro; limite excedido retorna 429 `rate_limited` com `Retry-After`; email não verificado retorna 403 `email_unverified`; vínculo ativo retorna 409 `already_member`. Retirar exige `If-Match`; pedido já fechado retorna 409 `not_open` |
| `o/<org>/identity-providers/`, `identity-providers/<id>/admission/` | GET; GET/PUT com `If-Match` | Capacidade `identity.configure`. Lista apenas os provedores ligados à organização, sem segredos nem opções de protocolo. `PUT` exige exatamente `jit_provisioning`, `jit_requires_approval`, `default_role` (UUID ou `null`) e `allowed_email_domains`; revisão virtual `"0"` antes da primeira gravação; revisão desatualizada retorna 412; papel de outra organização retorna 400; papel com capacidade escolhido sem `access.manage` retorna 403 `privileged_role`; provedor de outra organização retorna 404 |
| `o/<org>/join-links/`, `join-links/<id>/revoke/` | GET, POST; POST com `If-Match` | Capacidade `invitations.manage`. A lista nunca traz token nem resumo. `POST {label, role, mode, days, max_uses}` (exatamente esses campos) cria e devolve `url` com o token uma única vez (201, `Cache-Control: no-store`); papel com capacidade pedido pelo gestor de pessoas retorna 403 `privileged_role`. Revogar exige `If-Match` (412 se desatualizado) |
| `o/<org>/join-requests/`, `join-requests/<id>/` | GET; POST decisão com `If-Match` | Capacidade `admission.decide`; apenas pedidos abertos da organização. `POST {"decision": "approve"\|"reject"}` (sem campos extras); revisão desatualizada retorna 412; pedido já decidido ou expirado retorna 409 `not_pending`; gestor de pessoas recebe 403 se o papel tiver capacidade |
| `o/<org>/catalog/` | GET | Apenas tabelas legíveis pelo ator; não expõe inventário técnico global |
| `o/<org>/search/` | GET | [Busca global](#busca-global) nas tabelas marcadas com `global_search` que o ator lê; aceita `q` e `limit`; sessão ou token |
| `o/<org>/audit/`, `audit/<event>/` | GET | Eventos autorizados; filtros `record_sysid`, `model_label`, `from`, `to` (data ou ISO 8601), `actor`, `operation` e `request_id` na lista; filtro desconhecido ou inválido retorna 400 `invalid_filter`; `table_name` e `db_principal` vêm vazios para quem não administra a organização |
| `platform/...` | GET | A [API de gestão da instância](platform-api.md): apenas tokens `platform` com o escopo da rota (ou sessão do administrador da plataforma); somente leitura |
| `o/<org>/membership/` | GET | Vínculo do próprio ator na organização (`sysid`, revisão) com ETag; 404 sem vínculo ativo |
| `o/<org>/limits/` | GET | Uso e limite das métricas limitadas da organização ([#698](https://github.com/alaorneto/skeleton/issues/698)): `{"results": [{"metric", "used", "limit", "reserved", "over_limit"}]}`; métricas ilimitadas não aparecem; exige `members.view` ou `members.manage` (403 sem a capacidade, 404 para não membro). Ver [limites da organização](organization-limits.md) |
| `o/<org>/membership/leave/` | POST | O ator sai da organização; exige `If-Match` com a revisão do próprio vínculo (428 sem ele, 412 se mudou) e corpo vazio; responde 204; retorna 409 `last_administrator` para o último administrador |
| `o/<org>/memberships/`, `<id>/` | GET, POST; GET/PATCH/DELETE no detalhe | `members.view` para leitura e `members.manage` para escrita; criação com `user` explícito; `PATCH` altera apenas `is_active`; sem `access.manage`, não desativa quem possui capacidade |
| `o/<org>/roles/`, `<id>/` | GET, POST; GET/PATCH/DELETE | `access.manage`; campos `code`, `name` e `description`; `is_builtin` é somente leitura |
| `o/<org>/groups/`, `<id>/` | GET, POST; GET/PATCH/DELETE | `access.manage`; campos `code`, `name` e `description` |
| `o/<org>/invitations/`, `<id>/` | GET, POST; GET/DELETE | `invitations.manage`; campos `email` e `role`; sem `access.manage`, aceita apenas papel sem capacidade (papel interno com capacidade responde 403 `privileged_role` sem gravar, como nos links de ingresso); `digest`, `token` e estado não são graváveis; sem `PATCH` |
| `o/<org>/member-roles/`, `group-members/`, `group-roles/`, `table-permissions/`, `record-grants/` | GET/POST coleção; GET/DELETE detalhe | `access.manage`; vínculos explícitos; sem escrita aninhada nem `PATCH` |
| `o/<org>/tables/<table_key>/records/` | GET, POST | Gateway por allowlist de `SecuredRecord` exposto |
| `.../records/<sysid>/` | GET, PATCH, DELETE | Registro autorizado único; ETag com revisão |
| `.../records/<sysid>/restore/` | POST | Restauração autorizada com `If-Match`; registro vivo responde 404 |
| `.../records/<sysid>/access/` | GET, PATCH | Exige `manage_access` somado a propriedade do registro ou administração da organização; `If-Match` obrigatório no `PATCH` |
| `o/<org>/tables/<table_key>/references/<field>/` | GET | Seletor limitado e paginado, com autorização na origem e no alvo; não abre consulta livre de tabelas |
| `o/<org>/invitations/<id>/resend/` | POST | `invitations.manage`, corpo `{}` e `If-Match`; revoga o convite anterior e retorna novo UUID e ETag, enviando o novo token apenas por e-mail |
| `o/<org>/billing/`, `billing/<ação>/` | GET; POST | [Cobrança](billing.md#superfícies): `GET` com `billing.view` (situação, assinatura, faturas, ofertas e armazenamento); `POST` com `billing.manage` em `contract`, `remove`, `cancel`, `resume`, `voucher`, `account` e `checkout` (retorna a URL da página do provedor). Erros seguem a API de serviços; continua acessível sob restrição de pagamento. Somente sessões acessam estas rotas |
| `o/<org>/apps/active/` | GET | Qualquer membro ativo, por sessão ou token: apps efetivas visíveis ao papel ([descoberta](#descoberta-para-clientes-nativos)) |
| `o/<org>/apps/`, `apps/<app_label>/configuration/` | GET; GET/PUT configuração | Capacidade `apps.manage` (gestor de apps ou administrador; demais recebem 404). Lista apenas apps disponíveis para a organização e apps ativas que a plataforma retirou (`available: false`), com `app_id`, `version`, `enabled` e `configuration`. Usa `If-Match: "0"` para estado virtual inicial. `PUT` em app indisponível retorna 400 `app_unavailable`, exceto para desativar uma app ativa; se a configuração gravada for incompatível com a versão instalada, `PUT` retorna 400 `configuration_incompatible` sem gravar. `locked_fields` lista a ativação e os campos travados ou geridos pela plataforma, e tentar alterá-los responde 409 `setting_locked` ([configurações geridas pela plataforma](managed-settings.md)) |

### Recursos dos subsistemas

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `me/tokens/`, `me/tokens/<id>/` | GET, POST; DELETE no detalhe | Tokens pessoais do próprio ator; [gestão de tokens](#gestão-de-tokens) (`skeleton/integrations/urls.py`) |
| `o/<org>/service-accounts/`, `<id>/`, `<id>/tokens/`, `<id>/tokens/<token_id>/` | GET, POST na coleção; DELETE no detalhe (idempotente, sem `If-Match`); GET, POST nos tokens; DELETE num token | Contas de serviço e seus tokens; [gestão de tokens](#gestão-de-tokens) |
| `o/<org>/webhooks/`, `<id>/`, `<id>/rotate/`, `<id>/ping/`, `<id>/deliveries/`, `<id>/deliveries/<delivery_id>/`, `.../resend/` | GET, POST na coleção; GET, PATCH, DELETE no detalhe; POST em `rotate`, `ping` e `resend`; GET nas entregas | Endpoints, segredo, teste e log de entregas; [webhooks](webhooks.md) |
| `notifications/`, `o/<org>/notifications/actions/` | GET; POST | Caixa de notificações do ator e ações sobre ela; [notificações](notifications.md) (`skeleton/notifications/urls.py`) |
| `me/notification-preferences/`, `me/notification-preferences/<type_code>/<channel>/` | GET; GET, PUT, DELETE | Preferências por tipo e canal; [notificações](notifications.md#preferências) |
| `o/<org>/notification-settings/`, `.../<type_code>/`, `.../<type_code>/texts/<idioma>/` | GET; GET, PUT; PUT, DELETE | Avisos da organização: ligar, desligar e personalizar texto, com `notifications.manage`; [notificações](notifications.md#configuração-da-organização) |
| `me/push-subscriptions/`, `me/push-subscriptions/unsubscribe/` | POST; POST | Assinaturas de web push do navegador; [notificações](notifications.md#web-push) |
| `o/<org>/tables/<table_key>/records/<sysid>/attachments/`, `<attachment_id>/`, `<attachment_id>/restore/` | GET, POST; DELETE; POST | Anexos do registro, com autorização derivada dele; [arquivos](files.md#anexos) (`skeleton/api/urls.py`, views em `skeleton/files/api_views.py`) |

`org`, `sysid` e identificadores de relação são sempre UUIDs. Obtenha primeiro a organização desejada em `organizations/` e use seu UUID em `o/<org>/`; nunca envie organização ou ator no corpo tentando alterar o contexto. `table_key` é o identificador registrado do modelo (`label_lower`, como `example_notes.note`), nunca um nome físico SQL. A única fonte dessa resolução é o registry declarado em código; modelos não expostos ou técnicos não entram no gateway.

Instalação física de software não faz parte do gateway REST. A plataforma opera em `/platform/updates/` pelas etapas `available/`, `fetch/`, `upload/`, `plan/` (prévia), `plan/add-dependency/` e `operations/<uuid>/` (aprovação), exigindo sessão, CSRF, MFA quando configurado e reautenticação recente para mutações. A aprovação vincula revisão, digest e geração ao plano do agente; `GET` nunca instala código. As operações `install`, `update`, `uninstall`, `reinstall` e `core_update` são globais. Durante manutenção a interface permanece fechada, e o acompanhamento usa um canal independente com ticket privado por operação, válido por cinco minutos. Remoção de dados ou schema não é oferecida por HTTP.

Campos aceitos nos recursos de vínculo: `member-roles` (`{membership, role}`), `group-members` (`{group, membership}`), `group-roles` (`{group, role}`), `table-permissions` (`{role, table, action}`) e `record-grants` (`{table, record_sysid, membership, group, role, action}`, com exatamente um sujeito não nulo). O campo `table` recebe o UUID do catálogo, nunca o nome SQL. Todas as relações são resolvidas e autorizadas dentro da mesma organização e apenas entre registros vivos. Uma `TablePermission` positiva para a ação continua obrigatória mesmo quando existe concessão de registro.

## `me` composto

`GET /api/v1/me/` retorna `sysid`, `email`, `first_name`, `last_name`, `revision` e `profile` (`{sysid, revision, display_name, timezone, language, color_scheme}`). `PATCH` aceita apenas os campos planos de texto `first_name`, `last_name`, `display_name`, `timezone`, `language` e `color_scheme` (outros tipos JSON retornam 400 `validation_error`; `color_scheme` aceita apenas `""`, `light` ou `dark`) e atualiza `User` e `Profile` atomicamente. Campos como `email`, senha, `is_staff`, `is_superuser`, `auth_version` e `local_password_allowed` nunca são editáveis por este recurso.

O ETag tem o formato composto `"<user_revision>-<profile_revision>"` (por exemplo, `"1-1"`), pois o recurso combina duas linhas distintas (`User` e `Profile`). O `PATCH` exige ambas as revisões em `If-Match` e as valida na mesma transação: conferir apenas a revisão do perfil não protegeria o nome da identidade contra alterações concorrentes.

## Gateway

O gateway oferece CRUD genérico (criação, leitura, atualização e exclusão lógica) exclusivamente sobre os campos declarados em `TableSpec.read_fields` e `write_fields`; os metadados sistêmicos são automaticamente somente leitura. Campos desconhecidos geram erro em vez de serem descartados silenciosamente.

Para evitar atribuição indevida de campos protegidos (*mass assignment*), `organization`, `sysid`, autoria, revisão e campos de acesso são proibidos no `POST` e no `PATCH` comuns. Na criação, `owner` assume o ator autenticado e `visibility` nasce como `private`. Para alterar acesso, utilize o recurso separado `access/`: seu `PATCH` aceita apenas `owner` (UUID), `visibility` e `owning_group` (UUID ou `null`), sempre coerentes com vínculo e grupo ativos. O recurso exige `manage_access` e que o ator seja o proprietário ou administrador da organização; leitores comuns não podem sequer fazer `GET` em `access/`.

Chaves estrangeiras de negócio recebem apenas o UUID, resolvido contra um queryset legível da mesma organização, sem representação aninhada que possa vazar registros privados ou excluídos. A listagem aceita filtros exatos por `sysid`, `owner`, `visibility` e campos escalares elegíveis de `read_fields` — não há `contains`, regex, caminhos com ponto, SQL nem DSL de consulta.

Em tabelas que declaram `search_fields`, a listagem aceita ainda o parâmetro `q` (busca textual de 2 a 200 caracteres):

- utiliza o dicionário `portuguese`, ignorando acentos e reconhecendo flexões ("contratos" encontra "contrato");
- a última palavra casa por prefixo (`contr` encontra `contrato`; `contrato loc` encontra "Contrato de locação"), enquanto as anteriores casam como palavra inteira;
- aplica o filtro após a autorização e antes da paginação, de modo que `count` reflete apenas o que o ator pode ler;
- ordena os resultados por relevância.

No seletor de referências (`references/<field>/`), `q` busca por prefixo no rótulo (com `%`, `_` e `\` escapados como texto literal via `ESCAPE '\'`, e disponibilidade de `pg_trgm` em cache por processo). Quando `pg_trgm` está disponível, combina a busca com similaridade por palavra inteira (`strict_word_similarity`, limiar padrão 0,5), tolerando erros de digitação em palavras do rótulo sem casar trechos de palavras diferentes.

Sem busca textual, a listagem ordena por `created_at` e `sysid` e retorna `{count, page, page_size, results}` com padrão de 25 e máximo de 100 itens por página. O deslocamento máximo permitido é de 1 000 000 de linhas; qualquer página além disso responde 400 `invalid_pagination`. Contagem, listagem e auditoria são sempre autorizadas antes da paginação.

Referências cujo alvo não é visível ao ator aparecem como `null`, sem UUID, sem rótulo e sem invocar `__str__` de negócio. A projeção da lista autoriza alvos em lote por política e modelo. O recurso `references/<field>/` aceita exclusivamente `record`, `access` (`0` ou `1`), `page` e `page_size`, sem parâmetros duplicados: sem `record` exige `create` na origem; com `record` exige `update`; com `access` exige `manage_access`. Retorna `{count, page, page_size, results}` (máximo 100) com UUID e rótulo autorizados. Referências de identidade e acesso utilizam vínculos ativos e resolvedores dedicados, nunca enumeração global. No HTML há um seletor equivalente fora do admin, com fallback para copiar o UUID; toda escrita revalida a FK no servidor.

Apps podem registrar `RecordOperations` em código para aplicar serviços próprios nas operações de HTML e API. O núcleo valida autorização, allowlists e coerção antes de chamar o serviço; se o adaptador omitir uma operação, ela é negada sem fallback para o CRUD genérico. Metadados de apresentação não alteram o contrato JSON nem as permissões. Booleanos anuláveis preservam `null` quando o modelo permite; campos *write-only* nunca são adicionados à leitura por causa de um widget.

A API aceita e produz somente JSON. `SKELETON_JSON_MAX_BYTES` limita os bytes efetivamente lidos antes da decodificação (padrão `1048576`), inclusive em streams sem `Content-Length` confiável; excesso responde 413 `request_too_large`, sem escrita parcial (configure um teto igual ou menor no proxy). A paginação HTML preserva filtros validados com URL encoding ao mudar de página. A Browsable API do DRF permanece desligada, e cabeçalhos `Accept` incompatíveis com JSON recebem 406 com corpo JSON.

## Busca global

`GET /api/v1/o/<org>/search/?q=<termo>&limit=<n>` pesquisa um termo em todas as tabelas marcadas com `TableSpec.global_search` ([extensão](extending.md#busca-global), [#582](https://github.com/alaorneto/skeleton/issues/582)) que o ator tem permissão para ler na organização. Aceita sessão ou token (somente `GET`).

- **Parâmetros:** apenas `q` (2 a 200 caracteres, com a mesma busca textual de `?q=` na lista da tabela e última palavra por prefixo) e `limit` (resultados por tabela, de 1 a 20, padrão 5), cada um enviado no máximo uma vez. Ausência de `q`, tamanho fora do intervalo, `limit` inválido, parâmetro desconhecido ou repetido retornam 400 `invalid_filter`.
- **Resposta:** `{"results": [...]}` com um grupo por tabela que tenha resultados, ordenados por `GlobalSearchPolicy.order` e, em empate, pelo rótulo da tabela. Cada grupo traz `table_key`, `label` (rótulo da tabela), `items` (`sysid` e `label`, o campo de título do registro, nunca `__str__`) e `has_more` (indica que há mais resultados além de `limit`). Não há contagem total nem paginação dentro do grupo: para listar todos, use `tables/<table_key>/records/?q=...`.

```json
{"results": [{"table_key": "example_notes.note", "label": "Anotações", "has_more": false,
  "items": [{"sysid": "6f1c…", "label": "Contrato de locação"}]}]}
```

- **Autorização:** aplica as mesmas regras da listagem de cada tabela (`TablePermission` de leitura, escopos do token, app ativa e `authorized_queryset` por registro). Tabelas ilegíveis não geram grupo, de modo idêntico a tabelas sem resultados; sem correspondências, a resposta é `{"results": []}`. Quem não tem vínculo com a organização recebe 404. Apenas `sysid` e o título (em `read_fields`) são retornados; `sensitive_fields` nunca aparecem.
- **Custo:** executa uma consulta por tabela marcada que o ator lê, cada uma apoiada no índice GIN da tabela.

## Concorrência e erros

O `GET` de detalhe devolve um ETag forte no formato `"<revision>"`, identificando a versão exata lida. Para `PATCH`, `DELETE` e `restore`, envie esse valor **com as aspas** no header `If-Match` (a criação via `POST` não exige revisão prévia). Todo cabeçalho `If-Match` passa pelo mesmo parser (`skeleton.api.errors.parse_revision`), que recusa `"0"` exceto nas rotas de primeira gravação (`PUT` de configuração de app e de admissão por provedor). O servidor aplica controle otimista de concorrência (CAS): grava apenas se a revisão atual ainda corresponder à enviada, evitando sobrescritas silenciosas. Formulários HTML usam um campo oculto de revisão e respondem 409 em conflito; a API responde 412.

| Resultado | HTTP/code |
|---|---|
| Criação / atualização / exclusão lógica / restauração | 201 / 200 / 204 / 200 |
| `If-Match` ausente | 428 `revision_required` |
| Formato de `If-Match` inválido | 400 |
| Revisão desatualizada | 412 `revision_conflict` |
| Inexistente, invisível ou de outra organização | 404 |
| Tabela ou ação negada | 403 |
| Pessoa sem o MFA exigido por alguma regra ([quem cadastra o MFA](authentication-sso.md#mfa-cifrado-e-obrigatório)), em qualquer rota, inclusive com token pessoal | 403 `mfa_required` |
| Erro de validação ou atribuição indevida de campos | 400 |
| Unicidade, referência viva ou conflito de último administrador | 409 |
| Coluna congelada, exclusão ou restauração de registro selado | 409 `record_sealed` |
| Instância atrás do portão do deploy, inclusive com token | 503 `maintenance` com `Retry-After` |

Trate os códigos HTTP para orientar a próxima ação, nunca para repetir cegamente a escrita:

- **428:** obtenha a revisão atual em um `GET` antes de mutar;
- **412:** releia o registro e concilie as alterações com o usuário;
- **400:** corrija o corpo da requisição ou o formato do header;
- **409:** a operação viola uma regra de integridade e não foi aplicada;
- **403:** resolva autenticação, MFA ou permissão conforme o código de erro;
- **404:** não diferencia registro inexistente de registro oculto, preservando o isolamento entre organizações.

Em respostas de sucesso, 201 indica criação, 200 traz o recurso atualizado e 204 confirma exclusão lógica sem corpo.

Respostas de erro usam o envelope JSON `{code, detail, fields?}`. No portão de manutenção do deploy, acrescentam `ends_at` (sempre `null`) e orientam o cliente a aguardar o `Retry-After` ([manutenção](maintenance.md)). `X-Request-ID` é gerado no servidor, sem traceback, SQL nem segredos. Nunca use um UUID enviado pelo cliente como ator; revisões servem apenas ao controle de concorrência, nunca como concessão de acesso.

Criação e reenvio de convite confirmam a persistência no banco, não a entrega SMTP. Se o envio de e-mail falhar após o commit, a resposta mantém 201 e inclui o header `X-Email-Delivery: failed`; nesse caso, use `resend` com a revisão atual para gerar um novo convite e invalidar o anterior. A auditoria de negócio exibe apenas os `read_fields` do `TableSpec`, sem descartar a captura gravada no banco.

## Curl com sessão verdadeira

Faça login no navegador em `/accounts/login/`, concluindo confirmação de e-mail e MFA quando exigidos. Para testar com `curl`, copie **de forma privada** os cookies `sessionid` e `csrftoken` da sessão (ou exporte-os para um cookie jar privado), sem publicá-los, logá-los ou versioná-los. Nunca use `force_login` como smoke test de autenticação.

```sh
BASE=http://127.0.0.1:8000
# SESSION e CSRF vêm do login real e ficam apenas no ambiente local.
# ORG é UUID obtido em organizations; NOTE é UUID devolvido pelo POST.
curl -sS -b "sessionid=$SESSION; csrftoken=$CSRF" "$BASE/api/v1/organizations/"
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" "$BASE/api/v1/me/"
# Substitua 1-1 pelo ETag realmente retornado:
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' \
  -H 'If-Match: "1-1"' -H "Referer: $BASE/profile/" \
  --data '{"display_name":"Alice","timezone":"America/Sao_Paulo","color_scheme":"dark"}' "$BASE/api/v1/me/"

curl -i -X POST -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' -H "Referer: $BASE/" \
  --data '{"code":"primeira","title":"Minha nota","body":"Conteúdo sensível"}' \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/"
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/$NOTE/"
# Use o ETag obtido no GET, não presuma revisão 1 de um registro existente:
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' -H 'If-Match: "1"' \
  -H "Referer: $BASE/" --data '{"title":"Novo título"}' \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/$NOTE/"
```

Os exemplos assumem a app Notes instalada, ativa na organização e com as permissões de tabela concedidas. O primeiro `GET` lista as organizações acessíveis; `me/` demonstra a revisão composta; o `POST` cria uma nota privada; o `GET` seguinte obtém a revisão atual para o `PATCH`. Repetir o `PATCH` com a revisão anterior retorna 412: releia e concilie sem sobrescrever automaticamente. Para compartilhar a nota, utilize o endpoint separado de acesso:

```sh
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
  -H "Referer: $BASE/" --data '{"visibility":"organization","owning_group":null}' \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/$NOTE/access/"
```

`ETAG` deve incluir as aspas do header atual. Releia o registro após cada mutação antes de enviar `DELETE` ou `restore`. Em HTTPS, envie URL, `Origin` e `Referer` exatamente em HTTPS. Cookies e códigos de recuperação não devem ficar no histórico do shell nem em arquivos compartilhados. A API nunca oferece exportação global de usuários, exclusão física (*hard delete*) nem contorno de isolamento organizacional.

## Fluxos de processo

Tarefas e instâncias dos [fluxos de processo](flows.md) são expostas em `o/<org>/flows/`. As views apenas delegam ao [`FlowService`](service-api.md#flowservice), que aplica todas as regras de autorização. Desde [#410](https://github.com/alaorneto/skeleton/issues/410), tarefas e instâncias aceitam token de API (pessoal ou de conta de serviço) com os escopos `flows:read` (leituras) e `flows:write` (comandos em tarefas e instâncias); sem o escopo necessário, retornam 403. O escopo apenas libera a rota — a autorização efetiva continua sendo a do `FlowService`, e consultar uma instância via leitura do registro de negócio exige também o escopo de leitura daquela tabela. Painel de operação e rascunhos permanecem exclusivos de sessão, pois tokens nunca carregam capacidades administrativas; pelo mesmo motivo, comandos administrativos (`suspend`, `resume` e `retry`) são recusados a tokens.

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `o/<org>/flows/tasks/` | GET | Caixa de tarefas do ator; `scope=mine` (padrão), `available` (filas) ou `all`; `status` aceito apenas com `all` |
| `o/<org>/flows/tasks/<id>/` | GET | Tarefa com campos do formulário, ações e dados do processo; ETag com a revisão da tarefa |
| `o/<org>/flows/tasks/<id>/claim/`, `release/` | POST | Assumir ou devolver uma tarefa de fila; corpo vazio |
| `o/<org>/flows/tasks/<id>/reassign/` | POST | `{"to_user": "<uuid>", "comment": "..."}` |
| `o/<org>/flows/tasks/<id>/complete/` | POST | `{"action": "<chave>", "data": {campo: valor}, "comment": "..."}` |
| `o/<org>/flows/tasks/<id>/decide/` | POST | `{"decision": "approved" \| "rejected", "data": {...}, "comment": "..."}` |
| `o/<org>/flows/operations/` | GET | Painel de operação, restrito a quem administra fluxos (`flows.manage`); filtros `flow_key` e `version` |
| `o/<org>/flows/migrations/<chave>/<versão>/` | GET, POST | Migração de instâncias de uma versão: `GET` exibe a prévia (`moves` e `skipped` por instância); `POST` enfileira a execução e responde 202 com `job_id`; restrito a quem administra fluxos |
| `o/<org>/flows/absences/` | GET, POST | Ausências com substituto: lista as próprias ou todas (para quem administra fluxos); `POST {"user", "substitute", "starts_at", "ends_at"}` |
| `o/<org>/flows/absences/<id>/` | DELETE | Remove a ausência, exigindo `If-Match` |
| `o/<org>/flows/drafts/` | GET, POST | Fluxos em dados: lista e cria rascunhos `{"specification": {...}}` (formato de `FlowSpec.to_json`, sem versão); restrito a quem administra fluxos |
| `o/<org>/flows/drafts/<id>/` | GET, PATCH | Rascunho com `problems`; `PATCH` substitui a especificação, exigindo `If-Match` |
| `o/<org>/flows/drafts/<id>/publish/` | POST | Publica a próxima versão, exigindo `If-Match`; retorna 400 com os problemas de validação |
| `o/<org>/flows/instances/` | GET | Instâncias visíveis; filtros exatos `flow_key`, `subject_id` e `status` |
| `o/<org>/flows/instances/<id>/` | GET | Instância com etapas ativas e dados não sensíveis; ETag com a revisão da instância |
| `o/<org>/flows/instances/<id>/history/` | GET | Histórico ordenado |
| `o/<org>/flows/instances/<id>/cancel/`, `suspend/`, `resume/`, `retry/` | POST | Administração da instância; `cancel` e `suspend` aceitam `{"reason": "..."}` |

- As listagens usam paginação por cursor: `{"results": [...], "next_cursor": "..." | null, "page_size": 25}`, controladas por `cursor` e `page_size` (máximo 100).
- Todo `POST` exige `If-Match` com a revisão obtida no `GET`: a da **tarefa** nas ações de tarefa e a da **instância** nas demais. Ausência retorna 428 e revisão antiga retorna 412, sem gravar.
- Tarefa ou instância inexistente, de outra organização, invisível ou pertencente a app desativada responde 404. Se for visível mas o ator não puder executar a ação, responde 403. Formulário inválido retorna 400 com `fields`. Estado incompatível (instância suspensa, tarefa encerrada) retorna 409 com o código do conflito.
- Campos não declarados no formulário da etapa ou da transição são recusados, como no gateway. Ator, organização, responsável e revisão nunca vêm do corpo.
- Iniciar uma instância não possui rota genérica aqui: é responsabilidade da app dona do fluxo.
- Todas as rotas constam no [schema OpenAPI](#schema-openapi), cada ação com seu caminho literal. Os schemas de resposta derivam dos DTOs do `FlowService` (`skeleton/flows/api_views.py`), e todos os comandos documentam `If-Match` (412 e 428) e `ETag`.

## Portal

A API do portal de pessoas externas fica em `/api/v1/portal/<org>/apps/<label>/` ([portal](portal.md#superfície-533)): aceita apenas sessão (nunca token Bearer), roda sob contexto externo aberto pelo middleware, responde 404 fora dele e envia `Cache-Control: no-store`. As views herdam de `skeleton.api.views.PortalAPI` e chamam exclusivamente serviços marcados com `allow_external`. Essas rotas não aparecem no schema OpenAPI dos membros.

## Schema OpenAPI

`GET /api/v1/openapi.json` devolve o contrato da API em [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0), apenas em JSON e sem página interativa ([#178](https://github.com/alaorneto/skeleton/issues/178)). Aceita sessão ou [token](#autenticação-por-token); sem credenciais válidas, responde 401 ou 403. O documento é gerado diretamente do código a cada requisição (`skeleton/api/openapi.py`), nunca mantido à mão:

- **Gateway:** cada `TableSpec` exposto gera caminhos concretos, como `/api/v1/o/{organization_id}/tables/example_notes.note/records/`. As propriedades de leitura vêm dos campos sistêmicos e de `read_fields`; o corpo de `POST` e `PATCH` vem de `write_fields`. Os tipos seguem o `_meta` do modelo pelo mesmo mapeamento da especificação congelada (`field_internal_type`): relações são UUID e aceitam `null` na leitura (referência invisível). Os filtros exatos da lista e os seletores `references/` também entram no documento.
- **Views dedicadas:** cada view em `/api/v1/` declara um atributo `openapi`, mapeando métodos HTTP para objetos `Operation` (resumo, schemas de corpo e resposta, paginação, ETag, `If-Match` e capacidade exigida). Isso abrange inclusive rotas fora de `skeleton/api/urls.py`, como notificações, apps, webhooks e tokens. Como o gerador percorre a árvore real de URLs, views de apps sob `api/v1/o/<org>/apps/<label>/` aparecem no endereço exato em que estão montadas. Views sem `openapi` ainda entram com corpo livre e `x-skeleton-undocumented: true`.
- **Componentes:** define `Error` (`{code, detail, fields}`, também resposta `default` de todas as operações), `Page`, parâmetros `page` e `page_size`, header `ETag`, parâmetro `If-Match` e respostas `PreconditionFailed` (412) e `PreconditionRequired` (428). Os esquemas de segurança incluem `bearerAuth` e `sessionAuth`; `bearerAuth` só é listado nas operações cujo método consta em `token_methods`.

O schema descreve apenas o que o ator autenticado alcança. O servidor percorre cada organização ativa do ator em transações independentes e somente leitura — como nas [rotas agregadas](concepts.md#autorização-e-contexto) (`for_each_organization`), omitindo organizações onde o MFA do ator esteja pendente — e agrega o resultado:

- tabelas aparecem somente quando o ator possui `TablePermission`, e cada método somente com a ação correspondente (`read` para `GET`, `create` para `POST` etc.); apps desativadas ocultam suas tabelas e rotas;
- permissões de outra organização nunca vazam para quem não pertence a ela;
- operações administrativas aparecem apenas para quem detém a capacidade necessária (`access.manage`, `integrations.manage` ou administração da organização);
- sob token, entram apenas métodos de `token_methods` e ações cobertas pelos escopos: um token `example_notes.note:read` enxerga apenas os `GET` dessa tabela, nenhuma rota administrativa e nenhum endpoint de tokens; tokens vinculados a uma organização consultam apenas essa organização;
- `sensitive_fields` nunca aparecem, nem pelo nome: não entram como propriedades, filtros nem pickers. Quando um campo sensível está em `read_fields`, a resposta real continua devolvendo-o e o objeto do schema fica aberto (sem `additionalProperties: false`) para não nomeá-lo; nos demais objetos, propriedades não declaradas são proibidas.

Como o schema reflete permissões, apps ativas e versão atuais, não o trate como contrato estático; a resposta traz `Cache-Control: private, no-store`. Apps documentam suas próprias views da mesma maneira, importando `Operation` e os construtores de `skeleton.api.openapi` (superfície pública do núcleo):

```python
from skeleton.api.openapi import INTEGER, Operation, nullable, obj, string
from skeleton.api.views import TenantAPI


class SummaryView(TenantAPI):
    openapi = {"GET": Operation("Resumo das anotações", obj({"count": INTEGER, "heading": nullable(string())}))}
```

A suíte do núcleo valida todas as respostas reais de `/api/v1/` contra esse schema por meio de `skeleton.testing.openapi` ([evidência dos testes](testing-evidence.md#openapi-da-api-epic-86)).

## Tokens de API

Além da sessão de navegador, a API aceita tokens Bearer para integrações e automações. Esta seção especifica o ciclo de vida do token; o comportamento na requisição está em [Autenticação por token](#autenticação-por-token).

| Item | Regra |
|---|---|
| Formato | `skt_<prefixo>_<segredo>`: prefixo único de 8 caracteres hexadecimais (localiza o registro) e segredo de 43 caracteres (`secrets.token_urlsafe(32)`, 256 bits) |
| Armazenamento | `sk_integrations.ApiToken` (`SystemRecord` global) grava apenas o prefixo e o SHA-256 do segredo (`digest`, campo sensível redigido na auditoria). O texto completo do token é exibido **uma única vez**, na emissão, e não pode ser recuperado depois |
| Tipos | `personal` (da própria pessoa, válido em todas as suas organizações), `service` (de uma conta de serviço, restrito à organização da conta) e `feed` (somente escopos de feed, como assinatura da agenda) |
| Validade | Obrigatória e no futuro, respeitando o teto `SKELETON_API_TOKEN_MAX_DAYS` (padrão 365, no catálogo da instância). Ausente, passada ou acima do teto retorna 400 |
| Escopos | `<table_key>:<ação>` para tabela exposta no gateway (`expose_api`) e ação em `read`, `create`, `update`, `delete` ou `restore`, ou um escopo nomeado registrado em código (`register_token_scope`). Escopos desconhecidos, de tabela não exposta ou de feed em token comum retornam 400. Escopos apenas **restringem** o papel do portador: nunca concedem permissão extra |
| Emissão | Pela própria pessoa autenticada (pessoal ou feed) ou por quem possui `integrations.manage` na organização da conta de serviço. Com `SKELETON_REQUIRE_ADMIN_MFA`, exige MFA cadastrada. Contas de serviço não emitem tokens pessoais, e um token nunca pode emitir outro token |
| Revogação | Grava `revoked_at` e aplica exclusão lógica, exigindo `If-Match`/revisão. Quem não pode gerir o token recebe o mesmo "inexistente" de um token ausente |
| Invalidação | Deixa de valer imediatamente na requisição seguinte ao expirar, ser revogado, ter o usuário desativado ou excluído, ou ter o `auth_version` incrementado (troca de senha, desativação ou revogação de sessões) |

Os serviços ficam em `skeleton.integrations.tokens.services`: `issue_personal_token`, `issue_service_token`, `revoke_token`, `personal_tokens`, `service_tokens` e `verify_token`. Falhas de validação levantam `ValidationError` por campo (`scopes`, `expires_at`, `name`); ausência de capacidade levanta `PermissionDenied`.

## Autenticação por token

Nas rotas de `/api/v1/`, o header `Authorization: Bearer skt_...` autentica a requisição como a pessoa ou conta de serviço dona do token. Rotas HTML ignoram esse cabeçalho.

| Situação | Resposta |
|---|---|
| Token válido numa view que aceita o método | Executa como o usuário do token, **sem CSRF**; o contexto registra os escopos, e `skeleton.source` (GUC e auditoria) recebe `http:token:<prefixo>` |
| Sessão e Bearer enviados juntos | 400 `ambiguous_credentials` |
| Token malformado, desconhecido, expirado, revogado, de usuário inativo, com `auth_version` alterado ou de feed (`kind=feed`, restrito às views de feed) | 401 `invalid_token`, com `WWW-Authenticate: Bearer realm="api", error="invalid_token"`, e `security_event("api_token.rejected")` sem registrar o token |
| View sem o método em `token_methods` | 403 `token_not_allowed` |
| Escopo ausente para a tabela e ação solicitadas | 403, mesmo que o papel permita |
| Token de conta de serviço (preso a uma organização) usado na URL de outra | 404 |
| Pessoa sem vínculo na organização da URL | 404, igual à sessão |

Recursos que aceitam autenticação por token e seus métodos permitidos:

| Recurso | Métodos | Escopo |
|---|---|---|
| `me/` | GET (nunca PATCH) | nenhum |
| `organizations/`, `o/<org>/membership/`, `o/<org>/limits/`, `o/<org>/catalog/` | GET | nenhum |
| `o/<org>/tables/<tabela>/records/`, `<id>/` | GET, POST, PATCH, DELETE | `<tabela>:read`, `create`, `update`, `delete` |
| `.../records/<id>/restore/` | POST | `<tabela>:restore` |
| `o/<org>/tables/<tabela>/references/<campo>/` | GET | `read` da tabela referenciada |
| `.../records/<id>/attachments/` | GET, POST | `files:read` ou `files:write`, além do `read`/`update` do registro exigido pelo serviço |
| `.../attachments/<id>/`, `.../restore/` | DELETE, POST | `files:write` |
| `.../records/<id>/comments/`, `.../comments/<id>/`, `.../comments/<id>/restore/` | GET, POST, PATCH, DELETE | Apenas `read` do registro exigido pelo serviço; edição, exclusão e restauração exigem `If-Match` ([comentários](comments.md)) |
| `server/` | GET | nenhum (anônimo; token válido também passa) |
| `o/<org>/apps/active/` | GET | nenhum |
| `notifications/`, `o/<org>/notifications/actions/` | GET; POST | nenhum: alcança apenas linhas do próprio ator; marcar como lida usa o `POST` de `actions/` |

Todos os demais recursos (vínculos, papéis, grupos, concessões, convites, ativação de apps, webhooks, auditoria, saída da organização e gestão de tokens) aceitam exclusivamente sessão. Views de apps que estendem `SessionAPI` só aceitam token se declararem `token_methods`.

```sh
# TOKEN vem da tela de tokens e fica só no ambiente local; não o coloque em histórico ou log.
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'   -d '{"code":"n1","title":"Nota","body":"texto"}' "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/"
```

## Access tokens OIDC de clientes nativos

Clientes nativos obtêm access tokens junto ao [provedor OIDC do núcleo](authentication-sso.md#provedor-oidc-para-clientes-nativos-603) e os enviam no header `Authorization: Bearer <access token>`. O `ApiTokenMiddleware` continua sendo o único ponto de autenticação Bearer: qualquer token que não comece com `skt_` é buscado pelo seu SHA-256 na tabela de tokens do allauth (índice único `(type, hash)`), verificando se é um access token vigente de cliente nativo ativo, de pessoa ativa e com o mesmo `auth_version` da emissão. A partir daí, aplicam-se exatamente as mesmas regras e respostas da [autenticação por token](#autenticação-por-token), na mesma ordem:

| Situação | Resposta |
|---|---|
| Token válido numa view que aceita o método (`token_methods`) | Executa como a pessoa, sem CSRF; `skeleton.source` (GUC e auditoria) recebe `http:oidc:<client_id>` |
| Sessão e Bearer enviados juntos | 400 `ambiguous_credentials` |
| Token vencido, revogado, de grant revogado, de cliente desativado ou revogado, de pessoa inativa ou com `auth_version` mudado | 401 `invalid_token` com `WWW-Authenticate: Bearer realm="api", error="invalid_token"`: renove usando o refresh token; se a renovação retornar `invalid_grant`, solicite novo login |
| View sem o método em `token_methods` | 403 `token_not_allowed` |
| Limites | Os mesmos `SKELETON_API_TOKEN_RATE_PER_MINUTE` (por access token) e `SKELETON_API_TOKEN_FAILURES_PER_MINUTE` (por IP) |

**Escopos.** Os escopos OIDC concedidos são mapeados para escopos do núcleo: `openid profile email`, sozinhos, produzem um **token pessoal amplo**, cobrindo todas as ações de tabela e escopos nomeados que não sejam de feed, sempre cruzados com o papel da pessoa em cada organização. Se o operador tiver registrado escopos específicos do núcleo para o cliente (`native_client register --scope`) e o cliente os solicitar, apenas eles valem. Em nenhuma hipótese o escopo amplia o papel, e tokens OIDC nunca administram: capacidades (`access.manage`, `apps.manage` etc.) ficam vazias sob qualquer token. O último uso é gravado no próprio token no máximo uma vez a cada 5 minutos e aparece em [dispositivos conectados](authentication-sso.md#provedor-oidc-para-clientes-nativos-603).

Com o provedor ligado, o [schema OpenAPI](#schema-openapi) inclui `openIdConnect` (`openIdConnectUrl` apontando para o discovery do issuer) ao lado de `bearerAuth` em `components.securitySchemes`, e cada operação que aceita token referencia ambos. Definir `SKELETON_OIDC_API_TOKENS=false` desabilita o reconhecimento na API (rollback) sem afetar o provedor.

```sh
# TOKEN é o access_token do fluxo OIDC; fica só no ambiente local.
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/organizations/"
```

## Descoberta para clientes nativos

Dois endpoints permitem que um cliente nativo descubra o servidor e o que pode utilizar nele.

`GET /api/v1/server/` é anônimo por desenho e expõe apenas dados públicos do produto, com `Cache-Control: public, max-age=60`:

```json
{"product": "Skeleton", "core_version": "0.1.0", "api_versions": ["v1"], "oidc_issuer": "https://app.example.com"}
```

`product` vem de `SKELETON_PRODUCT_NAME` (padrão `Skeleton`) e `oidc_issuer` traz o issuer do [provedor OIDC](authentication-sso.md#provedor-oidc-para-clientes-nativos-603), ou `null` quando desligado (mesmo num endereço de organização, o issuer permanece o do domínio base). Nenhuma organização, host interno, configuração ou segredo é exposto. Atrás do portão do deploy, o endpoint também retorna 503 com `code: maintenance`, permitindo distinguir "servidor em atualização" de "não é um servidor Skeleton".

`GET /api/v1/o/<org>/apps/active/` aceita sessão ou token (`token_methods = ("GET",)`) de qualquer membro ativo e responde `{"results": [{"app_id", "label", "version", "title"}]}` com as apps efetivas na organização (instaladas, disponíveis, ativas e com dependências atendidas) cuja navegação seja alcançável pelo papel do ator — ou que não declarem navegação. Nenhum dado de licença ou faturamento é incluído. Não membros e tokens de conta de serviço de outra organização recebem 404 neutro; apps inativas não aparecem.

As notificações da pessoa também aceitam token: `GET notifications/` (listagem) e `POST o/<org>/notifications/actions/` (marcar como lida, não lida ou arquivada). Qualquer linha de outra pessoa responde 404, igual a uma inexistente, sem efeito colateral.

## Gestão de tokens

A gestão de tokens exige sessão: nenhum token pode emitir, listar ou revogar tokens (responde 403 `token_not_allowed`).

| Interface | Quem | O que faz |
|---|---|---|
| `/profile/tokens/` | A própria pessoa | Lista seus tokens pessoais e de feed (prefixo, nome, escopos e validade), emite novos tokens com nome, escopos e validade em dias e revoga existentes. Contas de serviço não têm acesso a esta página |
| `/o/<org>/integrations/service-accounts/` e `<conta>/` | `integrations.manage` (404 para demais) | Cria e desativa contas de serviço, lista, emite e revoga tokens de cada conta |
| `GET`, `POST /api/v1/me/tokens/`; `DELETE /api/v1/me/tokens/<id>/` | A própria pessoa | Equivalente à página; `POST` recebe `name`, `scopes` e `expires_at` (ISO 8601 com fuso) |
| `GET`, `POST /api/v1/o/<org>/service-accounts/`; `DELETE .../<conta>/` | `integrations.manage` | Lista e cria contas (`{"name": ...}`); `DELETE` desativa a conta |
| `GET`, `POST .../service-accounts/<conta>/tokens/`; `DELETE .../tokens/<id>/` | `integrations.manage` | Gerencia os tokens da conta de serviço |

- Emitir ou revogar tokens e criar ou desativar contas de serviço exigem autenticação recente (`reauthentication_required`, 403 na API — avaliado após o 404 de quem não tem `integrations.manage`; no HTML, solicita reautenticação). Com `SKELETON_REQUIRE_ADMIN_MFA`, a emissão exige também MFA cadastrada (`mfa_required`).
- As três listagens da API (`me/tokens/`, `service-accounts/` e `<conta>/tokens/`) são paginadas: aceitam apenas `page` e `page_size` (outros parâmetros retornam 400 `invalid_filter`) e respondem com `count`, `page`, `page_size` e `results`. Cada token inclui `usable` (`false` quando não pode mais autenticar — conta desativada ou excluída, `auth_version` mudado, expirado ou revogado), exibido na tela como "Inválido".
- Cada pessoa ou conta de serviço pode manter no máximo `SKELETON_API_TOKEN_MAX_PER_OWNER` tokens vivos (padrão 50, no catálogo da instância). Tokens expirados continuam contando até serem revogados; acima do teto, a emissão responde 400 (erro exibido no campo `name` na página).
- A resposta de criação (página HTML ou 201 na API) é o único momento em que o texto completo do token aparece, com `Cache-Control: no-store`. Listagens e demais endpoints nunca expõem segredo nem digest.
- A revogação exige `If-Match` com a revisão atual (428 se ausente, 412 se desatualizada). Tokens de outra pessoa ou de outra organização respondem 404, como inexistentes.
- Desativar uma conta de serviço invalida todos os seus tokens imediatamente via incremento de `auth_version`.

## Limites e registro de uso dos tokens

O controle de taxa usa o cache compartilhado (`skeleton.common.ratelimit`, janela fixa de 60 segundos). Em produção, o cache deve ser Redis; com `DEBUG=False` e cache em memória local, `manage.py check --deploy` emite o aviso `skeleton.W029`, pois cada worker contaria de forma isolada.

| Limite | Padrão | Ao passar |
|---|---|---|
| Requisições por token (`SKELETON_API_TOKEN_RATE_PER_MINUTE`) | 600 por minuto | 429 `rate_limited` com `Retry-After`, barrado antes da view, sem escrita |
| Falhas de autenticação por token, por IP (`SKELETON_API_TOKEN_FAILURES_PER_MINUTE`) | 20 por minuto | 429 com `Retry-After` para qualquer Bearer desse IP, sem consultar o banco (contenção de força bruta sobre prefixos e segredos) |

O IP do cliente é obtido segundo a mesma topologia de proxies do allauth (`DJANGO_TRUSTED_PROXY_COUNT` ou `DJANGO_TRUSTED_CLIENT_IP_HEADER`). Em caso de indisponibilidade do cache, as requisições passam: o limitador mitiga abuso, mas não substitui a autorização. Cada resposta 429 gera `security_event("api_token.rate_limited")`.

A data do último uso fica registrada na tabela operacional `sk_integrations.ApiTokenActivity`, atualizada no máximo uma vez a cada 5 minutos por token (marcador no cache e janela no banco), sem revisão e **sem `AuditEvent`**. Listagens HTML e API exibem o campo `last_used_at`. Por ora, não há limite geral da API para usuários autenticados por sessão.
