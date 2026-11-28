# Portal: pessoas externas

O portal é a superfície dedicada a **pessoas externas** — como clientes, pacientes ou fornecedores que não são membros da organização — para acessar somente o que uma app da organização libera para elas, sem nenhum poder de membro e sem ocupar assento. Este documento detalha o vínculo, os convites e as regras para as apps; o contrato de contextos e as negações estão em [controle de acesso](access-control.md#acesso-externo-portal).

## Vínculo e política

- A pessoa externa utiliza um `User` comum, autenticado pelo allauth. A condição de "externa" pertence ao vínculo **`ExternalAccess`** (organização, pessoa, app que a admitiu e status ativo), completamente separado de `Membership`. A mesma pessoa pode ser membro em uma organização e externa em outra — ou acumular ambos na mesma organização, caso em que o portal reconhece apenas o vínculo externo.
- **`ExternalAccessPolicy`** controla se o portal está ativo na organização (desligado quando não há registro) e define a validade dos convites (padrão de 72 horas, configurável entre 1 e 720 horas). O administrador gerencia a política, os acessos e os convites pendentes em **Pessoas externas** (`/o/<org>/admin/external/`), exigindo a capacidade `external.manage`.
- A métrica de plano `external_people` contabiliza os acessos externos ativos e é verificada no momento do aceite; a métrica `members` nunca inclui pessoas externas ([cobrança](billing.md)).

## Convite e aceite (#532)

O acesso ao portal ocorre exclusivamente por convite: não há autoinscrição externa.

1. **Emitir.** A app chama `ExternalAccessService().invite(actor=..., email=..., app_label=..., subject_ref=..., idempotency_key=...)` após autorizar sua própria ação (informando seu `app_label`, com a app ativa), ou um membro com `external.manage` emite o convite diretamente. `subject_ref` é um UUID opaco escolhido pela app para representar seu registro (por exemplo, o cliente). Qualquer convite aberto anterior para o mesmo par `(app_label, subject_ref)` é revogado; repetir a mesma `idempotency_key` devolve o convite existente sem reenviar email. Se o portal estiver desligado, a chamada retorna `Conflict("external_disabled")`.
2. **Token.** O banco armazena apenas o resumo SHA-256 (`token_digest`). O token em claro aparece uma única vez, no link enviado por email (`/portal/invite/<org>/<token>/`). `token_digest` é sensível e redigido na auditoria; `email` é tratado como dado pessoal (`[REDACTED]` na auditoria).
3. **Aceitar.** A pessoa abre o link, entra ou cria uma conta com o endereço convidado e confirma o email — o **email verificado** da conta precisa coincidir exatamente com o convidado. No aceite, sob `system_context(reason="external:validated")`, o núcleo cria ou reativa o `ExternalAccess`, marca o convite como aceito e invoca, **na mesma transação**, o manipulador registrado pela app. Se o manipulador falhar, toda a transação é desfeita e o convite permanece válido.
4. **Recusas neutras.** Token desconhecido, expirado, já usado, revogado, de outra organização, ou conta com email diferente ou não verificado exibem a mesma página neutra. As tentativas de aceite sofrem limite de taxa por pessoa e por IP; atingir o limite do plano recusa o aceite e mantém o convite intacto.
5. **Revogar.** `revoke(access_id=..., expected_revision=...)` desativa o acesso: o portal fecha já na requisição seguinte e nenhum dado é apagado. `revoke_invitation` e `resend_invitation` (que gera um novo token para o mesmo endereço e registro) exigem CAS.

A app declara seu manipulador de admissão no `ready()`:

```python
from skeleton.service_api.external import register_external_admission


def link_customer(access_id, user_id, subject_ref):
    PortalLink.objects.create(access_id=access_id, customer_id=subject_ref)


register_external_admission("acme_customers", link_customer)
```

## Serviço

Métodos de `ExternalAccessService` ([API de serviços](service-api.md#catálogo-do-núcleo)):

| Método | Autorização | Erros |
|---|---|---|
| `invite(*, actor, email, app_label, subject_ref, idempotency_key) -> ExternalInvitationInfo` | `external.manage` ou a própria app | `Forbidden`; `Conflict("external_disabled")`; `Unavailable`; `InvalidInput` |
| `revoke_invitation(*, actor, invitation_id, expected_revision)`, `resend_invitation(...) -> ExternalInvitationInfo` | `external.manage` ou a app do convite | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` |
| `accesses(*, actor, app_label=None, cursor=None, page_size=25) -> Page[ExternalAccessInfo]`, `invitations(*, actor)` | `external.manage` | `Forbidden` |
| `access_for(*, actor, app_label, subject_ref) -> ExternalAccessInfo \| None` | `external.manage` ou a própria app | `Forbidden`; `InvalidInput` |
| `revoke(*, actor, access_id, expected_revision) -> ExternalAccessInfo` | `external.manage` ou a app que admitiu a pessoa | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `me(*, actor) -> ExternalAccessInfo` | apenas no portal (`allow_external=True`) | `Forbidden` fora do portal; `NotFound` |

`ExternalAccessInfo` expõe `sysid`, `user_id`, `display_name`, `app_label`, `is_active` e `revision`. `ExternalInvitationInfo` expõe `sysid`, `email`, `app_label`, `subject_ref`, `expires_at`, `accepted` e `revision`.

## Superfície (#533)

| Rota | Finalidade |
|---|---|
| `/portal/` | Organizações em que a pessoa possui acesso externo ativo e portal ligado (autodescoberta, com uma transação externa somente leitura por organização) |
| `/portal/<org>/` | Apps com portal ativas na organização |
| `/portal/<org>/apps/<label>/...` | Páginas de portal da app (`runtime.portal_urls`), no namespace `<label>_portal` |
| `/api/v1/portal/<org>/apps/<label>/...` | API de portal da app (`runtime.portal_api_urls`), no namespace `<label>_portal_api`, somente por sessão |
| `/portal/invite/<org>/<token>/` | Aceite de convite para o portal |

- **Contexto.** Em `/portal/<org>/` e `/api/v1/portal/<org>/`, o middleware de tenant abre `external_context` em vez de `tenant_context`. Qualquer impedimento — ausência de acesso, membro sem acesso externo, portal desligado, outra organização ou uso de token Bearer — responde 404 (enquanto páginas redirecionam usuários anônimos para o login). A raiz `portal/` é reservada e não pode ser capturada por apps, assim como `platform/` e `admin/`.
- **Estado da cobrança.** Se a organização estiver `suspended`, o portal exibe uma página neutra de serviço indisponível (503); se estiver `restricted`, opera em modo somente leitura (`GET` funciona e métodos não seguros retornam 403).
- **Rotas da app.** Antes de chamar cada view, o núcleo exige login, contexto externo e app ativa e disponível (além de contratada, quando a cobrança está ligada); caso contrário, retorna 404. As páginas saem sem cache e com proteção CSRF, e a API envia `Cache-Control: no-store`. Rotas de portal nunca são acessíveis por `/o/<org>/` e vice-versa.
- **Decoradores.** As views da app usam `skeleton.web.views.portal_page` (login, sem cache e contexto externo, convertendo recusas dos serviços em 404) e `skeleton.api.views.PortalAPI` (somente sessão e contexto externo).
- **Layout.** Utiliza `skeleton/portal/base.html`, com a marca da organização, sem navegação de membros e sem JavaScript.
- **Manifesto.** Declarar `runtime.portal_urls` ou `runtime.portal_api_urls` (módulos da própria app, no manifesto v2) exige `compatibility.skeleton` com piso mínimo de `PORTAL_CORE_MINIMUM` (`0.0.0`, presente em todo núcleo publicado), garantido também pelo `package build` no artefato. O gerador de apps não inclui portal por padrão.
- **OpenAPI.** As rotas `/api/v1/portal/` não aparecem no esquema dos membros (`/api/v1/openapi.json`); `skeleton.api.openapi.api_routes(portal=True)` lista exclusivamente as rotas de portal.

## Anexos (#534)

Uma tabela libera anexos no portal declarando `AttachmentPolicy(max_bytes=..., types=(...), external_reader=pode_ler)`, em que `pode_ler(record, user)` verifica a ligação definida pela app (por exemplo, confirmando que o registro pertence ao cliente vinculado àquela pessoa e já foi liberado). O download ocorre em `/portal/<org>/files/<attachment_id>/`, e `FileService().external_download_url` gera o endereço ([arquivos](files.md#download-pelo-portal-534)). O portal não permite upload, remoção nem links públicos.

## Notificações (#535)

Para notificar pessoas externas, a app declara um tipo de notificação com `audience="external"` (somente email, sem alvo e com campos declarados) e chama `NotificationService().notify(..., recipients=Recipients(users=(user_id,)))`. O email leva o link do portal ([notificações](notifications.md)); não existe central de notificações dentro do portal.

## Regra para as apps

No portal, todo método de serviço de app marcado com `@service_method(..., allow_external=True)` autoriza o acesso pela **ligação da própria app** com a pessoa, nunca por `authorized_queryset` (que sempre nega em contexto externo):

```python
@service_method(read_only=True, since="X.Y.Z", allow_external=True)
def my_documents(self, *, actor) -> tuple[DocumentSummary, ...]:
    access = ExternalAccessService().me(actor=actor)  # Falha fora do contexto externo.
    customer_ids = PortalLink.objects.filter(access_id=access.sysid, is_active=True).values("customer_id")
    return tuple(summary(r) for r in Document.objects.filter(customer_id__in=customer_ids, released=True))
```

O método deve retornar apenas registros alcançados pela ligação, responder de forma idêntica para registros inexistentes ou não vinculados, nunca aceitar um identificador de cliente enviado na requisição sem validar a ligação e delegar efeitos externos a jobs. Valide cada método com `skeleton.testing.external.assert_external_contract` ([testes](testing.md#kit-do-portal)).

### Superfícies do núcleo para os recursos da própria app (#741)

Dentro de um método `allow_external=True`, após autorizar pela ligação da app, é possível acionar três superfícies do núcleo passando a pessoa externa como `actor`, na mesma transação da requisição: numerar sequências, publicar eventos e gerenciar projeções no calendário. Em caso de rollback, todas as três são desfeitas e o número é devolvido.

| Superfície | O que o portal pode fazer | Regra |
|---|---|---|
| `SequenceService().next_number` | Numerar usando uma sequência da app | A sequência deve ter `owner_app` igual à app chamadora ([sequências](sequences.md)) |
| `EventService().publish` | Publicar um tipo de evento da app | O tipo deve ter `owner_app` igual à app chamadora; basta o registro existir na organização (já autorizado pela ligação; `authorized_queryset` continua negando); o evento registra a pessoa como ator e `source="http:portal"` ([eventos](events.md)) |
| `CalendarService().put_managed_event`, `remove_managed_event`, `managed_event` | Criar, mover, remover e ler o evento gerenciado da app | `source_app` deve ser a app chamadora em um calendário compartilhado e gerenciado por ela (`source_app` do calendário); o organizador é o dono do calendário; participantes não mudam (`attendee_user_ids` vazio); eventos de uma chave situados em calendários não gerenciados pela app não podem ser alterados pelo portal ([referência da agenda](../skeleton/contrib/calendar/docs/service-api.md)) |

A app chamadora é identificada automaticamente pela cadeia de chamadas da API de serviços — nunca por argumento: o método invocador precisa ser `allow_external=True`, pertencer à app dona e ter seu módulo verificado como no `system_context`. Fora dessa cadeia, o portal continua fechado:

- views de portal, testes ou métodos do núcleo que chamem essas superfícies diretamente recebem `Forbidden("external_not_allowed")`;
- sequências, tipos de evento ou calendários de outra app retornam `Forbidden` (e calendários não gerenciados pela app retornam `NotFound`);
- o contexto externo não se converte em `system_context`, não ganha leitura genérica de registros nem enfileira jobs com `run_as="actor"`; para membros, nada muda.

A auditoria do contador da sequência e do evento de calendário, assim como o próprio `DomainEvent`, registram a pessoa externa como ator e `source="http:portal"`.

## Privacidade e ciclo de vida (#536)

A exportação de dados pessoais lista os acessos externos (organização, app, situação e datas), enquanto a eliminação da identidade os desativa e os preserva como histórico — exigindo novo aceite em eventual convite futuro. Desativar a pessoa, o acesso ou a organização, ou desligar o portal, encerra o acesso ao portal já na requisição seguinte ([privacidade](privacy.md)).

## O que não existe

- **Autoinscrição externa:** o acesso é concedido apenas por convite.
- **Token Bearer para pessoa externa:** o portal funciona somente por sessão; tokens nunca abrem `external_context`.
- **Upload, remoção ou restauração de anexos pelo portal**, assim como links públicos ou assinados de arquivo.
- **Central de notificações no portal:** avisos a pessoas externas seguem apenas por email.
- **Gateway genérico de registros no portal** e qualquer privilégio de membro, mesmo para quem também seja membro da organização.
- **MFA obrigatória por organização para pessoas externas:** prevalece a preferência individual da pessoa.
