# Plano: versões, pacotes de apps, atualização e login gov.br

Status: contratos e lógica implementados. Este documento conserva o plano aprovado e a evidência histórica do planejamento; uso operacional está em [extensão](extending.md), [deployment](deployment.md) e [autenticação](authentication-sso.md). Resultados realmente exercitados e pré-requisitos externos estão em [testing](testing.md).

## 1. Base observada no planejamento, antes da implementação

- `pyproject.toml` declara `django-skeleton` na versão `0.1.0`; não configura um build de distribuição do núcleo separado do projeto derivado.
- `skeleton/system/app_registry.py` já fornece `AppManifest`, dependências, configuração, menus e inclusão de rotas. Não possui versão nem contrato de pacote.
- `sk_system.AppInstallation` representa ativação/configuração **por organização**, não instalação física. Esse significado será preservado.
- `config/settings/project.py` e `config/urls_project.py` pertencem ao projeto derivado; o núcleo não importa apps de negócio.
- `PlatformAdminSite` limita `/admin/` a plataforma: usuário ativo, staff e superuser, com MFA quando exigido.
- O processo web usa credencial PostgreSQL runtime. Migrations usam credencial separada, ausente do web. `AppConfig.ready()` não faz consulta ou DDL.
- O deployment atual usa imagem construída com `COPY . .`, Gunicorn e collectstatic no build. Não há agente de instalação/atualização.
- O parser de SSO admite OIDC, SAML e GitHub. OIDC/SAML exigem `organization_slug`; signup social depende de convite ou JIT organizacional. Portanto gov.br global não é somente acrescentar uma URL de IdP.

Referências: [arquitetura](architecture.md), [extensão](extending.md), [deployment](deployment.md), [autenticação](authentication-sso.md), [catálogo](table-catalog.md).

## 2. Decisões e limites

1. Versão de código é **global por deployment**. Todas as organizações usam o mesmo release de cada app; habilitação e configuração continuam organizacionais. Não carregar versões diferentes de uma app no mesmo processo Django.
2. Código e migrations de apps são código privilegiado. Manifesto e assinatura controlam procedência e arquivos, mas não transformam Python em sandbox. Só instalar publishers aprovados pela plataforma.
3. O admin solicita uma operação assíncrona; um agente de deployment independente executa cópia, preparação de ambiente, migrations, troca de release e reinício. Não executar `pip`, DDL ou comandos de sistema dentro da request.
4. O web não recebe credencial de migração, escrita nos releases ativos, socket Docker ou shell remoto. O agente tem privilégios delimitados à instalação e fica fora do pacote atualizável do skeleton.
5. Releases são preparados em diretórios imutáveis separados. Nunca sobrescrever arquivos que workers estejam executando. Django exige novo processo para atualizar apps, URLs e modelos.
6. A interface oferece instalar, atualizar e desinstalar código. Remoção física de dados/schema não será ação HTTP/admin: continua manutenção offline revisada.
7. Downgrade não é atualização comum. Não prometer rollback automático de banco com migration destrutiva ou após novas escritas.
8. Não editar arquivos Python do núcleo para registrar cada app: settings, rotas, menus e demais integrações necessárias usam contratos declarativos preexistentes ou novos contratos explicitamente aprovados.

## 3. Versionamento e catálogo de releases

### 3.1 Identidade e versão

- Adotar SemVer para o contrato público: major incompatível; minor aditivo; patch corretivo. Usar parser de biblioteca, não comparação lexical.
- Separar `manifest_version`, `version` do componente, intervalo de compatibilidade do núcleo e estado de migrations. Número de migration não é versão do produto.
- Núcleo: manter uma única fonte de versão no metadata de build; gerar metadata de release e expor consulta via `importlib.metadata` na instalação distribuída. A versão do projeto derivado é independente.
- Para checkout sem distribuição instalada, o comando de desenvolvimento lê a fonte versionada e informa explicitamente origem `source`; não inventa uma instalação confirmada. Build converte prereleases SemVer para PEP 440 quando necessário e verifica equivalência.
- Apps: versão, ID estável, label Django e package Python são obrigatórios no manifesto canônico. O registry recebe a versão desse contrato, sem outra string manual divergente.
- Além da versão, registrar digest do artefato, build/commit quando disponível, publisher e geração do deployment. Duas builds distintas da mesma versão não são intercambiáveis silenciosamente.
- Uma app local entra no mesmo inventário como `project-local`, com manifesto e versão declarados. Atualização administrada exige sua conversão explícita em app empacotada; nunca sobrescrever código local por coincidência de nome.

