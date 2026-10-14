# Autenticação, convites, SSO e MFA

Autenticação responde **quem entrou**; autorização decide **o que essa pessoa pode fazer em cada organização**. SSO (login único por um provedor externo) substitui a prova de identidade, não as permissões locais. Consulte [identidade e organizações](concepts.md#identidade-e-organizações) e [login e protocolos](concepts.md#login-e-protocolos) para a visão geral.

## Identidade e admissão

O login por email é gerenciado pelo django-allauth, através do único backend `SkeletonAuthenticationBackend`. Após autenticar, o servidor mantém uma sessão no banco; o navegador envia um cookie com seu identificador, não a senha a cada acesso. `User.email` é normalizado e único, inclusive para contas com marca de exclusão lógica (tombstone). Contas excluídas ou inativas não autenticam nem são recriadas por SSO. A versão `auth_version` guardada na sessão é comparada a cada requisição: mudança de senha/MFA, desativação e revogação global invalidam sessões antigas. Permissões Django globais são vazias para usuários comuns; só superuser ativo/não deletado acessa o admin global. RBAC, a autorização por papéis na organização, não usa `auth.Group`.

Troca efetiva de email primário verificado incrementa auth_version mesmo que o allauth já tenha sincronizado User.email antes do signal. A sessão que realiza a troca conserva a nova versão; todas as demais são revogadas. User restore incrementa a versão no mesmo CAS que remove o tombstone e conserva is_active=False. UserManager.create_user/create_superuser e o comando createsuperuser criam UserProfile na mesma transação.

O formulário de User no admin inclui revisão original escondida. Alterações de nomes/flags e revogação são persistidas numa instância bloqueada coerente, sem forjar revision; formulário stale e tentativa de remover último admin retornam 409. Language da API exige string e opção configurada: lista/objeto é validação 400 sem escrita.


Cadastro é fechado por padrão. Quando habilitado explicitamente, o cadastro público exige confirmação de email e cria somente User/Profile: não cria organização nem Membership (vínculo organizacional). A confirmação de email é obrigatória tanto no cadastro local como no social. Logout, confirmação de email e início social não são ações por GET. As ações de formulário usam POST com proteção CSRF: o token comprova que a requisição veio de uma página autorizada, evitando que outro site use o cookie da vítima para executar ações. CSRF não substitui login nem autorização. Reset de senha não faz login automático; operações sensíveis exigem reautenticação, uma nova prova recente de identidade.

### Convite

O administrador da organização emite um convite com email, papel e organização fixos. O serviço gera um token URL-safe de 32 bytes, guarda apenas seu resumo SHA-256 (digest) e limita a validade a sete dias. A mensagem contém `/invite/<org_uuid>/<token>/`; o token em texto original aparece somente nessa mensagem, nunca em auditoria, logs ou callback SSO. GET valida o convite e guarda uma referência no servidor, mas não o consome. POST com CSRF revalida token, validade, email e organização e o consome atomicamente com CAS e `accepted_at IS NULL`: duas tentativas concorrentes ou a repetição do mesmo convite têm somente um vencedor.

Conta nova usa o cadastro allauth com email fixado. A posse de um convite validado pode servir de prova de acesso àquele email; não serve de prova da senha ou da identidade de uma conta existente. Para uma conta existente, são necessários login/reautenticação e email verificado igual ao convidado. Reconvidar reativa a Membership existente, sem duplicar o vínculo. Convite inválido, expirado ou consumido apresenta uma página neutra ou o código de API `invalid_invitation`, sem revelar se a conta existe.

A sessão armazena apenas UUIDs de organização/convite e o digest, nunca o token original. Na criação, a prova de email validada continua disponível somente na requisição corrente depois do consumo, para configurar `EmailAddress` no allauth. Ela não pode autenticar uma conta existente nem ser reutilizada em outra requisição.

### Senhas, emails e conexão

Uma identidade criada por SSO recebe senha inutilizável e `local_password_allowed=False`; contas locais ou criadas por convite usam True. Essa política pertence à identidade global, mesmo quando a pessoa participa de várias organizações. “SSO-only” significa que o login local por senha está proibido, não que a pessoa tem permissão em algum tenant. Reset mantém uma resposta pública neutra, mas não envia mensagem para contas SSO-only, inativas ou excluídas. Definir/trocar senha e concluir reset revalidam a política, mesmo com token antigo. Não há endpoint paralelo de senha na API.

`ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS=False` impede a mensagem genérica que o allauth enviaria depois de filtrar uma identidade sem direito a senha local. A resposta pública continua neutra; não enviar email a conta desconhecida, inativa ou SSO-only não autoriza expor sua existência.

Um email informado pelo provedor e coincidente com uma conta local **não** conecta as contas automaticamente: `SOCIALACCOUNT_EMAIL_AUTHENTICATION/AUTO_CONNECT=False` e `can_authenticate_by_email=False`. Entre na conta existente e faça conexão explícita após reautenticação. `User.email` e `EmailAddress` só sincronizam após confirmação e não reutilizam emails de contas excluídas.

`SOCIALACCOUNT_STORE_TOKENS=False` sozinho não protege o cadastro social ainda pendente. Após validar o protocolo, o adapter remove tokens e conserva na sessão somente `process`, `next` e `headless` necessários ao fluxo. `extra_data` retém apenas o binding e, durante a admissão inicial ainda não confirmada, o email pendente. Tokens e claims (atributos declarados pelo provedor) completos não são serializados nesse fluxo.

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

JIT default False; default role member, nunca role/tenant de claim/query/body. Mesmo trust_verified_email=True só vale se o protocolo validado realmente atestar email verificado; do contrário confirmação local. Claims de grupo não promovem staff/superuser. Provider_id vincula issuer/entity ID e organização; não mude identidade por baixo do mesmo ID. Crie ID novo, revise permissões/binding e migração de subjects administrativamente, sem auto-conectar por email.

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

**Dados pessoais:** o roteiro define `sub` como CPF. Ele permanece exclusivamente como UID técnico do SocialAccount e no estado técnico server-side allauth quando necessário, sem auto-link por CPF, cópia em User/Profile, nome de conexão com CPF, endpoint público, log ou payload de auditoria próprio. Extra_data antes dos lookup writes contém apenas fingerprint; telefone/foto/nome/confiabilidades e claims integrais não são armazenados. O admin não registra SocialAccount/SocialToken. Restrinja também acesso operacional a DB, sessões e backups: criptografia, base legal, retenção e atendimento LGPD são responsabilidade do deployment e exigem revisão explícita; não há promessa de cifragem de UID pela biblioteca.

Logout local usa `/accounts/logout/` POST com CSRF e fluxo oficial allauth. Para sessão autenticada via gov.br (ou conexão validada nessa sessão), o browser recebe redirect ao `logout` do ambiente configurado **após** encerrar a sessão local. Só é enviado `post_logout_redirect_uri` HTTPS fixo do JSON privado, sem query/fragment; o parâmetro `next` do usuário não escolhe callback nem endpoint externo. GET não encerra sessão. A URL de retorno deve estar previamente liberada pelo gov.br e servir uma página real, normalmente o login local.

### Credenciamento e evidência obrigatória

Solicite credenciais pela [integração oficial](https://acesso.gov.br/roteiro-tecnico/solicitacaocredencialprocesso.html); providencie secret privado, callback HTTPS exato `/accounts/oidc/<provider_id>/login/callback/`, URL de logout previamente autorizada, domínio, escopos aprovados e conectividade TLS/JWK. Produção exige domínio oficial de governo conforme o roteiro (gov.br, mil.br, edu.br, jus.br, leg.br etc.) e elegibilidade/aprovação do órgão responsável; um domínio privado comum não prova esse requisito.

Após provisionar, reinicie os processos e rode `validate_provider_bindings` com credencial runtime. Homologue login/admissão/conexão/logout reais em navegador externo, com a barra de endereço visível no vídeo exigido, incluindo MFA e ausência de afiliação automática. Credenciais gov.br externas **não foram disponibilizadas nesta implementação**: login/logout contra staging e produção permanecem pré-requisitos de verificação, não sucessos simulados. `tests/accounts/test_govbr.py` cobre biblioteca/fluxos com JWTs RS256 locais e transporte HTTP controlado; nem esses testes nem Keycloak substituem homologação gov.br.

## MFA cifrado e obrigatório

MFA (autenticação multifator) exige uma prova adicional além do método de login. São suportados TOTP, códigos temporários gerados por um aplicativo autenticador, e `recovery_codes`, códigos de recuperação para perda desse aplicativo. Email verificado é obrigatório; códigos de recuperação são mostrados uma única vez e o recurso de navegador confiável fica desligado. Em produção, administradores da plataforma e `organization_admin` precisam cadastrar MFA antes de usar superfícies privilegiadas. HTML redireciona para `mfa_index`; a API retorna 403 `mfa_required`. MFA, perfil e logout continuam acessíveis para permitir concluir o cadastro. A claim `amr` do IdP não dispensa essa política. Em desenvolvimento, o padrão False permite a configuração inicial; cadastrar MFA ainda requer chaves de cifragem.

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

## Admin, mensagens e logs

`/admin/` serve somente configurações/parametrizações e releases. Listas/formulários de identidade e outros registros globais ficam em `/platform/records/`, fora do painel. Ambos usam o mesmo `PlatformAdminSite`, superuser ativo e `secure_admin_login` allauth, sem senha paralela Django; MFA, CSRF e CAS continuam exigidos. Não se registram recursos tenant ou edição genérica de SocialToken/Authenticator/SocialApp/auth.Group. Operações de identidade usam lifecycle para não esquecer `auth_version`. Org admin configura apps no painel de parâmetros tenant e usa as páginas empresariais para registros.

Emails são renderizados/materializados no contexto e enviados após commit. SMTP não é transação distribuída; falha de delivery é real, deve ser registrada/apresentada, não alegada como sucesso. Resend explícito allauth permite retomar confirmação. Não existe outbox/fila/retry adicional prometido.

Logs JSON stdout são minimizados: sem senhas, sessão/cookies, tokens, URLs de convite/reset, assertions ou chaves. Testes reais e pré-requisitos de Keycloak/GitHub estão em [testing](testing.md).
