# Autenticação, convites, SSO e MFA

## Identidade e admissão

Login é allauth por email, via único backend `SkeletonAuthenticationBackend`. Sessão vive no banco. User.email normalizado é único inclusive tombstones. Deleted/inactive não autentica nem é reprovisionado por SSO. `auth_version` na sessão é comparado a cada request: mudança de senha/MFA, desativação e revogação global invalidam sessões antigas. Permissões Django globais são vazias para usuários comuns; só superuser ativo/não deletado acessa admin global. RBAC tenant não usa auth.Group.

Troca efetiva de email primário verificado incrementa auth_version mesmo que o allauth já tenha sincronizado User.email antes do signal. A sessão que realiza a troca conserva a nova versão; todas as demais são revogadas. User restore incrementa a versão no mesmo CAS que remove o tombstone e conserva is_active=False. UserManager.create_user/create_superuser e o comando createsuperuser criam UserProfile na mesma transação.

O formulário de User no admin inclui revisão original escondida. Alterações de nomes/flags e revogação são persistidas numa instância bloqueada coerente, sem forjar revision; formulário stale e tentativa de remover último admin retornam 409. Language da API exige string e opção configurada: lista/objeto é validação 400 sem escrita.


Cadastro é fechado por padrão. Público opt-in exige confirmação de email e cria somente User/Profile; não organiza tenant nem Membership. Email verification é mandatory para local/social. Logout, confirmação de email e início social não são ações por GET. Reset não faz login automático; operações sensíveis exigem reautenticação.

### Convite

Org admin emite convite: token urlsafe de 32 bytes, digest SHA256 persistido, validade sete dias e email/role/org fixos. Mensagem contém `/invite/<org_uuid>/<token>/`; plaintext só na mensagem, nunca audit/log ou callback SSO. GET valida e guarda referência server-side sem consumir. POST CSRF revalida token/expiry/email/tenant e consome atomicamente com CAS accepted_at IS NULL. Só um vencedor em corrida/replay.

Conta nova usa signup allauth com email fixado. Posse validada do convite pode ser prova desse email; conhecer token não autentica conta existente. Conta existente exige login/reautenticação e email verificado igual. Reconvidar reativa Membership existente. Invalid/expired/consumed usa página neutra/API `invalid_invitation`, sem expor existência de conta.

A sessão armazena apenas UUIDs de organização/convite e o digest, nunca o token plaintext. Na criação, a prova de email validada continua disponível somente no request corrente depois do consumo, permitindo a configuração de EmailAddress pelo allauth; ela não pode autenticar uma conta existente nem ser reutilizada em outro request.

### Senhas, emails e conexão

SSO cria senha inutilizável e `local_password_allowed=False`; local/convite usa True. A política é da identidade global, inclusive se pertence a várias orgs. Reset mantém resposta neutra mas não entrega mensagem a SSO-only/inativo/deletado. Set/change/reset completion revalidam a política mesmo com token antigo. Não há endpoint paralelo de senha na API.

`ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS=False` impede a mensagem genérica que o allauth enviaria depois de filtrar uma identidade sem direito a senha local. A resposta pública continua neutra; não enviar email a conta desconhecida, inativa ou SSO-only não autoriza expor sua existência.

Email social coincidente **não** conecta automaticamente: SOCIALACCOUNT_EMAIL_AUTHENTICATION/AUTO_CONNECT=False e can_authenticate_by_email=False. Faça login local + conexão explícita após reauth. User.email/EmailAddress só sincronizam após confirmação e não reutilizam tombstones. `SOCIALACCOUNT_STORE_TOKENS=False` sozinho não protege signup pendente: após validação protocolar, o adapter remove tokens e conserva somente process/next/headless necessários na sessão. Extra_data retém apenas binding e, durante admissão inicial não confirmada, o email pendente. Tokens/claims completos não são serializados nesse fluxo.

## JSON privado de provedores

`SSO_PROVIDER_CONFIG_PATH` aponta para arquivo fora do Git, com único objeto `providers`. IDs são únicos/URL-safe. Protocolos: oidc/saml/github/govbr; OIDC/SAML corporativos exigem organization_slug, enquanto govbr é identidade global e rejeita configuração de tenant/JIT/papel. Parsing de settings valida tipos/URLs/secrets sem DB. Services/check pós-migração resolvem org/role ativos e binding estável.

Settings materializam `SKELETON_PROVIDER_CONFIGURATION` uma vez no startup; bindings e configuração allauth usam esse mesmo snapshot. Mudanças no arquivo/env exigem reinício controlado. Não há releitura por request. `verified_email`, scopes configurados e auth_params arbitrários são rejeitados; email verificado exige evidência do protocolo e trust explícito. Query scope/auth_params não amplia consentimento nem altera client_id/redirect_uri/state/nonce. OIDC usa openid/profile/email e preserva prompt de reautenticação; govbr usa openid/email/profile; GitHub somente user:email. Todos os fluxos OIDC exigem ID token assinado e nonce aleatório vinculado ao estado allauth de uso único e ao provider ID; back-channel TLS não dispensa assinatura.

As classes de extensão de segurança OIDC/SAML/GitHub são registradas mesmo quando não há credenciais nem apps configuradas. O registry allauth conserva essas classes desde o primeiro carregamento; adicionar um perfil não pode reutilizar inadvertidamente a classe upstream sem minimização/nonce. Isso não habilita login sem um binding autorizado. O adapter seleciona endpoints gov.br pelo perfil global do snapshot de configuração, não por descoberta ou settings editáveis de SocialApp.

Depois de migrate/catalog/schema check, rode com os settings **runtime** que carregam os providers:

```sh
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py validate_provider_bindings --database default
```

O preflight valida org/role vivos e todos os fingerprints históricos sem login nem alteração de subjects. Erro bloqueia deployment. O fingerprint v2 inclui protocolo, issuer/entity, client_id, tenant e entidade SP configurada: também a atualização do contrato exige revisar bindings existentes. Não há atualização automática de fingerprints. Mudança de domínio de identidade exige provider_id novo e migração administrativa revisada.


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

A admissão pendente é estado de servidor conservado entre novos callbacks antes da confirmação; não vem de claims extras nem se perde nos lookup writes allauth. A decisão inicial de Membership é serializada pelo lock da organização, o mesmo usado pelo offboarding. Completar essa admissão não é autorização para reativar vínculo existente.


## Protocolos e callbacks

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

Tipos: TOTP e recovery_codes; email verificado obrigatório; recovery codes mostram uma vez; trusted browser desligado. Produção exige MFA para plataforma e organization_admin antes de superfícies privilegiadas; HTML vai ao `mfa_index`, API retorna 403 `mfa_required`. MFA/profile/logout seguem acessíveis para concluir enrollment. Claim IdP `amr` não pula essa política. Dev default False permite bootstrap; enrollment requer chaves.

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

`/admin/` só superuser ativo e login protegido por secure_admin_login allauth, sem senha paralela Django. Não registra recursos tenant ou edição genérica de SocialToken/Authenticator/SocialApp/auth.Group. Operações de identidade usam lifecycle para não esquecer auth_version. Org admin usa páginas empresariais.

Emails são renderizados/materializados no contexto e enviados após commit. SMTP não é transação distribuída; falha de delivery é real, deve ser registrada/apresentada, não alegada como sucesso. Resend explícito allauth permite retomar confirmação. Não existe outbox/fila/retry adicional prometido.

Logs JSON stdout são minimizados: sem senhas, sessão/cookies, tokens, URLs de convite/reset, assertions ou chaves. Testes reais e pré-requisitos de Keycloak/GitHub estão em [testing](testing.md).
