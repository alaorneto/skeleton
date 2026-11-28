# Autenticação, convites, SSO e MFA

Autenticação responde **quem entrou**; autorização decide **o que essa pessoa pode fazer em cada organização**. O SSO (login único por um provedor externo) substitui apenas a prova de identidade, nunca as permissões locais. Consulte [identidade e organizações](concepts.md#identidade-e-organizações) e [login e protocolos](concepts.md#login-e-protocolos) para a visão geral.

## Identidade e admissão

O login por email é gerenciado pelo django-allauth por meio do único backend `SkeletonAuthenticationBackend`. Após autenticar, o servidor mantém a sessão no banco e o navegador envia apenas o cookie com o identificador da sessão, nunca a senha. `User.email` é normalizado e único, inclusive entre contas com marca de exclusão lógica (tombstone). Contas excluídas ou inativas não autenticam nem são recriadas via SSO.

A cada requisição, o campo `auth_version` armazenado na sessão é comparado ao da identidade: troca de senha ou MFA, desativação e revogação global invalidam sessões antigas imediatamente. Permissões globais do Django são vazias para usuários comuns; `has_perm` só retorna verdadeiro para o administrador da plataforma segundo o predicado `is_platform_admin` (ativo, não excluído, `is_staff` e `is_superuser`). O controle de acesso por papéis na organização (RBAC) não usa `auth.Group`.

Uma troca efetiva de email primário verificado incrementa `auth_version`, mesmo que o allauth já tenha sincronizado `User.email` antes do signal: a sessão que executou a troca mantém a nova versão, e todas as demais são revogadas. Restaurar um `User` incrementa `auth_version` no mesmo CAS que remove o tombstone e preserva `is_active=False`. `UserManager.create_user`, `create_superuser` e o comando `createsuperuser` criam o `UserProfile` na mesma transação.

O formulário de identidade em `/platform/users/<id>/` envia a revisão original em campo oculto. Alterações de nomes, flags e revogação são gravadas sobre uma instância bloqueada coerente, sem forjar revisão: formulários desatualizados e tentativas de remover o último administrador retornam 409. Na API, o campo `language` exige uma string correspondente a uma opção configurada — listas ou objetos retornam erro de validação 400 sem gravar nada.

O cadastro público é fechado por padrão. Quando habilitado explicitamente, exige confirmação de email e cria apenas `User` e `UserProfile`, sem criar organização nem `Membership` (vínculo organizacional). A confirmação de email é obrigatória tanto no cadastro local quanto no social. Logout, confirmação de email e início de login social nunca funcionam por `GET`: ações de formulário exigem `POST` com proteção CSRF — o token comprova que a requisição partiu de uma página autorizada, evitando que outro site abuse do cookie da vítima (embora CSRF não substitua login nem autorização). A redefinição de senha não efetua login automático, e operações sensíveis exigem reautenticação (uma nova prova recente de identidade).

Nas operações sensíveis do núcleo — mutações em `/platform/`, telas globais `sensitive`, conexão de conta social e aceite de convite por conta existente — a regra reside em `skeleton.accounts.reauthentication`. Ela aceita o último login ou a última reautenticação registrados na mesma sessão, para o mesmo usuário, dentro do prazo `ACCOUNT_REAUTHENTICATION_TIMEOUT`; expirado esse prazo, a requisição é redirecionada para reautenticar e retomada ao concluir.

Quem possui MFA cadastrado confirma exclusivamente via MFA (código do aplicativo autenticador, código de recuperação ou chave de segurança), nunca com a senha: `AccountAdapter.get_reauthentication_methods` retorna apenas os métodos de MFA, e a página de senha (`/accounts/reauthenticate/`) redireciona para a de MFA (`/accounts/2fa/reauthenticate/`), pois a senha prova apenas conhecimento, enquanto o código comprova a posse do dispositivo. Sem MFA, a confirmação continua pela senha. `skeleton.accounts.reauthentication.reauthentication_url` devolve o endereço apropriado para redirecionamentos explícitos. Apenas o aceite de convite mantém o fallback da biblioteca para contas sem método local de reautenticação (SSO sem senha nem MFA). As telas nativas do allauth (emails, senha e MFA) seguem a regra da própria biblioteca, que desde a versão 65.21 pede reautenticação explícita mesmo logo após o login; use os helpers do núcleo em vez de `allauth.account.decorators.reauthentication_required`.

### Convite

O administrador da organização emite um convite com email, papel e organização fixos. O serviço gera um token URL-safe de 32 bytes, armazena apenas seu resumo SHA-256 (`digest`) e limita a validade a sete dias. A mensagem envia o link `/invite/<org_uuid>/<token>/`; o token original aparece apenas nessa mensagem, nunca na auditoria, em logs ou em callbacks SSO. O `GET` valida o convite e guarda uma referência no servidor sem consumi-lo. O `POST` com CSRF revalida token, validade, email e organização e consome o convite atomicamente com CAS e `accepted_at IS NULL`: entre tentativas concorrentes ou repetidas, apenas uma vence.

Uma conta nova segue o cadastro do allauth com o email fixado. A posse de um convite validado prova acesso àquele endereço de email, mas nunca prova a senha nem a identidade de uma conta já existente: para contas existentes, exigem-se login ou reautenticação e email verificado idêntico ao convidado. Reconvidar reativa a `Membership` existente, sem duplicar vínculos. Convites inválidos, expirados ou já consumidos exibem uma página neutra ou o código de API `invalid_invitation`, sem revelar se a conta existe.

O convite para o **portal** de pessoas externas segue o mesmo desenho, sem papel nem `Membership`: resumo SHA-256, link `/portal/invite/<org_uuid>/<token>/`, aceite condicionado ao email verificado igual ao convidado, recusas neutras e limite de tentativas por pessoa e por IP ([portal](portal.md#convite-e-aceite-532)).

A sessão guarda apenas os UUIDs de organização e convite e o resumo do token, nunca o token em texto claro. Na criação de conta, a prova de email validada permanece disponível somente durante a requisição corrente após o consumo, para configurar o `EmailAddress` no allauth — ela não autentica uma conta existente nem pode ser reutilizada em outra requisição.

### Solicitação de ingresso

A organização define em `/o/<org>/admission/` (capacidade `admission.configure`, exclusiva do administrador) se aceita pedidos de ingresso, se aparece no diretório público e qual papel atribui aos aprovados. Sem política gravada, todas as opções ficam desligadas. O papel `organization_admin` nunca é concedido por solicitação: a administração plena continua dependendo de convite ou designação.

- **Pedir.** Em `/join/`, pessoas com email verificado visualizam as organizações que aceitam solicitações e escolheram ser listadas. `/join/<org_uuid>/` recebe o pedido com uma mensagem opcional de até 500 caracteres. Organizações que não aceitam pedidos, inexistentes ou inativas retornam o mesmo 404 neutro. O limite de 10 pedidos por hora por pessoa (no cache compartilhado) é contabilizado antes de qualquer consulta à organização, impedindo que um 429 revele sua existência. Reenviar enquanto há um pedido aberto retorna o mesmo pedido.
- **Contexto.** Como o solicitante ainda não possui vínculo, a gravação abre `system_context(reason="join:request")` para aquela organização, acionado pelo serviço após suas próprias validações e restrito a atores autenticados. Antes de entrar em um tenant, a listagem e a verificação de aceitação consultam o índice global `OrganizationDirectoryEntry`, sincronizado com a política na mesma transação. O solicitante acompanha seus próprios pedidos em todas as organizações via autodescoberta (`own_rows`); quem decide vê apenas os da própria organização.
- **Decidir.** O administrador e o gestor de pessoas (`admission.decide`) aprovam ou recusam pedidos em `/o/<org>/requests/`, em contexto tenant, respeitando a ordem de travas Organization → pedido → vínculo e CAS sobre a revisão exibida (entre duas decisões concorrentes, uma vence e a outra recebe 409). A aprovação cria o vínculo com o papel definido (`admit_member`). O gestor de pessoas não pode aprovar pedidos cujo papel carregue capacidade administrativa.
- **Readmissão.** Como a aprovação é uma decisão humana, ela pode reativar um vínculo anterior, e a fila exibe o aviso "Já foi membro". Já admissões automáticas (link, domínio ou SSO) nunca reativam vínculos antigos: elas geram um pedido.
- **Prazo e aviso.** Um pedido aberto expira em 30 dias (estado derivado; um novo pedido substitui o expirado) e pode ser retirado pelo próprio solicitante. O email comunicando a decisão é montado dentro da transação e enviado apenas após o commit.

### Links de ingresso

Com `join_links_enabled` ativo na política, quem possui `invitations.manage` cria em `/o/<org>/admission/links/` links multiuso com nome, papel, modo (entrada direta ou mediante aprovação), validade de até 90 dias e limite de 1 a 10.000 usos. O token tem 32 bytes aleatórios e é exibido uma única vez na resposta de criação (sem cache); o banco armazena apenas o SHA-256, redigido na auditoria. Papel, modo, validade, limite e resumo são imutáveis, e a revogação ocorre por exclusão lógica com efeito imediato. O gestor de pessoas não pode criar links com papéis que carreguem capacidade, e `organization_admin` nunca é concedido por link.

- **Abrir.** `GET /join/<org_uuid>/<token>/` valida o link em `system_context(reason="join:link")` — admitido antes do login, usando o resumo do token como prova — e salva na sessão apenas a referência com o resumo. Links expirados, revogados, esgotados, de outra organização ou usados com a opção desligada exibem a mesma página neutra.
- **Usar.** `POST` com CSRF, usuário autenticado e email verificado trava primeiro a organização e depois o link. Sem vínculo anterior, o modo direto admite com o papel do link e o modo com aprovação cria um `JoinRequest(channel=link)`. Quem já foi membro sempre gera um pedido, pois admissão automática nunca reativa vínculo. Membros ativos e pessoas com pedido já aberto não consomem uso. Quando `max_uses=1` recebe resgates concorrentes, apenas um vence.
- **Cadastro pelo link.** Disponível apenas com `signup_via_link_enabled`. O cadastro cria a conta e um pedido `awaiting_verification` que reserva um uso; ao confirmar o email, o fluxo admite diretamente (em links diretos) ou deixa o pedido pendente. A confirmação funciona mesmo em outro dispositivo sem sessão, porque o serviço localiza os pedidos da pessoa via autodescoberta atuando como o titular do email recém-confirmado (`verified_owner`). Sem essa opção ativa, o link não libera o cadastro público.

### Domínio de email verificado

Com `domain_join_enabled` ativo na política, o administrador (`admission.configure`) cadastra em `/o/<org>/admission/domains/` um domínio (normalizado em IDNA), o modo de admissão (direta ou com aprovação) e o papel. O serviço gera um desafio público para publicação em registro `TXT` sob `_skeleton-challenge.<domínio>`. Ao acionar "Verificar", o DNS é consultado com timeout de 3 segundos fora de qualquer trava, travando organização e domínio somente depois. Um domínio já verificado por outra organização falha com erro neutro: a posse é registrada no índice global `DomainClaim`, único por domínio e gravado exclusivamente pela verificação. Domínios pendentes ou falhos não admitem ninguém. Não há validação DNSSEC (risco conhecido), mitigada pela reverificação periódica e pela revogação de disputas pela plataforma.

- **Admissão.** Quando uma pessoa confirma um email — ou conclui cadastro social com email verificado —, `apply_verified_email_admission` consulta a posse daquele domínio (no máximo uma organização) e age em `system_context(reason="join:domain")`, tendo o email confirmado como prova. Sem vínculo anterior, admite (modo direto) ou abre um `JoinRequest(channel=domain)`. Ex-membros sempre geram um pedido. Se a mesma requisição já escolheu outra organização (via link, convite ou SSO), a admissão por domínio é ignorada naquele momento e permanece acessível como sugestão na página inicial.
- **Sugestão.** A página inicial lista organizações que verificaram o domínio de algum email verificado da pessoa e aceitam entrada por domínio; o botão "Entrar" aplica a mesma regra.
- **Reverificação.** `manage.py verify_domains --recheck` (agendado via cron ou systemd timer) consulta o DNS fora das transações e grava uma transação por organização. Após três falhas consecutivas, o domínio passa a `failed` e perde a posse, suspendendo admissões automáticas até nova verificação.
- **Disputa.** Em `/platform/domains/`, a plataforma revoga a posse de um domínio (com CAS e autenticação recente), sem alterar a linha da organização, que precisará verificar o domínio novamente.

### Senhas, emails e conexão

**Contas de serviço** (`account_type=service`, veja [controle de acesso](access-control.md#contas-de-serviço)) nunca realizam login interativo. O backend recusa a autenticação e não devolve sessão (`get_user`), `AccountAdapter.pre_login` e `SocialAccountAdapter.pre_social_login` negam de forma genérica, e a redefinição de senha mantém resposta neutra sem enviar email. Como a senha é inutilizável, o login responde exatamente como para um email desconhecido. Integrações autenticam por tokens próprios, nunca por sessão.

Identidades criadas via SSO recebem senha inutilizável e `local_password_allowed=False`, enquanto contas locais ou criadas por convite recebem `True`. Essa política pertence à identidade global, independentemente de quantas organizações a pessoa integre. Ser "SSO-only" impede apenas o login local por senha — não representa permissão em nenhum tenant. A redefinição de senha mantém resposta pública neutra sem enviar mensagens para contas SSO-only, inativas ou excluídas; definir ou trocar senha e concluir a redefinição revalidam a política, inclusive com tokens antigos. Não existe endpoint paralelo de senha na API.

`ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS=False` suprime a mensagem genérica que o allauth enviaria após filtrar uma conta sem permissão de senha local. A resposta pública segue neutra, sem revelar se a conta é desconhecida, inativa ou SSO-only.

Um email retornado pelo provedor que coincida com uma conta local **nunca** conecta as contas automaticamente (`SOCIALACCOUNT_EMAIL_AUTHENTICATION=False`, `SOCIALACCOUNT_EMAIL_AUTO_CONNECT=False` e `can_authenticate_by_email=False`). É necessário entrar na conta existente e realizar a conexão explicitamente após reautenticação. `User.email` e `EmailAddress` só sincronizam após confirmação e nunca reutilizam endereços de contas excluídas.

`SOCIALACCOUNT_STORE_TOKENS=False` isoladamente não basta para proteger um cadastro social pendente. Após validar o protocolo, o adapter descarta os tokens e mantém na sessão apenas `process`, `next` e `headless`, indispensáveis ao fluxo. `extra_data` retém apenas o binding e, durante uma admissão inicial ainda não confirmada, o email pendente — tokens e claims completos nunca são serializados nesse fluxo.

### Sessão entre o domínio principal e os endereços de organização

Quando `SKELETON_ORGANIZATION_BASE_DOMAIN` está configurado ([#481](https://github.com/alaorneto/skeleton/issues/481)), um único login vale no domínio principal e em todos os endereços `<slug>.<domínio base>`. `SiteSessionMiddleware` (`skeleton/tenancy/site_middleware.py`), usado no lugar do `SessionMiddleware` do Django, decide o domínio do cookie **por requisição**: respostas de hosts iguais ou subordinados ao domínio base gravam o cookie com `Domain=.<domínio base>` (inclusive ao removê-lo no logout); respostas de outros hosts — como um domínio próprio ([#489](https://github.com/alaorneto/skeleton/issues/489)) — gravam o cookie restrito ao próprio host. As flags `HttpOnly`, `SameSite=Lax` e `Secure` permanecem iguais. Com o domínio base ativo, o cookie passa a chamar-se `skeleton_session` para evitar conflitos com cookies antigos restritos a um único host; no dia em que o domínio base é configurado, todos os usuários precisam entrar novamente uma vez.

O cookie de CSRF nunca é compartilhado: cada host emite o seu e a checagem de `Origin` do Django compara com o host da própria requisição, bloqueando com 403 qualquer formulário de `acme.<base>` enviado para `outra.<base>`. Como `SameSite=Lax` não isola subdomínios sob o mesmo domínio base, essa checagem é essencial — razão pela qual `CSRF_TRUSTED_ORIGINS` nunca aceita curingas. O check `skeleton.E039` reprova configurar `SESSION_COOKIE_DOMAIN` ou `CSRF_COOKIE_DOMAIN` em conjunto com o domínio base, e `skeleton.W039` avisa se o host de `SKELETON_PUBLIC_URL` ficar fora dele (impedindo o compartilhamento da sessão). O domínio base precisa ser **dedicado** a esta aplicação, pois todo subdomínio abaixo dele receberá o cookie de sessão.

### Login no endereço da organização

Em um host `<slug>.<domínio base>` ([#486](https://github.com/alaorneto/skeleton/issues/486)), a tela de login refere-se apenas àquela organização ("Use sua conta para acessar `<nome>`.") e exibe somente provedores gerais e os vinculados àquela organização, via template tag `site_providers` (`skeleton/web/templatetags/sites.py`), que consulta `skeleton.accounts.providers.provider_organization_id`. Esse filtro é apenas de apresentação: quem pode autenticar por qual provedor continua decidido pelo `SocialAccountAdapter`, e `list_apps` permanece inalterado para que o retorno de qualquer provedor continue resolvendo.

Como OIDC e SAML retornam à URL de callback registrada no domínio principal (registrar uma URL por organização em cada provedor seria inviável), o botão do provedor no host da organização é um **link para o domínio principal** (`<SKELETON_PUBLIC_URL>/accounts/<provedor>/login/?process=login&next=<URL absoluta do host da organização>`). O allauth exibe a confirmação no domínio principal ("Continuar para `<organização>` com `<provedor>`", em `socialaccount/login.html`), redireciona ao provedor, recebe o retorno no domínio principal e envia o navegador para o `next`; graças ao cookie de sessão compartilhado, a pessoa chega autenticada ao host da organização. O LDAP usa formulário local e funciona diretamente no próprio host. Quando a sessão não é compartilhada (domínio principal fora do domínio base, ou domínio próprio), os botões de OIDC e SAML ficam ocultos, mantendo senha, LDAP e MFA. Em um domínio próprio, o cookie vale apenas para aquele host: o login lá não vale no domínio principal e vice-versa.

Um parâmetro `next` absoluto só é aceito por `AccountAdapter.is_safe_url` quando `skeleton.tenancy.site.is_site_url` confirma que ele aponta para uma organização **com endereço ativo**, no mesmo esquema e porta de `SKELETON_PUBLIC_URL`, sem usuário/senha, barra invertida nem caracteres de controle, e apenas se a requisição atual ocorrer em um host que compartilhe a sessão. Qualquer outro host sob o domínio base (inexistente, desligado, reservado ou com mais de um rótulo) é recusado, mesmo estando coberto por `.<domínio base>` em `ALLOWED_HOSTS`. Emails de conta disparados a partir do host da organização (confirmação ou redefinição de senha) usam o nome dela e links desse host.

## Provedores no banco (#29)

Provedores corporativos OIDC, SAML e LDAP também podem ser armazenados no banco de dados em `sk_accounts.IdentityProvider`, cadastrados pela plataforma e ativos já na requisição seguinte, sem reinício. Cada registro vincula-se a uma organização via `bound_organization` (nunca `organization`, pois a tabela é global). O `SocialApp` usado pelo allauth é montado em memória por `SocialAccountAdapter.list_apps` uma vez por requisição, sem gravar na tabela editável `SocialApp`. `show_on_login=false` oculta o botão da tela de login (`settings.hidden` do allauth) sem desativar o provedor; `enabled=false` o desliga já na requisição seguinte, inclusive para contas previamente conectadas.

- **Identidade imutável.** `provider_id`, protocolo, organização, `issuer` (OIDC), entity ID do IdP (SAML), diretório (LDAP), `client_id`, entity ID do SP e `fingerprint` nunca mudam — tanto o guard do banco quanto `update_identity_provider` recusam alterações. Como o fingerprint segue a mesma tupla v2 do arquivo JSON, rotacionar o certificado SAML do IdP em `settings` preserva os subjects. Mudar a identidade requer cadastrar um novo provedor e executar migração administrativa dos subjects.
- **Identificador da organização preso.** Como o fingerprint incorpora o slug da organização, uma organização que tenha provedor vinculado (no banco ou no arquivo) não pode trocar seu identificador ([#765](https://github.com/alaorneto/skeleton/issues/765)): a alteração é recusada no campo para evitar invalidar silenciosamente os `SocialAccount` já vinculados.
- **Opções e segredos.** `settings` aceita apenas a allowlist do protocolo: vazia em OIDC (escopos, prova de email e PKCE ficam no código); em SAML, `idp` (`sso_url`, `slo_url`, `metadata_url`, `x509cert`), `sp.x509cert` e `attribute_mapping`. As flags de assinatura e de recusa de SSO iniciado pelo IdP são sempre impostas em código. O segredo do cliente OIDC e a chave privada do SP SAML são cifrados com `IDENTITY_PROVIDER_ENCRYPTION_KEYS` (`MultiFernet`) e redigidos na auditoria; deixar o campo de segredo vazio em uma atualização preserva o valor atual.
- **Admissão.** JIT, papel padrão e domínios de email permitidos são configurados pela organização em `IdentityProviderAdmission` (sem linha gravada, o JIT fica desligado). O adapter lê essa linha em `system_context(reason="sso:validated")` da organização do provedor; o papel nunca vem de claim, query ou corpo da requisição.
- **Coexistência com o arquivo.** Provedores como gov.br e GitHub permanecem no JSON. Se um `provider_id` existir no arquivo, o arquivo prevalece sem consultar o banco; o banco atende os demais. O comando `manage.py import_identity_providers --from-config <arquivo> [--dry-run]` migra provedores `oidc` e `saml` do JSON para o banco preservando o fingerprint (além da regra de JIT e do papel padrão em `IdentityProviderAdmission`), mantendo os `SocialAccount` válidos. Enquanto a entrada permanecer no arquivo, ela continua ativa e `validate_provider_bindings` emite aviso; remova-a do arquivo para passar a usar o banco. Fingerprints divergentes para o mesmo `provider_id` geram erro, e o cadastro recusa qualquer `provider_id` já ocupado no arquivo por outra identidade.

### Cadastro pela plataforma (#30)

Em `/platform/identity-providers/`, o administrador da plataforma lista os provedores do banco, cadastra provedores OIDC (`issuer`, `client_id`, segredo) ou SAML (entity ID do IdP, `client_id`, entity ID do SP, URLs, certificados e chave privada do SP) e edita apenas os campos mutáveis. Os campos de identidade aparecem somente para leitura, e qualquer `POST` que tente alterá-los é recusado. Segredo e chave privada são somente escrita: enviá-los em branco preserva o valor atual, e nem a interface nem mensagens de erro os exibem.

Toda gravação exige CSRF, CAS (revisão obsoleta retorna 409) e autenticação recente. O botão "Testar" consulta a descoberta OIDC (`/.well-known/openid-configuration`) ou os metadados SAML pelo cliente HTTP de saída (somente HTTPS público, 5 s de conexão e 10 s de leitura, sem redirecionamentos) e grava apenas `last_check_at` e `last_check_code`, sem efetuar login. A página de cada organização em `/platform/` exibe o card "Provedores de identidade".

### Regras de admissão da organização (#31)

Em `/o/<org>/identity/` (capacidade `identity.configure`, do administrador), a organização visualiza os provedores vinculados a ela pela plataforma em uma projeção sem segredos nem opções de protocolo (nome, protocolo, estado ativo, URL de login e último teste) e configura, por provedor:

- **JIT** (`jit_provisioning`): cria o vínculo no primeiro login; desligado por padrão e quando não há registro;
- **Aprovação** (`jit_requires_approval`): faz o primeiro login gerar um `JoinRequest(channel=sso)` pendente para decisão na fila de solicitações, em vez de criar o vínculo diretamente;
- **Papel padrão**: papel ativo da organização, nunca vindo de claim, query ou corpo. Quem não tem `access.manage` não pode escolher papéis com capacidade; o administrador pode escolher qualquer papel;
- **Domínios permitidos** (até 50, normalizados em IDNA): identidades novas com email fora da lista recebem a recusa neutra padrão ("Identity admission denied."), sem revelar a restrição.

A gravação utiliza CAS a partir da revisão virtual 0 e atualiza exclusivamente `IdentityProviderAdmission`. Na API, os endpoints equivalentes são `GET /api/v1/o/<org>/identity-providers/` e `GET`/`PUT /api/v1/o/<org>/identity-providers/<id>/admission/` com `If-Match`. Outras organizações não visualizam nem alteram esses provedores (404).

### LDAP (#32)

O LDAP é implementado como um provider do allauth (`skeleton.accounts.ldap.provider.LDAPProvider`, registrado via `skeleton/accounts/provider.py` sem criar novo app label), reaproveitando `pre_social_login`, o bloqueio de auto-connect por email, a etapa de MFA, `local_password_allowed=False` e o JIT da organização. O cliente (`skeleton/accounts/ldap/client.py`, sobre `ldap3`) é o único ponto que se comunica com o diretório.

- **Configuração (plataforma).** Em `/platform/identity-providers/new/?protocol=ldap`, configuram-se: a URL do diretório com a base de busca (`ldaps://servidor/dc=empresa,dc=com`, ou `ldap://` apenas com StartTLS), imutável e parte do fingerprint; DN e senha cifrada da service account; filtro contendo exatamente um marcador `{username}`; atributo identificador (`entryUUID` ou `objectGUID`, lido com `bytes_le`); atributo de email; grupo exigido opcional; arquivo de CA opcional (validado ao salvar como arquivo regular PEM X.509 com menos de 1 MiB); e timeout de 1 a 30 s. TLS sempre verifica o certificado (`CERT_REQUIRED`), recusando `ldap://` sem StartTLS. O botão "Testar" realiza o bind da service account e lê a base.
- **Login.** `/accounts/ldap/<provider_id>/login/` exibe o formulário. O `POST` (com CSRF e senha marcada como parâmetro sensível) aplica dois limites de taxa antes de contatar o diretório — por IP (20 tentativas em 10 min) e por provedor + usuário (5 tentativas em 10 min) —, recusa usuário ou senha vazios (evitando bind anônimo), busca o DN usando a service account com valor escapado no filtro e executa o bind como a pessoa. Qualquer falha — de usuário inexistente a diretório indisponível — retorna "Usuário ou senha inválidos.". Um bind bem-sucedido gera um `SocialLogin` com `uid` igual ao identificador do diretório e prossegue por `complete_social_login`; a senha nunca é armazenada nem registrada.
- **Identidade.** Alterar o email no diretório não cria uma nova identidade, pois o `uid` é o UUID. Um email coincidente com conta local nunca conecta automaticamente; conectar a uma conta existente exige reautenticação recente, como nos demais provedores.
- **Limites.** Não há suporte a Kerberos/SPNEGO nem a LDAP sem TLS. O limite por IP no login LDAP e no aceite de convite externo utiliza `skeleton.common.client_ip.client_ip`, seguindo a mesma topologia de proxies confiáveis do allauth (`DJANGO_TRUST_PROXY`, `ALLAUTH_TRUSTED_PROXY_COUNT` e `ALLAUTH_TRUSTED_CLIENT_IP_HEADER`). O pacote `ldap3` 2.9.1 emite dois `DeprecationWarning` vindos do `pyasn1` ao importar (acompanhado em [#32](https://github.com/alaorneto/skeleton/issues/32)).

### Sincronização do diretório (#33)

O comando `manage.py sync_directory --provider <id> | --all [--dry-run] [--provision] [--max-deactivations N] [--format json]` lê cada provedor LDAP ativo (com busca paginada fora de transação) e processa cada organização em uma transação própria sob `system_context(reason="directory:sync")`:

- **Desativação:** desativa o vínculo de quem entrou por aquele provedor e não consta mais no diretório ou no grupo exigido. Cada desativação segue o ciclo de vida padrão do administrador (revoga papéis, grupos e concessões, mantendo vínculo e histórico) em um savepoint isolado; o último administrador é pulado e reportado. Ninguém é reativado automaticamente.
- **Salvaguardas:** diretório vazio, erro de busca ou número de desativações acima de `--max-deactivations` abortam a organização sem gravar alterações e encerram o comando com código de erro.
- **Provisionamento (`--provision`):** desligado por padrão; executa **depois** da confirmação das desativações, em savepoint próprio por pessoa. Admite, conforme `IdentityProviderAdmission`, quem já autenticou pelo provedor mas nunca teve vínculo na organização (ex-membros nunca são readmitidos). Estouro de cota de membros (`quota`) ou papel padrão indisponível (`invalid_role`) entram em `skipped` sem desfazer as desativações nem as demais admissões.
- **Relatório e agendamento:** a saída inclui apenas contagens e UUIDs, e o provedor grava `last_sync_at` e `last_sync_code`. O agendamento cabe ao operador ([deployment](deployment.md#sincronização-de-diretório-ldap)): não há agendador interno nem sincronização de nome, email ou mapeamento de grupos LDAP para papéis.

## JSON privado de provedores

A variável `SSO_PROVIDER_CONFIG_PATH` aponta para um arquivo privado fora do Git cujo objeto raiz contém `providers`. Cada `provider_id` precisa ser único e seguro para URL. Os protocolos aceitos são `oidc`, `saml`, `github` e `govbr`. Provedores corporativos OIDC e SAML exigem `organization_slug`, pois podem admitir membros em uma organização específica; já o gov.br representa identidade global e recusa configuração de organização, papel ou JIT (criação automática de vínculo no primeiro login). Ao carregar os settings, o parser valida tipos, URLs e referências de segredos sem consultar o banco; após as migrations, serviços e checks validam organizações, papéis ativos e estabilidade do binding.

Os settings carregam `SKELETON_PROVIDER_CONFIGURATION` uma única vez na inicialização do processo; bindings e allauth compartilham essa mesma configuração sem reler o arquivo a cada requisição. Alterações no arquivo ou no ambiente exigem reinício controlado.

Não é permitido declarar `verified_email`, escopos arbitrários ou `auth_params` para forjar provas ou ampliar permissões. Os escopos solicitados são fixos por protocolo: OIDC pede `openid`, `profile` e `email` (preservando a solicitação de reautenticação); gov.br pede `openid`, `email` e `profile`; GitHub pede somente `user:email`. Parâmetros de query `scope` ou `auth_params` nunca ampliam consentimento nem alteram `client_id`, `redirect_uri`, `state` ou `nonce`. Aceitar email como verificado exige evidência do protocolo somada à confiança explicitamente configurada. Todos os fluxos OIDC exigem ID token assinado e `nonce` aleatório vinculado ao estado de uso único do allauth e ao `provider_id`; uma conexão TLS entre servidores não dispensa a assinatura.

As classes de extensão de segurança para OIDC, SAML e GitHub são registradas mesmo quando não há credenciais nem apps configuradas, garantindo que o registry do allauth mantenha essas subclasses desde a carga inicial e impedindo que novos perfis reutilizem inadvertidamente a classe upstream sem minimização ou `nonce` (isso não libera login sem um binding autorizado). O adapter seleciona os endpoints do gov.br a partir do perfil global carregado no snapshot, nunca por descoberta nem por settings editáveis de `SocialApp`.

Após `migrate`, sincronização do catálogo e verificação de schema, execute o preflight com os settings **runtime** que carregam os provedores:

```sh
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py validate_provider_bindings --database default
```

Esse comando valida organizações e papéis vivos bem como todos os fingerprints históricos, sem fazer login nem alterar subjects. O fingerprint resume a configuração que fixa o domínio de identidade de um binding — na versão v2, combina protocolo, issuer ou entity ID, `client_id`, organização e entidade SP. Qualquer divergência bloqueia o deployment, e fingerprints não são atualizados automaticamente: para mudar o domínio de identidade, crie outro `provider_id` e conduza uma migração administrativa revisada, pois reutilizar o mesmo ID poderia associar uma pessoa do novo emissor à conta de outra.

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

`secret_env` resolve o segredo apenas em memória no processo, e `private_key_env` resolve `private_key` recursivamente. Segredos literais aninhados ou combinações de valor literal com referência são rejeitados sem expor valores nas mensagens de erro; certificados públicos literais são permitidos. Nunca coloque segredos literais em arquivos de exemplo. O provedor GitHub usa `provider_id="github"`, `client_id` e `secret_env`, solicitando apenas o escopo `user:email`. Sem binding corporativo, o primeiro ingresso depende de convite, nunca de afiliação inferida do login social.

Regras de admissão para provedores no arquivo:

- `jit_provisioning` é `false` por padrão, e `default_role_code` assume `member` — papel e organização nunca vêm de claims, query ou corpo. No arquivo, `default_role_code` nunca pode apontar para um papel interno com capacidade (`organization_admin`, `people_manager`, `app_manager`): a carga dos settings falha e o adapter também recusa o binding em tempo de execução (já nas regras de admissão no banco, o papel é definido por quem possui `access.manage`). O GitHub, sendo provedor público, recusa `jit_provisioning: true`.
- Mesmo com `trust_verified_email=True`, o email só é considerado verificado quando o protocolo validado efetivamente o atesta; caso contrário, exige-se confirmação local. Claims de grupo nunca promovem `is_staff` ou `is_superuser`.
- Se uma pessoa confirmar o email para uma admissão pendente que se tornou impossível (por exemplo, organização desativada ou papel removido), o email é confirmado normalmente e apenas a admissão é pulada, registrando `sso.jit_admission_skipped`.
- Sem email atestado pelo protocolo, a confirmação local conclui somente a admissão inicial pendente daquele email e binding de origem, nunca reativando uma `Membership` suspensa ou removida — confirmar um email adicional nunca desfaz offboarding, e readmissões exigem convite autorizado. Após confirmar um cadastro social sem sessão iniciada, a pessoa precisa iniciar o SSO novamente, pois a confirmação não realiza login automático.
- A admissão pendente é mantida como estado no servidor: preserva a decisão inicial entre callbacks até a confirmação, ignora claims extras como substitutos e não é apagada pelas escritas internas de consulta do allauth. A criação inicial de `Membership` usa o mesmo bloqueio da organização aplicado no offboarding, prevenindo condições de corrida entre admissão e remoção.

## Protocolos e callbacks

O **IdP** (provedor de identidade) autentica a pessoa; a aplicação local atua como cliente OIDC ou **SP** (prestador de serviço) SAML. Em OIDC, **issuer** identifica o emissor e **subject** (normalmente `sub`) identifica a pessoa dentro desse emissor — o par emissor e subject, e não o email mutável, define a identidade externa. Em SAML, `entity_id` identifica cada entidade e o UID escolhido deve ser imutável. Um **binding** liga de forma administrada essa configuração externa à identidade ou organização local.

OIDC (OpenID Connect) estende o OAuth para entregar uma prova de identidade — o ID token assinado (um access token OAuth isolado não serve como prova de identidade). SAML transporta declarações de identidade em XML assinado. O perfil gov.br utiliza OIDC com endpoints oficiais e credenciamento próprio, sem protocolo customizado, enquanto o GitHub usa o fluxo OAuth integrado pela biblioteca.

No retorno ao callback, `state` vincula a resposta ao fluxo iniciado e é consumido uma única vez; `nonce` amarra o ID token àquela tentativa específica, impedindo reaproveitamento de tokens; e PKCE `S256` exige a prova secreta gerada no início para trocar o código por tokens, mitigando interceptação. Essas defesas complementam a assinatura, a validação de `issuer`/`audience` e o HTTPS, e nunca devem ser desativadas por parâmetros de URL.

| Protocolo | Callback e endpoints | Identidade e requisitos |
|---|---|---|
| OIDC | `/accounts/oidc/<provider_id>/login/callback/` | `sub` estável por issuer; discovery via `server_url` administrado; PKCE `S256` e `client_secret_basic` |
| Gov.br OIDC | `/accounts/oidc/<provider_id>/login/callback/` | Perfil global com endpoints oficiais fixos por `staging` ou `production`; PKCE `S256`, `state`, `nonce`, assinatura e `client_secret_basic`; sem discovery fornecido pelo usuário |
| GitHub OAuth | `/accounts/github/login/callback/` | Subject GitHub; app real e escopo `user:email`, sem `repo` ou `read:org` |
| SAML | `/accounts/saml/<slug>/login/`, `acs/`, `sls/`, `metadata/` | `app.client_id=slug`; `provider_id` estável; UID imutável, nunca email |

Exemplo de `app.settings` para SAML (substitua entidade, certificado e URLs pelos dados reais; nunca copie chaves do IdP):

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

Em vez de `x509cert`, o IdP pode ser configurado via `metadata_url` confiável. Validações de assinatura, XML e regras do protocolo ficam integralmente sob responsabilidade do allauth e do `python3-saml`, nunca em código próprio. Tanto a resposta quanto a assertion devem vir assinadas, e SSO iniciado pelo IdP (não solicitado) é recusado. Em produção, TLS e certificados confiáveis são obrigatórios; HTTP é aceito apenas em loopback `DEBUG` para fixtures, e nunca se deve adulterar a trust store para aceitar certificados inválidos.

## Perfil global gov.br

O perfil reutiliza o allauth 65.19.7 — seu `OAuth2Client`, geração de PKCE, gerenciamento de `state` e validador JWT/JWK. A extensão OIDC exige assinatura válida, `issuer` exato (incluindo a barra final oficial), `audience`/`client_id`, validade temporal e `nonce`, vinculando o estado ao provider para impedir que um fluxo seja consumido pelo callback de outro perfil. Não há parser OAuth/JWT próprio nem login direto por token. Apenas o ID token validado é utilizado: o núcleo não consulta `userinfo`, foto ou níveis de confiabilidade, nem persiste access, refresh ou ID tokens em `SocialToken` ou em cadastros pendentes.

Configuração privada mínima (substitua domínio, client ID e referência de segredo pelas credenciais reais):

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

`environment` aceita apenas `staging` ou `production`, apontando respectivamente para `https://sso.staging.acesso.gov.br/` e `https://sso.acesso.gov.br/` com os caminhos fixos `authorize`, `token`, `jwk`, `userinfo` (não consultado) e `logout`, sem depender de discovery não documentado. Ambiente, issuer e `client_id` compõem o fingerprint: homologação e produção exigem `provider_id`s distintos, sem nunca trocar o domínio de identidade sob um binding existente. O perfil global recusa `organization_slug`, `jit_provisioning`, `default_role_code`, escopos ou endpoints customizados.

O escopo revisado para login é **`openid email profile`**, a ser aprovado no credenciamento. Embora o [roteiro oficial](https://acesso.gov.br/roteiro-tecnico/iniciarintegracao.html) descreva `govbr_confiabilidades` e `govbr_confiabilidades_idtoken`, eles não são necessários à identidade global e não são solicitados pelo núcleo. Níveis bronze/prata/ouro, `amr`, grupos, CNPJ e claims de tenant ou papel nunca concedem afiliação, privilégios nem dispensa de MFA.

O botão **Entrar com GOV.BR** envia `POST` com CSRF e segue a aparência de signin do [Design System de Governo](https://www.gov.br/ds/components/signin?tab=designer), sem scripts nem assets remotos de autenticação. O navegador segue para o IdP, retorna pelo callback do allauth e é redirecionado para uma URL limpa, mantendo a API baseada apenas na sessão Django.

A admissão continua fechada por padrão, ocorrendo apenas por convite autorizado ou por conexão explícita de uma conta existente após login ou reautenticação recente. Sem vínculo corporativo, `SKELETON_PUBLIC_SIGNUP=true` cria somente `User` e `UserProfile` globais, sem organização, `Membership` ou papel. Convites concedem exclusivamente o vínculo autorizado pelo serviço de convites, nenhuma confirmação de email do perfil global reativa vínculos suspensos ou removidos, e conectar o gov.br não altera a política de senha local da conta existente. A conexão exige autenticação recente inclusive para contas SSO-only, evitando o fallback permissivo de reautenticação da biblioteca quando não há método local cadastrado.

Emails coincidentes nunca conectam automaticamente: faça login na conta existente e conecte de forma explícita. Em uma admissão autorizada cujo retorno do gov.br não inclua email, o allauth apresenta o formulário de cadastro para coletar um email **real** e exige confirmação local. Em um convite, o campo permanece fixado no endereço autorizado (já validado), recusando trocas via claim ou formulário, sem gerar endereços sintéticos. A claim `email_verified` só vale como prova quando é o booleano `true` ou a string exata `"true"` com `trust_verified_email=true`; `"false"`, números, listas e outras representações não confirmam o email.

**Dados pessoais:** o roteiro do gov.br define `sub` como o CPF. Esse valor permanece estritamente como UID técnico em `SocialAccount` e no estado server-side do allauth quando necessário — nunca há auto-link por CPF, cópia em `User` ou `UserProfile`, exibição em nomes de conexão, endpoint público, log ou payload de auditoria próprio. Antes das escritas de lookup, `extra_data` contém apenas o fingerprint; telefone, foto, nome, confiabilidades e claims integrais não são salvos, e o Django admin não registra `SocialAccount` nem `SocialToken`.

Como a biblioteca não cifra o UID armazenado, proteja também o acesso operacional ao banco de dados, às sessões e aos backups: criptografia em repouso, base legal, retenção e atendimento à LGPD são responsabilidades do deployment e exigem revisão explícita. Quando a pessoa solicita eliminação e a plataforma aprova ([privacidade](privacy.md#eliminação-da-identidade)), suas contas sociais — incluindo o UID — são removidas junto com os emails e os autenticadores MFA.

O logout local ocorre via `POST /accounts/logout/` com CSRF pelo fluxo oficial do allauth (`GET` nunca encerra sessão). Em sessões autenticadas pelo gov.br — ou nas quais a conta gov.br foi conectada —, o navegador é redirecionado ao endpoint `logout` do ambiente configurado **somente após** encerrar a sessão local, enviando apenas o `post_logout_redirect_uri` HTTPS fixo do JSON privado (sem query nem fragmento); o parâmetro `next` da requisição nunca escolhe o callback nem endpoints externos. Essa URL de retorno precisa estar previamente cadastrada no gov.br e servir uma página real, geralmente a tela de login local.

### Credenciamento e evidência obrigatória

Solicite as credenciais pela [integração oficial](https://acesso.gov.br/roteiro-tecnico/solicitacaocredencialprocesso.html), providenciando segredo privado, callback HTTPS exato (`/accounts/oidc/<provider_id>/login/callback/`), URL de logout pré-autorizada, domínio, escopos aprovados e conectividade TLS/JWK. O ambiente de produção exige domínio oficial de governo conforme o roteiro (`gov.br`, `mil.br`, `edu.br`, `jus.br`, `leg.br` etc.) e aprovação do órgão responsável; domínios privados comuns não atendem a esse requisito.

Após provisionar, reinicie os processos e execute `validate_provider_bindings` com a credencial runtime. Homologue login, admissão, conexão e logout reais em navegador externo — com a barra de endereço visível no vídeo exigido — verificando também MFA e ausência de afiliação automática. Como credenciais reais do gov.br **não foram disponibilizadas durante o desenvolvimento**, testes contra staging e produção permanecem pré-requisitos operacionais de homologação, nunca sucessos simulados. A suíte `tests/accounts/test_govbr.py` cobre biblioteca e fluxos com JWTs RS256 locais e transporte HTTP controlado, mas nem esses testes nem Keycloak substituem a homologação oficial.

## MFA cifrado e obrigatório

A autenticação multifator (MFA) exige uma segunda prova além do método principal de login. O núcleo suporta TOTP (códigos temporários gerados por aplicativo autenticador) e `recovery_codes` (códigos de recuperação para perda do dispositivo). Ter um email verificado é pré-requisito; códigos de recuperação são exibidos uma única vez, e o recurso de navegador confiável permanece desativado. A claim `amr` de um IdP nunca dispensa nenhuma das regras abaixo.

**Quem precisa cadastrar o MFA.** As regras são cumulativas — nenhuma afrouxa outra, e um TOTP cadastrado satisfaz todas. Contas de serviço nunca cadastram MFA. A checagem central fica em `skeleton.accounts.mfa.mfa_enrollment_pending`:

| Regra | Quem | Onde se configura |
|---|---|---|
| `SKELETON_REQUIRE_PLATFORM_ADMIN_MFA` | Todo administrador completo da plataforma (`is_platform_admin`), tanto o do setup quanto os promovidos depois | Fixa em `True` no perfil de produção (nem o ambiente nem o SQLite da instância podem desligá-la). Em desenvolvimento e testes o padrão é `False`, onde as chaves de MFA são opcionais |
| `SKELETON_REQUIRE_ADMIN_MFA` | Quem possui papel de plataforma ou capacidade administrativa na organização da requisição | Tela da instância (`/platform/instance/`); `true` por padrão em produção |
| `SKELETON_REQUIRE_MFA` | Toda pessoa | Tela da instância; `false` por padrão |
| Política da organização (`OrganizationSettings.mfa_policy`) | `admins`: quem tem capacidade administrativa nesta organização; `all`: todos os membros. Em branco segue a plataforma | Administrador da organização, em **Configurações → Segurança** (`/o/<org>/settings/security/`) |

A política da organização vale tanto nas rotas internas dela quanto em leituras agregadas (`for_each_organization`, incluindo agenda pessoal e OpenAPI): organizações cuja política exige MFA ainda pendente são omitidas dessas leituras até que o TOTP seja cadastrado — com exceção explícita da exportação de dados pessoais (LGPD, `respect_mfa=False`, que já exige reautenticação recente). A política não se aplica a outras organizações, páginas globais anteriores à entrada na organização nem ao portal de pessoas externas. Alterá-la exige administrador (`organization_lock`), CSRF, revisão das configurações (CAS, retornando 409 se desatualizada) e gera auditoria em `OrganizationSettings`; quem ativa a exigência já precisa ter TOTP cadastrado para não bloquear a si mesmo. A política fica fora de `OrganizationService.update_settings`, impedindo que apps a modifiquem.

**Só a página de MFA.** Enquanto alguma regra exigir MFA e a pessoa ainda não possuir TOTP, `skeleton.accounts.mfa_middleware.MFAEnrollmentMiddleware` (posicionado após o contexto tenant, validado pelo check `E009`) intercepta qualquer rota fora da lista permitida: requisições HTML são redirecionadas (302) para `mfa_index`, e requisições de API recebem `403 mfa_required`, inclusive com token pessoal. A lista permitida (`ALLOWED_ROUTES`) libera somente as telas de MFA do allauth (cadastro de TOTP, códigos de recuperação e reautenticação), reautenticação, verificação de email (exigida pelo allauth antes de cadastrar MFA), logout e imagens da marca. Nessas páginas, o shell oculta a navegação principal, a barra lateral da organização e o menu "Minha conta", exibindo apenas a marca e o botão de sair.

Como o MFA pertence à identidade, cadastrá-lo uma vez vale para todas as organizações da pessoa. Regras da plataforma bloqueiam todas as páginas; já uma regra originada na organização da requisição (sua política própria ou `SKELETON_REQUIRE_ADMIN_MFA` aplicado a uma capacidade ali) bloqueia apenas as rotas daquela organização — em outras organizações ou em páginas globais como o perfil, a pessoa navega normalmente até retornar a ela. Como as páginas de MFA são globais, a organização grava uma marcação na sessão do navegador ao redirecionar a pessoa para lá, mantendo essas páginas sem menus durante o cadastro; essa marca afeta apenas a aparência, nunca as decisões de acesso, e desaparece assim que a pessoa sai do cadastro ou conclui o TOTP. Tokens não marcam sessão e são avaliados a cada chamada no próprio contexto. Os guardas das superfícies privilegiadas (`platform_page`, `tenant_page`, rotas de apps e API) mantêm a checagem `mfa_required` como defesa em profundidade.

Cadastrar MFA exige as chaves de cifragem descritas abaixo; o setup inicial gera esse keyring automaticamente na primeira partida.

### Redefinir o MFA de quem perdeu o autenticador (#736)

Quem perde simultaneamente o aplicativo autenticador **e** os códigos de recuperação não consegue completar o login. Não há autoatendimento, pois um link enviado por email rebaixaria o MFA para senha mais email. A recuperação é feita pelo administrador da plataforma em `/platform/users/<id>/`, onde são exibidos apenas os tipos de MFA cadastrados (nunca os segredos) e o botão **Redefinir MFA**. A tela de confirmação (`/platform/users/<id>/mfa/reset/`) exige reautenticação recente, e o `POST` (com CSRF e a revisão da identidade) invoca `skeleton.accounts.services.reset_mfa`:

- remove todos os autenticadores da pessoa (TOTP, códigos de recuperação e demais tipos);
- grava `User.mfa_reset_at` e incrementa `auth_version` no mesmo `save`, gerando um evento de auditoria atribuído ao administrador e encerrando todas as sessões abertas da pessoa, inclusive as que já haviam concluído MFA;
- após o commit, envia aviso por email (`mfa/email/mfa_reset_*.txt`, sem link de ação) e registra o evento de segurança `mfa_reset` contendo apenas o `sysid`.

A operação recusa sem gravar nada: a própria conta do administrador (que usa a página de MFA da própria conta), contas de serviço, identidades removidas, identidades sem MFA e revisões desatualizadas (409). Papéis limitados da plataforma e administradores de organização não podem redefinir MFA, pois a identidade é global e compartilhada entre organizações. No próximo acesso, a pessoa entra com senha ou SSO e cadastra um novo MFA em `/accounts/2fa/`; com `SKELETON_REQUIRE_ADMIN_MFA`, administradores são conduzidos a esse cadastro antes de acessar superfícies privilegiadas. Sempre confirme a identidade do solicitante por outro canal antes de redefinir, já que esse fluxo é alvo frequente de engenharia social.

Se não houver outro administrador da plataforma disponível (por exemplo, o único perdeu o próprio MFA), o operador executa no servidor:

```sh
.venv/bin/python manage.py reset_mfa --email pessoa@example.com --reason "chamado 123"
.venv/bin/python manage.py reset_mfa --email pessoa@example.com --reason "chamado 123" --confirm
```

Sem `--confirm`, o comando apenas lista os tipos de MFA cadastrados naquela identidade. Com `--confirm`, executa o mesmo procedimento da interface dentro de um `command_context`, registrando o motivo na origem do evento de auditoria (`system:command:mfa:reset <motivo>`) e imprimindo apenas o `sysid` e a quantidade de autenticadores removidos.

### Chaves de cifragem

Gere cada chave Fernet em ambiente privado:

```sh
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Nunca publique a saída nem derive chaves a partir de `SECRET_KEY`. Armazene `MFA_ENCRYPTION_KEYS` em um cofre de segredos como lista ordenada: a primeira chave cifra novos registros e todas decifram registros antigos via `MultiFernet`. A ausência ou invalidez de chaves faz a configuração e o cadastro falharem, sem nenhum fallback em texto claro.

### Rotação offline

1. Faça backup protegido e valide a recuperação com as chaves atuais.
2. Distribua a lista `nova,antiga1,antiga2` para todos os processos, mantendo as chaves antigas até o término da rotação.
3. Offline, com a credencial de migração e a mesma lista de chaves, execute:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py rotate_mfa_keys
```

4. O comando bloqueia os registros de `Authenticator`, pré-valida as estruturas suportadas pelo release e aplica `MultiFernet.rotate` em `data.secret` (TOTP), `data.seed` e em cada entrada de `data.migrated_codes` (códigos de recuperação), sem cifrar o JSON inteiro e preservando `type`, `used_mask` e `viewed_at`. Estruturas desconhecidas abortam antes de modificar qualquer linha, e a saída exibe apenas contagens e IDs, nunca segredos ou códigos.
5. Comprove o login com TOTP e verifique em fixture que um código de recuperação já utilizado continua consumido; só então remova as chaves antigas do runtime. Como backups anteriores ainda dependem delas, mantenha as chaves antigas em custódia compatível com a política de retenção.

### Segredos de provedores de identidade

Client secrets OIDC, chaves privadas SAML e senhas de service account LDAP gravados no banco usam o keyring dedicado `IDENTITY_PROVIDER_ENCRYPTION_KEYS`, independente de `MFA_ENCRYPTION_KEYS` para que a rotação de um não afete o outro. Aplicam-se as mesmas regras: lista ordenada de chaves Fernet (a primeira cifra, todas decifram), nunca derivada de `SECRET_KEY` e sem fallback em texto claro. O módulo compartilhado de cifra é `skeleton/common/crypto.py`, utilizado também pelo MFA sem alterar seu formato de dados.

- **Campo cifrado:** modelos declaram `EncryptedTextField` (keyring padrão `IDENTITY_PROVIDER_ENCRYPTION_KEYS`). Ao atribuir um texto, o valor é cifrado antes de ir ao banco; ao ler, retorna um `SealedSecret` cujo `repr` e `str` nunca revelam o conteúdo. O valor em claro só existe em memória no instante do uso, via `reveal()`. `dumpdata` exporta o texto cifrado.
- **Auditoria:** todo campo cifrado precisa constar em `TableSpec.sensitive_fields` (verificado por `skeleton.E026`), garantindo redação integral na auditoria; o núcleo não infere segredos desconhecidos automaticamente.
- **Chaves ausentes:** se houver segredos gravados com keyring vazio, `check --database default` (`skeleton.E027`) e `check_skeleton_schema` falham, e qualquer leitura ou escrita levanta `ImproperlyConfigured`. Chaves malformadas já reprovam `skeleton.E026` na inicialização.
- **Rotação offline:** siga o mesmo roteiro do MFA usando `nova,antiga`:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py rotate_identity_provider_keys
```

O comando trava as linhas, decifra todas antes da primeira gravação (sem a chave antiga, nada é modificado) e imprime apenas contagens e IDs. Após concluir, a chave antiga pode sair do runtime, permanecendo sob custódia enquanto existirem backups antigos.

## Provedor OIDC para clientes nativos (#603)

O núcleo também atua como **provedor** OIDC para clientes nativos cadastrados pelo operador — como aplicativos desktop que consomem exclusivamente a API HTTP. Todo o protocolo (Authorization Code, validação PKCE, emissão e rotação de tokens, revogação, discovery e JWKS) é fornecido por `allauth.idp.oidc`; o pacote `skeleton/accounts/idp/` apenas aplica as políticas do núcleo sobre essas views, sem reimplementar o fluxo. A autenticação passa pelas páginas web habituais no navegador do sistema, preservando senha, MFA, SSO e LDAP.

| Endpoint | Caminho |
|---|---|
| Discovery | `/.well-known/openid-configuration` |
| JWKS | `/.well-known/jwks.json` |
| Autorização | `/identity/o/authorize` |
| Token | `/identity/o/api/token` |
| Revogação | `/identity/o/api/revoke` |
| UserInfo | `/identity/o/api/userinfo` |
| Logout iniciado pelo cliente | `/identity/o/logout` |

Sem chave de assinatura configurada, todas essas rotas retornam 404 e `GET /api/v1/server/` reporta `oidc_issuer: null` ([descoberta](api.md#descoberta-para-clientes-nativos)). Fluxo de dispositivo (`/identity/o/device`, `/identity/o/api/device/code`), registro dinâmico (`IDP_OIDC_DCR_ENABLED`), CIMD e introspecção ficam desabilitados e retornam 404.

**Política do cliente nativo.** Todo cliente é público (sem segredo, `token_endpoint_auth_method` igual a `none`) e obedece às seguintes restrições:

- **PKCE `S256` obrigatório:** `code_challenge_method=plain`, ausência de método ou desafio fora do formato de 43 caracteres base64url são recusados antes de chegar ao allauth (já que o `oauthlib` aceitaria `plain`);
- **Redirect restrito a loopback por IP** (RFC 8252 §7.3): registra-se `http://127.0.0.1/<caminho>` ou `http://[::1]/<caminho>`, sem porta nem query; na autorização, aceita-se qualquer porta com o caminho exato, recusando `http://localhost`, outros caminhos e hosts não loopback. Pedidos recusados com redirect registrado retornam `error=invalid_request` para esse redirect; sem redirect válido, retornam 400;
- **Grants e resposta:** aceita apenas `authorization_code` e `refresh_token` (demais retornam `400 unsupported_grant_type`) e somente `response_type=code`;
- **Escopos:** `openid profile email` mais os escopos do núcleo autorizados pelo operador para aquele cliente ([escopos na API](api.md#access-tokens-oidc-de-clientes-nativos));
- **Cliente desativado:** não recebe código, não renova (`401 invalid_client`) e não autentica na API.

**Registro.** O operador registra, lista e revoga clientes por meio do comando abaixo, executado em `system_context` com motivo próprio. O registro auditado fica em `sk_accounts.NativeClient`, enquanto o `Client` do allauth é mantido como espelho de protocolo na mesma transação:

```sh
python manage.py native_client register --client-id acme-desktop --name "Acme Desktop" --redirect-uri http://127.0.0.1/callback --skip-consent
python manage.py native_client list
python manage.py native_client revoke-all --client-id acme-desktop [--disable]
```

`--skip-consent` pula a tela de consentimento para aquele cliente registrado pelo operador; sem a flag, a pessoa autoriza o cliente no primeiro acesso. `--scope <escopo>` (repetível) adiciona escopos do núcleo, como `example_notes.note:read` ou `files:read`, validados com as mesmas regras de tokens. `revoke-all` remove todos os tokens do cliente e grava `tokens_revoked_at`; combinada com `--disable`, também fecha o cliente. Registro e revogação são auditados pelos triggers de `NativeClient`.

**Validade.** O access token é opaco e expira em `IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN` segundos (padrão 3600). O refresh token é rotacionado a cada renovação — invalidando imediatamente o anterior — e tem validade de `IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN` segundos (padrão 90 dias, renovados a cada rotação). Como o allauth não detecta reuso de refresh tokens, o cliente deve renovar **um token por vez** e descartar o antigo assim que receber o novo. O banco grava apenas o hash SHA-256 de cada token (`allauth_idp_oidc_token`).

**Invalidação.** Na mesma transação da emissão, cada token recebe o carimbo do `auth_version` da pessoa e do grant correspondente (representando a entrada em um dispositivo, preservada durante a rotação). Troca de senha, alteração de MFA, desativação, exclusão ou qualquer incremento de `auth_version` invalidam todos os tokens OIDC da pessoa já na próxima chamada, igual às sessões web e aos tokens `skt_`: API e UserInfo respondem 401 e a renovação responde `invalid_grant`, apagando o grant. Tokens sem carimbo nunca autenticam. Revogar um token pelo endpoint de revogação encerra o grant inteiro, e a eliminação da identidade remove todos os seus tokens OIDC.

**MFA.** O código de autorização só é emitido para uma sessão autenticada, exigindo conclusão prévia do TOTP quando cadastrado. Com `SKELETON_REQUIRE_ADMIN_MFA`, portadores de papel de plataforma sem TOTP são levados a `mfa_index` antes de receber qualquer código, como nas páginas de plataforma. Capacidades administrativas de organização nunca são exercidas por token: tokens OIDC jamais administram ([API](api.md#access-tokens-oidc-de-clientes-nativos)).

**Dispositivos conectados.** Em `/profile/devices/`, a pessoa visualiza cada acesso ativo (aplicativo, data de conexão, último uso e estado) e pode revogá-lo, apagando os tokens daquele grant. O `POST` de revogação filtra sempre pelo próprio usuário, impedindo acesso a grants de terceiros.

**Issuer e host canônico.** O issuer adota a origem de `SKELETON_PUBLIC_URL` quando definida, refletida em todos os endpoints do discovery; na ausência dela, usa a origem da requisição, dependendo de `SECURE_PROXY_SSL_HEADER` e do cabeçalho `Host` atrás de proxy. Em endereços de organização (`<slug>.<domínio base>`), as rotas do provedor ficam indisponíveis (o host recusa `/.well-known/` e `/identity/`), cabendo ao cliente descobrir o issuer do domínio base via `GET /api/v1/server/`. Detalhes de geração e rotação de chaves estão em [deployment](deployment.md#provedor-oidc-para-clientes-nativos-603).

Roteiro de teste manual (smoke test) contra o servidor de desenvolvimento com chave configurada e cliente registrado:

```sh
VERIFIER=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
CHALLENGE=$(python -c "import base64,hashlib,sys; print(base64.urlsafe_b64encode(hashlib.sha256(sys.argv[1].encode()).digest()).rstrip(b'=').decode())" "$VERIFIER")
# Abra no navegador, entre e copie o code do redirect para 127.0.0.1:
echo "$BASE/identity/o/authorize?client_id=acme-desktop&response_type=code&scope=openid%20profile%20email&redirect_uri=http://127.0.0.1:53682/callback&code_challenge=$CHALLENGE&code_challenge_method=S256&state=x"
curl -sS -X POST "$BASE/identity/o/api/token" -d grant_type=authorization_code -d client_id=acme-desktop \
  -d redirect_uri=http://127.0.0.1:53682/callback -d "code_verifier=$VERIFIER" -d "code=$CODE"
```

**Rollback:** revogue todos os tokens (`native_client revoke-all`) antes de remover `allauth.idp.oidc` de `INSTALLED_APPS`; as tabelas permanecem no banco.

## Admin, mensagens e logs

Identidades, organizações e demais registros globais são geridos exclusivamente em `/platform/`, por views explícitas que exigem administrador da plataforma (`is_platform_admin`: `is_staff` e `is_superuser`, ativo e não excluído), sessão do allauth (sem senha paralela do Django admin), MFA quando exigido, CSRF e CAS. Não existe login paralelo do Django admin: `/admin/` e `/admin/login/` retornam 404. Nunca registre recursos tenant nem editores genéricos para `SocialToken`, `Authenticator`, `SocialApp` ou `auth.Group`. Operações sobre identidades devem sempre usar os serviços de ciclo de vida para manter `auth_version` atualizado. Administradores de organização configuram apps no painel de parâmetros da organização e operam registros pelas páginas de negócio.

Emails são renderizados e materializados dentro do contexto da requisição e enviados somente após o commit. Como SMTP não participa de transações distribuídas, falhas de entrega são reais e devem ser registradas e reportadas, nunca tratadas como sucesso. O reenvio explícito do allauth permite retomar confirmações quando necessário, mas o fluxo de conta não garante outbox, fila nem retentativa automática adicional.

Os logs JSON em `stdout` são estritamente minimizados: nunca registram senhas, sessões, cookies, tokens, URLs de convite ou redefinição, assertions ou chaves. Os testes reais e os pré-requisitos de Keycloak e GitHub estão documentados em [testing](testing.md).
