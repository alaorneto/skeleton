# Criar um projeto e adicionar negócio

Uma app de negócio é um módulo Django do projeto derivado, como tarefas ou anotações; o núcleo fornece identidade, isolamento, autorização e auditoria. Este roteiro separa três responsabilidades: escrever modelos e serviços seguros, declarar como a app se integra e publicar código para instalação. Consulte [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados).

## Copiar o template sem renomear o núcleo

Use a função Git template do seu provedor ou clone/cópia versionada. Não copie .env, .venv, bancos, .private, cookies ou chaves de fixture.

```sh
git clone "$TEMPLATE_REPO" meu-projeto
```

Configure identidade visual/nomes do projeto, env privado, hosts/remetente/IdPs e secrets. Preserve módulos `skeleton`, app labels e AUTH_USER_MODEL. Apps próprias entram em `PROJECT_APPS` de `config/settings/project.py`; rotas do projeto em `config/urls_project.py`. Não edite núcleo para cada negócio; núcleo nunca importa applications/examples.

```python
# config/settings/project.py
PROJECT_APPS = ["applications.tasks.apps.TasksConfig"]
```

Crie um `AppConfig` com `name="applications.tasks"` e um label único, como `project_tasks`; o label identifica a app no Django e nas migrations, por isso não reutilize os labels do núcleo ou do exemplo. Registre metadados em `ready()` sem consultas ao banco. URLs e tarefas de segundo plano devem chamar serviços autorizados, não managers internos que escapem das políticas.

### Manifest, ativação e superfícies próprias

O manifesto é o contrato versionado da app: identifica o componente e declara o que ele precisa e o que oferece ao projeto. Cada app carrega o `manifest.json` canônico com `AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self)` e registra o resultado em `AppConfig.ready()`, separadamente dos `TableSpec` que descrevem suas tabelas. O contrato declara ID estável, versão, `publisher` (responsável pela publicação), label/package, intervalos de compatibilidade e dependências **por app ID com intervalo de versão**. Dependências de apps não são nomes de distribuições Python.

A seção de runtime descreve configuração escalar, navegação, módulos de URLs/API, política de habilitação e documentação. No formato 2, acrescenta `administration` para seções/telas de parâmetros e `model_ui` para apresentação de negócio fora do admin. `ConfigField`, `MenuEntry` e specs imutáveis de apresentação são representações do manifesto canônico; não mantenha uma segunda versão manual. Registrar esses contratos em `ready()` não consulta banco nem executa DDL. Dependências devem estar instaladas, com versões compatíveis e sem ciclos.

As rotas declaradas são incluídas antes dos gateways, exclusivamente sob `/o/<org>/apps/<app_label>/` e `/api/v1/o/<org>/apps/<app_label>/`. Views usam `tenant_page`/`TenantAPI` e serviços autorizados; esses prefixes recebem contexto tenant validado. `config.urls_project` também precede os gateways para não capturar rotas do projeto. Não crie rotas organizacionais fora deste contrato.

**Instalação global e ativação por organização são operações diferentes.** Instalar disponibiliza código e migrations no ambiente compartilhado; ativar autoriza que uma organização use uma app já instalada, sem instalar Python nem criar tabelas. `/o/<org>/apps/` administra ativação/configuração e apresenta a versão declarada. `AppInstallation`, um `OrganizationRecord` com UUID, guarda esse estado com infraestrutura de migration e auditoria.

A configuração JSON é redigida integralmente na auditoria. Segredos pertencem à configuração privada do deployment, nunca à configuração organizacional. Sem linha persistida, vale `default_enabled` do manifesto, preservando o comportamento de apps instaladas no upgrade. A API exige `If-Match`: use a revisão virtual `"0"` na primeira escrita e, depois, a revisão atual. CAS evita sobrescrever configuração concorrente; o bloqueio de Organization serializa também as validações de dependências. Somente `organization_admin` configura.

