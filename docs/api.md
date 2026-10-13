# API REST de sessão

Prefixo `/api/v1/`. Autenticação exclusivamente SessionAuthentication + IsAuthenticated; sem Basic, JWT ou endpoint de senha independente. Obtenha sessão pelo login HTML allauth. Requests unsafe precisam cookie de sessão **e** CSRF; sessão anônima recebe 403 DRF. HTML anônimo redireciona login. HTTPS/cookies secure em produção.

## Recursos

| URL relativa ao prefixo | Métodos | Contrato |
|---|---|---|
| `me/` | GET, PATCH | Própria identidade/perfil; ETag composto |
| `organizations/` | GET | Apenas organizações dos próprios vínculos ativos |
| `o/<org>/catalog/` | GET | Apenas tabelas legíveis; não inventário técnico global |
| `o/<org>/audit/`, `audit/<event>/` | GET | Eventos autorizados; filtros record_sysid/model_label na lista |
| `o/<org>/memberships/`, `<id>/` | GET, POST; GET/PATCH/DELETE no detalhe | Org admin; criar com user explícito; PATCH só is_active |
| `o/<org>/roles/`, `<id>/` | GET, POST; GET/PATCH/DELETE | code,name,description; is_builtin read-only |
| `o/<org>/groups/`, `<id>/` | GET, POST; GET/PATCH/DELETE | code,name,description |
| `o/<org>/invitations/`, `<id>/` | GET, POST; GET/DELETE | email,role; digest/token/estado não graváveis; sem PATCH |
| `o/<org>/member-roles/`, `group-members/`, `group-roles/`, `table-permissions/`, `record-grants/` | GET/POST coleção; GET/DELETE detalhe | Links explícitos; sem nested write/PATCH |
| `o/<org>/tables/<table_key>/records/` | GET, POST | Gateway allowlist de SecuredRecord exposto |
| `.../records/<sysid>/` | GET, PATCH, DELETE | Um registro autorizado; ETag/revisão |
| `.../records/<sysid>/restore/` | POST | Restore autorizado com If-Match |
| `.../records/<sysid>/access/` | GET, PATCH | Manage_access + ownership/org admin; If-Match no PATCH |
| `o/<org>/invitations/<id>/resend/` | POST | Org admin, corpo `{}` e If-Match; revoga convite antigo e retorna novo UUID/ETag/token só por email |
| `o/<org>/apps/`, `apps/<app_label>/configuration/` | GET; GET/PUT configuração | Org admin; app_id/version declarados, enabled/configuration; If-Match `"0"` para estado virtual inicial |

`org`, sysid e IDs de relações são UUID. `table_key` é label_lower, por exemplo `example_notes.note`, resolvido exclusivamente no registry. Modelos não expostos/técnicos nunca entram no gateway.

Instalação física não é recurso do gateway REST. A plataforma usa `/admin/system/releases/` e subrotas `upload/`, `preview/`, `fetch/`, `operations/<uuid>/`: sessão, CSRF, MFA quando exigido e reautenticação recente para mutações. Aprovação vincula revisão, digest e geração ao plano do agente; GET não instala código. `install`, `update`, `uninstall`, `reinstall` e `core_update` são operações globais. Durante manutenção, o admin fica fechado; acompanhamento usa canal independente com ticket privado, por operação, válido por cinco minutos. Remoção de dados/schema não é oferecida por HTTP.

Campos de links: member-roles={membership,role}; group-members={group,membership}; group-roles={group,role}; table-permissions={role,table,action}; record-grants={table,record_sysid,membership,group,role,action}, com exatamente um sujeito não nulo. Table é UUID do catálogo, não nome SQL. Todas relações são resolvidas/autorizadas same-tenant e vivas. TablePermission positiva da ação é obrigatória mesmo com grant.

## `me` composto

GET retorna sysid,email,first_name,last_name,revision e profile={sysid,revision,display_name,timezone,language}. PATCH só aceita campos planos first_name,last_name,display_name,timezone,language; atualiza User/Profile atomicamente. Email/senha/is_staff/is_superuser/auth_version/local_password_allowed nunca são campos desse recurso.

ETag `"<user_revision>-<profile_revision>"`; PATCH exige ambas e compara dentro do atomic. Revisão do perfil sozinha não protege o nome da identidade.

## Gateway

Read/write fields explícitos do TableSpec; metadata sistêmica é read-only automática. Unknown fields retornam erro, não são ignorados. `organization`, `sysid`, autoria, revisão e access fields são proibidos no POST/PATCH comum. Criação atribui owner=ator/private. Access PATCH aceita só owner UUID, visibility e owning_group UUID/null, coerentes com Membership/grupo vivos. Leitor comum não pode obter GET access.

FKs de negócio usam UUID resolvido em queryset legível da mesma org, nunca nested representação que revele privado/deletado. Lista aceita filtros exatos sysid/owner/visibility e campos escalares read_fields elegíveis. Não aceita contains/regex/dot path/SQL/query DSL. Ordena created_at/sysid; paginação `{count,page,page_size,results}`, tamanho 25, máximo 100. Conte/count/lista/audit são autorizados antes de paginação.

Somente JSON é aceito na API. `SKELETON_JSON_MAX_BYTES` limita bytes efetivamente lidos antes da decodificação (default 1048576), inclusive stream sem Content-Length confiável; excesso é 413 `request_too_large`, sem escrita parcial. Alinhe um limite de corpo igual ou menor no proxy. Paginação HTML preserva filtros já validados ao trocar page, com URL encoding.


## Concorrência e erros

GET detalhe retorna ETag forte `"<revision>"`. PATCH, DELETE e restore requerem If-Match; POST criação não. Form HTML usa revisão escondida e stale →409.

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

Repita PATCH com a revisão antiga: esperado 412; não reutilize resposta stale para sobrescrever automaticamente. Access separado:

```sh
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H 'Content-Type: application/json' -H "If-Match: $ETAG" \
  -H "Referer: $BASE/" --data '{"visibility":"organization","owning_group":null}' \
  "$BASE/api/v1/o/$ORG/tables/example_notes.note/records/$NOTE/access/"
```

`ETAG` inclui aspas do header atual. Releia após cada mutação antes de DELETE/restore. Em HTTPS use URL/origin/referer HTTPS exatos. Cookies/recovery codes não pertencem ao histórico de shell nem arquivos compartilhados. API não oferece export global de Users, hard delete ou bypass de tenant.
