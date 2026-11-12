# API REST

Esta API usa o prefixo `/api/v1/` e a mesma identidade das páginas HTML, por sessão ou por [token de API](#tokens-de-api). Faça login pelo allauth para obter uma sessão; o cookie `sessionid` identifica essa sessão mantida no servidor. Os mecanismos são a sessão (`SessionAuthentication`) e, nas views que os aceitam, [tokens de API](#autenticação-por-token); não há Basic, JWT nem endpoint independente de senha. A autenticação identifica a pessoa, mas cada operação ainda verifica vínculo, organização e permissões. Veja [autorização e contexto](concepts.md#autorização-e-contexto).

Esta é a API para clientes HTTP. Código de outra app, no mesmo processo, não chama esta API: usa a [API de serviços](service-api.md) da app ou do núcleo, que devolve DTOs e roda na transação de quem chama.

Métodos que podem mudar estado, como POST, PATCH, PUT e DELETE, precisam do cookie de sessão **e** da proteção CSRF. Requisições autenticadas por [token de API](#autenticação-por-token) não usam cookie nem CSRF. Em clientes como curl, envie o cookie `csrftoken` e o header `X-CSRFToken` correspondente; um cookie de sessão sozinho não basta. Uma requisição anônima à API recebe 403 do DRF, enquanto a página HTML redireciona ao login. Em produção, use HTTPS e cookies seguros; tokens do IdP não substituem essa sessão.

## Recursos

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `openapi.json` | GET | [Schema OpenAPI 3.1](#schema-openapi) do que o ator alcança; sessão ou token |
| `me/` | GET, PATCH | Própria identidade/perfil; ETag composto |
| `organizations/` | GET | Apenas organizações dos próprios vínculos ativos |
| `me/join-requests/`, `me/join-requests/<id>/withdraw/` | GET, POST; POST com If-Match | Os próprios pedidos de ingresso em todas as organizações; POST `{organization_id, message?}` cria (201) ou devolve o pedido aberto; organização que não aceita pedidos responde 404 neutro, limite excedido 429 `rate_limited` com `Retry-After`, email não verificado 403 `email_unverified`, vínculo ativo 409 `already_member`. Retirar exige If-Match; pedido já fechado 409 `not_open` |
| `o/<org>/identity-providers/`, `identity-providers/<id>/admission/` | GET; GET/PUT com If-Match | Capacidade `identity.configure`. Lista só os provedores ligados à organização, sem segredo nem opções de protocolo. PUT exige exatamente `jit_provisioning`, `jit_requires_approval`, `default_role` (UUID ou null) e `allowed_email_domains`; revisão virtual `"0"` antes da primeira gravação, stale 412, papel de outra organização 400, papel com capacidade escolhido sem `access.manage` 403 `privileged_role`, provedor de outra organização 404 |
| `o/<org>/join-links/`, `join-links/<id>/revoke/` | GET, POST; POST com If-Match | Capacidade `invitations.manage`. A lista nunca traz token nem resumo. POST `{label, role, mode, days, max_uses}` (exatamente esses campos) cria e devolve a `url` com o token uma única vez (201, `Cache-Control: no-store`); papel com capacidade pedido pelo gestor de pessoas responde 403 `privileged_role`. Revogar exige If-Match (412 se stale) |
| `o/<org>/join-requests/`, `join-requests/<id>/` | GET; POST decisão com If-Match | Capacidade `admission.decide`; só pedidos abertos da organização. POST `{"decision": "approve"\|"reject"}` (sem outros campos); revisão stale 412, pedido já decidido ou expirado 409 `not_pending`; o gestor de pessoas recebe 403 num pedido com papel que carrega capacidade |
| `o/<org>/catalog/` | GET | Apenas tabelas legíveis; não inventário técnico global |
| `o/<org>/audit/`, `audit/<event>/` | GET | Eventos autorizados; filtros `record_sysid`, `model_label`, `from`, `to` (data ou ISO 8601), `actor`, `operation` e `request_id` na lista (#184); filtro desconhecido ou inválido é 400 `invalid_filter` |
| `o/<org>/membership/` | GET | O próprio vínculo do ator nesta organização (sysid, revisão) com ETag; 404 sem vínculo ativo |
| `o/<org>/membership/leave/` | POST | O ator sai da organização; exige `If-Match` com a revisão do próprio vínculo (428 sem ele, 412 se mudou), corpo vazio; 204; 409 `last_administrator` para o último administrador |
| `o/<org>/memberships/`, `<id>/` | GET, POST; GET/PATCH/DELETE no detalhe | `members.view` para ler, `members.manage` para alterar; criar com user explícito; PATCH só is_active; sem `access.manage`, não desativa quem tem capacidade |
| `o/<org>/roles/`, `<id>/` | GET, POST; GET/PATCH/DELETE | `access.manage`; code,name,description; is_builtin read-only |
| `o/<org>/groups/`, `<id>/` | GET, POST; GET/PATCH/DELETE | `access.manage`; code,name,description |
| `o/<org>/invitations/`, `<id>/` | GET, POST; GET/DELETE | `invitations.manage`; email,role; sem `access.manage`, só papel sem capacidade (papel interno com capacidade responde 403 `privileged_role`, sem escrita, como nos links de ingresso); digest/token/estado não graváveis; sem PATCH |
| `o/<org>/member-roles/`, `group-members/`, `group-roles/`, `table-permissions/`, `record-grants/` | GET/POST coleção; GET/DELETE detalhe | `access.manage`; links explícitos; sem nested write/PATCH |
| `o/<org>/tables/<table_key>/records/` | GET, POST | Gateway allowlist de SecuredRecord exposto |
| `.../records/<sysid>/` | GET, PATCH, DELETE | Um registro autorizado; ETag/revisão |
| `.../records/<sysid>/restore/` | POST | Restore autorizado com If-Match |
| `.../records/<sysid>/access/` | GET, PATCH | Manage_access + ownership/org admin; If-Match no PATCH |
| `o/<org>/tables/<table_key>/references/<field>/` | GET | Picker limitado/paginado, com autorização da origem e alvo; não abre tabela livre |
| `o/<org>/invitations/<id>/resend/` | POST | `invitations.manage`, corpo `{}` e If-Match; revoga convite antigo e retorna novo UUID/ETag/token só por email |
| `o/<org>/apps/`, `apps/<app_label>/configuration/` | GET; GET/PUT configuração | Capacidade `apps.manage` (gestor de apps ou administrador; os demais recebem 404); lista só as apps disponíveis para a organização e as ativas que a plataforma retirou (`available: false`); app_id/version declarados, enabled/configuration; If-Match `"0"` para estado virtual inicial; PUT numa app indisponível responde 400 `app_unavailable`, salvo a desativação de uma app ativa |

### Recursos dos subsistemas

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `me/tokens/`, `me/tokens/<id>/` | GET, POST; DELETE no detalhe | Tokens pessoais do próprio ator; [gestão de tokens](#gestão-de-tokens) (`skeleton/integrations/urls.py`) |
| `o/<org>/service-accounts/`, `<id>/`, `<id>/tokens/`, `<id>/tokens/<token_id>/` | GET, POST na coleção; DELETE no detalhe (idempotente; não usa `If-Match`); GET, POST nos tokens; DELETE num token | Contas de serviço e seus tokens; [gestão de tokens](#gestão-de-tokens) |
| `o/<org>/webhooks/`, `<id>/`, `<id>/rotate/`, `<id>/ping/`, `<id>/deliveries/`, `<id>/deliveries/<delivery_id>/`, `.../resend/` | GET, POST na coleção; GET, PATCH, DELETE no detalhe; POST em `rotate`, `ping` e `resend`; GET nas entregas | Endpoints, segredo, teste e log de entregas; [webhooks](webhooks.md) |
| `notifications/`, `o/<org>/notifications/actions/` | GET; POST | Caixa de notificações do ator e ações sobre ela; [notificações](notifications.md) (`skeleton/notifications/urls.py`) |
| `me/notification-preferences/`, `me/notification-preferences/<type_code>/<channel>/` | GET; GET, PUT, DELETE | Preferências por tipo e canal; [notificações](notifications.md#preferências) |
| `me/push-subscriptions/`, `me/push-subscriptions/unsubscribe/` | POST; POST | Assinaturas de web push do navegador; [notificações](notifications.md#web-push) |
| `o/<org>/tables/<table_key>/records/<sysid>/attachments/`, `<attachment_id>/`, `<attachment_id>/restore/` | GET, POST; DELETE; POST | Anexos do registro, com autorização derivada dele; [arquivos](files.md#anexos) (`skeleton/api/urls.py`, views em `skeleton/files/api_views.py`) |

`org`, `sysid` e IDs de relações são UUIDs. Primeiro escolha uma organização devolvida por `organizations/` e use seu UUID em `o/<org>/`; não envie a organização ou o ator no corpo para tentar mudar o contexto. `table_key` é o identificador registrado do modelo (`label_lower`), por exemplo `example_notes.note`, e nunca um nome SQL. O registry, a lista explícita de modelos permitidos no código, é a única fonte dessa resolução. Modelos não expostos ou técnicos não entram no gateway.

Instalação física não é recurso do gateway REST. A plataforma usa `/platform/updates/` e as etapas `available/`, `fetch/`, `upload/`, `plan/` (prévia), `plan/add-dependency/` e `operations/<uuid>/` (aprovação): sessão, CSRF, MFA quando exigido e reautenticação recente para mutações. Aprovação vincula revisão, digest e geração ao plano do agente; GET não instala código. `install`, `update`, `uninstall`, `reinstall` e `core_update` são operações globais. Durante manutenção, a interface fica fechada; acompanhamento usa canal independente com ticket privado, por operação, válido por cinco minutos. Remoção de dados/schema não é oferecida por HTTP.

Campos de links: member-roles={membership,role}; group-members={group,membership}; group-roles={group,role}; table-permissions={role,table,action}; record-grants={table,record_sysid,membership,group,role,action}, com exatamente um sujeito não nulo. Table é UUID do catálogo, não nome SQL. Todas relações são resolvidas/autorizadas same-tenant e vivas. TablePermission positiva da ação é obrigatória mesmo com grant.

## `me` composto

GET retorna sysid,email,first_name,last_name,revision e profile={sysid,revision,display_name,timezone,language}. PATCH só aceita campos planos first_name,last_name,display_name,timezone,language; atualiza User/Profile atomicamente. Email/senha/is_staff/is_superuser/auth_version/local_password_allowed nunca são campos desse recurso.

O ETag é `"<user_revision>-<profile_revision>"`, por exemplo `"1-1"`, porque a resposta reúne duas linhas: User e Profile. O PATCH exige as duas revisões em `If-Match` e as compara dentro da mesma transação. A revisão do perfil sozinha não protegeria o nome da identidade de uma alteração concorrente.

## Gateway

O gateway oferece CRUD genérico (criação, leitura, alteração e exclusão lógica) somente para campos explicitamente declarados em `TableSpec.read_fields` e `write_fields`; os metadados sistêmicos são automaticamente somente leitura. Campos desconhecidos retornam erro em vez de serem ignorados. `organization`, `sysid`, autoria, revisão e campos de acesso são proibidos no POST/PATCH comum: aceitar esses valores do cliente permitiria atribuição indiscriminada de campos protegidos (mass assignment). A criação atribui `owner` ao ator autenticado e visibilidade `private`. Use o recurso separado `access/` para mudar acesso: seu PATCH aceita somente owner UUID, visibility e owning_group UUID/null, coerentes com vínculo e grupo ativos. Ele exige `manage_access` e ser proprietário ou administrador da organização; um leitor comum não pode nem obter GET access.

FKs de negócio usam UUID resolvido em queryset legível da mesma org, nunca nested representação que revele privado/deletado. Lista aceita filtros exatos sysid/owner/visibility e campos escalares read_fields elegíveis. Não aceita contains/regex/dot path/SQL/query DSL. Uma tabela com `search_fields` aceita também `q`, uma busca textual de 2 a 200 caracteres:

- usa o dicionário `portuguese`, sem acentos e com variações ("contratos" acha "contrato");
- filtra depois da autorização e antes da paginação, então `count` só conta o que o ator lê;
- ordena por relevância.

O picker de referências aceita `q` para prefixo do rótulo e, com `pg_trgm`, similaridade por palavra inteira (`strict_word_similarity`, limiar padrão 0,5), que tolera erro de digitação numa palavra do rótulo, mas não casa pedaços de outra palavra (#180, #181).

 Ordena created_at/sysid; paginação `{count,page,page_size,results}`, tamanho 25, máximo 100. Conte/count/lista/audit são autorizados antes de paginação.

Referências invisíveis são representadas como null, sem UUID/label e sem `__str__` de negócio. A projeção da lista autoriza alvos em lote por política/modelo. O recurso `references/<field>/` aceita somente `record`, `access=0/1`, `page` e `page_size`, sem duplicatas. Sem `record`, exige create na origem; com registro, update; com access, manage_access. Retorna `{count,page,page_size,results}` de UUID/label autorizados; máximo 100. Referências de identidade/acesso usam vínculos ativos/resolvedores dedicados, nunca enumeração global. HTML tem seletor equivalente fora do admin e fallback para copiar UUID; toda escrita revalida FK.

Apps podem registrar `RecordOperations` em código para impor serviços próprios nas mesmas operações HTML/API. Core valida autorização/allowlists/coerção antes do serviço; se um adaptador não declara uma operação, ela é negada sem fallback. Metadados de apresentação não mudam o contrato JSON ou grants. Nullable boolean mantém null quando o modelo o permite; write-only nunca é acrescentado à leitura pela presença de um widget.

Somente JSON é aceito na API. `SKELETON_JSON_MAX_BYTES` limita bytes efetivamente lidos antes da decodificação (default 1048576), inclusive stream sem Content-Length confiável; excesso é 413 `request_too_large`, sem escrita parcial. Alinhe um limite de corpo igual ou menor no proxy. Paginação HTML preserva filtros já validados ao trocar page, com URL encoding. As respostas também são sempre JSON: a interface HTML do DRF (Browsable API) fica desligada, e um `Accept` que não admite JSON recebe 406 com corpo JSON (#230).


## Concorrência e erros

O GET de detalhe retorna um ETag forte `"<revision>"`, o marcador exato da versão lida. Para PATCH, DELETE e restore, copie esse valor **com as aspas** para `If-Match`; criação por POST não exige revisão prévia. O servidor usa CAS (compare-and-swap): só grava se a revisão atual ainda for a que você leu. Assim, duas alterações da mesma versão não sobrescrevem silenciosamente uma à outra. Formulários HTML enviam uma revisão escondida e respondem 409 quando ela está desatualizada; a API usa 412.

| Resultado | HTTP/code |
|---|---|
| Create/update/delete lógico/restore | 201 / 200 / 204 / 200 |
| If-Match ausente | 428 `revision_required` |
| Formato If-Match inválido | 400 |
| Revisão stale | 412 `revision_conflict` |
| Inexistente/invisível/outro tenant | 404 |
| Tabela/ação negada | 403 |
| Admin sem enrollment obrigatório | 403 `mfa_required` |
| Validação/mass assignment | 400 |
| Unicidade/referência viva/conflito de último admin | 409 |

Use os códigos para decidir a próxima ação, não como motivo para repetir cegamente a escrita. 428 pede a revisão obtida no GET; 412 pede reler o estado e conciliar a alteração com o usuário. 400 exige corrigir o corpo ou formato do header. 409 indica que a operação viola uma regra de integridade e não foi aplicada. 403 exige resolver autenticação, MFA ou permissão conforme o erro; 404 não distingue registro inexistente de registro oculto, evitando revelar dados de outra organização. Em sucesso, 201 indica criação, 200 traz a representação atualizada e 204 confirma exclusão lógica sem corpo.

Erros JSON `{code,detail,fields?}`; X-Request-ID é gerado no servidor, sem traceback/SQL/segredo. Não use UUID do cliente como ator. Revisões são controle de concorrência, não grants.

Criação/reenvio de convite confirma persistência, não entrega SMTP. Falha pós-commit conserva 201 e acrescenta `X-Email-Delivery: failed`; use resend com a revisão atual para gerar nova prova e invalidar a anterior. Audit de negócio apresenta somente fields de leitura do TableSpec; captura persistida não é descartada para filtrar a representação.


## Curl com sessão verdadeira

Faça login no navegador em `/accounts/login/`, incluindo confirmação/MFA quando exigidos. Para curl, copie **privadamente** os valores de sessionid e csrftoken dessa sessão; não os publique/logue/versione. Alternativamente exporte cookies para jar privado. Não use force_login como smoke de autenticação.

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
  --data '{"display_name":"Alice","timezone":"America/Sao_Paulo"}' "$BASE/api/v1/me/"

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

Os exemplos pressupõem a app Notes instalada, habilitada nessa organização e as permissões de tabela necessárias. O primeiro GET lista organizações acessíveis; `me` demonstra uma revisão composta; o POST cria uma nota privada; o GET seguinte obtém a revisão da nota para o PATCH. Repetir o PATCH com a revisão antiga deve retornar 412: releia e decida como conciliar, sem sobrescrever automaticamente. Para compartilhar a nota, use a operação de acesso separada:

```sh
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
  -H "Referer: $BASE/" --data '{"visibility":"organization","owning_group":null}' \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/$NOTE/access/"
```

`ETAG` inclui aspas do header atual. Releia após cada mutação antes de DELETE/restore. Em HTTPS use URL/origin/referer HTTPS exatos. Cookies/recovery codes não pertencem ao histórico de shell nem arquivos compartilhados. API não oferece export global de Users, hard delete ou bypass de tenant.

## Fluxos de processo

Tarefas e instâncias dos [fluxos de processo](flows.md) ficam em `o/<org>/flows/`. As views só chamam o [`FlowService`](service-api.md#flowservice), que autoriza cada chamada. Só sessão: um token de API recebe 403 `token_not_allowed`.

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `o/<org>/flows/tasks/` | GET | Caixa de tarefas do ator; `scope=mine` (padrão), `available` (filas) ou `all`; `status` só com `all` |
| `o/<org>/flows/tasks/<id>/` | GET | Tarefa com campos do formulário, ações e dados do processo; ETag com a revisão da tarefa |
| `o/<org>/flows/tasks/<id>/claim/`, `release/` | POST | Assumir ou devolver uma tarefa de fila; corpo vazio |
| `o/<org>/flows/tasks/<id>/reassign/` | POST | `{"to_user": "<uuid>", "comment": "..."}` |
| `o/<org>/flows/tasks/<id>/complete/` | POST | `{"action": "<chave>", "data": {campo: valor}, "comment": "..."}` |
| `o/<org>/flows/tasks/<id>/decide/` | POST | `{"decision": "approved" \| "rejected", "data": {...}, "comment": "..."}` |
| `o/<org>/flows/instances/` | GET | Instâncias visíveis; filtros exatos `flow_key`, `subject_id`, `status` |
| `o/<org>/flows/instances/<id>/` | GET | Instância com etapas ativas e dados não sensíveis; ETag com a revisão da instância |
| `o/<org>/flows/instances/<id>/history/` | GET | Histórico ordenado |
| `o/<org>/flows/instances/<id>/cancel/`, `suspend/`, `resume/`, `retry/` | POST | Administração da instância; `cancel` e `suspend` aceitam `{"reason": "..."}` |

- As listas usam cursor: `{"results": [...], "next_cursor": "..." | null, "page_size": 25}`; envie `cursor` e `page_size` (até 100).
- Todo POST exige `If-Match` com a revisão lida no GET: da **tarefa** nas ações de tarefa, da **instância** nas demais. Sem o header, 428; revisão antiga, 412; nada é escrito.
- Tarefa ou instância inexistente, de outra organização, não visível ou de app desativada responde 404. Visível, mas sem direito à ação, 403. Formulário inválido, 400 com `fields`. Estado incompatível (instância suspensa, tarefa encerrada), 409 com o código do conflito.
- Campo não declarado no formulário da etapa ou da transição é recusado, como no gateway. Ator, organização, responsável e revisão nunca vêm do corpo.
- Iniciar uma instância não tem rota aqui: é operação da app dona do fluxo.
- Todas as rotas estão no [schema OpenAPI](#schema-openapi), cada ação com o próprio caminho literal. Os corpos de resposta são gerados dos DTOs do `FlowService` (`skeleton/flows/api_views.py`), e os comandos documentam `If-Match` (412/428) e `ETag`.

## Schema OpenAPI

`GET /api/v1/openapi.json` devolve o contrato da API em [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0), só em JSON e sem página interativa ([#178](https://github.com/alaorneto/skeleton/issues/178)). Ele aceita sessão ou [token](#autenticação-por-token); sem nenhum dos dois, a resposta é 401 ou 403. O documento é gerado do código a cada requisição (`skeleton/api/openapi.py`), nunca mantido à mão:

- **Gateway:** cada `TableSpec` exposto vira caminhos concretos, como `/api/v1/o/{organization_id}/tables/example_notes.note/records/`. As propriedades são os campos sistêmicos e `read_fields`; o corpo de POST/PATCH, os `write_fields`. Os tipos vêm do `_meta` do modelo, pelo mesmo mapeamento da especificação congelada (`field_internal_type`): relações são UUID, e numa leitura do gateway aceitam `null` (referência invisível). Os filtros de igualdade da lista e os pickers de `references/` também aparecem.
- **Views dedicadas:** cada view de `/api/v1/` declara um atributo `openapi`, um mapa de método HTTP para `Operation` (resumo, schemas de corpo e resposta, paginação, ETag, `If-Match`, capacidade exigida). Isso vale para as rotas de fora de `skeleton/api/urls.py`, como notificações, apps, webhooks e tokens. As rotas vêm da árvore de URLs real, então uma view de app sob `api/v1/o/<org>/apps/<label>/` aparece onde está montada. Uma view sem `openapi` ainda aparece, com corpo livre e `x-skeleton-undocumented: true`.
- **Componentes:** `Error` (`{code, detail, fields}`, também a resposta `default` de toda operação), `Page` e os parâmetros `page`/`page_size`, o header `ETag`, o parâmetro `If-Match` e as respostas `PreconditionFailed` (412) e `PreconditionRequired` (428). Os esquemas de segurança são `bearerAuth` e `sessionAuth`; cada operação lista `bearerAuth` só quando a view aceita token naquele método (`token_methods`).

O schema descreve só o que o ator alcança. O servidor lê cada organização ativa do ator numa transação própria e somente leitura, como as [rotas agregadas](concepts.md#autorização-e-contexto) (`for_each_organization`), e junta o resultado:

- uma tabela aparece só com TablePermission do ator, e cada operação só com a ação correspondente (`read` para GET, `create` para POST, e assim por diante); app desativada esconde as tabelas e as rotas dela;
- uma permissão da organização B nunca aparece para quem não é membro de B: nada de outra organização é lido;
- operações administrativas aparecem só com a capacidade exigida (`access.manage`, `integrations.manage`, administrador da organização);
- com token, só os métodos de `token_methods` e as ações dos escopos: um token `example_notes.note:read` vê apenas os GET daquela tabela, nenhuma administração e nenhum endpoint de tokens; um token vinculado a uma organização lê só essa organização;
- `sensitive_fields` nunca aparecem, nem pelo nome: não são propriedades, filtros nem pickers. Quando um campo sensível está em `read_fields`, a resposta real o traz e o objeto do schema fica aberto (sem `additionalProperties: false`), de modo que o campo não é nomeado; nos demais objetos, propriedades fora do schema são proibidas.

O documento muda com permissões, apps e versões: não o grave como contrato fixo. A resposta leva `Cache-Control: private, no-store`. Apps documentam as próprias views da mesma forma (importe `Operation` e os construtores de `skeleton.api.openapi`, uma superfície pública do núcleo):

```python
from skeleton.api.openapi import INTEGER, Operation, nullable, obj, string
from skeleton.api.views import TenantAPI


class SummaryView(TenantAPI):
    openapi = {"GET": Operation("Resumo das anotações", obj({"count": INTEGER, "heading": nullable(string())}))}
```

A suíte do núcleo valida toda resposta real de `/api/v1/` contra esse schema com `skeleton.testing.openapi` ([evidência dos testes](testing-evidence.md#openapi-da-api-epic-86)).

## Tokens de API

Além da sessão, a API aceita tokens de API (epic #79). Esta seção descreve o token em si (#45); a autenticação nas requisições está em [Autenticação por token](#autenticação-por-token).

| Item | Regra |
|---|---|
| Formato | `skt_<prefixo>_<segredo>`: prefixo de 8 caracteres hexadecimais (único, localiza a linha) e segredo de 43 caracteres (`secrets.token_urlsafe(32)`, 256 bits) |
| Armazenamento | `sk_integrations.ApiToken` (`SystemRecord` global) guarda só o prefixo e o SHA-256 do segredo (`digest`, campo sensível, redigido na auditoria). O texto do token aparece **uma vez**, na emissão, e não pode ser recuperado |
| Tipos | `personal` (da própria pessoa, vale em todas as organizações dela), `service` (de uma conta de serviço, preso à organização da conta) e `feed` (só escopos de feed, como a assinatura da agenda) |
| Validade | Obrigatória e no futuro, até `SKELETON_API_TOKEN_MAX_DAYS` dias (padrão 365, no catálogo da instância). Validade ausente, passada ou acima do limite é recusada (400) |
| Escopos | `<table_key>:<ação>` para uma tabela exposta no gateway (`expose_api`) e `read`, `create`, `update`, `delete` ou `restore`; ou um escopo nomeado registrado em código (`register_token_scope`). Escopo desconhecido, de tabela não exposta ou de feed num token comum é recusado (400). O escopo só **restringe** o papel da pessoa: nunca concede permissão que ela não tenha |
| Emissão | Pela própria pessoa autenticada (pessoal ou feed) ou por quem tem `integrations.manage` na organização da conta de serviço. Com `SKELETON_REQUIRE_ADMIN_MFA`, exige MFA cadastrada. Contas de serviço não emitem tokens pessoais, e um token nunca emite outro token |
| Revogação | `revoked_at` e soft delete, com `If-Match`/revisão. Quem não pode gerir o token recebe o mesmo "inexistente" de um token que não existe |
| Invalidação | Um token deixa de valer na próxima requisição quando expira, é revogado, a pessoa é desativada ou excluída, ou o `auth_version` dela muda (troca de senha, desativação, revogação de sessões) |

Os serviços ficam em `skeleton.integrations.tokens.services`: `issue_personal_token`, `issue_service_token`, `revoke_token`, `personal_tokens`, `service_tokens` e `verify_token`. Erros de validação são `ValidationError` por campo (`scopes`, `expires_at`, `name`); a falta de capacidade é `PermissionDenied`.

## Autenticação por token

Em `/api/v1/`, `Authorization: Bearer skt_...` autentica a requisição como a pessoa (ou conta de serviço) do token (#46). As rotas HTML ignoram o cabeçalho.

| Situação | Resposta |
|---|---|
| Token válido numa view que aceita o método | A requisição roda como o usuário do token, **sem CSRF**; o contexto guarda os escopos, e o `skeleton.source` (GUC e auditoria) é `http:token:<prefixo>` |
| Sessão e Bearer na mesma requisição | 400 `ambiguous_credentials` |
| Token malformado, desconhecido, expirado, revogado, de usuário inativo, com `auth_version` mudado ou de feed (`kind=feed`, que só abre as views de feed) | 401 `invalid_token`, com `WWW-Authenticate: Bearer realm="api", error="invalid_token"`, e `security_event("api_token.rejected")` sem o token |
| View sem o método em `token_methods` | 403 `token_not_allowed` |
| Escopo ausente para a tabela e ação | 403, mesmo que o papel permita |
| Token de conta de serviço (preso à organização) na URL de outra organização | 404 |
| Pessoa sem vínculo na organização da URL | 404, como para a sessão |

Views que aceitam token, com os métodos:

| Recurso | Métodos | Escopo |
|---|---|---|
| `me/` | GET (nunca PATCH) | nenhum |
| `organizations/`, `o/<org>/membership/`, `o/<org>/catalog/` | GET | nenhum |
| `o/<org>/tables/<tabela>/records/`, `<id>/` | GET, POST, PATCH, DELETE | `<tabela>:read`, `create`, `update`, `delete` |
| `.../records/<id>/restore/` | POST | `<tabela>:restore` |
| `o/<org>/tables/<tabela>/references/<campo>/` | GET | `read` da tabela referenciada |
| `.../records/<id>/attachments/` | GET, POST | `files:read` ou `files:write`, além do `read`/`update` do registro que o serviço já exige |
| `.../attachments/<id>/`, `.../restore/` | DELETE, POST | `files:write` |
| `.../records/<id>/comments/`, `.../comments/<id>/`, `.../comments/<id>/restore/` | GET, POST, PATCH, DELETE | Só o `read` do registro que o serviço já exige; edição, exclusão e restauração com `If-Match` ([comentários](comments.md)) |

Todo o resto (vínculos, papéis, grupos, concessões, convites, ativação de apps, webhooks, auditoria, saída da organização e a gestão de tokens) só aceita sessão. Uma view de app que estende `SessionAPI` também só aceita token se declarar `token_methods`.

```sh
# TOKEN vem da tela de tokens e fica só no ambiente local; não o coloque em histórico ou log.
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json'   -d '{"code":"n1","title":"Nota","body":"texto"}' "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/"
```

## Gestão de tokens

A gestão de tokens (#47) só aceita sessão: um token não emite, lista nem revoga tokens (403 `token_not_allowed`).

| Interface | Quem | O que faz |
|---|---|---|
| `/profile/tokens/` | A própria pessoa | Lista os tokens pessoais e de feed dela (prefixo, nome, escopos, validade), cria um token com nome, escopos e validade em dias e revoga. Contas de serviço não têm esta página |
| `/o/<org>/integrations/service-accounts/` e `<conta>/` | `integrations.manage` (404 para os demais) | Cria e desativa contas de serviço, lista e emite os tokens de cada uma e os revoga |
| `GET`, `POST /api/v1/me/tokens/`; `DELETE /api/v1/me/tokens/<id>/` | A própria pessoa | Mesmo que a página. `POST` recebe `name`, `scopes` e `expires_at` (ISO 8601 com fuso) |
| `GET`, `POST /api/v1/o/<org>/service-accounts/`; `DELETE .../<conta>/` | `integrations.manage` | Lista e cria contas (`{"name": ...}`); `DELETE` desativa a conta |
| `GET`, `POST .../service-accounts/<conta>/tokens/`; `DELETE .../tokens/<id>/` | `integrations.manage` | Tokens da conta |

- Criar exige autenticação recente (`reauthentication_required`, 403, na API; a página pede a reautenticação) e, com `SKELETON_REQUIRE_ADMIN_MFA`, MFA cadastrada (`mfa_required`).
- A resposta da criação (página ou `201`) é a única que traz o texto do token, com `Cache-Control: no-store`. As listas e o resto da API nunca trazem o segredo nem o digest.
- Revogar exige `If-Match` com a revisão (`428` sem ele, `412` desatualizado). Um token de outra pessoa ou de outra organização responde 404, como um inexistente.
- Desativar a conta de serviço invalida todos os tokens dela na hora (`auth_version`).

## Limites e registro de uso dos tokens

Os limites (#48) usam o cache compartilhado (`skeleton.common.ratelimit`, janela fixa de 60 s). Em produção o cache é Redis; com `DEBUG=False` e cache local, `manage.py check --deploy` avisa (`skeleton.W029`), porque cada worker contaria sozinho.

| Limite | Padrão | Ao passar |
|---|---|---|
| Requisições por token (`SKELETON_API_TOKEN_RATE_PER_MINUTE`) | 600 por minuto | 429 `rate_limited` com `Retry-After`, antes da view: nada é gravado |
| Falhas de autenticação por token, por IP (`SKELETON_API_TOKEN_FAILURES_PER_MINUTE`) | 20 por minuto | 429 com `Retry-After` para qualquer Bearer desse IP, sem consultar o banco (força bruta de prefixos e segredos) |

O IP vem da mesma topologia de proxies do allauth (`DJANGO_TRUSTED_PROXY_COUNT` ou `DJANGO_TRUSTED_CLIENT_IP_HEADER`). Se o cache cair, as requisições passam: o limitador freia abuso e não é a autorização. Cada 429 gera `security_event("api_token.rate_limited")`.

O último uso de cada token fica em `sk_integrations.ApiTokenActivity`, uma tabela operacional: gravada no máximo uma vez a cada 5 minutos por token (marcador no cache e janela no banco), sem revisão e **sem `AuditEvent`**. As listas de tokens e a API mostram `last_used_at`. Não há, por ora, limite geral da API por usuário de sessão.
