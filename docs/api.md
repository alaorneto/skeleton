# API REST de sessão

Esta API usa o prefixo `/api/v1/` e a mesma identidade das páginas HTML. Faça login pelo allauth para obter uma sessão; o cookie `sessionid` identifica essa sessão mantida no servidor. `SessionAuthentication` e `IsAuthenticated` são os únicos mecanismos: não há Basic, JWT nem endpoint independente de senha. A autenticação identifica a pessoa, mas cada operação ainda verifica vínculo, organização e permissões. Veja [autorização e contexto](concepts.md#autorização-e-contexto).

Métodos que podem mudar estado, como POST, PATCH, PUT e DELETE, precisam do cookie de sessão **e** da proteção CSRF. Em clientes como curl, envie o cookie `csrftoken` e o header `X-CSRFToken` correspondente; um cookie de sessão sozinho não basta. Uma requisição anônima à API recebe 403 do DRF, enquanto a página HTML redireciona ao login. Em produção, use HTTPS e cookies seguros; tokens do IdP não substituem essa sessão.

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
| `o/<org>/tables/<table_key>/references/<field>/` | GET | Picker limitado/paginado, com autorização da origem e alvo; não abre tabela livre |
| `o/<org>/invitations/<id>/resend/` | POST | Org admin, corpo `{}` e If-Match; revoga convite antigo e retorna novo UUID/ETag/token só por email |
| `o/<org>/apps/`, `apps/<app_label>/configuration/` | GET; GET/PUT configuração | Org admin; app_id/version declarados, enabled/configuration; If-Match `"0"` para estado virtual inicial |

`org`, `sysid` e IDs de relações são UUIDs. Primeiro escolha uma organização devolvida por `organizations/` e use seu UUID em `o/<org>/`; não envie a organização ou o ator no corpo para tentar mudar o contexto. `table_key` é o identificador registrado do modelo (`label_lower`), por exemplo `example_notes.note`, e nunca um nome SQL. O registry, a lista explícita de modelos permitidos no código, é a única fonte dessa resolução. Modelos não expostos ou técnicos não entram no gateway.

Instalação física não é recurso do gateway REST. A plataforma usa `/admin/system/releases/` e subrotas `upload/`, `preview/`, `fetch/`, `operations/<uuid>/`: sessão, CSRF, MFA quando exigido e reautenticação recente para mutações. Aprovação vincula revisão, digest e geração ao plano do agente; GET não instala código. `install`, `update`, `uninstall`, `reinstall` e `core_update` são operações globais. Durante manutenção, o admin fica fechado; acompanhamento usa canal independente com ticket privado, por operação, válido por cinco minutos. Remoção de dados/schema não é oferecida por HTTP.

Campos de links: member-roles={membership,role}; group-members={group,membership}; group-roles={group,role}; table-permissions={role,table,action}; record-grants={table,record_sysid,membership,group,role,action}, com exatamente um sujeito não nulo. Table é UUID do catálogo, não nome SQL. Todas relações são resolvidas/autorizadas same-tenant e vivas. TablePermission positiva da ação é obrigatória mesmo com grant.

## `me` composto

GET retorna sysid,email,first_name,last_name,revision e profile={sysid,revision,display_name,timezone,language}. PATCH só aceita campos planos first_name,last_name,display_name,timezone,language; atualiza User/Profile atomicamente. Email/senha/is_staff/is_superuser/auth_version/local_password_allowed nunca são campos desse recurso.

O ETag é `"<user_revision>-<profile_revision>"`, por exemplo `"1-1"`, porque a resposta reúne duas linhas: User e Profile. O PATCH exige as duas revisões em `If-Match` e as compara dentro da mesma transação. A revisão do perfil sozinha não protegeria o nome da identidade de uma alteração concorrente.

## Gateway

O gateway oferece CRUD genérico (criação, leitura, alteração e exclusão lógica) somente para campos explicitamente declarados em `TableSpec.read_fields` e `write_fields`; os metadados sistêmicos são automaticamente somente leitura. Campos desconhecidos retornam erro em vez de serem ignorados. `organization`, `sysid`, autoria, revisão e campos de acesso são proibidos no POST/PATCH comum: aceitar esses valores do cliente permitiria atribuição indiscriminada de campos protegidos (mass assignment). A criação atribui `owner` ao ator autenticado e visibilidade `private`. Use o recurso separado `access/` para mudar acesso: seu PATCH aceita somente owner UUID, visibility e owning_group UUID/null, coerentes com vínculo e grupo ativos. Ele exige `manage_access` e ser proprietário ou administrador da organização; um leitor comum não pode nem obter GET access.

FKs de negócio usam UUID resolvido em queryset legível da mesma org, nunca nested representação que revele privado/deletado. Lista aceita filtros exatos sysid/owner/visibility e campos escalares read_fields elegíveis. Não aceita contains/regex/dot path/SQL/query DSL. Ordena created_at/sysid; paginação `{count,page,page_size,results}`, tamanho 25, máximo 100. Conte/count/lista/audit são autorizados antes de paginação.

Referências invisíveis são representadas como null, sem UUID/label e sem `__str__` de negócio. A projeção da lista autoriza alvos em lote por política/modelo. O recurso `references/<field>/` aceita somente `record`, `access=0/1`, `page` e `page_size`, sem duplicatas. Sem `record`, exige create na origem; com registro, update; com access, manage_access. Retorna `{count,page,page_size,results}` de UUID/label autorizados; máximo 100. Referências de identidade/acesso usam vínculos ativos/resolvedores dedicados, nunca enumeração global. HTML tem seletor equivalente fora do admin e fallback para copiar UUID; toda escrita revalida FK.

Apps podem registrar `RecordOperations` em código para impor serviços próprios nas mesmas operações HTML/API. Core valida autorização/allowlists/coerção antes do serviço; se um adaptador não declara uma operação, ela é negada sem fallback. Metadados de apresentação não mudam o contrato JSON ou grants. Nullable boolean mantém null quando o modelo o permite; write-only nunca é acrescentado à leitura pela presença de um widget.

Somente JSON é aceito na API. `SKELETON_JSON_MAX_BYTES` limita bytes efetivamente lidos antes da decodificação (default 1048576), inclusive stream sem Content-Length confiável; excesso é 413 `request_too_large`, sem escrita parcial. Alinhe um limite de corpo igual ou menor no proxy. Paginação HTML preserva filtros já validados ao trocar page, com URL encoding.


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