### 3.2 Estado persistente

Adicionar no app `sk_system`, sem novo app label do núcleo:

| Entidade proposta | Responsabilidade |
|---|---|
| `ComponentInstallation` / `SystemRecord` | ID estável, kind core/app, label/package quando aplicável, versão confirmada, digest, publisher, geração e estado global; referência ao inventário imutável do release e eventual schema retido. |
| `DeploymentOperation` / `SystemRecord` | Solicitação autorizada, tipo, versões de origem/destino, geração esperada, digest do plano aprovado, etapa, resultado e identificadores de backup/release. |

- UUIDs, specs explícitos sem gateway, infraestrutura congelada em migrations, auditoria por triggers, CAS e acesso apenas de plataforma.
- Atores históricos usam os campos existentes; a solicitação recebe ator e request ID do servidor. Segredos e payloads sensíveis não entram no histórico; JSON privado deve ser integralmente redigido.
- Não criar tabela por arquivo inicialmente: inventário completo por release e journal ficam em artefatos imutáveis, endereçados por digest, no armazenamento privado do agente. O banco guarda referências verificáveis e estado de controle.
- Versão exibida como instalada vem do release realmente ativo e confirmado pelos workers; registro no banco é reconciliado com esse release. Divergência é erro operacional visível, não atualização silenciosa.
- O agente conserva journal fora do banco da aplicação, para sobreviver a restauração do DB, reinício e atualização do próprio core. Reconciliação conserva a evidência externa; não fabrica eventos de negócio perdidos em restore.

### 3.3 Descoberta de novas versões

Definir desde já o contrato de um índice de releases JSON assinado: componente, versão, canal, compatibilidade, artefato, digest, assinatura, notas e instruções de migração.

- Upload manual funciona sem serviço externo de catálogo.
- A interface consulta fontes HTTPS previamente aprovadas ou um índice local administrado; URLs não vêm de body/claims nem do próprio pacote como autorização de rede.
- Mostrar `disponível`, `incompatível`, `nenhuma atualização` e `consulta indisponível`, com data da última consulta. Erro de rede não significa estar atualizado.
- O índice pode ser publicado estaticamente; marketplace, agendamento e instalação automática não são requisitos deste plano.
- Rejeitar índice expirado, assinatura inválida e regressão de geração; selecionar apenas canais aprovados. Rotação/revogação de chaves é política de plataforma externa aos pacotes.

## 4. Formato de pacote e manifesto de app

### 4.1 Artefato

Extensão proposta: `.skapp`, um ZIP com layout fechado:

```text
manifest.json
manifest.sig
app/
  __init__.py
  apps.py
  models.py
  migrations/
  templates/<app_label>/
  static/<app_label>/
  docs/spec.md
  docs/adr.md
  demais arquivos declarados da app
wheels/                 # dependências declaradas, se necessárias
```

O payload `app/` será materializado somente em `applications/<package_name>/` no novo release. Dependências são instaladas no virtualenv isolado desse release a partir de wheels declarados; não são espalhadas pelo código do projeto. Artefatos, backups e arquivos coletados de static ficam em raízes de deployment controladas pelo agente, fora do código da app.

### 4.2 Contrato obrigatório do manifesto

