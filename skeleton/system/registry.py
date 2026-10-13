from dataclasses import dataclass


@dataclass(frozen=True)
class TableSpec:
    model: type
    description: str
    label: str | None = None
    scope: str = "organization"
    read_fields: tuple = ()
    write_fields: tuple = ()
    sensitive_fields: tuple = ()
    expose_api: bool = False


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
        "sk_accounts.user": ("Usuários", "Identidade global, credenciais locais e estado de acesso à plataforma.", "global", ("password",)),
        "sk_accounts.userprofile": ("Perfis de usuários", "Preferências e apresentação pessoal da identidade, sem regras de negócio.", "global", ()),
        "sk_tenancy.organization": ("Organizações", "Fronteira de isolamento organizacional e estado de disponibilidade.", "global", ()),
        "sk_tenancy.membership": ("Membros", "Vínculo de uma identidade a uma organização; sua remoção revoga acesso local.", "organization", ()),
        "sk_tenancy.invitation": ("Convites", "Admissão de uso único, vinculada ao email, à organização e ao papel.", "organization", ("token_digest",)),
        "sk_access.role": ("Papéis", "Perfis empresariais que agregam permissões positivas de tabelas.", "organization", ()),
        "sk_access.memberrole": ("Papéis de membros", "Atribuição direta e revogável de um papel a um membro.", "organization", ()),
        "sk_access.accessgroup": ("Grupos", "Agrupamento empresarial usado para visibilidade e atribuição de papéis.", "organization", ()),
        "sk_access.groupmember": ("Membros de grupos", "Associação revogável de um membro a um grupo da mesma organização.", "organization", ()),
        "sk_access.grouprole": ("Papéis de grupos", "Papéis herdados pelos membros ativos de um grupo.", "organization", ()),
        "sk_access.tablepermission": ("Permissões de tabelas", "Concessão positiva de uma ação de tabela a um papel; ausência significa negação.", "organization", ()),
        "sk_access.recordgrant": ("Concessões de registros", "Compartilhamento explícito de leitura, edição ou remoção de um registro com um sujeito.", "organization", ()),
        "sk_system.tabledefinition": ("Catálogo de tabelas", "Inventário físico classificado, finalidade e contrato de segurança de cada tabela.", "global", ()),
        "sk_system.fielddefinition": ("Catálogo de campos", "Inventário físico de colunas, tipos, relações e sensibilidade de cada tabela.", "global", ()),
        "sk_system.appinstallation": ("Apps organizacionais", "Ativação e configuração declarada de extensões por organização.", "organization", ("configuration",)),
        "sk_system.componentinstallation": ("Componentes instalados", "Versão e procedência global do release confirmado.", "global", ()),
        "sk_system.deploymentoperation": ("Operações de deployment", "Plano aprovado e resultado auditado de instalação, atualização e desinstalação.", "global", ("plan", "result")),
        "sk_audit.auditevent": ("Auditoria", "Histórico transacional imutável de alterações, com dados sensíveis redigidos.", "global", ()),
    }
    for key, (label, description, scope, sensitive) in definitions.items():
        register(TableSpec(model=apps.get_model(key), label=label, description=description,
                           scope=scope, sensitive_fields=sensitive))
