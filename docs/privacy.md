# Privacidade: direitos do titular

O núcleo atende os direitos da pessoa sobre os dados que ele mesmo guarda: a identidade global, o perfil, os tokens e, em cada organização, o vínculo, as notificações e os anexos enviados. Os registros de negócio pertencem a cada organização, que é a controladora deles, e ficam fora daqui.

A consulta opcional de versão nova ao GitHub (`SKELETON_UPDATE_REPOSITORY`, #502) não envia dado pessoal nem identificação da instalação: só o pedido do release mais recente, com `User-Agent: django-skeleton`; o GitHub vê o endereço IP do servidor, por isso ela é desligada por padrão.

## Endereço próprio da organização

Ligar o endereço próprio (`/o/<org>/settings/`, seção "Endereço próprio", [#480](https://github.com/alaorneto/skeleton/issues/480)) torna públicos, para qualquer pessoa que abra `<slug>.<domínio base>`, mesmo sem login: o nome de exibição, a cor da marca, o logo, o ícone e o idioma padrão da organização. A tela avisa disso antes da ativação. Esses valores são copiados para o índice global `OrganizationSite`, que guarda só eles e só enquanto o endereço está ligado; desligar esvazia a cópia. Nenhum dado pessoal nem dado de negócio vai para o índice. O logo publicado é servido sem login em `/site/logo/` do host da organização ([arquivos](files.md#armazenamento-privado)). A exportação dos próprios dados é sempre completa, com todas as organizações da pessoa, mesmo pedida no endereço de uma organização (#483).

Com o endereço ligado, a **página pública** que o administrador publicar ([#623](organization-sites.md#página-pública)) também fica visível sem login no endereço: são só os blocos de texto que ele escreveu (títulos, parágrafos, contato e links), copiados para o índice global `OrganizationSitePage` enquanto a página estiver publicada e o endereço ligado; despublicar ou desligar esvazia a cópia. Nenhum dado de membro, de pessoa ou de registro entra na página; um e-mail ou telefone de contato num bloco fica público por decisão de quem o escreveu.

## Marca da instância

O nome do produto, o rodapé, o logo, o logo para o modo escuro, o ícone e o tema ativo da instância ([aparência](appearance.md), #591) são públicos por decisão do administrador da plataforma: aparecem no login e são servidos sem autenticação em `/brand/` ([arquivos](files.md#armazenamento-privado)), também nos endereços das organizações. É uma segunda saída de bytes do armazenamento privado sem login, limitada às três chaves da linha única `InstanceBrand`. Nenhum dado pessoal nem dado de organização entra nessa marca; os logs e os eventos de segurança das alterações não levam valores.

## Página inicial pública da plataforma

A página inicial pública (`/platform/appearance/home/`, [#622](appearance.md#página-inicial-pública)) é conteúdo escrito pelo administrador da plataforma para ser público: quem abre a raiz da plataforma sem login a vê enquanto ela estiver publicada. São só os blocos de texto que ele escreveu (títulos, parágrafos, contato e links); a página não lê nada de organização nem de pessoa, e o evento de segurança da alteração não leva o conteúdo. Um e-mail ou telefone de contato posto num bloco fica público por decisão de quem o escreveu.

## Exportação dos próprios dados

Em `/profile/privacy/`, a pessoa baixa um ZIP com `dados.json` ([#190](https://github.com/alaorneto/skeleton/issues/190)). O arquivo traz:

- a identidade (email, nomes, criação, último login) e o perfil (nome de exibição, idioma, fuso);
- os metadados dos tokens de API: prefixo, nome, escopos, validade e revogação, nunca o digest;
- em cada organização em que a pessoa tem vínculo ativo, o vínculo, as notificações recebidas (tipo, datas, alvo e snapshot), os metadados dos anexos que ela enviou e os formulários de fluxo que ela enviou (`flow_submissions`: fluxo, instância, etapa, data e valores, inclusive os sensíveis, que são dela; [#422](https://github.com/alaorneto/skeleton/issues/422)).

- os acessos externos ao [portal](portal.md) (#536): organização, app que admitiu a pessoa, situação, criação e último acesso; nada sobre terceiros (os dados que uma app guarda sobre a pessoa são responsabilidade da app).

A exportação é montada na própria requisição. Ela é uma rota agregada que lê cada organização num contexto só de leitura, com o vínculo revalidado. O ZIP sai como anexo e com `no-store`, então não há arquivo guardado para proteger ou expirar. O download exige autenticação recente e grava o evento de segurança `privacy.export`. Nunca aparecem dados de outra pessoa, segredos ou registros de negócio.

## Eliminação da identidade

Os acessos externos ao portal (#536) são desativados pelo job de cada organização e ficam como histórico, como os vínculos; um convite posterior exige um novo aceite, que reativa a mesma linha.

A pessoa pede a eliminação na mesma página, `/profile/privacy/` ([#191](https://github.com/alaorneto/skeleton/issues/191)). O pedido exige autenticação recente e só cria um `ErasureRequest` (`sk_accounts.erasurerequest`, global e auditado) em `pending`: nada mais muda. Enquanto está pendente, a pessoa pode retirá-lo. Há no máximo um pedido pendente por pessoa, e contas de serviço são recusadas (elas se desativam pela organização).

Um administrador da plataforma decide em `/platform/privacy/`, por POST com CSRF, autenticação recente e a revisão que viu (CAS). Ninguém decide o próprio pedido. Recusar só marca `rejected`. Aprovar anonimiza a identidade na mesma transação da decisão:

- email `anon-<sysid>@erased.invalid`, nomes vazios, `is_active=False`, senha inutilizável e senha local desligada;
- `auth_version` incrementado, então toda sessão aberta termina na próxima requisição;
- perfil de volta ao padrão (nome de exibição, idioma e fuso vazios);
- emails, contas sociais e autenticadores MFA da allauth apagados;
- tokens de API revogados e excluídos, preferências de notificação e inscrições de push excluídas.

A aprovação passa pela mesma guarda da desativação (`skeleton.assert_user_deactivation`): quem é o último administrador ativo de alguma organização não é anonimizado, e a mensagem pede que outro administrador seja designado antes. A guarda mantém as travas das organizações até o commit, então ninguém torna a pessoa a última administradora no meio da operação. Qualquer recusa desfaz a transação inteira e o pedido continua pendente.

A mesma transação enfileira um job `accounts.forget_in_organization` por organização. Cada um roda no contexto da própria organização, porque as linhas de tenant só são alcançáveis ali, e apaga as linhas de caixa de entrada e as entregas de notificação da pessoa (`forget_person`, ao lado da retenção) e desativa os webhooks que agem como ela. Nos fluxos de processo, ele remove dos formulários que a pessoa enviou os campos declarados `personal=True`, e também dos dados da instância enquanto eles guardam o valor que ela enviou ([#422](https://github.com/alaorneto/skeleton/issues/422)); os outros campos e os envios de outras pessoas ficam. O job é idempotente: rodar de novo não encontra mais nada.

Os registros de negócio das organizações não mudam: continuam apontando para a mesma identidade, agora anônima (as relações são `PROTECT`), e os vínculos ficam como histórico. A auditoria antiga também fica; os `personal_fields` já saem `[REDACTED]` nos eventos novos e o comando `redact_audit_personal_data` (#189) redige os antigos. A eliminação não tem volta: a aprovação é a salvaguarda.

Diferenças em relação ao texto da issue: o pedido fica em `/profile/privacy/`, a página do titular que já existe, e não em `/me/privacy/`. O domínio é `erased.invalid`, porque o validador de email recusa um domínio sem ponto. A anonimização da identidade acontece na aprovação, e não num job global: a guarda do último administrador no banco só aceita o plano de controle da plataforma, e fazê-la na mesma transação elimina a janela entre a aprovação e a execução. Os jobs ficam com a limpeza por organização.