| Grupo | Campos/contrato |
|---|---|
| Formato | `manifest_version`, tipo `app` e versão do formato suportada. |
| Identidade | `app_id` estável, `app_label` Django, `package_name`, nome, descrição, versão e publisher. Labels/namespace do núcleo são reservados. |
| Entrypoint | AppConfig dentro do próprio package; módulos de URLs/API e raiz de documentação internos. |
| Compatibilidade | Intervalos de skeleton, Python e Django; apps dependentes por ID e intervalo de versão; conflitos explícitos. |
| Arquivos | Inventário exato de todos os membros do payload e wheels, caminho relativo, função, tamanho e SHA-256. Destino é derivado pelo instalador, nunca um caminho absoluto escolhido pelo pacote. |
| Runtime | Configuração escalar, menus, rotas tenant e política de habilitação; representação serializável do contrato atual de `AppManifest`. |
| Migrations | Labels envolvidos, folhas-alvo esperadas e releases de origem suportados. Executor calcula o grafo real e confere o plano; manifesto não fornece comando shell. |
| Dependências Python | Distribuições/versões fixadas, wheels/digests e marcadores de plataforma. Não aceitar sdist ou script de instalação arbitrário. |
| Lifecycle | Política de retenção de dados, dependências bloqueantes e requisitos de manutenção/backup. |
| Procedência | Referência de chave de publisher aprovada, notas de release e documentação obrigatória. |

Contrato de assinatura: `manifest.sig` assina os bytes exatos de `manifest.json`; o manifesto referencia os hashes de todos os payloads. O hash e a assinatura do pacote inteiro são publicados no índice quando houver. Não usar uma chave pública trazida pelo pacote como prova de confiança.

`manifest.json` é o contrato canônico serializável. Evoluir o `AppManifest` existente para carregar esse contrato e verificar consistência com AppConfig/specs. Migrar todas as apps próprias e o exemplo; não manter manifests paralelos conflitantes. Registry continua metadata pura em `ready()`.

### 4.3 Controle de arquivos e integrações externas

- Proibir caminhos absolutos, `..`, links simbólicos/hardlinks, dispositivos, membros duplicados, colisões normalizadas, nomes reservados e colisões entre apps. Limitar tamanho comprimido/descomprimido, quantidade de membros e taxa de expansão.
- Todo membro precisa estar declarado; nenhuma operação de extração genérica pode escapar da raiz de staging. Conferir hashes antes de executar/importar Python, inclusive build e static processing.
- O agente gera um único inventário de deployment a partir dos manifests aprovados. Settings agregam apps locais e gerenciadas sem consultar o DB durante startup. URLs usam o registry e os prefixes atuais.
- Arquivos gerados compartilhados são propriedade do agente, não de uma app. Ao instalar/remover uma app, regenerar o inventário a partir do conjunto completo, sem desfazer contribuições de outras apps.
- Rotas comuns já cabem no contrato atual; não copiar trechos para `config/urls.py`, `config/urls_project.py` ou settings do projeto.
- Se uma integração indispensável ainda não tiver contrato, implementar no núcleo um tipo declarativo revisado antes de aceitar esse pacote. Não aceitar patches textuais, append em arquivos, paths arbitrários ou hooks shell de lifecycle.
- Toda alteração externa efetivamente produzida, inclusive inventário, dependências de ambiente e static derivado, entra no plano e journal: path/recurso, proprietário lógico, hash anterior/posterior e ação. Recursos gerados são reconstruíveis.
- Uploads, arquivos de usuários e secrets não pertencem ao pacote. Migrações de dados não podem apagar uploads ou manipular arquivos externos sem procedimento específico revisado.

### 4.4 Ferramentas de publicação

Adicionar comandos para validar manifesto, construir pacote, assinar release, inspecionar conteúdo e gerar índice. Build verifica specs/docs, compatibilidade, namespaces, inventário e dependências. Fixtures de exemplo serão publicadas como pacotes reais para exercitar instalação e atualização, não apenas ZIPs mockados.

## 5. Instalação, atualização e desinstalação de apps

### 5.1 Operação end-to-end

