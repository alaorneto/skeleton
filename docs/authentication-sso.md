# Autenticação, convites, SSO e MFA

Autenticação responde **quem entrou**; autorização decide **o que essa pessoa pode fazer em cada organização**. SSO (login único por um provedor externo) substitui a prova de identidade, não as permissões locais. Consulte [identidade e organizações](concepts.md#identidade-e-organizações) e [login e protocolos](concepts.md#login-e-protocolos) para a visão geral.

## Identidade e admissão

O login por email é gerenciado pelo django-allauth, através do único backend `SkeletonAuthenticationBackend`. Após autenticar, o servidor mantém uma sessão no banco; o navegador envia um cookie com seu identificador, não a senha a cada acesso. `User.email` é normalizado e único, inclusive para contas com marca de exclusão lógica (tombstone). Contas excluídas ou inativas não autenticam nem são recriadas por SSO. A versão `auth_version` guardada na sessão é comparada a cada requisição: mudança de senha/MFA, desativação e revogação global invalidam sessões antigas. Permissões Django globais são vazias para usuários comuns; `has_perm` só é verdadeiro para administrador da plataforma pelo predicado `is_platform_admin` (ativo, não excluído, `is_staff` e `is_superuser`). RBAC, a autorização por papéis na organização, não usa `auth.Group`.

Troca efetiva de email primário verificado incrementa auth_version mesmo que o allauth já tenha sincronizado User.email antes do signal. A sessão que realiza a troca conserva a nova versão; todas as demais são revogadas. User restore incrementa a versão no mesmo CAS que remove o tombstone e conserva is_active=False. UserManager.create_user/create_superuser e o comando createsuperuser criam UserProfile na mesma transação.

O formulário de identidade em `/platform/users/<id>/` inclui revisão original escondida. Alterações de nomes/flags e revogação são persistidas numa instância bloqueada coerente, sem forjar revision; formulário stale e tentativa de remover último admin retornam 409. Language da API exige string e opção configurada: lista/objeto é validação 400 sem escrita.


Cadastro é fechado por padrão. Quando habilitado explicitamente, o cadastro público exige confirmação de email e cria somente User/Profile: não cria organização nem Membership (vínculo organizacional). A confirmação de email é obrigatória tanto no cadastro local como no social. Logout, confirmação de email e início social não são ações por GET. As ações de formulário usam POST com proteção CSRF: o token comprova que a requisição veio de uma página autorizada, evitando que outro site use o cookie da vítima para executar ações. CSRF não substitui login nem autorização. Reset de senha não faz login automático; operações sensíveis exigem reautenticação, uma nova prova recente de identidade.

Nas operações sensíveis do núcleo (mutações em `/platform/`, telas globais `sensitive`, conexão de conta social e aceite de convite por conta existente), a regra está em `skeleton.accounts.reauthentication`. Vale o último login ou a última reautenticação registrados nesta sessão, para o mesmo usuário, dentro de `ACCOUNT_REAUTHENTICATION_TIMEOUT`. Depois disso, a requisição vai para `/accounts/reauthenticate/` e retoma ao concluir. Só o aceite de convite mantém o fallback da biblioteca para contas sem método local de reautenticação (SSO sem senha nem MFA). As telas do próprio allauth, como emails, senha e MFA, seguem a regra da biblioteca: desde a 65.21 elas pedem reautenticação explícita mesmo logo após o login. Use os helpers do núcleo em vez de `allauth.account.decorators.reauthentication_required`, que adota essa regra mais estrita.

### Convite

O administrador da organização emite um convite com email, papel e organização fixos. O serviço gera um token URL-safe de 32 bytes, guarda apenas seu resumo SHA-256 (digest) e limita a validade a sete dias. A mensagem contém `/invite/<org_uuid>/<token>/`; o token em texto original aparece somente nessa mensagem, nunca em auditoria, logs ou callback SSO. GET valida o convite e guarda uma referência no servidor, mas não o consome. POST com CSRF revalida token, validade, email e organização e o consome atomicamente com CAS e `accepted_at IS NULL`: duas tentativas concorrentes ou a repetição do mesmo convite têm somente um vencedor.

Conta nova usa o cadastro allauth com email fixado. A posse de um convite validado pode servir de prova de acesso àquele email; não serve de prova da senha ou da identidade de uma conta existente. Para uma conta existente, são necessários login/reautenticação e email verificado igual ao convidado. Reconvidar reativa a Membership existente, sem duplicar o vínculo. Convite inválido, expirado ou consumido apresenta uma página neutra ou o código de API `invalid_invitation`, sem revelar se a conta existe.

O convite para o **portal** de pessoas externas (#532) segue o mesmo desenho, sem papel nem `Membership`: resumo SHA-256, link `/portal/invite/<org_uuid>/<token>/`, aceite só com o email verificado da conta igual ao convidado, recusas neutras e tentativas limitadas por pessoa e IP ([portal](portal.md#convite-e-aceite-532)).

A sessão armazena apenas UUIDs de organização/convite e o digest, nunca o token original. Na criação, a prova de email validada continua disponível somente na requisição corrente depois do consumo, para configurar `EmailAddress` no allauth. Ela não pode autenticar uma conta existente nem ser reutilizada em outra requisição.

### Solicitação de ingresso

A organização decide em `/o/<org>/admission/` (capacidade `admission.configure`, só do administrador) se aceita pedidos de ingresso, se aparece na lista pública e com que papel entra quem for aprovado. Sem política gravada, tudo fica desligado. O papel `organization_admin` nunca é concedido por pedido; a administração plena continua vindo de convite ou designação.

- **Pedir.** Em `/join/`, uma conta com email verificado vê as organizações que aceitam pedidos e optaram por aparecer. `/join/<org_uuid>/` aceita o pedido, com mensagem opcional de até 500 caracteres. Uma organização que não aceita pedidos, inexistente ou inativa responde o mesmo 404 neutro. O limite de pedidos por pessoa (10 por hora, no cache compartilhado) é contado antes de qualquer leitura sobre a organização: o 429 não revela se ela existe. Reenviar com um pedido aberto devolve o mesmo pedido.
- **Contexto.** O solicitante não tem vínculo, então a gravação usa `system_context(reason="join:request")` para aquela organização, aberto pelo serviço depois das próprias verificações e só para um ator autenticado. Antes de haver tenant, a lista e a aceitação são lidas do índice global `OrganizationDirectoryEntry`, espelhado da política na mesma transação. O solicitante vê os próprios pedidos em todas as organizações pela autodescoberta (`own_rows`); quem decide vê só os da própria organização.
- **Decidir.** O administrador e o gestor de pessoas (`admission.decide`) aprovam ou recusam em `/o/<org>/requests/`, no próprio contexto tenant, com a ordem de travas Organization → pedido → vínculo e CAS pela revisão exibida. De duas decisões sobre a mesma revisão, uma vence e a outra recebe 409. A aprovação cria um vínculo com o papel do pedido (`admit_member`). O gestor de pessoas não aprova pedido cujo papel carrega capacidade administrativa.
- **Readmissão.** Aprovar é decisão humana e pode reativar um vínculo anterior; a fila avisa "Já foi membro". Admissões automáticas (link, domínio, SSO) nunca reativam: elas geram um pedido.
- **Prazo e aviso.** Um pedido aberto expira em 30 dias (estado derivado; um novo pedido substitui o expirado) e pode ser retirado pelo solicitante. O email com a decisão é montado na transação e enviado só depois do commit.

### Links de ingresso

Com `join_links_enabled` na política, quem gerencia convites (`invitations.manage`) cria em `/o/<org>/admission/links/` links multiuso com nome, papel, entrada direta ou com aprovação, validade de até 90 dias e limite de 1 a 10.000 usos. O token tem 32 bytes aleatórios e aparece uma única vez, na resposta da criação (sem cache); o banco guarda só o SHA-256, redigido na auditoria. Papel, modo, validade, limite e resumo são imutáveis; revogar é exclusão lógica e desliga o link na hora. O gestor de pessoas não cria link com papel que carrega capacidade, e `organization_admin` nunca é concedido por link.

- **Abrir.** `GET /join/<org_uuid>/<token>/` valida o link em `system_context(reason="join:link")`, que pode ser aberto antes do login (o resumo do token é a prova, como no convite), e guarda na sessão só a referência com o resumo. Link expirado, revogado, esgotado, de outra organização ou com links desligados mostra a mesma página neutra.
- **Usar.** `POST` com CSRF, autenticado e com email verificado, trava a organização e depois o link. Sem vínculo anterior, o modo direto admite com o papel do link e o modo com aprovação cria um `JoinRequest(channel=link)`. Quem já foi membro sempre gera um pedido: admissão automática nunca reativa vínculo. Membro ativo e pedido já aberto não consomem uso. Com `max_uses=1` e resgates concorrentes, um só vence.
- **Cadastro pelo link.** Só com `signup_via_link_enabled`. O cadastro cria a conta e um pedido `awaiting_verification` que reserva um uso. A confirmação do email conclui: admite (link direto) ou deixa o pedido pendente. Ela funciona em outro dispositivo, sem sessão, porque o serviço lê os próprios pedidos da pessoa pela autodescoberta, atuando como a dona do email recém-confirmado (`verified_owner`). Sem a opção, o link não abre o cadastro público.

### Domínio de email verificado

Com `domain_join_enabled` na política, o administrador (`admission.configure`) cadastra em `/o/<org>/admission/domains/` um domínio (normalizado em IDNA), o modo de entrada (direta ou com aprovação) e o papel. O serviço gera um desafio público, que a organização publica num registro TXT em `_skeleton-challenge.<domínio>`. "Verificar" consulta o DNS com tempo limite de 3 s, antes de qualquer trava, e só depois trava a organização e o domínio. Um domínio já verificado por outra organização falha com erro neutro: a posse fica no índice global `DomainClaim`, um por domínio, escrito só pela verificação. Domínio pendente ou falho não admite ninguém. Não há validação DNSSEC (risco registrado no plano); a mitigação é a reverificação e a disputa pela plataforma.

- **Admissão.** Quando uma pessoa confirma um email (e no cadastro social com email verificado), `apply_verified_email_admission` procura a posse do domínio desse email, que aponta para no máximo uma organização, e age em `system_context(reason="join:domain")`, o email confirmado sendo a prova. Sem vínculo anterior, admite (modo direto) ou cria um `JoinRequest(channel=domain)`. Quem já foi membro sempre gera um pedido. Se a mesma requisição já escolheu outra organização (link, convite ou SSO), a admissão por domínio é pulada e fica disponível pela sugestão da página inicial.
- **Sugestão.** A página inicial lista as organizações que verificaram o domínio de um email verificado da pessoa e aceitam admissão por domínio; "Entrar" aplica a mesma regra.
- **Reverificação.** `manage.py verify_domains --recheck` (agende com cron ou systemd timer) consulta o DNS fora das transações e grava uma transação por organização. Três falhas seguidas marcam o domínio como `failed` e removem a posse, suspendendo a admissão automática até nova verificação.
- **Disputa.** Em `/platform/domains/`, a plataforma revoga a posse de um domínio (CAS e autenticação recente). A linha da organização não é tocada; ela precisa verificar de novo.

### Senhas, emails e conexão

**Contas de serviço** (`account_type=service`, ver [controle de acesso](access-control.md#contas-de-serviço)) nunca fazem login interativo. O backend recusa a autenticação e não devolve a sessão (`get_user`), o `AccountAdapter.pre_login` e o `SocialAccountAdapter.pre_social_login` negam de forma genérica, e o reset de senha mantém a resposta neutra sem enviar email. Com a senha inutilizável real, o login responde igual a um email desconhecido. Uma integração vai agir por tokens próprios (#45), não por sessão.

Uma identidade criada por SSO recebe senha inutilizável e `local_password_allowed=False`; contas locais ou criadas por convite usam True. Essa política pertence à identidade global, mesmo quando a pessoa participa de várias organizações. “SSO-only” significa que o login local por senha está proibido, não que a pessoa tem permissão em algum tenant. Reset mantém uma resposta pública neutra, mas não envia mensagem para contas SSO-only, inativas ou excluídas. Definir/trocar senha e concluir reset revalidam a política, mesmo com token antigo. Não há endpoint paralelo de senha na API.

`ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS=False` impede a mensagem genérica que o allauth enviaria depois de filtrar uma identidade sem direito a senha local. A resposta pública continua neutra; não enviar email a conta desconhecida, inativa ou SSO-only não autoriza expor sua existência.

Um email informado pelo provedor e coincidente com uma conta local **não** conecta as contas automaticamente: `SOCIALACCOUNT_EMAIL_AUTHENTICATION/AUTO_CONNECT=False` e `can_authenticate_by_email=False`. Entre na conta existente e faça conexão explícita após reautenticação. `User.email` e `EmailAddress` só sincronizam após confirmação e não reutilizam emails de contas excluídas.

`SOCIALACCOUNT_STORE_TOKENS=False` sozinho não protege o cadastro social ainda pendente. Após validar o protocolo, o adapter remove tokens e conserva na sessão somente `process`, `next` e `headless` necessários ao fluxo. `extra_data` retém apenas o binding e, durante a admissão inicial ainda não confirmada, o email pendente. Tokens e claims (atributos declarados pelo provedor) completos não são serializados nesse fluxo.

### Sessão entre o domínio principal e os endereços de organização

Com `SKELETON_ORGANIZATION_BASE_DOMAIN` configurado ([#481](https://github.com/alaorneto/skeleton/issues/481)), um login vale para o domínio principal e para todos os `<slug>.<domínio base>`. `SiteSessionMiddleware` (`skeleton/tenancy/site_middleware.py`), no lugar do `SessionMiddleware` do Django, decide o domínio do cookie **por requisição**: resposta de um host igual ao domínio base ou abaixo dele grava o cookie com `Domain=.<domínio base>`, inclusive ao apagá-lo no logout; resposta de qualquer outro host (um domínio próprio, [#489](https://github.com/alaorneto/skeleton/issues/489)) grava o cookie só do host, como antes. `HttpOnly`, `SameSite=Lax` e `Secure` não mudam. Com o domínio base, o cookie passa a se chamar `skeleton_session`, para que um cookie antigo só do host nunca concorra com o novo; no dia em que o domínio base é configurado, todos entram de novo uma vez.

O cookie de CSRF nunca é compartilhado: cada host recebe o seu e a checagem de `Origin` do Django compara com o host da própria requisição, então um formulário de `acme.<base>` postado para `outra.<base>` é recusado com 403. `SameSite=Lax` não separa dois subdomínios do mesmo domínio base; quem os separa é essa checagem, e por isso `CSRF_TRUSTED_ORIGINS` nunca recebe curinga. O check `skeleton.E039` reprova `SESSION_COOKIE_DOMAIN` ou `CSRF_COOKIE_DOMAIN` definidos com o domínio base, e `skeleton.W039` avisa quando o host de `SKELETON_PUBLIC_URL` fica fora dele (a sessão não seria compartilhada). O domínio base precisa ser **dedicado** a esta aplicação: todo host abaixo dele recebe o cookie de sessão.

### Login no endereço da organização

No host `<slug>.<domínio base>` ([#486](https://github.com/alaorneto/skeleton/issues/486)) a tela de login fala só da organização ("Use sua conta para acessar <nome>.") e lista só os provedores gerais e os presos à organização do host: a tag `site_providers` (`skeleton/web/templatetags/sites.py`) usa `skeleton.accounts.providers.provider_organization_id`. O filtro é de apresentação; quem entra por qual provedor continua decidido pelo `SocialAccountAdapter`, e `list_apps` não muda, para o retorno de qualquer provedor continuar resolvendo.

OIDC e SAML voltam do provedor para a URL de retorno registrada lá, que é a do domínio principal; registrar uma URL por organização em cada provedor não é viável. Por isso, no host da organização, o botão do provedor é um **link para o domínio principal** (`<SKELETON_PUBLIC_URL>/accounts/<provedor>/login/?process=login&next=<URL absoluta do host da organização>`). O allauth mostra a confirmação no domínio principal ("Continuar para <organização> com <provedor>", `socialaccount/login.html`), a pessoa autentica no provedor, volta ao domínio principal e é redirecionada para o `next`; como o cookie de sessão é compartilhado, chega autenticada no host da organização. LDAP é um formulário local e funciona no próprio host. Quando a sessão não é compartilhada (o domínio principal fora do domínio base, ou um domínio próprio, #489), os botões de OIDC e SAML não aparecem; senha, LDAP e MFA continuam. Num domínio próprio o cookie de sessão é só daquele host: o login lá não vale no domínio principal, e vice-versa.

O `next` absoluto é aceito por `AccountAdapter.is_safe_url` só quando `skeleton.tenancy.site.is_site_url` diz que é o endereço de uma organização **com endereço ativo**, com o mesmo esquema e a mesma porta de `SKELETON_PUBLIC_URL`, sem usuário/senha, barra invertida ou caractere de controle, e só quando a requisição atual está num host que compartilha a sessão. Qualquer outro host abaixo do domínio base (inexistente, desligado, reservado, dois rótulos) é recusado, embora `.<domínio base>` esteja em `ALLOWED_HOSTS`. Os emails de conta enviados a partir do host da organização (confirmação, redefinição de senha) saudam com o nome dela e levam links desse host.

## Provedores no banco (#29)

Provedores corporativos OIDC e SAML (LDAP em #32) também podem ficar no banco, em `sk_accounts.IdentityProvider`, cadastrados pela plataforma e válidos na requisição seguinte, sem reinício. Cada linha é ligada a uma organização (`bound_organization`, nunca `organization`: a linha é global). O `SocialApp` que o allauth usa é montado em memória pelo `SocialAccountAdapter.list_apps`, uma vez por requisição; nada vai para a tabela `SocialApp` editável. `show_on_login=false` esconde o botão da tela de login (`settings.hidden` do allauth) sem desligar o provedor; `enabled=false` o desliga na requisição seguinte, inclusive para contas já conectadas.

- **Identidade imutável.** `provider_id`, protocolo, organização, issuer (OIDC), entity ID do IdP (SAML), diretório (LDAP), `client_id`, entity ID do SP e o fingerprint não mudam: o guard da tabela e `update_identity_provider` recusam. O fingerprint é a mesma tupla v2 do arquivo, então trocar o certificado SAML do IdP (em `settings`) mantém os subjects. Outra identidade exige outro provedor, com migração administrativa dos subjects.
- **Opções e segredos.** `settings` aceita só a allowlist do protocolo: nada em OIDC (escopos, prova de email e PKCE são do código); em SAML, `idp` (`sso_url`, `slo_url`, `metadata_url`, `x509cert`), `sp.x509cert` e `attribute_mapping`. As flags de assinatura e de recusa de IdP-initiated são sempre ligadas pelo código. O segredo do cliente OIDC e a chave privada do SP SAML ficam cifrados com `IDENTITY_PROVIDER_ENCRYPTION_KEYS` (MultiFernet, #19) e redigidos na auditoria; um segredo vazio na atualização mantém o atual.
- **Admissão.** JIT, papel padrão e domínios de email ficam com a organização, em `IdentityProviderAdmission` (#31): sem linha, JIT desligado. O adapter lê essa linha em `system_context(reason="sso:validated")` da organização do provedor; o papel nunca vem de claim, query ou corpo.
- **Coexistência com o arquivo.** gov.br e GitHub continuam no JSON. Um `provider_id` presente no arquivo é atendido pelo arquivo, sem consultar o banco; o banco atende os demais. `manage.py import_identity_providers --from-config <arquivo> [--dry-run]` copia os provedores `oidc`/`saml` do JSON para o banco com o mesmo fingerprint (e a regra JIT e o papel do arquivo na `IdentityProviderAdmission`), mantendo os `SocialAccount` válidos. Enquanto a entrada continuar no arquivo, ela vale e `validate_provider_bindings` avisa; remova-a do arquivo para passar a usar a linha do banco. Fingerprints diferentes para o mesmo `provider_id` são erro, e o cadastro recusa um `provider_id` que o arquivo usa com outra identidade.

### Cadastro pela plataforma (#30)

Em `/platform/identity-providers/`, o administrador da plataforma lista os provedores do banco, cadastra um OIDC (issuer, `client_id`, segredo) ou SAML (entity ID do IdP, `client_id`, entity ID do SP, URLs, certificados e chave privada do SP) e edita só os campos mutáveis. Os campos de identidade aparecem como leitura e um POST que os inclua é recusado. Segredo e chave privada são só de escrita: o campo em branco mantém o valor, e nem a página nem as mensagens de erro os mostram. Toda gravação exige CSRF, CAS (revisão stale responde 409) e autenticação recente. "Testar" busca a descoberta OIDC (`/.well-known/openid-configuration`) ou a metadata SAML pelo cliente HTTP de saída (só HTTPS público, 5 s de conexão e 10 s de leitura, sem redirecionamento) e grava apenas `last_check_at` e `last_check_code`; não faz login. A página de cada organização em `/platform/` tem o card "Provedores de identidade".

### Regras de admissão da organização (#31)

Em `/o/<org>/identity/` (capacidade `identity.configure`, do administrador), a organização vê os provedores que a plataforma ligou a ela, numa projeção sem segredos nem opções de protocolo (nome, protocolo, se está ativo, URL de login e último teste), e ajusta por provedor:

- **JIT** (`jit_provisioning`): criar o acesso no primeiro login; desligado por padrão e sem linha.
- **Aprovação** (`jit_requires_approval`): o primeiro login cria um `JoinRequest(channel=sso)` pendente, decidido na fila de solicitações, em vez do vínculo.
- **Papel padrão**: um papel ativo da organização, nunca vindo de claim, query ou corpo. Quem não tem `access.manage` não escolhe papel com capacidade; o administrador pode escolher qualquer um.
- **Domínios permitidos** (até 50, IDNA): uma identidade nova com email fora da lista recebe a mesma recusa neutra de sempre ("Identity admission denied."), sem revelar a regra.

A gravação usa CAS a partir da revisão virtual 0 e escreve só `IdentityProviderAdmission`. A API equivalente é `GET /api/v1/o/<org>/identity-providers/` e `GET`/`PUT /api/v1/o/<org>/identity-providers/<id>/admission/` com If-Match. Outra organização não vê nem altera esses provedores (404).

### LDAP (#32)

LDAP é um provider do allauth (`skeleton.accounts.ldap.provider.LDAPProvider`, registrado por `skeleton/accounts/provider.py`, sem novo app label), para reaproveitar `pre_social_login`, a ausência de auto-connect por email, a etapa de MFA, `local_password_allowed=False` e o JIT da organização. O cliente (`skeleton/accounts/ldap/client.py`, sobre `ldap3`) é o único ponto que fala LDAP.

- **Configuração (plataforma).** Em `/platform/identity-providers/new/?protocol=ldap`: o diretório é uma URL LDAP com a base de busca (`ldaps://servidor/dc=empresa,dc=com`, ou `ldap://` só com StartTLS), imutável e parte do fingerprint; DN e senha (cifrada) da service account; filtro com exatamente um `{username}`; identificador `entryUUID` ou `objectGUID` (lido com `bytes_le`); atributo de email; grupo exigido opcional; arquivo de CA opcional e tempo limite de 1 a 30 s. TLS sempre verifica o certificado (`CERT_REQUIRED`); `ldap://` sem StartTLS é recusado. "Testar" faz o bind da service account e lê a base.
- **Login.** `/accounts/ldap/<provider_id>/login/` mostra o formulário; o POST (CSRF, senha como parâmetro sensível) aplica dois limites antes de falar com o diretório, por IP (20 em 10 min) e por provedor e usuário (5 em 10 min), recusa usuário ou senha vazios (evita o bind anônimo), busca com a service account usando o valor escapado no filtro e então faz o bind como a pessoa com o DN encontrado. Qualquer falha, de usuário inexistente a diretório fora do ar, mostra "Usuário ou senha inválidos.". O bind válido vira um `SocialLogin` com `uid` igual ao identificador do diretório e segue por `complete_social_login`. A senha não é guardada nem registrada.
- **Identidade.** Mudar o email no diretório não cria outra identidade (o `uid` é o UUID); um email igual ao de uma conta local não conecta sozinho. Conectar a uma conta existente exige reautenticação recente, como nos outros providers.
- **Limites.** Sem Kerberos/SPNEGO nem LDAP sem TLS. O `ldap3` 2.9.1 emite dois `DeprecationWarning` do `pyasn1` ao importar; o spike com Python 3.14 está registrado em [#32](https://github.com/alaorneto/skeleton/issues/32).

### Sincronização do diretório (#33)

`manage.py sync_directory --provider <id> | --all [--dry-run] [--provision] [--max-deactivations N] [--format json]` lê o diretório de cada provedor LDAP ativo (busca paginada, fora de transação) e, numa transação por organização em `system_context(reason="directory:sync")`, desativa o vínculo de quem entrou por aquele provedor e não aparece mais no diretório ou no grupo exigido. Cada desativação usa o mesmo ciclo de vida do administrador (revoga papéis, grupos e concessões, mantém o vínculo e o histórico) num savepoint próprio; o último administrador é pulado e reportado. Ninguém é reativado. Diretório vazio, erro de busca ou mais desativações que `--max-deactivations` abortam sem escrever naquela organização e terminam com código de erro. `--provision` (desligado por padrão) admite, conforme a `IdentityProviderAdmission`, quem já entrou pelo provedor mas nunca teve vínculo ali; ex-membros nunca. O relatório traz só contagens e UUIDs, e o provedor guarda `last_sync_at`/`last_sync_code`. O agendamento é do operador ([deployment](deployment.md#sincronização-de-diretório-ldap)); não há agendador interno, nem sincronização de nome, email ou grupos para papéis.

## JSON privado de provedores

`SSO_PROVIDER_CONFIG_PATH` aponta para um arquivo privado fora do Git, cujo objeto raiz contém `providers`. Cada `provider_id` deve ser único e seguro para uso em URL. Os protocolos aceitos são `oidc`, `saml`, `github` e `govbr`. OIDC/SAML corporativos exigem `organization_slug`, pois podem admitir membros em uma organização específica. Gov.br representa identidade global e rejeita configuração de organização, papel ou JIT (criação de vínculo no primeiro login). Ao carregar settings, o parser valida tipos, URLs e referências de segredos sem consultar o banco; após migrations, os serviços e checks verificam organização/papel ativos e a estabilidade do binding.

Settings carregam `SKELETON_PROVIDER_CONFIGURATION` uma única vez no início do processo; bindings e allauth usam essa mesma configuração, sem reler o arquivo a cada requisição. Mudanças no arquivo ou ambiente exigem reinício controlado.

Não é permitido declarar `verified_email`, scopes configurados ou `auth_params` arbitrários para forjar prova ou ampliar permissões. Scopes são os conjuntos de dados solicitados ao provedor: OIDC usa `openid/profile/email` e preserva a solicitação de reautenticação; gov.br usa `openid/email/profile`; GitHub solicita somente `user:email`. Parâmetros de query `scope/auth_params` não ampliam consentimento nem alteram `client_id`, `redirect_uri`, `state` ou `nonce`. Considerar email verificado exige evidência do protocolo e confiança explícita configurada. Todos os fluxos OIDC exigem ID token assinado e nonce aleatório ligado ao estado allauth de uso único e ao provider ID; uma conexão TLS entre servidores não dispensa assinatura.

As classes de extensão de segurança OIDC/SAML/GitHub são registradas mesmo quando não há credenciais nem apps configuradas. O registry allauth conserva essas classes desde o primeiro carregamento; adicionar um perfil não pode reutilizar inadvertidamente a classe upstream sem minimização/nonce. Isso não habilita login sem um binding autorizado. O adapter seleciona endpoints gov.br pelo perfil global do snapshot de configuração, não por descoberta ou settings editáveis de SocialApp.

Depois de migrate/catalog/schema check, rode com os settings **runtime** que carregam os providers:

```sh
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py validate_provider_bindings --database default
```

Esse comando é a verificação prévia (preflight): valida organizações/papéis vivos e todos os fingerprints históricos, sem fazer login nem alterar subjects. Fingerprint é o resumo da configuração que fixa o domínio de identidade de um binding; a versão v2 inclui protocolo, issuer/entity, `client_id`, organização e entidade SP configurada. Qualquer erro bloqueia o deployment. Atualizar esse contrato também exige revisar bindings existentes, pois seus fingerprints não são atualizados automaticamente. Para mudar o domínio de identidade, crie outro `provider_id` e faça uma migração administrativa revisada; reutilizar o ID poderia ligar uma pessoa do novo emissor à conta de outra pessoa.


```json
{
  "providers": [
    {
      "protocol": "oidc",
      "provider_id": "empresa-a-oidc",
      "organization_slug": "empresa-a",
      "jit_provisioning": true,
      "default_role_code": "member",
      "trust_verified_email": true,
      "app": {
        "name": "Empresa A",
        "client_id": "skeleton-oidc",
        "secret_env": "EMPRESA_A_OIDC_SECRET",
        "settings": {
          "server_url": "https://idp.example.org/realms/empresa-a",
          "uid_field": "sub",
          "token_auth_method": "client_secret_basic",
          "oauth_pkce_enabled": true
        }
      }
    }
  ]
}
```

`secret_env` resolve secret só no processo; `private_key_env` resolve private_key recursivamente. Segredo literal aninhado e combinações literal+referência são rejeitados sem incluir valores nos erros; certificado público é permitido. Não coloque segredo literal no arquivo de exemplo. Provedor GitHub usa `provider_id="github"`, `client_id` e `secret_env`; escopo só `user:email`. Sem binding corporativo, primeiro ingresso requer convite, não afiliação inferida do social login.

JIT default False; default role member, nunca role/tenant de claim/query/body. No arquivo, `default_role_code` nunca pode ser um papel interno com capacidade (`organization_admin`, `people_manager`, `app_manager`): a carga dos settings falha, e o adapter também nega no uso um binding do arquivo cujo papel carrega capacidade (#228); o papel escolhido nas regras de admissão da organização vem de quem tem `access.manage`. O GitHub, provedor público, não aceita `jit_provisioning: true`. Confirmar o email de uma pessoa com admissão pendente que deixou de ser possível (organização desativada, papel removido) confirma o email e só pula a admissão, com o evento `sso.jit_admission_skipped`. Mesmo trust_verified_email=True só vale se o protocolo validado realmente atestar email verificado; do contrário confirmação local. Claims de grupo não promovem staff/superuser. Provider_id vincula issuer/entity ID e organização; não mude identidade por baixo do mesmo ID. Crie ID novo, revise permissões/binding e migração de subjects administrativamente, sem auto-conectar por email.

Sem email atestado pelo protocolo, exige-se confirmação local. Essa confirmação só completa uma admissão inicial pendente explicitamente para o email/binding de origem, e nunca reativa Membership existente suspensa/removida. Confirmar email adicional não desfaz offboarding. Convite autorizado continua sendo o caminho de readmissão. Após confirmar cadastro social que ainda não iniciou sessão, inicie novamente o SSO; a confirmação não é login automático.

A admissão pendente é um estado do servidor: ele conserva a decisão inicial entre novos callbacks até a confirmação e não aceita claims extras como substituto. As escritas de consulta/identificação do allauth também não apagam esse estado. A criação inicial de Membership usa o mesmo bloqueio da organização que a revogação de vínculo (offboarding), impedindo corrida entre admissão e remoção. Concluir essa admissão não autoriza reativar um vínculo existente.


## Protocolos e callbacks

O **IdP** (provedor de identidade) autentica a pessoa; o serviço local é o cliente OIDC ou **SP** (prestador de serviço) SAML. **Issuer** é o identificador do emissor OIDC; **subject**, normalmente `sub`, identifica a pessoa dentro desse emissor. O par emissor/subject, e não o email mutável, define a identidade externa. Em SAML, `entity_id` identifica a entidade e o UID escolhido deve ser imutável. Um binding é o vínculo administrado entre essa configuração externa e a identidade/organização local.

OIDC (OpenID Connect) usa OAuth para obter uma prova de identidade, o ID token assinado; um access token OAuth isolado não é essa prova. SAML transporta declarações de identidade em XML assinado. O perfil gov.br é OIDC com endpoints oficiais e credenciamento específico, não um protocolo próprio. GitHub usa seu fluxo OAuth integrado pela biblioteca.

No retorno ao callback, `state` liga a resposta ao fluxo iniciado e é consumido uma única vez; `nonce` liga o ID token àquela tentativa e impede aceitar um token de outra tentativa; PKCE S256 exige a prova secreta gerada no início para trocar o código por tokens, reduzindo o risco de interceptação do código. Essas proteções são complementares à assinatura, à validação de issuer/audience e ao HTTPS. Não acrescente parâmetros de URL para desativá-las.

| Protocolo | Callback/endpoints | Identidade e requisitos |
|---|---|---|
| OIDC | `/accounts/oidc/<provider_id>/login/callback/` | `sub` estável por issuer; discovery server_url administrado; PKCE S256 e client_secret_basic |
| Gov.br OIDC | `/accounts/oidc/<provider_id>/login/callback/` | Perfil global com endpoints oficiais fixos por staging/production; PKCE S256, state, nonce, assinatura e client_secret_basic; sem discovery de endpoint fornecido pelo usuário |
| GitHub OAuth | `/accounts/github/login/callback/` | Subject GitHub; app real e user:email, sem repo/read:org |
| SAML | `/accounts/saml/<slug>/login/`, `acs/`, `sls/`, `metadata/` | app.client_id=slug; provider_id estável; UID imutável, não email |

Exemplo de `app.settings` SAML (adapte entidade/certificado/URLs reais, não copie chaves IdP):

```json
{
  "idp": {
    "entity_id": "https://idp.example.org/realms/empresa-a",
    "sso_url": "https://idp.example.org/realms/empresa-a/protocol/saml",
    "slo_url": "https://idp.example.org/realms/empresa-a/protocol/saml",
    "x509cert": "CERTIFICADO_PUBLICO_PEM_DO_IDP"
  },
  "sp": {"private_key_env": "EMPRESA_A_SAML_SP_PRIVATE_KEY"},
  "advanced": {
    "strict": true,
    "want_assertion_signed": true,
    "want_message_signed": true,
    "reject_deprecated_algorithm": true,
    "reject_idp_initiated_sso": true
  }
}
```

Esse trecho mostra a configuração; use certificado válido no deployment, não a string descritiva. IdP pode ser definido por metadata_url confiável em vez de x509cert. Assinaturas, XML e checks do protocolo pertencem a allauth/python3-saml, não a código próprio. Response e assertion assinadas são obrigatórias; IdP-initiated/unsolicited é rejeitado. TLS e certificados confiáveis são requisitos de produção. HTTP só loopback DEBUG de fixtures; não altere trust store para aceitar certificado inválido.

## Perfil global gov.br

O perfil reutiliza allauth 65.19.7, seu OAuth2Client, geração de PKCE, armazenamento/consumo de state e validador JWT/JWK. A extensão OIDC exige assinatura, issuer (inclusive a barra final oficial), audience/client ID, validade temporal e nonce. O estado também identifica o provider, para impedir consumo de um fluxo por callback de outro perfil. Não existe parser OAuth/JWT próprio ou login direto por token. Em govbr, somente o ID token validado é usado; não buscamos userinfo/foto/confiabilidades, nem conservamos access/refresh/ID tokens em SocialToken ou signup pendente.

Configuração privada mínima (substitua domínio/client ID/referência de secret por credenciais reais):

```json
{
  "providers": [
    {
      "protocol": "govbr",
      "provider_id": "govbr-homologacao",
      "environment": "staging",
      "post_logout_redirect_uri": "https://servico.gov.br/accounts/login/",
      "trust_verified_email": true,
      "app": {
        "client_id": "CLIENT_ID_CREDENCIADO",
        "secret_env": "GOVBR_CLIENT_SECRET"
      }
    }
  ]
}
```

`environment` só aceita `staging` ou `production`. Os endpoints são respectivamente `https://sso.staging.acesso.gov.br/` e `https://sso.acesso.gov.br/`, com caminhos `authorize`, `token`, `jwk`, `userinfo` (não consultado) e `logout`. Não se depende de discovery não documentado. Ambiente/issuer/client ID fazem parte do fingerprint; homologação e produção exigem IDs distintos, nunca troque o domínio de identidade sob um binding existente. Não configure `organization_slug`, `jit_provisioning`, `default_role_code`, scopes ou endpoints no perfil global.

O escopo de login revisado é **openid email profile**, a ser aprovado no credenciamento. O [roteiro oficial](https://acesso.gov.br/roteiro-tecnico/iniciarintegracao.html) também descreve `govbr_confiabilidades` e `govbr_confiabilidades_idtoken`; eles não são necessários à identidade global e não são solicitados pelo núcleo. Bronze/prata/ouro, `amr`, grupos, CNPJ e claims de tenant/papel não dão afiliação, privilégio ou bypass de MFA.

O botão **Entrar com GOV.BR** usa POST/CSRF e aparência signin do [Design System de Governo](https://www.gov.br/ds/components/signin?tab=designer), sem scripts ou assets remotos de autenticação. O browser segue para o IdP e volta pelo callback allauth; a resposta final redireciona para URL limpa e a API continua usando somente a sessão Django.

Admissão permanece fechada por padrão: convite autorizado ou conexão explícita de conta existente após login/reautenticação recente. Sem vínculo corporativo, `SKELETON_PUBLIC_SIGNUP=true` permite User/Profile global, sem Organization, Membership ou papel. Um convite pode conceder apenas o vínculo que o próprio serviço de convites já autorizou. Nenhuma confirmação de email do perfil global reativa vínculo suspenso/removido. Conectar não troca política de senha local da conta existente. A conexão exige registro recente de autenticação inclusive para contas SSO-only, evitando o fallback permissivo de reauth da biblioteca quando não há método local cadastrado.

Email coincidente nunca auto-conecta; use login existente e conexão explícita. Na admissão autorizada sem email gov.br, allauth apresenta seu formulário de signup para coleta de um email **real** e exige confirmação local. Convite fixa o campo no email autorizado, com sua prova já validada; não aceitamos trocar o email do convite por claim/form. Não há endereço sintético. `email_verified` só atesta prova quando é booleano `true` ou string exata `"true"` e `trust_verified_email=true`; `"false"`, números, listas e outras representações não confirmam email.

**Dados pessoais:** o roteiro define `sub` como CPF. Ele permanece exclusivamente como UID técnico do SocialAccount e no estado técnico server-side allauth quando necessário, sem auto-link por CPF, cópia em User/Profile, nome de conexão com CPF, endpoint público, log ou payload de auditoria próprio. Extra_data antes dos lookup writes contém apenas fingerprint; telefone/foto/nome/confiabilidades e claims integrais não são armazenados. O admin não registra SocialAccount/SocialToken. Restrinja também acesso operacional a DB, sessões e backups: criptografia, base legal, retenção e atendimento LGPD são responsabilidade do deployment e exigem revisão explícita; não há promessa de cifragem de UID pela biblioteca. Quando a pessoa pede a eliminação e a plataforma aprova ([privacidade](privacy.md#eliminação-da-identidade)), as contas sociais dela, com o UID, são apagadas junto com os emails e os autenticadores MFA.

Logout local usa `/accounts/logout/` POST com CSRF e fluxo oficial allauth. Para sessão autenticada via gov.br (ou conexão validada nessa sessão), o browser recebe redirect ao `logout` do ambiente configurado **após** encerrar a sessão local. Só é enviado `post_logout_redirect_uri` HTTPS fixo do JSON privado, sem query/fragment; o parâmetro `next` do usuário não escolhe callback nem endpoint externo. GET não encerra sessão. A URL de retorno deve estar previamente liberada pelo gov.br e servir uma página real, normalmente o login local.

### Credenciamento e evidência obrigatória

Solicite credenciais pela [integração oficial](https://acesso.gov.br/roteiro-tecnico/solicitacaocredencialprocesso.html); providencie secret privado, callback HTTPS exato `/accounts/oidc/<provider_id>/login/callback/`, URL de logout previamente autorizada, domínio, escopos aprovados e conectividade TLS/JWK. Produção exige domínio oficial de governo conforme o roteiro (gov.br, mil.br, edu.br, jus.br, leg.br etc.) e elegibilidade/aprovação do órgão responsável; um domínio privado comum não prova esse requisito.

Após provisionar, reinicie os processos e rode `validate_provider_bindings` com credencial runtime. Homologue login/admissão/conexão/logout reais em navegador externo, com a barra de endereço visível no vídeo exigido, incluindo MFA e ausência de afiliação automática. Credenciais gov.br externas **não foram disponibilizadas nesta implementação**: login/logout contra staging e produção permanecem pré-requisitos de verificação, não sucessos simulados. `tests/accounts/test_govbr.py` cobre biblioteca/fluxos com JWTs RS256 locais e transporte HTTP controlado; nem esses testes nem Keycloak substituem homologação gov.br.

## MFA cifrado e obrigatório

MFA (autenticação multifator) exige uma prova adicional além do método de login. São suportados TOTP, códigos temporários gerados por um aplicativo autenticador, e `recovery_codes`, códigos de recuperação para perda desse aplicativo. Email verificado é obrigatório; códigos de recuperação são mostrados uma única vez e o recurso de navegador confiável fica desligado. A claim `amr` do IdP não dispensa nenhuma das regras abaixo.

**Quem precisa cadastrar o MFA** (#740). As regras só somam: nenhuma afrouxa outra, e um TOTP cadastrado satisfaz todas. Contas de serviço nunca cadastram MFA. A avaliação é `skeleton.accounts.mfa.mfa_enrollment_pending`.

| Regra | Quem | Onde se configura |
|---|---|---|
| `SKELETON_REQUIRE_PLATFORM_ADMIN_MFA` | Todo administrador completo da plataforma (`is_platform_admin`), o criado no setup e os promovidos depois | Fixa em `True` no perfil de produção: o ambiente e o SQLite da instância não a desligam. Em desenvolvimento e testes o padrão é `False`, porque lá as chaves de MFA são opcionais |
| `SKELETON_REQUIRE_ADMIN_MFA` | Quem tem papel de plataforma ou capacidade administrativa na organização da requisição | Tela da instância (`/platform/instance/`); `true` por padrão em produção |
| `SKELETON_REQUIRE_MFA` | Toda pessoa | Tela da instância; `false` por padrão |
| Política da organização (`OrganizationSettings.mfa_policy`) | `admins`: quem tem capacidade administrativa nesta organização; `all`: todos os membros. Em branco segue a plataforma | Administrador da organização, em **Configurações → Segurança** (`/o/<org>/settings/security/`) |

A política da organização vale dentro dela: num contexto de outra organização, nas páginas globais antes de entrar nela ou no portal de pessoas externas, ela não se aplica. Mudá-la exige o administrador (`organization_lock`), CSRF, a revisão das configurações (CAS, 409 se mudou) e fica na auditoria de `OrganizationSettings`; quem liga a exigência precisa já ter TOTP, para não se trancar no ato. A política fica fora de `OrganizationService.update_settings`: apps não a mudam.

**Só a página de MFA.** Enquanto uma regra exige e o TOTP não existe, `skeleton.accounts.mfa_middleware.MFAEnrollmentMiddleware` (depois do contexto tenant, conferido pelo check E009) responde a qualquer rota fora da lista permitida: HTML vai para `mfa_index` (302) e a API recebe 403 `mfa_required`, também com token pessoal. A lista permitida (`ALLOWED_ROUTES`) tem só as telas de MFA do allauth (cadastro do TOTP, códigos de recuperação, reautenticação), a reautenticação e a verificação de email (o allauth não deixa cadastrar MFA sem email verificado), o logout e as imagens da marca. Nessas páginas o shell não mostra a navegação principal, a barra lateral da organização nem o menu "Minha conta": só a marca e o botão de sair. O MFA é da identidade: cadastrado uma vez, vale em todas as organizações da pessoa. As regras da plataforma valem em toda página. Uma regra que vem da organização da requisição (a política dela, ou `SKELETON_REQUIRE_ADMIN_MFA` sobre uma capacidade que a pessoa tem ali) segura só as páginas daquela organização: em outra organização e nas páginas globais, como o perfil, a pessoa segue livre até voltar a ela. Como as páginas de MFA são globais, a organização deixa uma marca na sessão do navegador ao mandar a pessoa para lá, e essas páginas continuam sem menus enquanto ela estiver no cadastro. A marca só muda a aparência, nunca o que responde, e some na primeira página fora do cadastro ou quando o TOTP existe. Um token não marca sessão; cada requisição dele é julgada no próprio contexto. Os guardas das superfícies privilegiadas (`platform_page`, `tenant_page`, rotas de apps, API) continuam aplicando `mfa_required` como defesa em profundidade.

Cadastrar MFA requer as chaves de cifragem abaixo; o setup gera o keyring na primeira partida.

Gere chave Fernet privadamente:

```sh
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Não publique saída, não derive de SECRET_KEY. Defina `MFA_ENCRYPTION_KEYS` em secret store: lista ordenada, primeira cifra novos dados, demais decifram antigos. Ausência/erro de chave falha configuração/enrollment, nunca plaintext fallback. MultiFernet faz cifra/decifra.

### Rotação offline

1. Faça backup protegido e teste recuperação com chaves atuais.
2. Distribua lista `nova,antiga1,antiga2` a todos os processos; preserve antigas até conclusão.
3. Offline, com acesso de migração e a mesma lista:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py rotate_mfa_keys
```

4. O comando trava Authenticator, pré-valida estruturas do release e usa MultiFernet.rotate em `data.secret` (TOTP), `data.seed` e cada `data.migrated_codes` (recovery). Não cifra JSON inteiro; mantém type/used_mask/viewed_at. Estrutura desconhecida falha antes de alterar linhas. Saída só contagem/IDs, nunca segredo/código.
5. Comprove login TOTP e que recovery já usado continua consumido em fixture; só então remova chaves antigas do runtime. Backup antigo ainda requer chaves antigas, portanto mantenha-as em custódia compatível com retenção.

### Segredos de provedores de identidade

Client secrets, chaves privadas SAML e senhas de service account LDAP gravados no banco usam um keyring próprio, `IDENTITY_PROVIDER_ENCRYPTION_KEYS`, separado de `MFA_ENCRYPTION_KEYS`: rotacionar um não afeta o outro. A regra é a mesma: lista ordenada de chaves Fernet, a primeira cifra e todas decifram, nunca derivada de `SECRET_KEY`, sem fallback em texto claro. O cipher comum fica em `skeleton/common/crypto.py`; o MFA usa o mesmo módulo, com o mesmo formato de dados de antes.

- **Campo cifrado:** modelos declaram `EncryptedTextField` (keyring padrão `IDENTITY_PROVIDER_ENCRYPTION_KEYS`). Atribuir um texto o cifra antes de chegar ao banco; ler devolve um `SealedSecret`, cujo `repr`/`str` nunca mostra o valor. O texto claro só existe no processo, na hora do uso, por `reveal()`. `dumpdata` leva o texto cifrado.
- **Auditoria:** o campo precisa estar em `TableSpec.sensitive_fields` (check `skeleton.E026`), para a auditoria redigir o valor inteiro. O núcleo não detecta segredos desconhecidos automaticamente.
- **Chaves ausentes:** com segredos gravados e o keyring vazio, `check --database default` reprova (`skeleton.E027`), `check_skeleton_schema` também, e qualquer leitura ou escrita levanta `ImproperlyConfigured`. Uma chave mal formada reprova o check `skeleton.E026` já na subida.
- **Rotação offline:** o mesmo roteiro do MFA, com `nova,antiga`:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py rotate_identity_provider_keys
```

O comando trava as linhas, decifra todas antes da primeira escrita (sem a chave antiga, nada muda) e imprime só contagens e IDs. Depois dele, a chave antiga pode sair do runtime; backups anteriores continuam exigindo-a em custódia.

## Provedor OIDC para clientes nativos (#603)

O núcleo também é **provedor** OIDC para clientes nativos registrados pelo operador, como um aplicativo de desktop que usa só a API HTTP (epic #602). O protocolo é o do `allauth.idp.oidc` (Authorization Code, verificação PKCE, emissão e rotação de tokens, revogação, discovery e JWKS); o pacote `skeleton/accounts/idp/` só põe a política do núcleo na frente das views dele e não reimplementa nada do fluxo. O login passa pelas páginas de sempre, no navegador do sistema: senha, MFA, SSO e LDAP continuam valendo.

| Endpoint | Caminho |
|---|---|
| Discovery | `/.well-known/openid-configuration` |
| JWKS | `/.well-known/jwks.json` |
| Autorização | `/identity/o/authorize` |
| Token | `/identity/o/api/token` |
| Revogação | `/identity/o/api/revoke` |
| UserInfo | `/identity/o/api/userinfo` |
| Logout iniciado pelo cliente | `/identity/o/logout` |

Sem chave de assinatura, todas essas rotas respondem 404 e `GET /api/v1/server/` informa `oidc_issuer: null` ([descoberta](api.md#descoberta-para-clientes-nativos)). O fluxo de dispositivo (`/identity/o/device`, `/identity/o/api/device/code`), o registro dinâmico (`IDP_OIDC_DCR_ENABLED`), o CIMD e a introspecção ficam fora: respondem 404.

**Política do cliente nativo.** Todo cliente é público (sem segredo, `token_endpoint_auth_method` `none`) e:

- **PKCE `S256` imposto**: `code_challenge_method=plain`, método ausente ou desafio fora do formato de 43 caracteres base64url são recusados antes do allauth (o oauthlib aceitaria `plain`);
- **redirect só loopback por IP** (RFC 8252 §7.3): registra-se `http://127.0.0.1/<caminho>` ou `http://[::1]/<caminho>`, sem porta nem query; na autorização vale qualquer porta, com o caminho exato. `http://localhost`, outro caminho e host não loopback são recusados. Um pedido recusado com redirect registrado volta a ele com `error=invalid_request`; sem redirect válido, a resposta é 400;
- só os grants `authorization_code` e `refresh_token` (outros: 400 `unsupported_grant_type`) e só `response_type=code`;
- escopos `openid profile email` e os escopos do núcleo que o operador registrou para o cliente ([escopos na API](api.md#access-tokens-oidc-de-clientes-nativos));
- cliente desativado não recebe código, não renova (401 `invalid_client`) e não autentica na API.

**Registro.** O operador registra, lista e revoga pelo comando, que roda num `system_context` com motivo próprio; o registro auditado é `sk_accounts.NativeClient` (a linha `Client` do allauth é o espelho de protocolo, criado na mesma transação):

```sh
python manage.py native_client register --client-id acme-desktop --name "Acme Desktop" --redirect-uri http://127.0.0.1/callback --skip-consent
python manage.py native_client list
python manage.py native_client revoke-all --client-id acme-desktop [--disable]
```

`--skip-consent` dispensa a página de consentimento só para esse cliente, registrado pelo operador; sem ele, a pessoa autoriza o cliente na primeira entrada. `--scope <escopo>` (repetível) acrescenta um escopo do núcleo, como `example_notes.note:read` ou `files:read`, validado como os escopos de tokens. `revoke-all` apaga todos os tokens do cliente e grava `tokens_revoked_at`; com `--disable`, fecha o cliente. Registro e revogação entram na auditoria pelos triggers de `NativeClient`.

**Validade.** O access token é opaco e vale `IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN` segundos (padrão 3600). O refresh token é rotacionado a cada renovação, o anterior deixa de valer na hora, e o novo vale `IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN` segundos (padrão 90 dias, deslizantes com a rotação). O allauth não detecta reuso de refresh token: o cliente deve renovar **um de cada vez** e descartar o refresh anterior assim que receber o novo. O banco guarda só o SHA-256 de cada token (`allauth_idp_oidc_token`).

**Invalidação.** Cada token emitido recebe, na mesma transação da emissão, um carimbo com o `auth_version` da pessoa e o grant a que pertence (uma entrada num dispositivo, mantida na rotação). Troca de senha, troca de MFA, desativação, exclusão e qualquer outra mudança de `auth_version` invalidam todos os tokens OIDC da pessoa na próxima chamada, como acontece com a sessão web e os tokens `skt_`: a API e o UserInfo respondem 401 e a renovação responde `invalid_grant`, apagando o grant. Token sem carimbo nunca autentica. Revogar um token pelo endpoint de revogação apaga o grant inteiro. A eliminação da identidade apaga todos os tokens OIDC dela.

**MFA.** O código só sai para uma sessão autenticada: quem tem TOTP precisa completar a etapa de MFA do login antes. Com `SKELETON_REQUIRE_ADMIN_MFA`, quem tem papel de plataforma e ainda não cadastrou TOTP é levado a `mfa_index` antes de qualquer código, como nas páginas de plataforma. Capacidades administrativas de organização não passam por token: um token OIDC nunca administra ([API](api.md#access-tokens-oidc-de-clientes-nativos)).

**Dispositivos conectados.** Em `/profile/devices/` a pessoa vê cada entrada ativa (aplicativo, conectado em, último uso, estado) e a revoga; revogar apaga os tokens daquele grant. Outra pessoa nunca alcança o grant: o `POST` de revogação filtra pelo próprio usuário.

**Issuer e host canônico.** O issuer é a origem de `SKELETON_PUBLIC_URL`, quando definida, e todos os endpoints do discovery usam essa origem; sem ela, é a origem da própria requisição, que atrás do proxy depende de `SECURE_PROXY_SSL_HEADER` e do `Host` repassado. Num endereço de organização (`<slug>.<domínio base>`) as rotas do provedor não existem (o host recusa `/.well-known/` e `/identity/`); o cliente descobre o issuer do domínio base por `GET /api/v1/server/`. A chave e a rotação estão em [deployment](deployment.md#provedor-oidc-para-clientes-nativos-603).

Roteiro de smoke manual, contra o servidor de desenvolvimento com uma chave configurada e o cliente registrado:

```sh
VERIFIER=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
CHALLENGE=$(python -c "import base64,hashlib,sys; print(base64.urlsafe_b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).rstrip(b'=').decode())" "$VERIFIER")
# Abra no navegador, entre e copie o code do redirect para 127.0.0.1:
echo "$BASE/identity/o/authorize?client_id=acme-desktop&response_type=code&scope=openid%20profile%20email&redirect_uri=http://127.0.0.1:53682/callback&code_challenge=$CHALLENGE&code_challenge_method=S256&state=x"
curl -sS -X POST "$BASE/identity/o/api/token" -d grant_type=authorization_code -d client_id=acme-desktop \
  -d redirect_uri=http://127.0.0.1:53682/callback -d "code_verifier=$VERIFIER" -d "code=$CODE"
```

Rollback: revogue os tokens (`native_client revoke-all`) antes de tirar `allauth.idp.oidc` de `INSTALLED_APPS`; as tabelas ficam.

## Admin, mensagens e logs

Identidades, organizações e demais registros globais são administrados em `/platform/`, por views explícitas que exigem administrador da plataforma (`is_platform_admin`: staff+superuser ativo e não excluído), sessão allauth (sem senha paralela Django), MFA quando configurado, CSRF e CAS. Não existe login paralelo do Django admin: `/admin/` e `/admin/login/` respondem 404. Não se registram recursos tenant ou edição genérica de SocialToken/Authenticator/SocialApp/auth.Group. Operações de identidade usam lifecycle para não esquecer `auth_version`. Org admin configura apps no painel de parâmetros tenant e usa as páginas empresariais para registros.

Emails são renderizados/materializados no contexto e enviados após commit. SMTP não é transação distribuída; falha de delivery é real, deve ser registrada/apresentada, não alegada como sucesso. Resend explícito allauth permite retomar confirmação. Não existe outbox/fila/retry adicional prometido.

Logs JSON stdout são minimizados: sem senhas, sessão/cookies, tokens, URLs de convite/reset, assertions ou chaves. Testes reais e pré-requisitos de Keycloak/GitHub estão em [testing](testing.md).
