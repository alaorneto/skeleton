# Configurações geridas pela plataforma

Numa instância que vende um produto, a plataforma define valores que a organização cliente não pode trocar: a marca do produto, a ativação das apps do nível, parâmetros de app provisionados por ela e o local onde ficam os arquivos. É o [epic #700](https://github.com/alaorneto/skeleton/issues/700): travas ([#701](https://github.com/alaorneto/skeleton/issues/701)), campos de app geridos pela plataforma ([#702](https://github.com/alaorneto/skeleton/issues/702)), a política das organizações novas ([#703](https://github.com/alaorneto/skeleton/issues/703)) e o armazenamento por organização ([#704](https://github.com/alaorneto/skeleton/issues/704), [#705](https://github.com/alaorneto/skeleton/issues/705), [#706](https://github.com/alaorneto/skeleton/issues/706); [arquivos](files.md#locais-de-armazenamento-da-plataforma)).

## Travas

O administrador da plataforma trava uma configuração de uma organização com um valor e um motivo, em `/platform/organizations/<id>/settings/`. Travar grava o valor na organização, pelo mesmo caminho de gravação e com os mesmos validadores do serviço dela, e, enquanto a trava existir, toda escrita de uma pessoa da organização que mudaria o valor é recusada com `setting_locked`: pela tela, pela API REST e pela API de serviços. Destravar devolve a escolha à organização e mantém o valor gravado.

| Chave | O que trava |
|---|---|
| `organization.display_name`, `default_language`, `default_timezone`, `brand_color`, `business_hours`, `holidays`, `site_enabled` | Os campos de `OrganizationSettings` |
| `organization.logo`, `organization.icon` | A imagem atual: trocar e remover ficam recusados |
| `admission.discoverable`, `join_requests_enabled`, `join_links_enabled`, `signup_via_link_enabled`, `domain_join_enabled` | A política de admissão |
| `apps.<app_label>.enabled` | A ativação da app (verdadeiro: a organização não a desliga; falso: não a liga) |
| `notifications.<tipo>.enabled` | Um aviso de organização ou de app não obrigatório: ligado ou desligado para todos os membros ([notificações](notifications.md#configuração-da-organização)) |
| `notifications.<tipo>.text` | O texto do aviso: trocar e restaurar ficam recusados |
| `apps.<app_label>.<campo>` | Um campo de configuração da app gerido pela organização |

O registro é por provedores (`skeleton.tenancy.setting_locks.register_setting_provider`): o pacote de organizações registra `organization.*` e `admission.*`, e o sistema registra `apps.*` a partir dos manifestos instalados no momento. Uma trava é uma linha global `OrganizationSettingLock` por organização e chave; destravar é exclusão lógica e travar de novo restaura a mesma linha, com auditoria das operações.

**O que não é restringido.** A trava vale para as pessoas da organização. Infraestrutura e plataforma, em `system_context` (a cobrança ativando uma app contratada, a própria trava, a política das organizações novas), continuam podendo mudar o valor.

**O que a organização vê.** O campo travado aparece desabilitado com "Definido pela plataforma." e o valor travado; um POST forjado com outro valor recebe `setting_locked` (409 na tela) e nada é gravado. Os botões de trocar e remover um logo ou ícone travado somem. `OrganizationSettingsInfo.locked_fields` (API de serviços, desde 0.6.0) traz os campos travados das configurações, e `ApplicationState.locked_fields` e o `locked_fields` de `GET /api/v1/o/<org>/apps/<app>/configuration/` os da app (inclusive os campos geridos pela plataforma).

## Campos de app geridos pela plataforma

Um campo de configuração de um manifesto versão 2 pode declarar `"managed_by": "platform"` (o padrão é `"organization"`); o piso de core é `PLATFORM_MANAGED_CONFIG_CORE_MINIMUM` (0.6.0), conferido no build e no `package doctor`. Esse campo:

- nunca é editável pela organização: a tela de configuração da app o mostra só para leitura, um valor ausente mantém o gravado e um diferente é recusado com `setting_locked`;
- é gravado pelo administrador da plataforma por organização, em `/platform/organizations/<id>/apps/<app_label>/configuration/` (`configure_application_by_platform`, com CAS pela revisão de `AppInstallation`; sem a app ativa, a linha nasce desativada com os padrões);
- continua sendo lido pela app por `ApplicationService().my_configuration(...)`.

Campos secretos continuam proibidos no manifesto, geridos por quem for.

## Política das organizações novas

Em `/platform/organizations/policy/`, o administrador da plataforma define, por chave travável ou campo de app gerido pela plataforma, uma regra que toda organização criada depois recebe:

| Regra | Efeito |
|---|---|
| **Valor inicial** (`default`) | A organização nasce com o valor; pode mudá-lo, a menos que a regra trave |
| **Informado na criação** (`required`) | O formulário de nova organização (`/platform/organizations/new/`) pede o valor; sem ele, nada é criado |
| **Travar** (opção de qualquer regra, com motivo obrigatório) | A organização nasce com a trava, com o motivo da regra |

As regras são aplicadas por `create_organization(..., settings={chave: valor})` na mesma transação da criação, antes de `organization_created`: um valor ausente ou inválido desfaz a criação inteira. Uma regra cuja chave sumiu (uma app desinstalada) é ignorada, com o evento `provisioning.rule_skipped`, e aparece marcada na página. Um campo de app gerido pela plataforma já é da plataforma: travar não acrescenta nada a ele.

**Aplicar às existentes.** Uma regra de valor inicial pode ser aplicada às organizações ativas: o botão enfileira um job `tenancy.apply_provisioning_rule` por organização, cada um na própria transação e no contexto de sistema daquela organização. Uma organização em que a chave já está travada fica como está. Regras exigidas na criação não se aplicam às existentes.

## Locais de armazenamento

O local de armazenamento de cada organização é escolhido pela plataforma na criação e trocado só por ela, sem que a organização veja qual é. O catálogo, a resolução por organização e a criação estão em [arquivos](files.md#locais-de-armazenamento-da-plataforma).

## Segurança

- A trava é conferida no servidor em todos os caminhos de escrita; o cliente não escolhe se um campo está travado.
- `system_context(reason="platform:setting-lock")` está na lista fechada `PLATFORM_ADMIN_REASONS` e só é aberto a partir da requisição do administrador da plataforma (e pelos jobs de aplicação da política), uma organização por transação.
- As tabelas `OrganizationSettingLock` e `OrganizationProvisioningRule` são globais, com infraestrutura congelada e auditoria por trigger. Os campos traváveis não são segredos.