1. Upload privado pelo admin de plataforma; validação estrutural e criptográfica sem importar o código recebido.
2. Agente calcula plano: arquivos adicionados/alterados/removidos, integrações, dependências, migrations, organizações afetadas, indisponibilidade e política de recuperação.
3. Admin confirma **aquele plano**. Solicitação usa revisão, geração atual e digest; alteração entre preview e execução invalida a confirmação.
4. Agente adquire lock exclusivo do deployment, revalida origem/destino, capacidade/disco e dependências. Não executar operações concorrentes de core/apps.
5. Preparar release com projeto derivado preservado, core selecionado, todas as apps, wheels fixados e inventário gerado. Rodar checks estáticos em subprocesso isolado após aprovar publisher.
6. Entrar em manutenção: bloquear novas escritas/tráfego de aplicação e drenar web/jobs. O canal de controle do agente permanece disponível para o progresso, mesmo durante reinício do admin.
7. Criar backup consistente do DB e referências protegidas dos arquivos mutáveis/chaves necessários. Registrar artefatos e política de recuperação antes da primeira migration.
8. Executar o plano completo de migrations no ambiente novo com credencial de migração; mudanças transitivas de outras apps exigem constar do preview aprovado. Sincronizar catálogo e reconciliar permissões pelo serviço existente.
9. Usar a credencial **runtime** para `check_skeleton_schema`, bindings de SSO e demais checks de deployment. Executar collectstatic e smokes no novo release.
10. Trocar release/imagem, iniciar workers, confirmar versão/geração e smoke HTTP de aplicação e login. Somente então confirmar instalação e liberar tráfego.
11. Reconciliar estado/journal da operação, registrar sucesso/falha e disponibilizar histórico redigido no admin.

Sem import dinâmico no processo web antigo, DDL em `ready()`, migrations em cada worker ou retries silenciosos. Reexecutar a mesma solicitação não repete uma operação concluída; estado interrompido exige reconciliação pelas etapas verificadas.

### 5.2 Atualização de apps

- Preservar `app_id`, `app_label`, package e IDs dos registros. Mudança de identidade é migração revisada, não renomeação casual.
- Calcular compatibilidade de **todas** as apps, inclusive dependentes locais. Atualização conjunta pode ser necessária; apresentar um plano único e aprovado.
- Comparar inventários do release anterior e novo. Arquivos antigos declarados deixam o release ativo; nunca copiar por cima acumulando código obsoleto.
- Detectar alteração local por hashes antes da preparação; não sobrescrever nem apagar arquivo não possuído pelo instalador. Exibir conflito e exigir release revisado ou migração explícita para app local.
- Migrations já aplicadas são imutáveis; rejeitar pacote que altere seu conteúdo. Conservar o histórico necessário para bases novas e upgrades. Evitar remoção/squash até existir política explícita de versões suportadas.
- Preservar ativação/configuração por organização e ACLs. Evolução de configuração exige transformação versionada explícita e validada; alteração do default não substitui configuração persistida nem deve ativar involuntariamente tenants existentes.
- Ensaiar migrações no clone de uma instalação anterior e comprovar preservação de sysids, tombstones, relações e eventos de auditoria.

### 5.3 Desinstalação

Distinguir quatro ações: desativar por organização; desinstalar código globalmente; reinstalar; remover dados offline.

- O preview de desinstalação mostra impacto em **todas** as organizações, dependentes e referências de dados. Dependentes instalados bloqueiam retirada isolada; remoção conjunta precisa de plano explícito.
- Na retirada, bloquear superfícies da app, remover menus/rotas/registro ativo e código possuído do novo release, e recalcular dependências Python/static e integrações compartilhadas.
- Não aplicar automaticamente `migrate <app> zero`: pode perder dados e reverter migrations de apps dependentes.
- Preservar tabelas, histórico de migrations, `AppInstallation`, ACLs, audit e artefato assinado do último release. A ausência de código não autoriza exportar ou reativar dados.
- Implementar estado `schema_retained`: inventário histórico congelado, proveniente do release confiável, permite ao catálogo/check validar tabelas/triggers/FKs/RLS sem importar a app removida. Não isentar tabelas por prefixo ou por JSON editável no banco; nenhum modelo retido entra no gateway.
- Preservar um arquivo histórico de migrations, assinado e inventariado, separado do código ativo. Somente o ambiente migrator carrega AppConfigs históricos sem modelos, rotas, menus ou hooks de negócio, mantendo os labels originais e usando o executor oficial Django. O preflight deve provar que o grafo completo carrega e que não haverá reaplicação indevida; não basta guardar nomes no banco. Imports históricos que exijam código de negócio retirado bloqueiam a desinstalação até procedimento revisado; não editar migrations já aplicadas nem criar placeholders para simular nós existentes.
- Reinstalação valida identidade, digest de migrations aplicadas e schema retido antes de retomar upgrades. Preserva UUIDs; reativação deve ser explícita, nunca decorrência de um default novo.
- Dados/schema só podem ser removidos por ferramenta offline separada, com backup, análise de referências, guards e retenção; a interface não oferece purge. Manter evidência de arquivo/integração removidos e schema preservado.

