# Configurações geridas pela plataforma

Em uma instância que vende um produto, a plataforma pode fixar configurações que a organização cliente não deve alterar — como a marca do produto, a ativação das apps do nível contratado, parâmetros específicos de apps e o local onde os arquivos são armazenados. Esse conjunto corresponde ao [epic #700](https://github.com/alaorneto/skeleton/issues/700): travas de configuração ([#701](https://github.com/alaorneto/skeleton/issues/701)), campos de app geridos pela plataforma ([#702](https://github.com/alaorneto/skeleton/issues/702)), política das organizações novas ([#703](https://github.com/alaorneto/skeleton/issues/703)) e locais de armazenamento por organização ([#704](https://github.com/alaorneto/skeleton/issues/704), [#705](https://github.com/alaorneto/skeleton/issues/705), [#706](https://github.com/alaorneto/skeleton/issues/706); veja [arquivos](files.md#locais-de-armazenamento-da-plataforma)).

## Travas

Em `/platform/organizations/<id>/settings/`, o administrador da plataforma pode travar uma configuração específica de uma organização informando o valor e o motivo.

Ao aplicar a trava, o valor é gravado na organização pelo mesmo caminho e com os mesmos validadores do serviço dela. Enquanto a trava permanecer ativa, qualquer tentativa de um usuário da organização de alterar esse valor é recusada com `setting_locked` — na interface web, na API REST e na API de serviços. Destravar devolve a autonomia à organização, mantendo o valor gravado.

| Chave | O que trava |
|---|---|
| `organization.display_name`, `default_language`, `default_timezone`, `brand_color`, `business_hours`, `holidays`, `site_enabled` | Campos de `OrganizationSettings` |
| `organization.logo`, `organization.icon` | A imagem atual: troca e remoção ficam bloqueadas |
| `admission.discoverable`, `join_requests_enabled`, `join_links_enabled`, `signup_via_link_enabled`, `domain_join_enabled` | Política de admissão da organização |
| `apps.<app_label>.enabled` | Ativação da app (verdadeiro impede desligar; falso impede ligar). Bibliotecas first-party não possuem trava, pois acompanham as apps que as utilizam. |
| `notifications.<tipo>.enabled` | Aviso não obrigatório de organização ou de app: ligado ou desligado para todos os membros ([notificações](notifications.md#configuração-da-organização)) |
| `notifications.<tipo>.text` | Texto do aviso: edição e restauração ficam bloqueadas |
| `apps.<app_label>.<campo>` | Campo de configuração da app gerido pela organização |

As chaves traváveis são registradas por provedores via `skeleton.tenancy.setting_locks.register_setting_provider`: o pacote de organizações registra `organization.*` e `admission.*`, enquanto o sistema registra `apps.*` a partir dos manifestos instalados. Cada trava corresponde a uma linha global `OrganizationSettingLock` por organização e chave. Destravar aplica exclusão lógica, e travar novamente restaura a mesma linha, sempre com registro em auditoria.

**O que não é restringido.** A trava vale apenas para pessoas da organização. Infraestrutura e plataforma operando em `system_context` — como a cobrança ativando uma app contratada, a própria alteração da trava ou a política das organizações novas — continuam podendo atualizar o valor.

**O que a organização vê.** Na interface, o campo travado aparece desabilitado com o valor fixado e o aviso `"Definido pela plataforma."`, e os botões de trocar ou remover logos e ícones travados deixam de aparecer. Um POST forjado enviando outro valor recebe `setting_locked` (status 409 na tela) sem gravar nada.

Nas APIs, os campos travados são informados em:
- `OrganizationSettingsInfo.locked_fields` na API de serviços (desde 0.6.0), para configurações da organização;
- `ApplicationState.locked_fields` e o campo `locked_fields` de `GET /api/v1/o/<org>/apps/<app>/configuration/`, para configurações de app (incluindo campos geridos pela plataforma).

## Campos de app geridos pela plataforma

Um campo de configuração em um manifesto versão 2 pode declarar `"managed_by": "platform"` (o padrão é `"organization"`). O piso de core exigido é `PLATFORM_MANAGED_CONFIG_CORE_MINIMUM` (0.6.0), verificado no build e no `package doctor`.

Um campo com essa declaração segue três regras:
- nunca pode ser editado pela organização: a tela de configuração da app o exibe em modo somente leitura, omitir o campo na escrita preserva o valor atual e enviar um valor diferente retorna `setting_locked`;
- é gravado por organização pelo administrador da plataforma em `/platform/organizations/<id>/apps/<app_label>/configuration/` (`configure_application_by_platform`, com CAS pela revisão de `AppInstallation`; se a app ainda não estiver ativa, a linha nasce desativada com os valores padrão);
- continua acessível à app normalmente via `ApplicationService().my_configuration(...)`.

Campos secretos continuam proibidos no manifesto, independentemente de quem os gerencia.

## Política das organizações novas

Em `/platform/organizations/policy/`, o administrador da plataforma define, para qualquer chave travável ou campo de app gerido pela plataforma, uma regra aplicada automaticamente a toda organização criada a partir daquele momento:

| Regra | Efeito |
|---|---|
| **Valor inicial** (`default`) | A organização nasce com esse valor e pode alterá-lo depois, a menos que a regra também ative a trava |
| **Informado na criação** (`required`) | O formulário de nova organização (`/platform/organizations/new/`) exige o valor; sem ele, a organização não é criada |
| **Travar** (opção de qualquer regra, com motivo obrigatório) | A organização já nasce com a trava aplicada e com o motivo da regra |

As regras são aplicadas por `create_organization(..., settings={chave: valor})` dentro da mesma transação de criação, antes do evento `organization_created`: um valor ausente ou inválido desfaz toda a operação. Se a chave de uma regra deixar de existir (por exemplo, após desinstalar uma app), ela é ignorada com o evento `provisioning.rule_skipped` e aparece sinalizada na tela. Para campos de app já geridos pela plataforma, marcar a opção de travar não altera nada.

**Aplicar às existentes.** Regras de valor inicial podem ser aplicadas retroativamente às organizações ativas: o botão enfileira um job `tenancy.apply_provisioning_rule` por organização, executado em sua própria transação e no contexto de sistema daquela organização. Organizações que já tenham a chave travada não são alteradas. Regras do tipo "Informado na criação" não se aplicam a organizações existentes.

## Locais de armazenamento

O local de armazenamento de cada organização é escolhido pela plataforma na criação e só pode ser trocado por ela, sem ficar visível para a organização. Veja o catálogo, a resolução por organização e as regras de criação em [arquivos](files.md#locais-de-armazenamento-da-plataforma).

## Segurança

- A verificação das travas ocorre no servidor em todos os caminhos de escrita; o cliente nunca controla se um campo está travado.
- `system_context(reason="platform:setting-lock")` integra a lista fechada `PLATFORM_ADMIN_REASONS` e só pode ser aberto na requisição do administrador da plataforma (ou pelos jobs de aplicação da política), sempre uma organização por transação.
- `OrganizationSettingLock` e `OrganizationProvisioningRule` são tabelas globais com infraestrutura congelada e auditoria por trigger. Nenhum campo travável armazena segredos.