Desativar bloqueia endpoints próprios, gateway e políticas e remove o menu, mas preserva registros, IDs e ACL (regras de acesso). Reativar não concede `TablePermission`. Instalação, atualização e retirada global do código são solicitadas pela plataforma em `/admin/system/releases/` e executadas por um agente separado; a requisição web não executa instalador nem DDL. Remoção de dados/schema permanece exclusivamente offline e revisada.

O site padrão Django é um único `PlatformAdminSite`. `/admin/` aceita somente configurações/parametrizações; os registros globais convencionais `@admin.register` são descobertos em `/platform/records/`, com shell web separado e as mesmas proteções. Não registre negócio tenant nesse site: o viewer, catálogo e formulários de negócio ficam em `/o/<org>/catalog/` e `/o/<org>/tables/<table_key>/`.

O [contrato de administração e interfaces](application-admin-ui-plan.md) documenta grupos automáticos, telas de parâmetros, listas/layouts e serviços próprios. Novos manifestos usam `manifest_version=2` e compatibilidade inicial `>=0.3.0,<1.0.0`; o piso histórico v2 sem dimensões permanece 0.2.0, e v1/v2 assinados continuam legíveis sem normalizar JSON ou alterar bytes/digest. A presença de `col_span`/`rows`, inclusive `null`, exige piso 0.3.0 declarado na fonte antes de construir o pacote. Atualize primeiro agente e core para 0.3.0. `administration.screens` admite somente `configuration` ou `custom` de parâmetros, nunca `model_list`/metadados de tabela. `TableSpec`, não o layout, autoriza leitura/escrita/sensibilidade.