### 5.4 Falhas e recuperação

- Antes de migrations: descartar staging e manter release ativo intacto.
- Após mudanças no DB, rollback de arquivos sozinho só é permitido quando compatibilidade com o schema resultante foi comprovada no plano.
- Em falha sob manutenção e antes de novas escritas: restaurar backup consistente e release anterior, ou reparar e concluir sob aprovação do operador. Não presumir reversibilidade de `RunPython`, DDL não atômico ou efeitos externos.
- Após liberar tráfego: restauração de backup perderia novas escritas; exigir plano explícito de recuperação/reconciliação, sem rollback automático enganoso.
- Crash, perda do agente ou restore deixam journal persistente. Ao voltar, detectar etapa e manter manutenção até provar release/schema coerentes; não anunciar sucesso por um exit code isolado.

## 6. Atualização do skeleton em projetos derivados

### 6.1 Separar distribuição de base e projeto

- Publicar o core como wheel com namespace `skeleton`, migrations, templates/static, inventário técnico e defaults reutilizáveis. Declarar build backend e conteúdo explicitamente no `pyproject.toml`.
- Mover defaults compartilhados de settings e URLs para módulos do pacote do núcleo. O `config` do derivado fica como entrypoint/overlay e mantém `PROJECT_APPS`, `urls_project`, branding, WSGI/ASGI e ajustes do deployment.
- Não incluir `applications`, `examples`, `.env`, bancos, `.private`, `config` do derivado ou `manage.py` na atualização do core. O exemplo é artefato opt-in separado.
- Manter app labels, AUTH_USER_MODEL, migration names e caminhos importados por migrations históricas. Não tratar essas APIs persistidas como código obsoleto removível num patch.
- Publicar faixa de versões suportadas e contratos usados pelas apps: bases, specs, policies, contexto, migrations e manifests. Major pode exigir transformações documentadas; não resolver por aliases de compatibilidade indefinidos.
- Build do projeto combina core, apps e dependências em lock final reproduzível com hashes. Resolver conflitos antes de manutenção; nunca atualizar dependências soltas no virtualenv ativo.

### 6.2 Cutover dos projetos já derivados

1. Inventariar a cópia atual do núcleo e compará-la com um release base identificado. Sem base conhecida ou com divergências, classificar como fork/local modificado; não prometer upgrade automático.
2. Extrair customizações de negócio para `applications` e overlays de `config`; conflitos no núcleo recebem migração revisada, não overwrite automático.
3. Fixar a primeira distribuição equivalente e gerar lock/inventário. Rodar migrations/checks em clone com dados existentes, incluindo apps próprias e autenticação.
4. Migrar o template deste repositório para consumir a mesma distribuição/estrutura; documentar a adoção pelos derivados. Nenhum projeto externo pode ser migrado sem seu inventário real.
5. Validar app labels, tabelas, UUIDs e caminhos históricos antes de cortar; sem recriar banco ou resetar estado de migrations.

O release do core inclui manifesto de compatibilidade, wheel assinado, digest, notas e migrations. O mesmo executor de deployment e a mesma interface administram sua atualização; o plano inclui apps incompatíveis e, quando necessário, releases conjuntos.

### 6.3 Deployment suportado

- Instalação em diretório/virtualenv: agente prepara `releases/<generation>` e virtualenv próprio, troca ponteiro ativo de forma atômica e reinicia somente serviços previamente configurados pelo operador.
- Compose atual: agente constrói uma imagem nova com core/apps selecionados e collectstatic, executa job migrator separado e substitui o serviço web. Não extrair pacote na camada de um container em execução.
- O agente é o único componente autorizado a acessar ferramentas de deploy; nomes de serviços, raízes, comandos fixos e secrets vêm do provisionamento, nunca do manifesto.
- Volumes de dados/segredos permanecem fora dos releases. Ambos os caminhos precisam de provas próprias; sucesso em diretório não valida imagem/Compose.

## 7. Interface gráfica de plataforma

