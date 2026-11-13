from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


@dataclass(frozen=True)
class AttachmentPolicy:
    """Opt-in to attachments on a table: size limit and allowed MIME types (checked by content)."""

    max_bytes: int
    types: tuple[str, ...]


@dataclass(frozen=True)
class CommentPolicy:
    """Opt-in to comments on a table (#187): whoever reads a record reads and writes its comments."""

    enabled: bool = True


@dataclass(frozen=True)
class TableSpec:
    model: type
    description: "StrOrPromise"  # Translated lazily.
    label: "StrOrPromise | None" = None
    scope: str = "organization"
    read_fields: tuple = ()
    write_fields: tuple = ()
    sensitive_fields: tuple = ()
    expose_api: bool = False
    attachments: AttachmentPolicy | None = None
    comments: CommentPolicy | None = None
    # ((field, sequence key), ...): filled by the generic creation, never written by clients (#156).
    sequence_fields: tuple = ()
    # Text columns of the full-text search (#180): read_fields, text, never sensitive; the table's
    # migration installs the index with InstallSearchIndex.
    search_fields: tuple = ()
    # Personal data (#189): still readable through read_fields, but redacted by the audit
    # triggers, so the audit names the person only by sysid. Frozen with the infrastructure.
    personal_fields: tuple = ()

    def __post_init__(self):
        """Field lists are tuples of names: a bare string or a non-string item is refused at once (#310)."""
        for name in ("read_fields", "write_fields", "sensitive_fields"):
            value = getattr(self, name)
            if isinstance(value, str) or not all(isinstance(item, str) for item in value):
                raise TypeError(f"TableSpec.{name} must be a tuple of field names (got {value!r}).")
            object.__setattr__(self, name, tuple(value))
        if isinstance(self.sequence_fields, str):
            raise TypeError("TableSpec.sequence_fields must be a tuple of (field, sequence key) pairs.")
        # Items are validated by the sequence check, which reports them with the model's label.
        object.__setattr__(self, "sequence_fields", tuple(self.sequence_fields))


_registry = {}


def register(spec):
    key = spec.model._meta.label_lower
    if key in _registry and _registry[key] != spec:
        raise ValueError(f"Duplicate table specification: {key}")
    _registry[key] = spec
    return spec


def get_spec(key):
    return _registry[key]


def all_specs():
    return tuple(_registry[key] for key in sorted(_registry))