O core 0.3.0 distribui `django-crispy-forms==2.7` e o pack SSR `skeleton_crud` no wheel. Customize somente `runtime.model_ui` do manifesto canônico, sem arquivo UI paralelo nem editor no admin. Notes 0.2.0 demonstra abas Identificação/Conteúdo, labels Código/Título/Texto e spans `code:4`, `title:8`, `body:12` com `rows:8`; o [exemplo JSON completo](application-admin-ui-plan.md#3-manifesto-canônico-v2) mantém IDs, seções e ordem. `col_span` aceita inteiro exato 1..12, `rows` inteiro exato 1..40; omissão/`null` mantém defaults, mas bool/string/float são recusados. Rows exige textarea editável efetivo (ou override `textarea`/`json` compatível), nunca readonly/input/select/relação, sem mudar validators. Seção `columns:1/2` define largura padrão 12/6, campos largos usam 12 e spans explícitos prevalecem. A grade preserva DOM/foco e, até 600px, todos os campos ocupam a linha inteira.

Modelos elegíveis sem `model_ui` recebem defaults sem Form/template próprios. O renderer conserva CSRF, media, validação, CAS e persistência dos formulários existentes, incluindo perfil, acesso e configuração. O viewer genérico exige `SecuredRecord` exposto por `TableSpec.expose_api=True`; `SystemRecord`/`OrganizationRecord` globais ou técnicos continuam com serviços/superfícies dedicados, não ganham CRUD por declarar layout.

### Documentação obrigatória por app

`documentation_root` aponta para um diretório versionado com `spec.md` (`## Contract`, `## Acceptance`) e `adr.md` (`## Context`, `## Decision`, `## Consequences`). Especifique persistência, autorização, ativação/configuração, endpoints, sensibilidade, concorrência e aceites observáveis; registre decisões e tradeoffs no ADR. Checks rejeitam documentação faltante. Veja `examples/notes/docs/`.

### Publicar apps e releases assinados

Publicar produz um artefato `.skapp` verificável; não significa instalá-lo nem aprová-lo para produção. O fluxo abaixo cria as chaves do publicador, gera e revisa o manifesto, constrói o pacote e verifica sua integridade e procedência. O operador que instala mantém sua própria lista de publicadores confiáveis. Um hash SHA-256 detecta alteração de bytes; a assinatura demonstra que a chave confiável aprovou aquele manifesto. Hash correto, sozinho, não identifica quem publicou.

Os utilitários funcionam tanto por `manage.py package` quanto sem settings/Django inicializado por `.venv/bin/python -m skeleton.system.packages`. O agente externo usa as mesmas funções puras de validação; nunca importa código de um upload antes de verificar assinatura, inventário e hashes. Assinatura controla procedência, **não** transforma Python/migrations em sandbox: aprove somente publishers que podem executar código privilegiado.

Uma app usa `name = __package__` em seu AppConfig para funcionar no checkout e no namespace gerenciado `applications.<package_name>`. O `app_config` e os módulos declarados no JSON são sempre gerenciados (`applications.tasks.apps.TasksConfig`, `applications.tasks.urls`); o loader adapta módulos ao AppConfig local e valida identidade. Templates/static ficam em `templates/<app_label>/` e `static/<app_label>/`; a app não escreve em config, settings ou arquivos de outra app. Versione `spec.md` e `adr.md` com as seções obrigatórias acima e mantenha todas as migrations históricas.

```sh
# Chaves ficam FORA da app e do Git; a privada é criada com modo 0600.
.venv/bin/python -m skeleton.system.packages keygen \
  --private-key /private/publisher.pem --public-key /private/publisher.pub.pem
.venv/bin/python manage.py package init \
  --output applications/tasks/manifest.json --app-id com.example.tasks \
  --app-label project_tasks --package-name tasks --publisher example-publisher \
  --app-config-class TasksConfig --version 0.1.0
# Edite o JSON canônico: nome/descrição, compatibilidade, runtime e folhas de migrations.
.venv/bin/python manage.py package validate-manifest applications/tasks/manifest.json
.venv/bin/python manage.py package build \
  --app-root applications/tasks --manifest applications/tasks/manifest.json \
  --output /private/artifacts/tasks-0.1.0.skapp --private-key /private/publisher.pem
.venv/bin/python manage.py package validate /private/artifacts/tasks-0.1.0.skapp \
  --trust-store /private/trusted-publishers.json
.venv/bin/python manage.py package inspect /private/artifacts/tasks-0.1.0.skapp \
  --trust-store /private/trusted-publishers.json
.venv/bin/python manage.py package versions --manifest applications/tasks/manifest.json
```

O trust store, ou lista de chaves de publicadores confiáveis, é um JSON administrado pelo operador, por exemplo `{"example-publisher": "/private/publisher.pub.pem"}`. A chave do mapa é o `publisher` do manifesto; valores aceitam PEM público literal, caminho PEM ou `{"public_key": "/private/publisher.pub.pem", "publisher": "example-publisher"}`. Caminhos relativos do CLI são relativos ao arquivo de configuração. A API também aceita objetos `Ed25519PublicKey`. A chave pública pode verificar assinaturas; a privada deve ficar somente com quem está autorizado a publicar. Chaves públicas trazidas pelo próprio pacote nunca são âncoras de confiança. Revogação/rotação exige alterar explicitamente esse arquivo privado. `inspect` sem trust store mostra `"trusted": false`, mesmo quando os hashes são íntegros.

Build gera o inventário completo, excluindo somente o próprio manifesto e caches/artefatos de build conhecidos; recusa symlinks, secrets/private keys, output dentro da app, namespaces reservados, docs incompletos e dependências não inventariadas. O ZIP tem exatamente `manifest.json`, `manifest.sig`, `app/` e wheels opcionais. Assinatura Ed25519 cobre os bytes canônicos exatos do manifesto; SHA-256/tamanhos cobrem todos os payloads. Validação recusa membros extras/duplicados, colisões normalizadas, travessia, links/dispositivos, arquivos/diretórios conflitantes e limites de ZIP (512 MiB comprimido, 1 GiB expandido, 256 MiB por arquivo, 4096 membros, expansão máxima 200:1).

Declare dependências Python em `python_dependencies` como `{"name": "distribution", "version": "1.2.3", "wheel": "wheels/distribution-1.2.3-py3-none-any.whl", "sha256": "<SHA256>", "marker": "python_version >= '3.12'"}`; marker é opcional. Forneça cada wheel com `build --wheel /private/wheelhouse/distribution-1.2.3-py3-none-any.whl`. Build recalcula SHA-256; valida identidade/versão do METADATA e namespace de cada wheel. Não aceita sdists, wheels `.data` ou arquivos que se apropriem de core/config/applications. O ambiente novo instala wheels pelo lock final aprovado, nunca dentro do diretório da app.

Um wheel (`.whl`) é uma distribuição Python pronta para instalação, não a ativação de uma app no tenant. No exemplo de dependência, `name` e `version` identificam a distribuição, `wheel` identifica o arquivo inventariado e `sha256` fixa seus bytes. O marcador opcional limita os ambientes em que a dependência se aplica. Incluir esses arquivos no pacote permite preparar o ambiente aprovado sem buscar versões diferentes durante a instalação; isso não transforma uma dependência em código confiável sem revisão.

Versões de componentes exigem major.minor.patch; versões de distribuições Python e markers seguem PEP 440, inclusive duas partes como `8.2`/`python_version="3.14"`. O wheelhouse completo do ambiente tem limite agregado separado de 65536 membros e 1 GiB expandido; não recebe indevidamente o limite de um artefato isolado. Apps `project-local` continuam em `PROJECT_APPS`; manifesto e caminho carregados precisam coincidir com seu inventário, ou o startup falha fechado. Elas não são sobrescritas por um pacote com nome coincidente.

Extração offline exige raiz nova e separada: `package extract ARTIFACT --trust-store TRUST --destination /staging/applications/tasks --wheels-destination /staging/app-wheels`. Não faz merge/overwrite e revalida o artefato antes da cópia. `extract_package(verified, destination, wheels_destination=...)` retorna os roots criados; copia o contrato canônico e assinatura para a app como metadados gerados pelo instalador. Artefatos core têm apenas wheels e usam `destination=None` na API (omita `--destination` no CLI). O core wheel somente pode possuir `skeleton/` e seu `.dist-info`; atualização não substitui arquivos do projeto derivado.

```sh
# Reassinar app/core existente: inspeção completa antes da nova assinatura,
# output novo obrigatório; aprovação da procedência é decisão do operador.
.venv/bin/python manage.py package sign /private/artifacts/tasks-0.1.0.skapp \
  --output /private/artifacts/tasks-approved.skapp --private-key /private/publisher.pem
# Publicação estática; geração monotônica e expiração são obrigatórias.
.venv/bin/python manage.py package index /private/artifacts/tasks-approved.skapp \
  --output /private/index.json --private-key /private/publisher.pem \
  --trust-store /private/trusted-publishers.json --publisher example-publisher \
  --generation 1 --expires-at 2027-01-01T00:00:00Z --channel stable \
  --artifact-base https://releases.example.test/apps/
.venv/bin/python manage.py package validate-index /private/index.json \
  --trust-store /private/trusted-publishers.json --minimum-generation 1 --channel stable
```

O índice é `{catalog: {...}, signature: "<base64>"}`; assinatura cobre JSON canônico do corpo. Entradas contêm componente, tipo, versão, canal, compatibilidade, referência HTTPS/relativa ao artefato, SHA-256 do ZIP, assinatura do manifesto, publisher, notas e migrations. Índices expirados, adulterados e gerações abaixo da última confirmada são rejeitados; canais são allowlist do operador. O agente só consulta fontes previamente autorizadas, não URLs fornecidas pelo upload. `versions` distingue metadata da distribuição instalada de checkout (`origin: source`) e manifests locais (`project-local`); um arquivo JSON isolado nunca prova release instalado.

O exemplo pode ser publicado de verdade com `package build --app-root examples/notes --manifest examples/notes/manifest.json --output /private/notes.skapp --private-key /private/publisher.pem`; autorize o publisher `skeleton-examples` no trust store correspondente. Ativação/configuração tenant permanece independente da instalação física global.



## Exemplo completo existente: Note

Ative somente se desejado:

```sh
export SKELETON_ENABLE_EXAMPLE_APP=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
```

O exemplo fica em `examples/notes`, app label `example_notes`; pode permanecer desabilitado num projeto real. Não coloque seu módulo de negócio nesse namespace.

### Modelo

O modelo existente usa herança abstrata; metadata, tenant/access fields são colunas da tabela Note, não tabela-pai:

```python
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q
from skeleton.common.models import SecuredRecord

class Note(SecuredRecord):
    code = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])

    class Meta(SecuredRecord.Meta):
        constraints = SecuredRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "code"),
                condition=Q(deleted_at__isnull=True),
                name="example_notes_note_live_code",
            ),
        ]
        ordering = ("created_at", "sysid")
```

`body` pode ser vazio e tem limite de 10000 caracteres. É sensível: usuários autorizados podem ler seu conteúdo na nota, mas a auditoria o substitui por `[REDACTED]`, mantendo a evidência de que mudou. Isso não cifra o conteúdo da tabela de negócio. A restrição de unicidade usa `(organization, code)` apenas entre registros vivos: duas organizações podem usar o mesmo código. A exclusão lógica libera o código para uma nova nota; se isso ocorrer, restaurar a nota antiga retorna conflito, sem sobrescrever a nova.

### Spec allowlist

`NotesConfig.ready` registra:

```python
from skeleton.system.registry import TableSpec, register
from .models import Note

register(TableSpec(
    model=Note,
    description="Anotações de demonstração do contrato de registros",
    label="Anotações",
    scope="organization",
    read_fields=("code", "title", "body", "owner", "visibility", "owning_group"),
    write_fields=("code", "title", "body"),
    sensitive_fields=("body",),
    expose_api=True,
))
```

A allowlist é a lista explícita dos campos permitidos. Ela contém somente campos concretos, sem `eval` ou caminhos arbitrários para objetos relacionados. A leitura de owner/grupo retorna UUID, não um perfil ou grupo privado inteiro. `owner`, `visibility` e `owning_group` não entram em `write_fields`, pois mudar acesso exige o endpoint/serviço de `manage_access`. Campos sistêmicos são automaticamente somente leitura. `expose_api=True` disponibiliza a rota, mas não concede permissão: `TablePermission` continua obrigatória.

### Migration de segurança

`examples/notes/migrations/0001_initial.py` cria o modelo e depois instala infraestrutura no mesmo arquivo; depende de `("sk_audit", "0002_record_infrastructure")`. O gerador Django cria campos herdados/constraints/managers históricos; não omita os constraints da base. Operação relevante:

```python
from skeleton.common.migration_operations import InstallRecordInfrastructure

InstallRecordInfrastructure(
    "example_notes", "note",
    {
        "scope": "organization",
        "sensitive_fields": ["body"],
        "columns": [
            {"name": "sysid", "type": "UUIDField", "sensitive": False},
            {"name": "created_at", "type": "DateTimeField", "sensitive": False},
            {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
            {"name": "created_by", "type": "UUIDField", "sensitive": False},
            {"name": "updated_by", "type": "UUIDField", "sensitive": False},
            {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
            {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
            {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
            {"name": "organization_id", "type": "UUIDField", "sensitive": False},
            {"name": "owner_id", "type": "UUIDField", "sensitive": False},
            {"name": "owning_group_id", "type": "UUIDField", "sensitive": False},
            {"name": "visibility", "type": "CharField", "sensitive": False},
            {"name": "code", "type": "CharField", "sensitive": False},
            {"name": "title", "type": "CharField", "sensitive": False},
            {"name": "body", "type": "TextField", "sensitive": True},
        ],
    },
)
```

Para app nova, rode makemigrations, revise o histórico e acrescente operação após CreateModel. Para alteração de coluna/relação auditável, adicione operação após alteração, com spec histórica congelada atualizada. O exemplo completo de migration já existe no caminho acima; a operação isolada não substitui CreateModel. Não use catálogo editável/import de modelo atual em migrations.

## Permissões e fluxo HTML

Bootstrap/sync reconcilia TablePermission de organization_admin para a tabela exposta. Member não recebe grants automaticamente. Em `/o/<org>/access/roles/` crie role e atribua read/create/update/delete/restore e, se desejado, manage_access/audit via table-permissions. Atribua MemberRole ou GroupRole + GroupMember. Tudo exige org admin e, em produção, MFA.

1. Login real em `/accounts/login/`.
2. Dashboard `/o/<org>/`; catálogo `/o/<org>/catalog/` fornece tabela disponível.
3. Lista `/o/<org>/tables/example_notes.note/`.
4. Criar `/o/<org>/tables/example_notes.note/new/` com code/title/body; CSRF, owner=ator, private.
5. Detalhe `.../<sysid>/`; editar `.../<sysid>/edit/`, form com revision escondida.
6. Acesso `.../<sysid>/access/`: owner/visibility/grupo só com manage_access + ownership/admin.
7. Exclusão `.../<sysid>/delete/` e restore `.../<sysid>/restore/` via POST/revisão; GET não apaga. Tombstone não volta à lista viva.
8. Audit `/o/<org>/audit/`: body redigido; contagem/lista autorizadas.

Só botões permitidos aparecem, mas servidor revalida cada ação. Usuário comum que recebe visibilidade organization pode ler, não ganha escrita. Relacionamentos de novos apps exigem PROTECT + guard same-tenant e FK picker autorizado.

## Fluxo API de ponta a ponta

Use cookie/CSRF de login allauth real como em [api](api.md). Variáveis BASE/ORG/SESSION/CSRF são locais privadas. Exemplo com notas:

```sh
URL="$BASE/api/v1/o/$ORG/tables/example_notes.note/records"
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" "$URL/"
curl -i -X POST -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H 'Content-Type: application/json' \
  --data '{"code":"tutorial","title":"Exemplo","body":"Não vai para audit"}' "$URL/"
# NOTE é sysid devolvido; ETAG é o header atual incluindo aspas.
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" "$URL/$NOTE/"
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H "If-Match: $ETAG" \
  -H 'Content-Type: application/json' --data '{"title":"Editado"}' "$URL/$NOTE/"
# Releia ETAG antes de cada mutação seguinte.
curl -i -X DELETE -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H "If-Match: $ETAG" "$URL/$NOTE/"
# DELETE não devolve corpo; soft delete incrementa revision. Se ETAG antes era
# "4", restore precisa "5", não o ETag stale do registro vivo.
curl -i -X POST -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H 'If-Match: "5"' "$URL/$NOTE/restore/"
```

O `"5"` exemplifica a transição exata 4→5, não revisão fixa para qualquer nota. Grants/access podem também mudar revisão; não adivinhe num fluxo concorrente. Recarregue registro após restore. Alterar organização/owner no PATCH normal retorna erro; table_key físico/desconhecido não abre tabela.

## Novas operações de negócio

Implemente a regra de negócio em um serviço da app, não em `skeleton`. O serviço abre `tenant_context` com organização e ator confiáveis, obtém `authorized_queryset` para a ação pretendida, valida a regra e grava com `save`/CAS. Em uma view, use o contexto validado pela superfície tenant; em um job, abra uma transação independente por organização e materialize os resultados antes de sair do contexto. Nunca use `all_objects` ou SQL direto como atalho em endpoints.

Uma referência a outro registro organizacional precisa apontar para a mesma organização e para um alvo vivo que o ator possa ler. Combine validação no serviço, seleção autorizada de FK e infraestrutura de integridade no banco: apenas comparar UUIDs não assegura isso. Relações entre registros próprios usam `PROTECT`, para impedir remoção com referências vivas, e links de negócio são tabelas UUID explícitas, não M2M implícito. Marque campos sensíveis no `TableSpec` e na especificação congelada de migration antes de persistir dados em auditoria.

Use o checklist de [AGENTS](../AGENTS.md) e [testing](testing.md). Mantenha testes de API pública, A/B/default deny, auditoria redigida, concorrência, referências e schema checks. Testes por inspeção de strings de source não provam esses contratos.