Integrar ao `PlatformAdminSite`, sem um segundo admin ou login paralelo:

| Superfície proposta | Comportamento |
|---|---|
| `/admin/system/releases/` | Versão core, build/digest, geração ativa, origem, versão do projeto e apps instaladas/locais/schema retido. |
| Verificar versões | Consulta fonte aprovada, data, versão candidata, compatibilidade e notas; indisponibilidade explícita. |
| Enviar pacote | Upload privado limitado, validação, procedência e apresentação do conteúdo/impacto. |
| Preview e confirmação | Diff de arquivos e integrações, migrations transitivas, dependentes, manutenção, backup e recuperação; aprovação vinculada ao digest/geração. |
| Operação | Etapa atual, bloqueio/falha, progresso redigido e resultado; retoma visualização após reinício. |
| Histórico | Quem solicitou, versões/digests, alterações de arquivos/integrações, migrations e resultado. |
| Desinstalar | Preview global de dependências, código removido e dados/schema preservados; sem botão de purge. |

- Reutilizar sessão, CSRF, MFA e reautenticação para operações privilegiadas. Toda autorização é revalidada no servidor; esconder botão não é controle de acesso.
- Admin de organização continua em `/o/<org>/apps/`, sem poder instalar código, escolher publisher ou atualizar o core. Pode ver versão da app habilitada, sem logs/metadata privados de outras organizações.
- A confirmação é POST e requer revisão. Uma solicitação concluída não é repetida por refresh; GET não executa instalação ou logout.
- Durante manutenção, o canal autenticado de status do agente é read-only para a UI. Provisionar autenticação própria de curta duração, escopo por operação, sem reutilizar cookies/session secrets do Django nem permitir DDL por esse canal.
- Depois do reinício, o admin volta a consultar a operação via sessão normal. O agente independente oferece CLI de recuperação ao operador se o core não puder iniciar.

## 8. Login gov.br

### 8.1 Protocolo e integração

- Implementar um perfil gov.br explícito usando os pontos de extensão allauth/OIDC existentes; não escrever OAuth/JWT/JWK manualmente.
- Primeiro confirmar discovery/endpoints disponíveis no ambiente credenciado e as APIs da versão allauth fixada. Quando discovery não satisfizer o contrato, configurar endpoints explícitos por extensão oficial da biblioteca, sem endpoint informado pelo usuário.
- Authorization Code, PKCE S256, state de uso único, nonce vinculado ao ID token, client_secret_basic e validação de assinatura/issuer/audience/tempo por biblioteca. Conservar a exigência atual de verificar assinatura mesmo no back-channel.
- Escopos de login pertencem ao perfil revisado. Usar os necessários e aprovados no credenciamento; o roteiro oficial também descreve `govbr_confiabilidades` e `govbr_confiabilidades_idtoken`. Não permitir expansão de scope por query/body. Não adicionar regras de negócio de nível bronze/prata/ouro ao núcleo.
- Botão `Entrar com GOV.BR` no padrão oficial, iniciando fluxo por POST com CSRF e redirecionamento para navegador externo. Callback redireciona para uma URL limpa; sessão local Django, nunca token gov.br como sessão da API.
- Identidade estável por issuer + subject, separada entre homologação e produção; fingerprint inclui ambiente/issuer/client ID. Provider IDs novos quando o domínio de identidade mudar.

### 8.2 Admissão e dados pessoais

- Separar provedor de identidade **global** de SSO corporativo com organização fixa. Gov.br não escolhe tenant nem concede Membership, papel ou flags de plataforma.
- Default continua cadastro fechado: primeiro ingresso por convite autorizado; conta já existente conecta gov.br após login e reautenticação. Cadastro público, se habilitado pela política atual, cria somente User/Profile, sem organização.
- Não auto-conectar por email ou CPF e não reativar vínculo suspenso. Email coincidente exige conexão explícita; email ausente requer coleta e confirmação local no fluxo de admissão autorizado, sem email sintético.
- Tratar `email_verified` estritamente pelo perfil documentado. O roteiro mostra representações booleanas e string; validar equivalências explícitas, nunca usar `bool("false")`. Sem prova atestada, confirmação local.
- O roteiro identifica `sub` como CPF: classificar o UID do SocialAccount como dado pessoal, acesso restrito, sem exposição em admin/log/audit de payload. Não duplicar CPF em User/Profile. Decidir proteção/retention na revisão LGPD do deployment; allauth mantém a identidade técnica necessária.
- Claims completos, tokens, telefone, imagem e confiabilidades não serão persistidos pelo núcleo. Preservar minimização antes dos writes/serialização da sessão; qualquer necessidade futura é escopo de negócio com base legal e spec sensível própria.
- MFA da plataforma continua obrigatório conforme a política existente; `amr`/nível gov.br não concede bypass.

