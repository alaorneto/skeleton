# Endereços de organização

Este guia explica como funcionam os endereços próprios de uma organização — seja por slug (`acme.skeleton.com`) ou por domínio próprio (`portal.cliente.com.br`). O documento atende a três públicos: quem **opera** a instalação, quem **administra** uma organização e quem **desenvolve** apps e projetos derivados. O planejamento está no epic [#477](https://github.com/alaorneto/skeleton/issues/477); as regras normativas estão no [`AGENTS.md`](../AGENTS.md) e em [controle de acesso](access-control.md#host-de-organização).

## O que é

Sem esse recurso, todas as organizações são acessadas pelo domínio principal em `/o/<uuid>/…`, e a interface evidencia a existência de uma plataforma com várias organizações.

Quando o recurso está habilitado na instância e ativado pelo administrador da organização, `https://acme.skeleton.com/` apresenta o sistema **como se apenas a organização "acme" existisse**:

- URLs limpas, como `/tables/notes/` em vez de `/o/<uuid>/tables/notes/`;
- ausência de seletor de organização, de "Todas as organizações" e do link "Plataforma";
- notificações, tarefas e contadores restritos àquela organização;
- nome, cores, logo, ícone e título da aba próprios da organização, inclusive na tela de login.

No domínio principal, quase nada muda, exceto dois detalhes: o rótulo `route` das métricas das páginas organizacionais passa de `o/<uuid:organization_id>/…` para `<org:organization_id>…`, e o cookie de sessão passa a chamar-se `skeleton_session` quando o domínio base está configurado.

## Vocabulário

| Termo | Significado |
|---|---|
| domínio base | Valor de `SKELETON_ORGANIZATION_BASE_DOMAIN` (ex.: `skeleton.com`). Quando vazio, o recurso fica desligado na instância. |
| host da plataforma | Host de `SKELETON_PUBLIC_URL` e todo host listado explicitamente em `DJANGO_ALLOWED_HOSTS`. Mantém o comportamento padrão. |
| host de organização | `<slug>.<domínio base>` (um único rótulo) de uma organização com endereço ativo, ou um domínio próprio comprovado. |
| site | A organização acessada pelo seu host. `OrganizationSite` é o índice global, lido antes do login, que guarda a marca pública. |
| escopo de site | Variável de contexto contendo o `sysid` da organização do host, ativada pelo middleware durante a requisição (`skeleton.tenancy.routing.site_scope`). |
| URL canônica / limpa | `/o/<uuid>/tables/x/` (canônica) e `/tables/x/` (limpa, usada apenas no host da organização). |

## Para quem opera

### Variáveis

| Variável | Padrão | Efeito |
|---|---|---|
| `SKELETON_ORGANIZATION_BASE_DOMAIN` | vazio | Habilita endereços `<slug>.<domínio base>`. Deve conter apenas o domínio, sem esquema, caminho, porta ou curinga (`skeleton.E038`). |
| `SKELETON_PUBLIC_URL` | vazio | Obrigatória junto com o domínio base: fornece o esquema e a porta dos endereços (`https://app.skeleton.com` gera `https://acme.skeleton.com`). |
| `SKELETON_ORGANIZATION_CUSTOM_DOMAINS` | `false` | Habilita domínios próprios comprovados por DNS. Requer o domínio base configurado. |
| `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES` | `[]` | Setting do projeto derivado (fora do catálogo da instância): prefixos de rotas globais próprias liberadas no host da organização. |

As três primeiras variáveis compõem o catálogo da instância (`manage.py instance set organization_base_domain skeleton.com`).

### DNS, TLS e proxy

- **DNS:** crie um registro curinga `*.<domínio base>` apontando para a instalação, além do registro do domínio principal.
- **TLS:** providencie um certificado curinga para `*.<domínio base>`. Para domínios próprios, o proxy emite um certificado por domínio; em emissões sob demanda, ele consulta `GET http://<DJANGO_HEALTH_HOST>/health/site-host/?domain=<host>`, em que 200 autoriza a emissão e 404 a recusa. Veja [deployment](deployment.md#domínios-próprios-489).
- **Proxy:** repasse o cabeçalho `Host` original sem reescrever o caminho. Atrás de proxy, `DJANGO_TRUST_PROXY` continua definindo o esquema; a aplicação nunca lê `X-Forwarded-Host` diretamente.

### Domínio base dedicado

Como o cookie de sessão é emitido com `Domain=.<domínio base>`, todo host abaixo dele recebe esse cookie. Por isso, o domínio base precisa ser **dedicado** a esta aplicação: não delegue subdomínios a terceiros nem hospede outra aplicação sob ele. Mantenha `SKELETON_PUBLIC_URL` dentro desse domínio (por exemplo, `https://app.skeleton.com` com `skeleton.com`); fora dele, a sessão não será compartilhada e o check `skeleton.W039` emitirá aviso.

### O que muda ao configurar o domínio base

- `.<domínio base>` é adicionado automaticamente ao final de `ALLOWED_HOSTS`, inclusive em produção.
- O cookie de sessão passa a se chamar `skeleton_session`, exigindo que **todos façam login novamente uma vez**.
- O rótulo `route` das métricas das rotas web organizacionais é `<org:organization_id>…` (com ou sem domínio base). Requisições recusadas em um host de organização recebem o rótulo fixo `organization-site-refused`, nunca expondo host, slug ou `sysid`.

### Checks

| Check | Quando dispara |
|---|---|
| `skeleton.E038` | Domínio base com formato inválido ou sem `SKELETON_PUBLIC_URL` definida. |
| `skeleton.E039` | Domínio base configurado junto com `SESSION_COOKIE_DOMAIN` ou `CSRF_COOKIE_DOMAIN`: o núcleo precisa decidir o domínio do cookie por requisição. |
| `skeleton.W039` | Host de `SKELETON_PUBLIC_URL` fora do domínio base: sessão não compartilhada e sem SSO nos endereços de organização. |

Veja mais detalhes em [checks](checks.md).

### Nomes reservados e organizações antigas

Toda organização nova recebe um slug válido como rótulo DNS (1 a 63 caracteres `a-z`, `0-9` e `-`, sem hífen nas pontas e sem `xn--`) que não conste na lista de reservados em `skeleton/common/hostnames.py` (`www`, `api`, `app`, `admin`, `platform`, `login`, `static`, `mail`, `o`, `site`, entre outros). Organizações antigas cujo slug não atenda à regra continuam funcionando normalmente em `/o/<uuid>/`, mas só ganham endereço por slug depois de trocar o identificador para um valor compatível ([trocar o identificador](#trocar-o-identificador)).

## Para quem administra a organização

### Ligar e desligar

Em **Configurações** (`/o/<org>/settings/`), a seção "Endereço próprio" mostra o endereço calculado (`https://acme.skeleton.com`) e a opção "Ativar endereço próprio". Ao ativá-la, **nome de exibição, cor, logo e ícone tornam-se visíveis a qualquer pessoa que acesse o endereço, mesmo sem login** ([privacidade](privacy.md#endereço-próprio-da-organização)). Quando desligado, o endereço responde 404 como um endereço inexistente.

### Trocar o identificador

O identificador (slug) compõe o endereço `<slug>.<domínio base>` e é único entre todas as organizações, inclusive as excluídas ([#765](https://github.com/alaorneto/skeleton/issues/765)). O administrador da organização altera esse valor em **Configurações** (`/o/<org>/settings/`, seção "Identificador"), e o administrador da plataforma no detalhe da organização (`/platform/organizations/<id>/`).

Enquanto o usuário digita, a página indica se o identificador está disponível, em uso, reservado ou inválido. Esse retorno é apenas informativo: ao salvar, o servidor revalida tudo dentro da transação com a organização travada, verificação de revisão (CAS) e a restrição `unique` do banco — entre duas trocas simultâneas para o mesmo slug, apenas uma vence e a outra recebe "já está em uso". A alteração é auditada como `update` da organização.

Após a troca, **o endereço antigo para de responder imediatamente** (sem redirecionamento) e o slug anterior fica liberado para outra organização; links e favoritos antigos deixam de funcionar. Domínios próprios e `/o/<uuid>/` permanecem inalterados. Organizações com provedor de identidade vinculado (no banco ou no arquivo) não podem alterar o identificador, pois o fingerprint do vínculo inclui o slug ([provedores no banco](authentication-sso.md#provedores-no-banco-29)).

### Página pública

Com o endereço próprio ligado, o administrador pode publicar uma **página pública** da organização ([#623](https://github.com/alaorneto/skeleton/issues/623)) em **Configuração › Organização › Página pública** (`/o/<org>/admin/pages/public/`, ou `/admin/pages/public/` no próprio endereço). A página é composta por blocos de [texto simples](ui-design.md#páginas-em-blocos) (`hero`, `text`, `contact`, `links`), sempre escapados, editados sem JavaScript e com pré-visualização.

Visitantes anônimos que abrem `/` no endereço da organização veem essa página com nome, cor e logo publicados e, no bloco `hero`, o botão "Entrar" (que direciona ao login com a marca da organização). Sem página publicada, `/` redireciona ao login; com o endereço desligado, retorna o 404 neutro; e `/o/<uuid>/` no domínio principal nunca é público. Usuários autenticados continuam vendo o início interno. Quando o endereço está desligado, o editor avisa: "A página só aparece no endereço próprio da organização, que está desligado."

- Os dados da página ficam em `OrganizationPage` (slot `public`), sob isolamento tenant. Visitantes anônimos leem exclusivamente o espelho global `OrganizationSitePage`, gravado apenas por `sync_site_page` na mesma transação e sob o `organization_lock` da escrita: `enabled` vale verdadeiro somente quando o endereço está ligado e a página está publicada; desligado, o documento é esvaziado. Publicar, despublicar, ligar e desligar o endereço (`sync_site` chama `sync_site_page`) mantêm o espelho sincronizado.
- `published_site_page(organization_id)` (`skeleton.tenancy.pages`) lê apenas o espelho, sem contexto de tenant nem `system_context`, seguindo o mesmo padrão de `site_of`. `views.organization_home` (rota `web:dashboard`) só a consulta quando o host pertence à própria organização da URL.

### Logo e ícone

- **Logo:** PNG, JPEG ou WebP de até 512 KB.
- **Ícone:** PNG quadrado com exatamente 512 × 512 pixels e até 256 KB, utilizado como favicon e ícone de atalho tanto no endereço próprio quanto nas páginas da organização no domínio principal.

### Domínio próprio, passo a passo

Quando o recurso está habilitado na instância, a seção "Endereço próprio" exibe o link "Domínios próprios" (`/o/<org>/settings/domains/`):

1. Cadastre o domínio (`portal.cliente.com.br`), respeitando o limite de até 3 por organização.
2. Publique o registro `TXT` em `_skeleton-site.<domínio>` com o valor indicado na tela.
3. Aponte o domínio para a instalação via `CNAME` para o host do domínio principal (ou registros `A`/`AAAA` em domínios raiz).
4. Clique em "Verificar agora". Após a confirmação, o domínio passa a atender em até 30 segundos.
5. O certificado TLS é emitido pelo operador da instalação.

Se o registro `TXT` sair do ar, a reverificação diária (`manage.py verify_domains --recheck`) marca o domínio como falho após três falhas consecutivas e ele deixa de responder. Cada domínio pertence a no máximo uma organização, e a plataforma pode revogar posses disputadas em `/platform/domains/`.

### Links já enviados

Com o endereço próprio ligado, **novos** emails (notificações, convites e links de ingresso) passam a apontar para o endereço da organização. Ao desligá-lo, novos emails voltam ao domínio principal, enquanto links antigos apontando para o endereço desligado passam a responder 404.

## Para quem desenvolve

- **Rotas web de organização** devem começar com `ORG` de `skeleton.tenancy.routing` (`path(f"{ORG}relatorios/", ...)`), nunca com o literal `o/<uuid:organization_id>/` (um teste de guarda bloqueia o prefixo fixo). Rotas de apps montadas pelo núcleo já utilizam o conversor. As rotas da API continuam como `api/v1/o/<uuid:organization_id>/…`.
- `reverse` e `{% url %}` geram automaticamente a forma adequada — canônica no domínio principal e limpa no host da organização. Nunca concatene `/o/<uuid>/` manualmente: use `organization_prefix(organization_id)`.
- **Templates:** recebem `site_mode` (verdadeiro no host da organização), `organization_site` (o objeto `Site` do índice — não usa o nome `site` para não colidir com o allauth), `site_name` (sufixo do título: `<tela> · {{ site_name }}`) e `organization_icon_url`. Consulte [interface](ui-design.md#título-e-marca-no-endereço-da-organização).
- **Vínculos:** `memberships_for_user` e `for_each_organization` são estreitados para a organização do host. Informe `all_organizations=True` sempre que precisar decidir ou exportar dados globais da conta (ciclo de vida, segurança e privacidade). O estreitamento afeta apenas a apresentação, nunca a autorização.
- **Rotas globais próprias** no host de organização: adicione o prefixo em `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES`; sem isso, retornam 404 no host da organização.
- **Links em jobs:** use `skeleton.tenancy.site.organization_url(organization_id, path)` (ou `shareable_url` quando houver requisição). Um retorno `None` indica que não há endereço ativo, mantendo-se a URL do domínio principal.
- **Testes de host:** use `client.get(caminho, HTTP_HOST="acme.example.test")` com `SKELETON_ORGANIZATION_BASE_DOMAIN="example.test"`, `SKELETON_PUBLIC_URL="https://app.example.test"` e `ALLOWED_HOSTS=[".example.test", "testserver"]` (há fixtures prontas em `tests/site_support.py`). No cliente de teste, inspecione a view por `response.wsgi_request.resolver_match`, pois `response.resolver_match` resolve o caminho limpo fora do escopo de site.

## Segurança

| Regra | Onde o código garante | Teste que prova |
|---|---|---|
| 1. O host nunca concede acesso, apenas restringe. | `TenantContextMiddleware` continua validando vínculo e RLS; o middleware de site apenas reescreve ou recusa (`skeleton/tenancy/site_middleware.py`). | `tests/integration/test_organization_site_routing.py` (membro só de B no host de A), `tests/integration/test_organization_site_shell.py` |
| 2. Host desconhecido, desligado, reservado ou inválido retorna o mesmo 404 neutro. | `refused()` em `skeleton/tenancy/site_middleware.py`; `classify_host` em `skeleton/tenancy/site.py`. | `test_every_refused_host_answers_the_same` |
| 3. No host de A, caminhos de outra organização retornam 404, mesmo para membros das duas. | `route_for_site`; conversor `org` (`to_python`); segunda barreira em `TenantContextMiddleware`. | `test_another_organization_is_never_reachable_from_the_host`, `tests/unit/test_organization_routing.py` |
| 4. Rotas globais no host de organização seguem lista de permissão explícita (incluindo `/brand/`, tema e imagens da instância). | `allowed_global` e `ALLOWED_GLOBAL_PREFIXES`. | `test_platform_and_global_routes_are_refused`, `test_organization_hosts_load_the_instance_theme` |
| 5. Torna-se público apenas o que o administrador optou por publicar. | `OrganizationSite` escrito somente por `sync_site` e `OrganizationSitePage` somente por `sync_site_page` (a página pública só aparece com endereço ligado e página publicada); `/site/logo/` e `/site/icon/` servem apenas a chave do índice sob o prefixo da organização (`skeleton/tenancy/branding.py`). | `tests/integration/test_organization_site.py`, `test_a_tampered_key_in_the_index_is_refused`, `tests/integration/test_organization_public_page.py` |
| 6. Slug, host e `sysid` nunca entram em rótulos de métrica nem atributos de tracing. | `SITE_REFUSED_ROUTE` em `skeleton/observability/metrics.py`; o rótulo `route` usa o padrão da rota. | `test_refused_requests_have_a_fixed_metric_label` |
| 7. `system_context` nunca é aberto por causa do host. | `find_site`, `site_of` e o middleware consultam apenas o índice global. | Revisão de código; `tests/integration/test_organization_site.py` lê o índice com a role runtime sem contexto |

Complementando essas garantias, um `next` absoluto pós-login só é aceito quando aponta para o endereço de uma organização ativa (`is_site_url`, `AccountAdapter.is_safe_url`), e o `ALLOWED_HOSTS` dinâmico nunca aceita curingas (`SiteAllowedHosts`).

## Limites conhecidos

- A API continua em `/api/v1/o/<uuid>/…` mesmo no host da organização.
- Convites e pedidos de entrada preservam o UUID no caminho (`/invite/<uuid>/…`, `/join/<uuid>/`).
- Slugs antigos fora do formato de rótulo DNS não ganham endereço por slug enquanto não forem trocados.
- Alterar o identificador não cria redirecionamento a partir do endereço antigo, e o slug anterior pode ser adotado por outra organização.
- Em domínios próprios, a sessão é restrita ao host e botões de OIDC/SAML não são exibidos.
- O SSO iniciado no host da organização passa pela página de confirmação do allauth no domínio principal, deixando a URL da plataforma visível nessa etapa.
- Notificações push do navegador são vinculadas à origem: quem autorizou no domínio principal e no endereço da organização recebe duplicado.
- A comprovação ou revogação de um domínio próprio pode levar até 30 segundos para propagar em todos os processos.
- Não existem templates personalizados para erros 404/500: o Django retorna suas respostas em texto simples, sem marca.
- Apps derivadas que ainda incluam "· Skeleton" manualmente no título continuam exibindo "Skeleton" no host da organização até migrarem para `{{ site_name }}`.

## Ambiente local

Adicione ao arquivo de hosts (`/etc/hosts` ou `C:\Windows\System32\drivers\etc\hosts`):

```text
127.0.0.1 skeleton.test acme.skeleton.test outra.skeleton.test portal.cliente.test
```

Sem alterar o arquivo de hosts, nomes sob `*.localhost` já costumam resolver para `127.0.0.1` na maioria dos sistemas e navegadores (`skeleton.localhost`, `acme.skeleton.localhost`).

Configure a instância (ou as variáveis de ambiente) e inicie o servidor de desenvolvimento conforme o [README](../README.md) e [testes](testing.md):

```sh
python tools/dev_postgres.py start
python manage.py instance set organization_base_domain skeleton.test
python manage.py instance set public_url http://skeleton.test:8000
python manage.py instance set allowed_hosts skeleton.test,localhost,127.0.0.1
python manage.py runserver 0.0.0.0:8000
```

## Roteiro de verificação manual

1. Abra `http://skeleton.test:8000/` e faça login. **Esperado:** exibição de "Minhas organizações", como de costume.
2. Em `/o/<uuid da acme>/settings/`, ative o endereço próprio. **Esperado:** aviso sobre os dados que ficarão públicos; após salvar, a opção permanece marcada.
3. Abra `http://acme.skeleton.test:8000/` em outra aba. **Esperado:** sessão já autenticada (cookie compartilhado), painel da organização sem seletor de organização nem link "Plataforma", e URLs sem o prefixo `/o/`.
4. Navegue por uma app, um registro, Notificações e Tarefas; pela engrenagem ao lado do nome da organização, acesse a área de configuração (`/admin/`), Tabelas e Configurações gerais. **Esperado:** URLs limpas (`/admin/`, `/catalog/`, `/settings/`), contadores restritos à organização e menu lateral com "Voltar para a organização".
5. Acesse `http://acme.skeleton.test:8000/o/<uuid de outra organização>/`. **Esperado:** 404 "Not found.".
6. Acesse `http://naoexiste.skeleton.test:8000/` e `http://www.skeleton.test:8000/`. **Esperado:** o mesmo 404 "Not found.".
7. Faça logout e abra `http://acme.skeleton.test:8000/accounts/login/`. **Esperado:** nome, cores e logo da organização, com título "Entrar · `<nome>`".
8. Envie um ícone PNG 512 × 512 em Configurações. **Esperado:** o favicon da aba é atualizado nos dois hosts.
9. Saia da conta e acesse `http://acme.skeleton.test:8000/`. **Esperado:** redirecionamento ao login. Entre como administrador, publique a página pública em Configuração › Página pública e saia novamente. **Esperado:** `/` exibe a página pública com nome, cor, logo e botão "Entrar", enquanto `http://skeleton.test:8000/o/<uuid da acme>/` continua redirecionando ao login.
10. Desligue o endereço próprio. **Esperado:** `http://acme.skeleton.test:8000/` passa a retornar 404.
11. Com `organization_custom_domains` habilitado, cadastre `portal.cliente.test`, publique o registro `TXT` em um DNS de teste (ou crie a posse por fixture) e abra `http://portal.cliente.test:8000/`. **Esperado:** exibição da organização com login local, sem botões de SSO.

DNS real e emissão de certificados não são verificáveis localmente: este roteiro valida a resolução via arquivo de hosts e a verificação com resolvedor de teste.
