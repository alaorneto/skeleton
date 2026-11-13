# Privacidade: direitos do titular

O núcleo atende os direitos da pessoa sobre os dados que ele mesmo guarda: a identidade global, o perfil, os tokens e, em cada organização, o vínculo, as notificações e os anexos enviados. Os registros de negócio pertencem a cada organização, que é a controladora deles, e ficam fora daqui.

## Exportação dos próprios dados

Em `/profile/privacy/`, a pessoa baixa um ZIP com `dados.json` ([#190](https://github.com/alaorneto/skeleton/issues/190)). O arquivo traz:

- a identidade (email, nomes, criação, último login) e o perfil (nome de exibição, idioma, fuso);
- os metadados dos tokens de API: prefixo, nome, escopos, validade e revogação, nunca o digest;
- em cada organização em que a pessoa tem vínculo ativo, o vínculo, as notificações recebidas (tipo, datas, alvo e snapshot) e os metadados dos anexos que ela enviou.

A exportação é montada na própria requisição. Ela é uma rota agregada que lê cada organização num contexto só de leitura, com o vínculo revalidado. O ZIP sai como anexo e com `no-store`, então não há arquivo guardado para proteger ou expirar. O download exige autenticação recente e grava o evento de segurança `privacy.export`. Nunca aparecem dados de outra pessoa, segredos ou registros de negócio.

## Eliminação da identidade

A pessoa pede a eliminação na mesma página, `/profile/privacy/` ([#191](https://github.com/alaorneto/skeleton/issues/191)). O pedido exige autenticação recente e só cria um `ErasureRequest` (`sk_accounts.erasurerequest`, global e auditado) em `pending`: nada mais muda. Enquanto está pendente, a pessoa pode retirá-lo. Há no máximo um pedido pendente por pessoa, e contas de serviço são recusadas (elas se desativam pela organização).

Um administrador da plataforma decide em `/platform/privacy/`, por POST com CSRF, autenticação recente e a revisão que viu (CAS). Ninguém decide o próprio pedido. Recusar só marca `rejected`. Aprovar anonimiza a identidade na mesma transação da decisão:

- email `anon-<sysid>@erased.invalid`, nomes vazios, `is_active=False`, senha inutilizável e senha local desligada;
- `auth_version` incrementado, então toda sessão aberta termina na próxima requisição;
- perfil de volta ao padrão (nome de exibição, idioma e fuso vazios);
- emails, contas sociais e autenticadores MFA da allauth apagados;
- tokens de API revogados e excluídos, preferências de notificação e inscrições de push excluídas.

A aprovação passa pela mesma guarda da desativação (`skeleton.assert_user_deactivation`): quem é o último administrador ativo de alguma organização não é anonimizado, e a mensagem pede que outro administrador seja designado antes. A guarda mantém as travas das organizações até o commit, então ninguém torna a pessoa a última administradora no meio da operação. Qualquer recusa desfaz a transação inteira e o pedido continua pendente.

A mesma transação enfileira um job `accounts.forget_in_organization` por organização. Cada um roda no contexto da própria organização, porque as linhas de tenant só são alcançáveis ali, e apaga as linhas de caixa de entrada e as entregas de notificação da pessoa (`forget_person`, ao lado da retenção) e desativa os webhooks que agem como ela. O job é idempotente: rodar de novo não encontra mais nada.

Os registros de negócio das organizações não mudam: continuam apontando para a mesma identidade, agora anônima (as relações são `PROTECT`), e os vínculos ficam como histórico. A auditoria antiga também fica; os `personal_fields` já saem `[REDACTED]` nos eventos novos e o comando `redact_audit_personal_data` (#189) redige os antigos. A eliminação não tem volta: a aprovação é a salvaguarda.

Diferenças em relação ao texto da issue: o pedido fica em `/profile/privacy/`, a página do titular que já existe, e não em `/me/privacy/`. O domínio é `erased.invalid`, porque o validador de email recusa um domínio sem ponto. A anonimização da identidade acontece na aprovação, e não num job global: a guarda do último administrador no banco só aceita o plano de controle da plataforma, e fazê-la na mesma transação elimina a janela entre a aprovação e a execução. Os jobs ficam com a limpeza por organização.
