# Arquivos e anexos

O núcleo armazena arquivos exclusivamente em **armazenamento privado** (`skeleton/files`, label `sk_files`), sem URLs públicas diretas: os bytes só são entregues por views que validam a autorização a cada requisição.

## Armazenamento privado

`STORAGES["skeleton_files"]` é configurado em `skeleton/conf/base.py` e nos perfis de ambiente:

| Variável | Efeito |
|---|---|
| `SKELETON_FILES_BACKEND=local` (padrão) | Usa `FileSystemStorage` em `SKELETON_FILES_ROOT` (padrão `var/files` do projeto), sem `base_url` |
| `SKELETON_FILES_BACKEND=s3` | Usa `storages.backends.s3.S3Storage` com `default_acl=private`, URLs pré-assinadas de no máximo 60 s e sem sobrescrita. Exige o extra opcional `django-skeleton[s3]` (`django-storages` e `boto3`) e `SKELETON_FILES_S3_BUCKET` (com `SKELETON_FILES_S3_REGION` e `SKELETON_FILES_S3_ENDPOINT` opcionais) |
| `SKELETON_UPLOAD_MAX_BYTES` | Limite máximo de qualquer corpo multipart na API e nas páginas (padrão 10 MB), aplicado pelo upload handler `skeleton.common.uploads.BoundedUploadHandler` antes de gravar em memória ou disco. O upload de pacotes da plataforma usa `SKELETON_PACKAGE_MAX_BYTES` |
| `SKELETON_FILES_ORG_QUOTA_BYTES` | Cota padrão para a soma de anexos vivos de uma organização (vazio significa ilimitado). Acima desse limite, o envio responde 409 `quota_exceeded`. A plataforma pode sobrescrever esse valor por organização (veja [Cota por organização](#cota-por-organização)) |

O módulo `skeleton/files/storage.py` expõe `put_stream`, `open_object`, `delete`, `presigned_url` e `list_prefix`. As chaves seguem sempre o padrão `org/<organização>/<anexo>`, usando dois UUIDs: o nome original enviado pelo usuário nunca entra na chave, evitando travessia de diretórios, colisões ou vazamento de informação em URLs. `put_stream` grava na chave exata, recusa sobrescrever objetos existentes e interrompe a gravação caso o tamanho exceda o limite.

A medição e a limitação de tamanho em `put_stream` acontecem durante a leitura (`read()`), caminho comum a todos os backends — o armazenamento local lê via `File.chunks()`, enquanto o S3 do `django-storages` lê diretamente por `read()`, sem chamar `chunks()`. Tamanho gravado, hash e reserva de cota são calculados exclusivamente a partir dos bytes lidos, nunca pelo cabeçalho `Content-Length` enviado pelo cliente; arquivos acima do limite são recusados (`ObjectTooLarge`, 413) sem deixar objeto gravado. Um storage de teste que grava como o S3 (`tests/support.py`, `ReadBasedStorage`) exercita esse caminho.

O check de deploy `skeleton.E024` (`manage.py check --deploy`) reprova armazenamento ausente, backend local sem `location`, dentro de `STATIC_ROOT` ou com `base_url`, bem como backend S3 com ACL pública, URL sem assinatura ou domínio público.

**Marca pública do endereço da organização.** É uma das duas exceções em que bytes do armazenamento privado saem sem autenticação (a outra é a marca da instância, abaixo). Quando o administrador ativa o endereço próprio da organização ([privacidade](privacy.md#endereço-próprio-da-organização)), `/site/logo/` serve o logo **exclusivamente no host da organização**, usando apenas a chave registrada no índice `OrganizationSite` por `sync_site`. A chave precisa estar sob `org/<uuid da organização do host>/branding/`, o tipo do arquivo é validado novamente pelo conteúdo (`LOGO_TYPES`), e a resposta inclui `Cache-Control: public, max-age=300`, `nosniff` e CSP `sandbox`. No domínio principal, a rota retorna 404; nas páginas autenticadas `/o/<org>/`, o logo continua sendo servido por `/o/<org>/settings/logo/`.

O ícone da organização segue as mesmas regras: é chave viva para `files.orphans` (que preserva o `icon_key` atual e remove o anterior após o prazo de carência, assim como o logo), fica acessível publicamente apenas por `/site/icon/` no host da organização e pela chave do índice, aceitando somente PNG. `/favicon.ico` redireciona para esse ícone no host da organização e para o favicon padrão nos demais hosts. Nenhum outro arquivo de `skeleton/files` fica exposto dessa forma.

**Marca da instância.** O logo, o logo para modo escuro e o ícone da instância ([aparência](appearance.md#imagens)) ficam sob `platform/branding/<uuid4>`, único formato de chave fora de `org/<organização>/` aceito por `skeleton/files/storage.py` (em `KEY` e `list_prefix`). Como são públicos por decisão do administrador da plataforma, `/brand/logo/`, `/brand/logo-dark/` e `/brand/icon/` servem, sem autenticação e em qualquer host, apenas as três chaves da linha única `InstanceBrand`, revalidando o tipo pelo conteúdo e enviando os mesmos cabeçalhos (`public, max-age=300`, `nosniff`, CSP `sandbox`). Não há parâmetro de caminho, e chaves `platform/branding/…` nunca são servidas pelas rotas de organização (`open_site_image` exige o prefixo da organização). Objetos órfãos desse prefixo são limpos pelo job global `appearance.orphans`, não por `files.orphans`.

## Locais de armazenamento da plataforma

Em uma instância de produto, os arquivos de cada organização podem ser direcionados para um **local de armazenamento** dedicado — como um bucket S3 cobrado por GB à parte — escolhido pela plataforma e invisível para a organização ([#704](https://github.com/alaorneto/skeleton/issues/704), [#705](https://github.com/alaorneto/skeleton/issues/705), [#706](https://github.com/alaorneto/skeleton/issues/706); [configurações geridas pela plataforma](managed-settings.md)).

- **Catálogo (`StorageLocation`, `SystemRecord` global).** Em `/platform/storage/`, o administrador da plataforma cadastra, testa, edita e desativa locais: backends S3 compatíveis (`bucket_name`, `region_name`, `endpoint_url` apenas `https://` e sem credenciais na URL, `addressing_style`, `key_prefix`, `access_key_id` e `secret_access_key`) ou, exclusivamente nos perfis de desenvolvimento e teste (`SKELETON_FILES_DIRECTORY_LOCATIONS`), subdiretórios sob `SKELETON_FILES_LOCATIONS_ROOT`. A chave secreta é cifrada com o keyring `FILES_STORAGE_ENCRYPTION_KEYS` (salvar um local S3 sem esse keyring é recusado, e uma chave inválida reprova a partida com `skeleton.E026`), aparece como `[REDACTED]` na auditoria (`access_key_id` e `secret_access_key` são `sensitive_fields`) e nunca é exibida; ao editar, deixar o campo vazio mantém a chave gravada. No máximo um local pode ser marcado como **padrão** para novas organizações. A ação "Testar conexão" executa um `head_bucket` com timeout de 10 s (ou grava e remove um arquivo no diretório) e informa apenas sucesso ou erro genérico, sem expor a resposta do servidor. Todos os backends S3 são sempre privados (`default_acl=private`, URLs assinadas de no máximo 60 s, sem domínio público), e `key_prefix` entra como `location` do backend, preservando as chaves lógicas.
- **Atribuição por organização (`OrganizationStorage`).** Uma linha global associa a organização a um local; na ausência dela, vale o armazenamento padrão da instância. `skeleton/files/storage.py` resolve o backend a partir da chave: `storage_for_key("org/<uuid>/...")` seleciona o local da organização (`storage_for_organization`), enquanto `platform/...` usa sempre o armazenamento da instância (`private_storage()`, usado também no setup e em organizações sem local atribuído). Como `put_stream`, `open_object`, `delete`, `presigned_url`, `list_prefix` e `files.orphans` passam por essa resolução, anexos, logo, ícone e arquivos de exportação e importação CSV da organização ficam todos no local atribuído. O mapeamento fica em cache por processo por 60 s (nós distintos não compartilham memória) e é invalidado no processo que grava um local ou uma atribuição. `X-Accel-Redirect` e `X-Sendfile` aplicam-se apenas ao armazenamento local da instância; objetos de outros locais saem por URL pré-assinada ou em stream.
- **Troca e desativação de local.** Na página da organização (`/platform/organizations/<id>/`, linha "Local de armazenamento"), `assign_storage` permite trocar o local apenas enquanto a organização não possui arquivos: qualquer anexo ainda não expurgado, uso registrado em `StorageUsage` ou objeto remanescente sob os prefixos da organização recusa a troca com `storage_in_use`. Arquivos nunca são migrados automaticamente entre locais, e desativar um local em uso também é recusado.
- **Na criação da organização.** `create_organization(..., storage_location=...)` recebe o local escolhido no formulário da plataforma; se omitido, usa o local padrão ativo ou, na falta dele, o armazenamento da instância. Informar um local inativo recusa a criação por completo.
- **Cobrança por GB.** O espaço incluído vem do [plano base](billing.md#cotas): `Price.included_storage_bytes` define a franquia, e `storage_overage="bill"` com `overage_unit_bytes=1073741824` (1 GiB) e `overage_unit_amount` (valor por GB adicional) cobra o excedente; produtos do tipo `storage_pack` vendem pacotes extras de espaço. A [cota da plataforma](#cota-por-organização) (`StorageQuota`) continua atuando como teto manual.

## Anexos

`sk_files.Attachment` (`OrganizationRecord`, auditado) registra os metadados de cada arquivo anexado a um registro: `target_table` (FK `PROTECT` para o catálogo), `target_sysid`, `filename`, `content_type`, `size`, `sha256`, `storage_key` (campo sensível, redigido na auditoria e nunca devolvido), `state` (`pending`, `available`, `quarantined`, `rejected`), `scanned_at` e `scan_result_code`. Os bytes permanecem no armazenamento privado.

Para habilitar anexos em uma tabela, declare `AttachmentPolicy` no `TableSpec`:

```python
from skeleton.system.registry import AttachmentPolicy, TableSpec

TableSpec(..., attachments=AttachmentPolicy(max_bytes=5 * 1024 * 1024, types=("application/pdf", "image/png")))
```

| Regra | Resultado |
|---|---|
| Tabela sem opt-in | 404 |
| Tabela que não é exposta nem [derivada](access-control.md#tabelas-derivadas) | 404 |
| Alvo de outra organização, inexistente, excluído ou não legível | 404 (um alvo excluído oculta seus anexos) |
| Listar anexos | Exige permissão de leitura no alvo |
| Enviar, remover ou restaurar anexo | Exigem permissão de `update` no alvo; quem só lê recebe 403 |
| Tipo detectado pelo conteúdo fora da política, ou extensão incompatível | 400 |
| Tamanho acima de `max_bytes` ou de `SKELETON_UPLOAD_MAX_BYTES` | 413 |
| Remover e restaurar | Exigem `If-Match` (428 sem o cabeçalho, 412 com revisão antiga) |

Uma [tabela derivada](access-control.md#tabelas-derivadas) também aceita anexos com a mesma política: a autorização vem do registro âncora com a ação mapeada (ler e baixar exigem `read`; enviar e remover exigem `update`). Nesses casos, a app usa `FileService` e exibe os anexos em suas próprias páginas, pois as rotas genéricas de envio e remoção e o painel da página de registro aplicam-se apenas a tabelas expostas. A rota de download atende ambos os tipos e reautoriza a cada pedido, de modo que revogar a concessão na âncora bloqueia imediatamente o próximo download. O expurgo (`files.purge`) trata anexos de tabelas derivadas como quaisquer outros, e filhos selados com anexos congelados recusam novos envios.

O tipo MIME é identificado pelos primeiros bytes do arquivo (PDF, PNG, JPEG, GIF, WebP, ZIP, DOCX, XLSX e texto UTF-8 para `.txt` e `.csv`) por meio de uma tabela de assinaturas mantida em código e precisa coincidir com a extensão; o `Content-Type` informado pelo cliente é ignorado. Essa tabela própria evita dependências externas nos locks e reconhece apenas os tipos suportados pelas políticas. HTML e SVG nunca são aceitos.

Sem antivírus configurado, o anexo nasce `available`; com antivírus, nasce `pending` (veja [Antivírus](#antivírus)). Se a gravação no banco falhar após o upload, o objeto gravado no storage é removido. Remover um anexo faz apenas exclusão lógica — os bytes são mantidos para permitir restauração até o expurgo previsto na retenção.

| Superfície | Rota |
|---|---|
| API | `GET` e `POST /api/v1/o/<org>/tables/<table_key>/records/<sysid>/attachments/` (multipart, campo `file`), `DELETE .../attachments/<id>/`, `POST .../attachments/<id>/restore/` |
| HTML | Painel "Anexos" na página de detalhe do registro, com envio (`multipart/form-data`) e remoção via formulário. Envios sem arquivo, acima do limite ou além da cota retornam à página do registro com a mensagem apropriada ("Selecione um arquivo.", "O arquivo excede o limite de 10 MB." ou aviso de cota atingida), sem página de erro; a política da tabela é verificada antes |

### Anexos de registro selado (#529)

Em um registro selado ([modelo de dados](data-model.md#registros-selados-526)) cuja tabela declara `SealPolicy(attachments="frozen")` (padrão), envio, remoção e restauração de anexos são recusados com 409 `record_sealed` nas páginas e na API, e com `Conflict("record_sealed")` no `FileService`; o download continua permitido. Os anexos vivos (`sysid`, `sha256`, `size` e `filename`, ordenados por `sysid`) compõem o resumo do selo, de modo que `verify` detecta qualquer alteração posterior.

A operação trava a linha do registro antes de verificar o selo: um selo concorrente ou aguarda o anexo (incluindo-o) ou conclui antes e recusa o envio. Como anexos congelados não podem ser excluídos, `files.purge` nunca os remove. Com `attachments="open"`, os anexos seguem livres e ficam fora do resumo do selo. A cota de armazenamento não é alterada por essa regra.

## Download

Anexos só são baixados por `/o/<org>/tables/<table_key>/<sysid>/attachments/<id>/download/`, que revalida a autorização **a cada pedido**: exige leitura do alvo, anexo vivo e estado `available`. Anexos inexistentes, excluídos, `pending`, `quarantined`, `rejected` ou sem permissão respondem 404, e qualquer revogação de acesso passa a valer na requisição seguinte. Não há URL pública.

Como o middleware de tenant recusa respostas em streaming enquanto a transação e o contexto de banco estão abertos, a view devolve uma `DetachedFileResponse` (`skeleton/common/responses.py`) contendo apenas os valores já resolvidos (chave, nome e tipo). Somente após o commit da transação e o encerramento dos contextos o middleware converte esse objeto na resposta final, lendo os bytes do storage **sem executar nenhuma consulta ao ORM**.

| Entrega | Configuração |
|---|---|
| Streaming pelo Django (padrão) | `FileResponse` lendo diretamente do armazenamento local |
| Nginx | `SKELETON_FILES_ACCEL=nginx` e `SKELETON_FILES_ACCEL_PREFIX` (padrão `/_protected/`): resposta vazia com `X-Accel-Redirect: <prefixo>/<chave>` |
| Apache ou lighttpd | `SKELETON_FILES_ACCEL=sendfile`: envia `X-Sendfile` com o caminho local |
| S3 | Redirecionamento para URL pré-assinada de no máximo 60 s, forçando `Content-Disposition: attachment` |

Toda resposta de download inclui `Content-Disposition: attachment` (nome codificado segundo a RFC 5987), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: no-store` e `Referrer-Policy: no-referrer`. Nenhum anexo é servido inline (nem imagens); tipos fora da lista segura (como HTML ou SVG) saem como `application/octet-stream`.

### Download pelo portal (#534)

Pessoas externas baixam anexos exclusivamente pelo [portal](portal.md), na rota `/portal/<org>/files/<attachment_id>/`, e somente quando a tabela declara `AttachmentPolicy(..., external_reader=função)` em código. Essa função recebe o registro-alvo vivo e a pessoa externa e decide se ela pode lê-lo; sem `external_reader`, nenhum anexo da tabela é exposto no portal.

Anexos excluídos, expurgados, pendentes, em quarentena, de outra organização ou recusados pela função respondem 404, seguindo o mesmo padrão do download de membros. A view reautoriza a cada requisição e entrega os bytes apenas após a transação. O portal permite somente `GET` (não há upload, remoção nem restauração). Para montar o endereço em views de portal, a app usa `FileService().external_download_url(actor=..., attachment_id=...)` (`allow_external=True`). Assim como o download de membros, o download pelo portal não grava auditoria nem log próprio.

## Cota por organização

O uso atual fica registrado em `StorageUsage` ([tabela operacional](data-model.md#tabelas-operacionais-adr), uma linha por organização com RLS de tenant), e eventuais exceções definidas pela plataforma ficam em `StorageQuota` (`SystemRecord` global, auditado, campo `quota_organization`). A cota efetiva é resolvida na seguinte ordem:

1. `StorageQuota` da organização (`quota_bytes` vazio significa ilimitado);
2. franquia da assinatura (ou teto de excedente, em planos que cobram uso adicional) quando a [cobrança](billing.md#cotas) está habilitada;
3. `SKELETON_FILES_ORG_QUOTA_BYTES` como padrão da instância.

- **Reserva sob lock.** Após gravar os bytes, o upload bloqueia primeiro a linha da organização e depois a de `StorageUsage` (`SELECT ... FOR UPDATE`, sempre nessa ordem) para somar o tamanho na mesma transação em que grava `Attachment`. Uploads concorrentes que juntos ultrapassariam a cota são serializados por esses locks, e o segundo recebe 409 `quota_exceeded` com os bytes apagados. Uma verificação prévia sem lock já recusa arquivos que visivelmente não cabem antes mesmo da leitura.
- **Liberação e restauração.** A exclusão lógica devolve o espaço ocupado; restaurar um anexo reserva o espaço novamente e retorna 409 caso ultrapasse a cota (na API e no serviço).
- **Recontagem.** O job periódico `files.quota_recount` (diário, por organização) recalcula o contador a partir dos anexos vivos e atualiza `recounted_at`, corrigindo eventuais divergências.
- **Gestão pela plataforma.** A página da organização em `/platform/organizations/<id>/` exibe uso, cota efetiva e data da última recontagem. O formulário "Armazenamento" (`POST .../storage/`, exigindo autenticação recente e CAS pela revisão) permite escolher entre o padrão da instância, um limite próprio em MiB ou uso ilimitado. A plataforma vê apenas o total consolidado, nunca os anexos em si.

A cobrança em si fica fora do escopo deste subsistema (veja [cobrança](billing.md#cotas)).

## Antivírus

A variável `SKELETON_FILES_SCANNER` define o scanner de arquivos: `none` (padrão, no qual o anexo nasce `available`) ou `clamav`, que se comunica via protocolo `INSTREAM` com o `clamd` indicado em `SKELETON_FILES_CLAMAV_ADDRESS` (`tcp://clamav:3310` ou `unix:///run/clamav/clamd.ctl`).

Com o scanner ativo, o anexo nasce `pending` e enfileira o job `files.scan` na mesma transação. O job lê os bytes do armazenamento privado e os envia ao `clamd`:

| Resposta | Estado resultante |
|---|---|
| `stream: OK` | `available`, `scan_result_code=clean` |
| `stream: <assinatura> FOUND` (como o teste EICAR) | `quarantined`, `scan_result_code=infected:<assinatura>` |
| `clamd` indisponível, timeout ou resposta sem veredito | O job falha e é retentado com backoff até 10 vezes; o anexo permanece `pending` |

Como o download exige `available`, arquivos `pending` ou `quarantined` nunca são entregues (404). Um anexo que esgota as tentativas permanece `pending` (falha fechada) até ser verificado novamente: o job periódico `files.rescan_pending`, executado de hora em hora por organização, reenfileira até 500 anexos em estado `pending` há mais de uma hora sem verificação em andamento, permitindo que sejam processados automaticamente assim que o antivírus voltar. Se `SKELETON_FILES_SCANNER=none`, os anexos pendentes continuam bloqueados e o job termina `skipped`, sem falhar, até o scanner ser reativado.

## Objetos órfãos

Como o upload grava os bytes antes do commit da linha no banco, um rollback ou queda entre as duas etapas pode deixar um objeto sem metadado correspondente. O job periódico `files.orphans` (a cada 6 horas, por organização) lista `org/<organização>/` no armazenamento e remove apenas objetos **sem nenhuma linha `Attachment`** cuja idade exceda o prazo de carência (`SKELETON_FILES_ORPHAN_GRACE_SECONDS`, padrão 1 hora, para não interferir em uploads em andamento). Objetos ligados a linhas vivas ou excluídas logicamente nunca são removidos por esse job, preservando a capacidade de restauração.

O mesmo job também inspeciona `org/<organização>/branding/`, onde fica o logo da organização ([#168](https://github.com/alaorneto/skeleton/issues/168)): o logo ativo (`OrganizationSettings.logo_key`) é preservado, enquanto arquivos substituídos ou deixados por rollback são removidos após a carência (a troca e a remoção pela tela já apagam o objeto anterior depois do commit). O logo não consome a cota de anexos.

## Expurgo de anexos excluídos

Um anexo excluído logicamente mantém seus bytes armazenados por `SKELETON_FILES_DELETED_RETENTION_DAYS` (padrão 90 dias) para permitir restauração ([#192](https://github.com/alaorneto/skeleton/issues/192)). Passado esse prazo, o job diário `files.purge`, executado por organização, remove o objeto do storage e marca o anexo como `purged`.

- A linha permanece como *tombstone*, preservando metadados e histórico de auditoria. Tentar restaurar um anexo `purged` é recusado com 400.
- Anexos vivos ou excluídos dentro do prazo de retenção nunca são tocados.
- Cada linha é travada antes do expurgo: uma restauração concorrente ou vence antes (restaurando o anexo, que passa a ser pulado) ou aguarda e já o encontra marcado como `purged`.
- Os bytes são removidos do storage após o commit; se a exclusão física falhar, a próxima rodada tenta novamente.

Essa é a única remoção física de conteúdo de anexos no sistema, prevista como exceção nominal no `AGENTS.md`.
