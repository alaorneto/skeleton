# Endereços de organização

Este guia explica o endereço próprio de uma organização: `acme.skeleton.com` (por slug) ou `portal.cliente.com.br` (por domínio próprio). Ele serve a três leitores: quem **opera** a instalação, quem **administra** uma organização e quem **desenvolve** apps e projetos derivados. O plano é o epic [#477](https://github.com/alaorneto/skeleton/issues/477); as regras normativas estão no [`AGENTS.md`](../AGENTS.md) e no [controle de acesso](access-control.md#host-de-organização).

## O que é

Sem o recurso, toda organização é aberta pelo domínio principal em `/o/<uuid>/…`, e a interface mostra que existe uma plataforma com várias organizações.

Com o recurso ligado na instância e pelo administrador da organização, `https://acme.skeleton.com/` mostra o sistema **como se só a organização "acme" existisse**:

- URLs limpas: `/tables/notes/` em vez de `/o/<uuid>/tables/notes/`;
- sem seletor de organização, sem "Todas as organizações" e sem link "Plataforma";
- notificações, tarefas e contadores só da organização;
- nome, cores, logo, ícone e título da aba da organização, inclusive na tela de login.

No domínio principal nada muda além de duas coisas: o rótulo `route` das métricas das páginas de organização passou de `o/<uuid:organization_id>/…` para `<org:organization_id>…`, e o cookie de sessão se chama `skeleton_session` quando o domínio base está configurado.

## Vocabulário

| Termo | Significado |
|---|---|
| domínio base | Valor de `SKELETON_ORGANIZATION_BASE_DOMAIN` (ex.: `skeleton.com`). Vazio = recurso desligado na instância. |
| host da plataforma | Host de `SKELETON_PUBLIC_URL` e todo host listado explicitamente em `DJANGO_ALLOWED_HOSTS`. Comporta-se como sempre. |
| host de organização | `<slug>.<domínio base>` (um rótulo só) de uma organização com endereço ativo; e um domínio próprio comprovado. |
| site | A organização vista pelo seu host. `OrganizationSite` é o índice global, lido antes do login, com a marca pública. |
| escopo de site | Variável de contexto com o `sysid` da organização do host, ligada pelo middleware durante a requisição (`skeleton.tenancy.routing.site_scope`). |
| URL canônica / limpa | `/o/<uuid>/tables/x/` (canônica) e `/tables/x/` (limpa, só no host de organização). |

## Para quem opera

### Variáveis

| Variável | Padrão | Efeito |
|---|---|---|
| `SKELETON_ORGANIZATION_BASE_DOMAIN` | vazio | Liga os endereços `<slug>.<domínio base>`. Só o domínio, sem esquema, caminho, porta ou curinga (`skeleton.E038`). |
| `SKELETON_PUBLIC_URL` | vazio | Obrigatória com o domínio base: dá o esquema e a porta dos endereços (`https://app.skeleton.com` gera `https://acme.skeleton.com`). |
| `SKELETON_ORGANIZATION_CUSTOM_DOMAINS` | `false` | Liga os domínios próprios comprovados por DNS. Só vale com o domínio base. |
| `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES` | `[]` | Setting do projeto derivado (não está no catálogo da instância): prefixos de rotas globais próprias liberadas no host de organização. |

As três primeiras ficam no catálogo da instância (`manage.py instance set organization_base_domain skeleton.com`).

### DNS, TLS e proxy

- **DNS:** um registro curinga `*.<domínio base>` apontando para a instalação, além do registro do domínio principal.
- **TLS:** um certificado curinga para `*.<domínio base>`. Para domínios próprios, um certificado por domínio, emitido pelo proxy; com emissão sob demanda, o proxy consulta `GET http://<DJANGO_HEALTH_HOST>/health/site-host/?domain=<host>` (200 = pode emitir; 404 = não). Veja [deployment](deployment.md#domínios-próprios-489).
- **Proxy:** repasse o cabeçalho `Host` original e não reescreva o caminho. Atrás de proxy, `DJANGO_TRUST_PROXY` continua decidindo o esquema; a aplicação nunca lê `X-Forwarded-Host` por conta própria.

### Domínio base dedicado

Todo host abaixo do domínio base recebe o cookie de sessão (ele é emitido com `Domain=.<domínio base>`). Por isso o domínio base tem de ser **dedicado** a esta aplicação: não delegue subdomínios dele a terceiros nem hospede outra aplicação ali. Use um `SKELETON_PUBLIC_URL` dentro dele (`https://app.skeleton.com` com `skeleton.com`); fora dele, a sessão não é compartilhada e o check `skeleton.W039` avisa.

### O que muda ao configurar o domínio base

- `.<domínio base>` entra sozinho no fim de `ALLOWED_HOSTS`, também nos settings de produção.
- O cookie de sessão passa a se chamar `skeleton_session`: **todos entram de novo uma vez**.
- O rótulo `route` das métricas das rotas web de organização é `<org:organization_id>…` (desde #479, com ou sem domínio base). Requisições recusadas por um host de organização aparecem com o rótulo fixo `organization-site-refused`, nunca com host, slug ou `sysid`.

### Checks

| Check | Quando dispara |
|---|---|
| `skeleton.E038` | Domínio base com formato inválido, ou sem `SKELETON_PUBLIC_URL`. |
| `skeleton.E039` | Domínio base com `SESSION_COOKIE_DOMAIN` ou `CSRF_COOKIE_DOMAIN` definidos: o núcleo decide o domínio do cookie por requisição. |
| `skeleton.W039` | Host de `SKELETON_PUBLIC_URL` fora do domínio base: sessão não compartilhada, sem SSO nos endereços. |

Detalhes em [checks](checks.md).

### Nomes reservados e organizações antigas

Uma organização nova recebe um slug que é rótulo DNS (1 a 63 caracteres `a-z`, `0-9` e `-`, sem hífen nas pontas, sem `xn--`) e que não está na lista de reservados de `skeleton/common/hostnames.py` (`www`, `api`, `app`, `admin`, `platform`, `login`, `static`, `mail`, `o`, `site`, entre outros). Organizações antigas com slug fora da regra continuam funcionando em `/o/<uuid>/`, mas não ganham endereço por slug enquanto o slug não for trocado por um que siga a regra ([trocar o identificador](#trocar-o-identificador)).

## Para quem administra a organização

### Ligar e desligar

Em **Configurações** (`/o/<org>/settings/`), a seção "Endereço próprio" mostra o endereço (`https://acme.skeleton.com`) e a caixa "Ativar endereço próprio". Ao ativar, **o nome de exibição, a cor, o logo e o ícone ficam visíveis para qualquer pessoa que abra o endereço, mesmo sem login** ([privacidade](privacy.md#endereço-próprio-da-organização)). Desligado, o endereço responde como endereço inexistente (404).

### Trocar o identificador

O identificador (slug) forma o endereço `<slug>.<domínio base>` e é único entre todas as organizações, inclusive as excluídas ([#765](https://github.com/alaorneto/skeleton/issues/765)). O administrador da organização troca o identificador em **Configurações** (`/o/<org>/settings/`, seção "Identificador"), e o administrador da plataforma no detalhe da organização (`/platform/organizations/<id>/`). Enquanto a pessoa digita, a página diz se o identificador está disponível, já está em uso, é reservado ou tem formato inválido; isso só orienta, e ao salvar o servidor confere tudo de novo na transação, com a organização travada, a revisão (CAS) e a restrição `unique` do banco: de duas trocas simultâneas para o mesmo identificador, uma vence e a outra recebe "já está em uso". A troca entra na auditoria como `update` da organização.

Ao trocar, **o endereço antigo deixa de responder** (sem redirecionamento) e o identificador antigo fica livre para outra organização; links e favoritos com o endereço antigo param de funcionar. Domínios próprios e `/o/<uuid>/` não mudam. Uma organização com provedor de identidade preso a ela (no banco ou no arquivo) não troca o identificador: a impressão digital do vínculo inclui o slug ([provedores no banco](authentication-sso.md#provedores-no-banco-29)).

### Página pública

Com o endereço ligado, o administrador pode publicar uma **página pública** da organização ([#623](https://github.com/alaorneto/skeleton/issues/623)) em **Configuração › Organização › Página pública** (`/o/<org>/admin/pages/public/`, ou `/admin/pages/public/` no próprio endereço): blocos de [texto simples](ui-design.md#páginas-em-blocos) (`hero`, `text`, `contact`, `links`), sempre escapados, montados no editor sem JavaScript e com pré-visualização. Quem abre `/` do endereço sem login vê a página, com o nome, a cor e o logo publicados da organização e, no `hero`, o botão "Entrar" (que leva ao login com a marca dela). Sem página publicada, `/` continua levando ao login; com o endereço desligado, continua o 404 neutro; `/o/<uuid>/` no domínio principal nunca é público. Quem entrou continua vendo o início interno. O editor avisa quando o endereço está desligado: "A página só aparece no endereço próprio da organização, que está desligado."

- A página fica em `OrganizationPage` (slot `public`), com isolamento tenant. O que o visitante anônimo lê é o espelho global `OrganizationSitePage`, escrito só por `sync_site_page` na mesma transação e no `organization_lock` da escrita: `enabled` é "endereço ligado e página publicada"; desligado, o documento é esvaziado. Publicar, despublicar, ligar e desligar o endereço (`sync_site` chama `sync_site_page`) atualizam o espelho.
- `published_site_page(organization_id)` (`skeleton.tenancy.pages`) lê só o espelho, sem contexto de tenant e sem `system_context`, como `site_of`; `views.organization_home` (rota `web:dashboard`) só o consulta quando o host é o da própria organização da URL.

### Logo e ícone

- Logo: PNG, JPEG ou WebP até 512 KB.
- Ícone: PNG quadrado de exatamente 512 × 512 pixels, até 256 KB. Vira o favicon e o ícone de atalho no endereço da organização e nas páginas dela no domínio principal.

### Domínio próprio, passo a passo

Com o recurso ligado na instância, a seção "Endereço próprio" tem o link "Domínios próprios" (`/o/<org>/settings/domains/`):

1. Adicione o domínio (`portal.cliente.com.br`); até 3 por organização.
2. Publique o registro `TXT` em `_skeleton-site.<domínio>` com o valor mostrado na tela.
3. Aponte o domínio para a instalação: `CNAME` para o host do domínio principal (ou `A`/`AAAA` num domínio raiz).
4. Clique em "Verificar agora". Comprovado, o domínio passa a atender em até 30 s.
5. O certificado é emitido pelo operador da instalação.

Se o TXT sair do ar, a reverificação diária (`manage.py verify_domains --recheck`) marca o domínio como falho depois de três falhas seguidas e ele deixa de atender. Um domínio só vale para uma organização; a plataforma pode revogar uma posse disputada em `/platform/domains/`.

### Links já enviados

Com o endereço ligado, os emails **novos** (notificações, convites, links de ingresso) levam ao endereço da organização. Desligar faz os novos voltarem ao domínio principal; os links antigos para o endereço desligado passam a responder 404.

## Para quem desenvolve

- **Rotas web de organização** começam com `ORG` de `skeleton.tenancy.routing` (`path(f"{ORG}relatorios/", ...)`), nunca com o texto `o/<uuid:organization_id>/`; um teste de guarda recusa o prefixo fixo. As rotas das apps montadas pelo núcleo já usam o conversor. As rotas de API continuam `api/v1/o/<uuid:organization_id>/…`.
- `reverse` e `{% url %}` devolvem a forma certa sozinhos: canônica no domínio principal, limpa no host da organização. Nunca monte `/o/<uuid>/` por texto: use `organization_prefix(organization_id)`.
- **Templates:** `site_mode` (verdadeiro no host de organização), `organization_site` (o `Site` do índice; não se chama `site` porque o allauth já usa esse nome), `site_name` (sufixo do título: `<tela> · {{ site_name }}`) e `organization_icon_url`. Veja [interface](ui-design.md#título-e-marca-no-endereço-da-organização).
- **Vínculos:** `memberships_for_user` e `for_each_organization` são estreitados para a organização do host. Passe `all_organizations=True` em tudo que decide ou exporta dados da conta (ciclo de vida, segurança, privacidade). Estreitar é apresentação, nunca autorização.
- **Rota global própria** no host de organização: acrescente o prefixo a `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES`; sem isso ela responde 404 lá.
- **Links em jobs:** use `skeleton.tenancy.site.organization_url(organization_id, path)` (ou `shareable_url` com uma requisição); `None` quer dizer "sem endereço ativo", e o chamador mantém a URL do domínio principal.
- **Testes de host:** `client.get(caminho, HTTP_HOST="acme.example.test")` com `SKELETON_ORGANIZATION_BASE_DOMAIN="example.test"`, `SKELETON_PUBLIC_URL="https://app.example.test"` e `ALLOWED_HOSTS=[".example.test", "testserver"]`; `tests/site_support.py` tem as fixtures. No cliente de teste, leia a view por `response.wsgi_request.resolver_match`: `response.resolver_match` resolve o caminho limpo fora do escopo de site.

## Segurança

| Regra | Onde o código garante | Teste que prova |
|---|---|---|
| 1. O host nunca concede acesso, só restringe. | `TenantContextMiddleware` continua validando vínculo e RLS; o middleware de site só reescreve ou recusa (`skeleton/tenancy/site_middleware.py`). | `tests/integration/test_organization_site_routing.py` (membro só de B no host de A), `tests/integration/test_organization_site_shell.py` |
| 2. Host desconhecido, desligado, reservado ou inválido: mesmo 404 neutro. | `refused()` em `skeleton/tenancy/site_middleware.py`; `classify_host` em `skeleton/tenancy/site.py`. | `test_every_refused_host_answers_the_same` |
| 3. No host de A, caminho de outra organização é 404, mesmo para membro das duas. | `route_for_site`; conversor `org` (`to_python`); segunda barreira no `TenantContextMiddleware`. | `test_another_organization_is_never_reachable_from_the_host`, `tests/unit/test_organization_routing.py` |
| 4. Rotas globais no host de organização: lista de permissão explícita (inclui `/brand/`, o tema e as imagens da instância, #597). | `allowed_global` e `ALLOWED_GLOBAL_PREFIXES`. | `test_platform_and_global_routes_are_refused`, `test_organization_hosts_load_the_instance_theme` |
| 5. Público só o que o administrador aceitou publicar. | `OrganizationSite` escrito só por `sync_site` e `OrganizationSitePage` só por `sync_site_page` (a página pública, #623, só com o endereço ligado e a página publicada); `/site/logo/` e `/site/icon/` só pela chave do índice e sob o prefixo da organização (`skeleton/tenancy/branding.py`). | `tests/integration/test_organization_site.py`, `test_a_tampered_key_in_the_index_is_refused`, `tests/integration/test_organization_public_page.py` |
| 6. Slug, host e `sysid` nunca em rótulo de métrica nem atributo de tracing. | `SITE_REFUSED_ROUTE` em `skeleton/observability/metrics.py`; o rótulo `route` é o padrão da rota. | `test_refused_requests_have_a_fixed_metric_label` |
| 7. `system_context` nunca é aberto por causa do host. | `find_site`, `site_of` e o middleware só consultam o índice global. | Revisão de código; `tests/integration/test_organization_site.py` lê o índice com a role runtime sem contexto |

Além disso: o `next` absoluto pós-login só é aceito para o endereço de uma organização ativa (`is_site_url`, `AccountAdapter.is_safe_url`), e `ALLOWED_HOSTS` dinâmico nunca contém curinga (`SiteAllowedHosts`).

## Limites conhecidos

- A API continua em `/api/v1/o/<uuid>/…` também no host da organização.
- Convite e pedido de entrada mantêm o UUID no caminho (`/invite/<uuid>/…`, `/join/<uuid>/`).
- Slug antigo que não é rótulo DNS não ganha endereço por slug até ser trocado.
- Trocar o identificador não redireciona o endereço antigo, que pode ser tomado por outra organização.
- Em domínio próprio a sessão é local e OIDC/SAML não é oferecido.
- O SSO iniciado no host da organização passa pela página de confirmação do allauth no domínio principal (a URL da plataforma fica visível nesse passo).
- O push do navegador é por origem: quem aceitou no domínio principal e no endereço da organização recebe duas vezes.
- Comprovar ou revogar um domínio próprio leva até 30 s para valer em cada processo.
- Não há template próprio de erro 404/500: as respostas de erro são o texto puro do Django, sem marca.
- Os títulos com "· Skeleton" escritos à mão por apps derivadas continuam mostrando "Skeleton" no host da organização até trocarem para `{{ site_name }}`.

## Ambiente local

Acrescente ao arquivo de hosts (`/etc/hosts` ou `C:\Windows\System32\drivers\etc\hosts`):

```text
127.0.0.1 skeleton.test acme.skeleton.test outra.skeleton.test portal.cliente.test
```

Sem editar o arquivo de hosts, os nomes `*.localhost` já resolvem para `127.0.0.1` na maioria dos sistemas e navegadores (`skeleton.localhost`, `acme.skeleton.localhost`).

Configure a instância (ou o ambiente) e suba o servidor de desenvolvimento como no [README](../README.md) e em [testes](testing.md):

```sh
python tools/dev_postgres.py start
python manage.py instance set organization_base_domain skeleton.test
python manage.py instance set public_url http://skeleton.test:8000
python manage.py instance set allowed_hosts skeleton.test,localhost,127.0.0.1
python manage.py runserver 0.0.0.0:8000
```

## Roteiro de verificação manual

1. Abra `http://skeleton.test:8000/` e entre. **Esperado:** "Minhas organizações", como antes.
2. Em `/o/<uuid da acme>/settings/`, ative o endereço próprio. **Esperado:** aviso do que fica público; depois de salvar, a caixa marcada.
3. Abra `http://acme.skeleton.test:8000/` em outra aba. **Esperado:** já autenticado (sessão compartilhada); o painel da organização, sem seletor de organização nem "Plataforma"; os links sem `/o/`.
4. Navegue por um app, um registro, Notificações e Tarefas; pela engrenagem ao lado do nome da organização, abra a área de configuração (`/admin/`), Tabelas e Configurações gerais. **Esperado:** URLs limpas (`/admin/`, `/catalog/`, `/settings/`); contadores só da organização; o menu lateral da área com "Voltar para a organização".
5. Abra `http://acme.skeleton.test:8000/o/<uuid de outra organização>/`. **Esperado:** 404 "Not found.".
6. Abra `http://naoexiste.skeleton.test:8000/` e `http://www.skeleton.test:8000/`. **Esperado:** o mesmo 404 "Not found.".
7. Saia e abra `http://acme.skeleton.test:8000/accounts/login/`. **Esperado:** nome, cores e logo da organização; título "Entrar · <nome>".
8. Envie um ícone PNG 512 × 512 em Configurações. **Esperado:** o favicon da aba muda nos dois hosts.
9. Saia e abra `http://acme.skeleton.test:8000/`. **Esperado:** o login. Entre como administrador, publique a página pública em Configuração › Página pública e saia de novo. **Esperado:** `/` mostra a página com nome, cor e logo da organização e o botão "Entrar"; `http://skeleton.test:8000/o/<uuid da acme>/` continua indo para o login.
10. Desligue o endereço. **Esperado:** `http://acme.skeleton.test:8000/` passa a responder 404.
11. Com `organization_custom_domains` ligado, cadastre `portal.cliente.test`, publique o TXT num DNS de teste (ou crie a posse por fixture) e abra `http://portal.cliente.test:8000/`. **Esperado:** a organização, com login local (sem botões de SSO).

O DNS real e os certificados não são verificáveis num ambiente local: o roteiro cobre a resolução por arquivo de hosts e a verificação com um resolvedor de teste.