def register_core_specs():
    """Explicit core inventory; an undeclared new model fails system checks."""
    from django.apps import apps

    definitions = {
        "sk_accounts.user": (
            gettext_lazy("Usuários"),
            gettext_lazy("Identidade global, credenciais locais e estado de acesso à plataforma."),
            "global",
            ("password",),
        ),
        "sk_accounts.userprofile": (
            gettext_lazy("Perfis de usuários"),
            gettext_lazy("Preferências e apresentação pessoal da identidade, sem regras de negócio."),
            "global",
            (),
        ),
        "sk_accounts.identityprovider": (
            "Provedores de identidade",
            "Provedores OIDC, SAML e LDAP de cada organização, cadastrados pela plataforma; segredos cifrados.",
            "global",
            ("secret", "private_key"),
        ),
        "sk_accounts.platformroleassignment": (
            "Papéis de plataforma",
            "Auditor e operador de releases: papéis limitados de plataforma, concedidos pelo administrador.",
            "global",
            (),
        ),
        "sk_accounts.erasurerequest": (
            "Pedidos de eliminação",
            "Pedido de uma pessoa para anonimizar a identidade, aprovado pela plataforma (#191).",
            "global",
            (),
        ),
        "sk_tenancy.organization": (
            gettext_lazy("Organizações"),
            gettext_lazy("Fronteira de isolamento organizacional e estado de disponibilidade."),
            "global",
            (),
        ),
        "sk_tenancy.membership": (
            gettext_lazy("Membros"),
            gettext_lazy("Vínculo de uma identidade a uma organização; sua remoção revoga acesso local."),
            "organization",
            (),
        ),
        "sk_tenancy.invitation": (
            gettext_lazy("Convites"),
            gettext_lazy("Admissão de uso único, vinculada ao email, à organização e ao papel."),
            "organization",
            ("token_digest",),
        ),
        "sk_tenancy.organizationsettings": (
            gettext_lazy("Configurações da organização"),
            gettext_lazy("Nome de exibição, idioma e fuso padrão e marca da organização, editados pelo administrador."),
            "organization",
            ("logo_key",),
        ),
        "sk_tenancy.organizationadmissionpolicy": (
            "Política de admissão",
            "Como pessoas de fora pedem para entrar na organização; sem linha, tudo desligado.",
            "organization",
            (),
        ),
        "sk_tenancy.organizationdirectoryentry": (
            "Diretório de organizações",
            "Índice global de quem aparece em /join/ e aceita pedidos, espelhado da política de admissão.",
            "global",
            (),
        ),
        "sk_tenancy.joinlink": (
            "Links de ingresso",
            "Links multiuso revogáveis; só o resumo SHA-256 do token é guardado.",
            "organization",
            ("token_digest",),
        ),
        "sk_tenancy.verifieddomain": (
            "Domínios verificados",
            "Domínios de email que a organização prova controlar por DNS; admitem quem confirma email do domínio.",
            "organization",
            (),
        ),
        "sk_tenancy.domainclaim": (
            "Posse de domínios",
            "Índice global de qual organização verificou cada domínio, lido antes de haver tenant.",
            "global",
            (),
        ),
        "sk_tenancy.identityprovideradmission": (
            "Admissão por provedor de identidade",
            "Regras da organização para o seu provedor: JIT, aprovação, papel padrão e domínios permitidos.",
            "organization",
            (),
        ),
        "sk_tenancy.joinrequest": (
            "Solicitações de ingresso",
            "Pedido de uma pessoa para entrar; a aprovação cria o vínculo com o papel indicado.",
            "organization",
            (),
        ),
        "sk_access.role": (
            gettext_lazy("Papéis"),
            gettext_lazy("Perfis empresariais que agregam permissões positivas de tabelas."),
            "organization",
            (),
        ),
        "sk_access.memberrole": (
            gettext_lazy("Papéis de membros"),
            gettext_lazy("Atribuição direta e revogável de um papel a um membro."),
            "organization",
            (),
        ),
        "sk_access.accessgroup": (
            gettext_lazy("Grupos"),
            gettext_lazy("Agrupamento empresarial usado para visibilidade e atribuição de papéis."),
            "organization",
            (),
        ),
        "sk_access.groupmember": (
            gettext_lazy("Membros de grupos"),
            gettext_lazy("Associação revogável de um membro a um grupo da mesma organização."),
            "organization",
            (),
        ),
        "sk_access.grouprole": (
            gettext_lazy("Papéis de grupos"),
            gettext_lazy("Papéis herdados pelos membros ativos de um grupo."),
            "organization",
            (),
        ),
        "sk_access.tablepermission": (
            gettext_lazy("Permissões de tabelas"),
            gettext_lazy("Concessão positiva de uma ação de tabela a um papel; ausência significa negação."),
            "organization",
            (),
        ),
        "sk_access.recordgrant": (
            gettext_lazy("Concessões de registros"),
            gettext_lazy("Compartilhamento explícito de leitura, edição ou remoção de um registro com um sujeito."),
            "organization",
            (),
        ),
        "sk_system.tabledefinition": (
            gettext_lazy("Catálogo de tabelas"),
            gettext_lazy("Inventário físico classificado, finalidade e contrato de segurança de cada tabela."),
            "global",
            (),
        ),
        "sk_system.fielddefinition": (
            gettext_lazy("Catálogo de campos"),
            gettext_lazy("Inventário físico de colunas, tipos, relações e sensibilidade de cada tabela."),
            "global",
            (),
        ),
        "sk_system.appinstallation": (
            gettext_lazy("Apps organizacionais"),
            gettext_lazy("Ativação e configuração declarada de extensões por organização."),
            "organization",
            ("configuration",),
        ),
        "sk_system.appavailability": (
            gettext_lazy("Disponibilidade de apps"),
            gettext_lazy("Portão da plataforma: app disponível para todas as organizações ou só para as selecionadas."),
            "global",
            (),
        ),
        "sk_system.apporganizationgrant": (
            gettext_lazy("Concessões de apps"),
            gettext_lazy("Organização admitida pela plataforma a uma app em modo selecionado."),
            "global",
            (),
        ),
        "sk_system.componentinstallation": (
            gettext_lazy("Componentes instalados"),
            gettext_lazy("Versão e procedência global do release confirmado."),
            "global",
            (),
        ),
        "sk_system.deploymentoperation": (
            gettext_lazy("Operações de deployment"),
            gettext_lazy("Plano aprovado e resultado auditado de instalação, atualização e desinstalação."),
            "global",
            ("plan", "result"),
        ),
        "sk_jobs.job": (
            gettext_lazy("Jobs"),
            gettext_lazy("Fila de trabalho em segundo plano; payload só com identificadores, sem dados de negócio."),
            "global",
            (),
        ),
        "sk_jobs.workerheartbeat": (
            "Workers",
            gettext_lazy("Último sinal de vida de cada worker; leases de um worker silencioso expiram."),
            "global",
            (),
        ),
        "sk_events.domainevent": (
            gettext_lazy("Eventos de domínio"),
            gettext_lazy("Outbox de eventos publicados na transação da escrita; só campos declarados e não sensíveis."),
            "organization",
            (),
        ),
        "sk_notifications.notification": (
            gettext_lazy("Notificações"),
            gettext_lazy("Fato notificado, alvo e especificação dos destinatários; snapshot só com campos declarados."),
            "organization",
            (),
        ),
        "sk_notifications.notificationrecipient": (
            gettext_lazy("Notificações por pessoa"),
            gettext_lazy("Cópia da notificação de cada pessoa, com lida, arquivada e contagem agrupada."),
            "organization",
            (),
        ),
        "sk_notifications.usernotificationpreference": (
            gettext_lazy("Preferências de notificação"),
            gettext_lazy(
                "Modo de cada canal (in-app, email, push) por tipo de notificação, escolhido pela própria pessoa."
            ),
            "global",
            (),
        ),
        "sk_notifications.notificationdelivery": (
            gettext_lazy("Entregas de notificação"),
            gettext_lazy("Email, resumo ou push de uma notificação a uma pessoa, com tentativas e último resultado."),
            "organization",
            (),
        ),
        "sk_notifications.webpushsubscription": (
            gettext_lazy("Assinaturas de push"),
            gettext_lazy("Assinatura de web push de um navegador da pessoa; endpoint e chaves cifrados."),
            "global",
            ("endpoint", "endpoint_digest", "p256dh", "auth_secret"),
        ),
        "sk_files.attachment": (
            gettext_lazy("Anexos"),
            gettext_lazy("Metadados de arquivos anexados a registros; os bytes ficam no armazenamento privado."),
            "organization",
            ("storage_key",),
        ),
        "sk_files.storageusage": (
            gettext_lazy("Uso de armazenamento"),
            gettext_lazy("Bytes dos anexos vivos de cada organização, contador da cota sob lock."),
            "organization",
            (),
        ),
        "sk_files.storagequota": (
            gettext_lazy("Cotas de armazenamento"),
            gettext_lazy("Cota de anexos definida pela plataforma para uma organização; sem ela vale o padrão."),
            "global",
            (),
        ),
        "sk_integrations.webhookendpoint": (
            gettext_lazy("Webhooks"),
            gettext_lazy("URLs HTTPS que recebem eventos da organização, assinadas com segredo cifrado."),
            "organization",
            ("secret_ciphertext", "previous_secret_ciphertext"),
        ),
        "sk_integrations.apitoken": (
            gettext_lazy("Tokens de API"),
            gettext_lazy(
                "Credenciais Bearer de /api/v1/ com prefixo, escopos e validade; só o digest do segredo é guardado."
            ),
            "global",
            ("digest",),
        ),
        "sk_integrations.apitokenactivity": (
            "Uso de tokens de API",
            gettext_lazy("Último uso de cada token, gravado no máximo a cada 5 minutos; operacional, sem auditoria."),
            "global",
            (),
        ),
        "sk_integrations.webhookdelivery": (
            gettext_lazy("Entregas de webhook"),
            gettext_lazy("Corpo fixo com identificadores, tentativas e último resultado de cada evento por endpoint."),
            "organization",
            (),
        ),
        "sk_integrations.webhookendpointhealth": (
            gettext_lazy("Saúde dos webhooks"),
            gettext_lazy("Falhas seguidas por endpoint; o limite desativa o endpoint."),
            "organization",
            (),
        ),
        "sk_transfer.transferjob": (
            "Exportações e importações",
            "Pedidos de exportação e importação CSV, com o arquivo no storage privado até a retenção.",
            "organization",
            ("file_key", "report_key"),
        ),
        "sk_sequences.sequencecounter": (
            gettext_lazy("Sequências"),
            gettext_lazy("Último número entregue de cada sequência declarada, por organização e período."),
            "organization",
            (),
        ),
        "sk_flows.flowdefinition": (
            gettext_lazy("Definições de fluxo"),
            gettext_lazy("Especificação congelada de uma versão de fluxo de processo na organização."),
            "organization",
            (),
        ),
        "sk_flows.flowdraft": (
            gettext_lazy("Rascunhos de fluxo"),
            gettext_lazy("Fluxo definido em dados pela organização, com a versão publicada (#414)."),
            "organization",
            (),
        ),
        "sk_flows.flowabsence": (
            gettext_lazy("Ausências em fluxos"),
            gettext_lazy("Período em que as tarefas de fluxo de uma pessoa vão para um substituto (#418)."),
            "organization",
            (),
        ),
        "sk_flows.flowinstance": (
            gettext_lazy("Instâncias de fluxo"),
            gettext_lazy("Execução de um fluxo sobre um registro de negócio; dados sensíveis de formulário à parte."),
            "organization",
            ("sensitive_data",),
        ),
        "sk_flows.flowsteprun": (
            gettext_lazy("Etapas de instâncias"),
            gettext_lazy("Ativação de uma etapa numa instância de fluxo, com escopo de bloco e ramo."),
            "organization",
            (),
        ),
        "sk_flows.flowtask": (
            gettext_lazy("Tarefas de fluxo"),
            gettext_lazy("Trabalho de uma pessoa ou fila numa etapa de tarefa ou de aprovação, com decisão e prazo."),
            "organization",
            ("comment",),
        ),
        "sk_flows.flowsubmission": (
            gettext_lazy("Formulários de fluxo"),
            gettext_lazy("Formulário enviado numa etapa ou transição, guardado como foi aceito."),
            "organization",
            ("sensitive_values",),
        ),
        "sk_flows.flowactivity": (
            gettext_lazy("Histórico de fluxos"),
            gettext_lazy(
                "Narrativa ordenada de uma instância: quem fez o quê e quando, só com identificadores e rótulos."
            ),
            "organization",
            ("comment",),
        ),
        "sk_comments.comment": (
            "Comentários",
            "Texto simples de uma pessoa sobre um registro; a autorização vem do registro alvo.",
            "organization",
            (),
        ),
        "sk_billing.product": (
            gettext_lazy("Produtos de cobrança"),
            gettext_lazy("O que a plataforma vende: plano base, app ou pacote de armazenamento."),
            "global",
            (),
        ),
        "sk_billing.price": (
            gettext_lazy("Preços"),
            gettext_lazy("Preço de um produto, com moeda, ciclo, trial e franquias; os termos nunca mudam."),
            "global",
            (),
        ),
        "sk_billing.invoicecounter": (
            gettext_lazy("Numeração de faturas"),
            gettext_lazy("Último número de fatura de cada ano, na plataforma inteira, sob lock."),
            "global",
            (),
        ),
        "sk_billing.billingaccount": (
            gettext_lazy("Contas de cobrança"),
            gettext_lazy("Quem paga pela organização e o estado de acesso dela; dados de contato redigidos."),
            "organization",
            (),
        ),
        "sk_billing.subscription": (
            gettext_lazy("Assinaturas"),
            gettext_lazy("Contrato recorrente da organização, com moeda, ciclo e período corrente."),
            "organization",
            (),
        ),
        "sk_billing.subscriptionitem": (
            gettext_lazy("Itens de assinatura"),
            gettext_lazy("Plano, app ou pacote de armazenamento de uma assinatura, com datas e situação."),
            "organization",
            (),
        ),
        "sk_billing.invoice": (
            gettext_lazy("Faturas"),
            gettext_lazy("Valor devido pela organização; número e valores imutáveis depois de emitida."),
            "organization",
            (),
        ),
        "sk_billing.invoiceline": (
            gettext_lazy("Linhas de fatura"),
            gettext_lazy("Item cobrado, proporcional, excedente ou desconto de uma fatura; nunca muda."),
            "organization",
            (),
        ),
        "sk_billing.payment": (
            gettext_lazy("Pagamentos"),
            gettext_lazy("Tentativa de pagamento de uma fatura por um provedor ou registrada pela plataforma."),
            "organization",
            (),
        ),
        "sk_audit.auditevent": (
            gettext_lazy("Auditoria"),
            gettext_lazy("Histórico transacional imutável de alterações, com dados sensíveis redigidos."),
            "global",
            (),
        ),
    }
    # Personal data redacted in the audit (#189); the triggers name the person only by sysid.
    personal_fields = {
        "sk_accounts.user": ("email", "first_name", "last_name"),
        "sk_accounts.userprofile": ("display_name",),
        # Form data may carry anyone's personal data: the audit never copies it (#422).
        "sk_flows.flowinstance": ("data",),
        "sk_flows.flowsubmission": ("values",),
        "sk_billing.billingaccount": ("legal_name", "billing_email", "tax_id"),
    }
    # Identifiers the core lifecycle events carry (#43); everything else stays unreadable.
    read_fields = {
        "sk_tenancy.membership": ("user",),
        "sk_system.appinstallation": ("app_label",),
        # What the flow lifecycle events carry (docs/flows.md); form data is never an event field.
        "sk_flows.flowinstance": ("flow_key", "flow_version", "status", "outcome", "subject_table", "subject_sysid"),
        # What the billing events carry (#396); contact data of the account never.
        "sk_billing.invoice": ("number", "status", "currency", "total"),
        "sk_billing.subscription": ("status",),
    }
    for key, (label, description, scope, sensitive) in definitions.items():
        register(
            TableSpec(
                model=apps.get_model(key),
                label=label,
                description=description,
                scope=scope,
                read_fields=read_fields.get(key, ()),
                sensitive_fields=sensitive,
                personal_fields=personal_fields.get(key, ()),
            )
        )