### 8.3 Logout e credenciamento

- Logout local por POST/CSRF encerra a sessão Django e redireciona o navegador ao endpoint gov.br, com `post_logout_redirect_uri` previamente autorizado. Não construir redirect a partir de input do usuário.
- Secrets por referências de ambiente/secret store, JSON privado fora do Git; mudanças exigem reinício e `validate_provider_bindings` com credencial runtime.
- Obter credenciais de homologação e produção, registrar callback/logout HTTPS e realizar homologação real com vídeo do fluxo exigido. Não substituir essa aprovação por testes contra Keycloak.
- O roteiro oficial exige domínio oficial de governo para disponibilizar credenciais de produção. Um projeto privado sem elegibilidade pode implementar o perfil, mas não será considerado integrado em produção sem autorização do órgão responsável.

Fontes oficiais consultadas:

- [Passo a passo, parâmetros, dados pessoais, logout e homologação](https://acesso.gov.br/roteiro-tecnico/iniciarintegracao.html).
- [Solicitação de credenciais e requisito de domínio oficial](https://acesso.gov.br/roteiro-tecnico/solicitacaocredencialprocesso.html).

## 9. Sequência de implementação e aceites

| Etapa | Entrega | Aceite obrigatório |
|---|---|---|
| 1. Contratos | Fonte de versão, schemas de manifesto/índice, identidade estável, compatibilidade, modelo global e ADRs | Versões iguais no artefato, registry e inventário; prerelease/faixas corretas; tenant activation não vira instalação global. |
| 2. Distribuição do core | Wheel, defaults/URLs reutilizáveis, template thin, locks e runbook de adoção | Instalação limpa e upgrade de clone derivado preservam negócio, config, app labels, migrations e IDs. Fork desconhecido bloqueia overwrite. |
| 3. Pacotes | Build/assinatura/inspeção, inventário e integração declarativa | Pacote real verificável; traversal, links, colisões, bombs, extras não declarados e publisher inválido falham sem cópia/importação. |
| 4. Executor | Agente independente, staging, locks, manutenção, backup, migrator, checks runtime e troca de release | Instalar/atualizar pelo fluxo real; nenhum segredo de migração no web; uma operação concorrente vencedora; falhas/crash não liberam schema incoerente. |
| 5. Lifecycle completo | Diff, atualização conjunta, retenção de schema/migrations, remoção e reinstalação | App com dados é atualizada, removida e reinstalada sem perda/novo UUID; app dependente bloqueia retirada; arquivos órfãos não permanecem ativos e arquivos locais não são apagados. |
| 6. Admin e catálogo | UI, uploads, preview, aprovação, progresso/histórico e consulta de versões | Login real/MFA/CSRF; org admin negado; preview stale recusado; instalação de core/app completa pela interface; UI volta após reinício. |
| 7. Gov.br | Perfil allauth, admissão global, botão, callback, logout, minimização e documentação | Login/logout reais em homologação, conexão explícita, nenhuma afiliação automática; negativos de protocolo e contas; produção depende de credenciamento real. |

Gov.br pode avançar independentemente das etapas de empacotamento depois da definição do contrato de identidade. As etapas 4–6 precisam ser integradas para entregar instalação realmente operável pelo admin, não apenas um formulário de upload.

### 9.1 Verificação comportamental

- Pacotes: instalar uma app real com tabela SecuredRecord, rotas, static, configuração e migrations; produzir dados em A/B, atualizar, desinstalar e reinstalar.
- Core: projeto derivado com app própria e settings/rotas customizados; atualizar base sem sobrescrever esses arquivos e comprovar login, autorização, CAS, audit, UUIDs e schema.
- Infraestrutura: SQLite isolado e PostgreSQL com migrations owner/checks/assertions runtime; novos modelos globais recebem proteção/auditoria e testes de acesso de plataforma.
- Operação: falha de hash, wheel incompatível, falta de espaço, backup inválido, migration falhando/irreversível, perda de agente, reinício durante cutover e confirmação concorrente. Não usar mocks para provar backup, processo de deploy ou restauração.
- Schema retido: schema check positivo com app removida e negativos de trigger/RLS/FK adulterados; próximas migrations de outras apps continuam executáveis sem importação do package desinstalado.
- UI: Chromium com login real, pacote enviado, preview/confirm, progresso, versão final, rotas/static reais após reinício e autorização negada. Verificar ambos deployments diretório e Compose.
- Gov.br: homologação real; state/nonce/PKCE, assinatura, issuer/audience/expiry, replay, email ausente/não verificado, email coincidente, conta inativa/tombstone, logout e ausência de tokens/CPF em logs ou sessão serializada indevidamente. Fixture OIDC ajuda regressões, mas não substitui gov.br real.
- Cada incremento atualiza documentação afetada e registra somente comandos/resultados executados. Não adicionar testes de wiring/source text para simular aceites de operação.

### 9.2 Documentação afetada

Atualizar `README.md`, `architecture.md`, `extending.md`, `data-model.md`, `table-catalog.md`, `authentication-sso.md`, `deployment.md`, `testing.md` e `api.md` quando suas superfícies mudarem. Acrescentar contrato do pacote, publicação de releases e recuperação operacional; manter specs/ADRs de cada app. Revisar formalmente a regra atual de instalação exclusivamente offline: admin será **controle da operação**, não executor privilegiado HTTP.

## 10. Pré-requisitos e riscos abertos

- Provisionar agente, raízes de releases, armazenamento privado, canal de controle, migrator, supervisor/Compose e backup restaurável. Não existe essa infraestrutura no deployment atual.
- Definir publishers e chaves confiáveis, política de revogação e origem do índice. Sem isso, upload não pode executar código em produção.
- Inventariar os projetos derivados reais e suas alterações no núcleo. Este repositório não contém dados que permitam certificar a migração de cada derivado.
- Obter credenciamento gov.br, client IDs/secrets, domínio elegível e callbacks/logout autorizados. Nenhum fluxo real gov.br foi exercitado neste planejamento.
- A desinstalação com retenção exige resolver tanto schema checks quanto o grafo histórico de migrations: apenas apagar a pasta não atende ao contrato. Dependências/imports históricos não solucionáveis bloqueiam desinstalação, com diagnóstico explícito.
- O agente controla execução privilegiada, e um publisher aprovado pode distribuir código malicioso. Revisão de procedência e do release é fronteira de confiança, não propriedade garantida pelo formato ZIP.
- Backup não equivale a rollback sem perda; manutenção e corte de escritas são necessários. Reinício e indisponibilidade serão apresentados no preview.

## 11. Evidência deste planejamento

Executado sem modificar código de runtime:

```sh
DJANGO_SETTINGS_MODULE=config.settings.test SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python manage.py check
```

Resultado observado: `System check identified no issues (0 silenced).`

Probe descartável com `django.setup()`, registry, resolução de URL e parser real de providers confirmou: versão declarada `0.1.0`; `PROJECT_APPS=[]`; manifest `example_notes` sem campo de versão; namespace da rota `example_notes`; OIDC sem organização rejeitado com `ImproperlyConfigured: Corporate SSO requires organization_slug.` O probe não abriu banco nem fez login.

As descrições de testes anteriores em `testing.md` eram evidência histórica, não execuções do planejamento. Naquela etapa não existiam instalação de pacote, atualização, recuperação, UI nova, build Compose ou login gov.br; não foram alegados como verificados. As evidências posteriores da implementação são registradas separadamente em `testing.md`.

Probe adicional com `importlib.metadata.version("django-skeleton")` confirmou `PackageNotFoundError`: não há distribuição `django-skeleton` instalada no virtualenv consultado. A versão `0.1.0` observada é a declaração do checkout, não prova de um release distribuído instalado.
