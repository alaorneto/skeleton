# Portal: pessoas externas

O portal é a superfície onde uma **pessoa externa** (um cliente, um paciente, um fornecedor: alguém que não é membro) acessa o que uma app da organização expõe para ela, sem nenhum poder de membro e sem ocupar assento (epic #515). Este documento descreve o vínculo, o convite e a regra para as apps; o contrato de contextos e as negações estão em [controle de acesso](access-control.md#acesso-externo-portal).

## Vínculo e política

- A pessoa externa é um `User` comum, com login pelo allauth; "externo" é propriedade do vínculo **`ExternalAccess`** (organização, pessoa, app que a admitiu, ativo), separado de `Membership`. A mesma pessoa pode ser membro de uma organização e externa de outra, ou as duas coisas na mesma (no portal, só vale o externo).
- **`ExternalAccessPolicy`** liga o portal da organização (desligado sem linha) e diz a validade dos convites (padrão 72 horas, de 1 a 720). O administrador a edita em **Pessoas externas** (`/o/<org>/admin/external/`), com a capacidade `external.manage`, onde também vê os acessos e os convites pendentes e os revoga.
- A métrica de plano `external_people` conta os acessos ativos e é conferida no aceite; `members` nunca conta pessoas externas ([cobrança](billing.md)).

## Convite e aceite (#532)

Só há convite: não existe autoinscrição externa.

1. **Emitir.** A app chama `ExternalAccessService().invite(actor=..., email=..., app_label=..., subject_ref=..., idempotency_key=...)` depois de autorizar o ato dela (passando o próprio `app_label`, com a app ativa), ou um membro com `external.manage` convida. `subject_ref` é um UUID opaco que a app escolhe, o registro dela (o cliente). Um convite vivo para o mesmo `(app_label, subject_ref)` é revogado; a mesma `idempotency_key` devolve o mesmo convite, sem novo email. Com o portal desligado, `Conflict("external_disabled")`.
2. **Token.** O banco guarda só o resumo SHA-256; o token aparece uma vez, no link do email (`/portal/invite/<org>/<token>/`). `token_digest` é sensível e sai redigido na auditoria; `email` é dado pessoal (`[REDACTED]` na auditoria).
3. **Aceitar.** A pessoa abre o link, entra ou cria a conta com o email do convite e confirma o endereço; o **email verificado** da conta tem de ser o convidado. No aceite, em `system_context(reason="external:validated")`, o núcleo cria (ou reativa) o `ExternalAccess`, marca o convite e chama, **na mesma transação**, o manipulador que a app registrou; se ele falhar, o aceite inteiro é desfeito e o convite continua válido.
4. **Recusas neutras.** Token desconhecido, vencido, usado, revogado, de outra organização, email diferente ou não verificado respondem a mesma página. As tentativas de aceite são limitadas por pessoa e por IP; o limite do plano recusa o aceite e mantém o convite.
5. **Revogar.** `revoke(access_id=..., expected_revision=...)` desativa o acesso; o portal fecha na requisição seguinte e nada é apagado. `revoke_invitation` e `resend_invitation` (novo token, mesmo endereço e registro) usam CAS.

O manipulador da app é declarado no `ready()`:

```python
from skeleton.service_api.external import register_external_admission


def link_customer(access_id, user_id, subject_ref):
    PortalLink.objects.create(access_id=access_id, customer_id=subject_ref)


register_external_admission("acme_customers", link_customer)
```

## Serviço

`ExternalAccessService` ([API de serviços](service-api.md#catálogo-do-núcleo)):

| Método | Autorização | Erros |
|---|---|---|
| `invite(*, actor, email, app_label, subject_ref, idempotency_key) -> ExternalInvitationInfo` | `external.manage` ou a própria app | `Forbidden`; `Conflict("external_disabled")`; `Unavailable`; `InvalidInput` |
| `revoke_invitation(*, actor, invitation_id, expected_revision)`, `resend_invitation(...) -> ExternalInvitationInfo` | `external.manage` ou a app do convite | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` |
| `accesses(*, actor, app_label=None, cursor=None, page_size=25) -> Page[ExternalAccessInfo]`, `invitations(*, actor)` | `external.manage` | `Forbidden` |
| `access_for(*, actor, app_label, subject_ref) -> ExternalAccessInfo \| None` | `external.manage` ou a própria app | `Forbidden`; `InvalidInput` |
| `revoke(*, actor, access_id, expected_revision) -> ExternalAccessInfo` | `external.manage` ou a app que admitiu a pessoa | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `me(*, actor) -> ExternalAccessInfo` | só no portal (`allow_external=True`) | `Forbidden` fora do portal; `NotFound` |

`ExternalAccessInfo` tem `sysid`, `user_id`, `display_name`, `app_label`, `is_active` e `revision`; `ExternalInvitationInfo` tem `sysid`, `email`, `app_label`, `subject_ref`, `expires_at`, `accepted` e `revision`.

## Superfície (#533)

| Rota | O que é |
|---|---|
| `/portal/` | As organizações onde a pessoa tem acesso externo ativo e o portal ligado (autodescoberta, uma transação externa só de leitura por organização) |
| `/portal/<org>/` | As apps com portal ativas na organização |
| `/portal/<org>/apps/<label>/...` | As páginas de portal da app (`runtime.portal_urls`), no namespace `<label>_portal` |
| `/api/v1/portal/<org>/apps/<label>/...` | A API de portal da app (`runtime.portal_api_urls`), no namespace `<label>_portal_api`, só por sessão |
| `/portal/invite/<org>/<token>/` | O aceite de um convite |

- **Contexto.** Para `/portal/<org>/` e `/api/v1/portal/<org>/`, o middleware de tenant abre `external_context` em vez de `tenant_context`; qualquer falta (sem acesso, membro sem acesso externo, portal desligado, outra organização, token Bearer) responde 404 (a página pede login a quem não entrou). A raiz `portal/` é reservada: uma app não a captura, como `platform/` e `admin/`.
- **Estado da cobrança.** Organização `suspended`: o portal mostra uma página neutra de serviço indisponível (503); `restricted`: só leitura (`GET` funciona, métodos não seguros respondem 403).
- **Rotas da app.** Antes de cada view, o núcleo exige login, contexto externo e a app ativa e disponível (com a cobrança ligada, contratada); sem isso, 404. As páginas saem sem cache e com CSRF; a API responde `Cache-Control: no-store`. Uma rota de portal nunca é alcançável por `/o/<org>/` e o contrário também vale.
- **Decoradores.** `skeleton.web.views.portal_page` (login, sem cache, contexto externo; as recusas dos serviços viram 404) e `skeleton.api.views.PortalAPI` (só sessão, contexto externo) para as views da app.
- **Layout.** `skeleton/portal/base.html`, com a marca da organização e sem a navegação de membros; páginas sem JavaScript.
- **Manifesto.** `runtime.portal_urls` e `runtime.portal_api_urls` (módulos da própria app, em manifesto v2) exigem `compatibility.skeleton` com piso `>=0.6.0` (`PORTAL_CORE_MINIMUM`), e o `package build` o garante no artefato. O gerador de apps não cria portal por padrão.
- **OpenAPI.** As rotas `/api/v1/portal/` ficam fora do esquema dos membros (`/api/v1/openapi.json`); `skeleton.api.openapi.api_routes(portal=True)` lista só elas.

## Anexos (#534)

Uma tabela abre os anexos ao portal declarando `AttachmentPolicy(max_bytes=..., types=(...), external_reader=pode_ler)`, em que `pode_ler(record, user)` confere a ligação da app (o registro é do cliente ligado à pessoa, e foi liberado). O download fica em `/portal/<org>/files/<attachment_id>/`, e `FileService().external_download_url` dá o endereço ([arquivos](files.md#download-pelo-portal-534)). Não há upload, remoção nem link público.

## Regra para as apps

No portal, um serviço de app marcado `@service_method(..., allow_external=True)` autoriza pela **ligação da app** à pessoa, nunca por `authorized_queryset` (que nega em contexto externo):

```python
@service_method(read_only=True, since="X.Y.Z", allow_external=True)
def my_documents(self, *, actor) -> tuple[DocumentSummary, ...]:
    access = ExternalAccessService().me(actor=actor)  # Falha fora do contexto externo.
    customer_ids = PortalLink.objects.filter(access_id=access.sysid, is_active=True).values("customer_id")
    return tuple(summary(r) for r in Document.objects.filter(customer_id__in=customer_ids, released=True))
```

O método só devolve registros alcançados pela ligação; inexistente e não ligado respondem igual; nunca recebe identificador de cliente do cliente sem conferir a ligação; efeitos externos vão por job.
