# Privacidade: direitos do titular

O núcleo atende os direitos da pessoa sobre os dados que ele mesmo guarda: a identidade global, o perfil, os tokens e, em cada organização, o vínculo, as notificações e os anexos enviados. Os registros de negócio pertencem a cada organização, que é a controladora deles, e ficam fora daqui.

## Exportação dos próprios dados

Em `/profile/privacy/`, a pessoa baixa um ZIP com `dados.json` ([#190](https://github.com/alaorneto/skeleton/issues/190)). O arquivo traz:

- a identidade (email, nomes, criação, último login) e o perfil (nome de exibição, idioma, fuso);
- os metadados dos tokens de API: prefixo, nome, escopos, validade e revogação, nunca o digest;
- em cada organização em que a pessoa tem vínculo ativo, o vínculo, as notificações recebidas (tipo, datas, alvo e snapshot) e os metadados dos anexos que ela enviou.

A exportação é montada na própria requisição. Ela é uma rota agregada que lê cada organização num contexto só de leitura, com o vínculo revalidado. O ZIP sai como anexo e com `no-store`, então não há arquivo guardado para proteger ou expirar. O download exige autenticação recente e grava o evento de segurança `privacy.export`. Nunca aparecem dados de outra pessoa, segredos ou registros de negócio.

## Eliminação da identidade

A pessoa pede a eliminação em `/profile/privacy/`, e um administrador da plataforma decide em `/platform/privacy/` ([#191](https://github.com/alaorneto/skeleton/issues/191)). Nada muda antes da aprovação.

- **Pedido:**
  - exige autenticação recente e a confirmação de que a eliminação é irreversível;
  - há um pedido pendente por vez, e pedir de novo devolve o mesmo;
  - a pessoa pode cancelar enquanto ele está pendente;
  - contas de serviço são recusadas, porque quem as gere é a organização.
- **Decisão:**
  - exige autenticação recente, POST com CSRF e a revisão que o operador viu (CAS);
  - quem decide é outro administrador da plataforma, nunca a própria pessoa;
  - a recusa leva um motivo de até 500 caracteres, que a pessoa lê na mesma página;
  - um pedido decidido não é decidido de novo.
- **Bloqueios:** a aprovação é recusada, sem mudar nada, enquanto a pessoa for:
  - a última administradora ativa de alguma organização (o mesmo guard SQL de `deactivate_user`);
  - ou a última administradora da plataforma.

A aprovação anonimiza a identidade na mesma transação, no plano de controle da plataforma. Lá os guards de último administrador valem sob lock e o acesso termina na hora:

- **Identidade:**
  - o email vira `anon-<sysid>@anonymized.invalid` e os nomes ficam vazios;
  - a conta fica inativa, sem senha utilizável e sem senha local;
  - um novo `auth_version` encerra toda sessão na próxima requisição.
- **Cópias globais:**
  - os emails, as contas sociais e os autenticadores de MFA da allauth são apagados;
  - os tokens de API são revogados;
  - as assinaturas de push e as preferências de notificação são removidas (soft delete);
  - o perfil volta ao padrão.
- **Organizações:** um job `accounts.anonymize_organization` por organização ativa apaga as linhas de notificação e as entregas da pessoa ali, e desativa o vínculo, revogando papéis, grupos e concessões. Uma transação seleciona um só tenant, por isso cada organização é um job. O job só toca uma identidade já anonimizada, e na segunda execução não encontra mais nada.

Os registros de negócio continuam íntegros e apontam para o mesmo sysid, que nada mais identifica; a organização é a controladora deles. A auditoria cita a pessoa só pelo sysid (`personal_fields`, #189), e os eventos antigos são redigidos uma vez pelo comando `redact_audit_personal_data`. O pedido e a decisão ficam na auditoria (`sk_accounts_erasurerequest`), com os eventos de segurança `privacy.erasure_requested`, `privacy.erasure_completed` e `privacy.erasure_rejected`.

Linhas de notificação numa organização desativada no momento da aprovação ficam até a retenção normal de notificações.
