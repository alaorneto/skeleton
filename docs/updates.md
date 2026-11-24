# Atualizar o sistema base e as apps

Este guia responde a quem opera uma instalação: como atualizo, de onde vêm as versões, como uso um fork e o que fazer quando algo dá errado. Ele reúne o que o épico #491 entregou; a referência completa do agente, das chaves de configuração e das rotas continua em [deployment](deployment.md#agente-independente-de-deployment).

## Visão geral

Quem publica assina; o agente de deployment, no servidor, verifica e instala; a tela da plataforma só pede e mostra. Nada é aplicado sem que o operador veja o plano e confirme, e cada passo é uma operação completa do agente.

```
publicador ── workflow "Artefatos de release" ──► release do GitHub
   (chave privada no ambiente "release")           django-skeleton-<v>.skapp      (pacote do core)
                                                    skeleton-<v>.release.skapp     (pacote de release)
                                                    skeleton_deployment_agent-<v>-py3-none-any.whl
                                                    skeleton-index.json            (índice assinado)
                                                             │
                                  agente ◄───────────────────┘  verifica assinatura (approved_publishers),
                                    │                            hashes, linha de versão, alvo
                                    ▼
                          /platform/updates/  ── mostra o caminho; o operador prepara e confirma cada passo
```

O agente confere a assinatura do índice e de cada pacote contra as chaves que o operador aprovou no arquivo do agente (`approved_publishers`); o GitHub só entrega bytes. A tela nunca cadastra chave nem token e nunca baixa nada: ela fala com o agente pelo socket de controle.

## Vocabulário

| Termo | Significado |
|---|---|
| pacote de componente | `.skapp` de `kind` `core` ou `app`. |
| pacote de release | `.skapp` de `kind` `release`: um arquivo por release do core, com o pacote do core, os wheels das dependências, o lock e o wheel do agente ([formato](deployment.md#pacote-de-release)). |
| alvo | Interpretador e plataforma: `implementation_name`, `python_version`, `platform_machine`, `sys_platform`. Ex.: `cp314-manylinux_2_28_x86_64`. |
| ambiente do publicador | Lock e wheels de um alvo, vindos de um pacote de release e guardados pelo agente. |
| índice de releases | O catálogo assinado, publicado como o asset `skeleton-index.json` do release mais recente. |
| fonte | Onde o agente procura releases: `path`, `url` ou `github`; do arquivo do agente ou cadastrada na tela. |
| caminho de atualização | Lista ordenada de versões entre a instalada e a mais nova alcançável. |
| passo | Uma operação completa do agente: plano, confirmação, manutenção, backup, migrations, cutover. |
| linha | Par `(major, minor)` de uma versão. |

## Atualizar por arquivo

1. Baixe o pacote de release (`skeleton-<versão>.release.skapp`) do release do GitHub e confira-o, se quiser, com a chave pública do publicador:

   ```sh
   .venv/bin/python -m skeleton.system.packages validate skeleton-0.6.1.release.skapp \
     --trust-store /caminho/privado/publishers.json
   ```

2. Envie-o em `/platform/updates/` → "Enviar pacote". O agente verifica o pacote inteiro, guarda o pacote de release e o pacote do core que veio nele e abre "Preparar plano" já com a ação "Atualizar núcleo" e o digest do core. Se o pacote não traz a plataforma deste servidor, a tela avisa que valem o lock e o wheelhouse configurados no agente.
3. "Visualizar impacto" pede a prévia. Confira os componentes, as migrations, os arquivos e, quando houver, "Dependências que deixam de ser instaladas".
4. Na tela da operação, confirme (autenticação recente) e acompanhe até `succeeded`.

Pelo shell do servidor, o mesmo fluxo é:

```sh
AGENT=/opt/skeleton-agent/current/venv/bin/skeleton-deployment-agent
CONFIG=/srv/skeleton/private/agent.json
"$AGENT" --config "$CONFIG" upload /srv/skeleton/artifacts/skeleton-0.6.1.release.skapp
"$AGENT" --config "$CONFIG" preview core_update --artifact "$CORE_DIGEST"   # o core.digest da resposta
"$AGENT" --config "$CONFIG" submit --plan-id "$PLAN_UUID" --plan-digest "$PLAN_DIGEST" \
  --generation "$GENERATION" --operation-id "$OPERATION_UUID" --actor-id "$ACTOR_UUID" --request-id "$REQUEST_UUID"
"$AGENT" --config "$CONFIG" operation "$OPERATION_UUID"
```

## Atualizar pelo GitHub

1. Em "Fontes", cadastre o repositório (`dono/nome`) e escolha o publicador entre os aprovados no servidor. Sem nenhuma fonte GitHub, o formulário já sugere o repositório oficial, `alaorneto/skeleton`.
2. Em "Novas versões", "Verificar agora" consulta as fontes. A página mostra a última consulta e, para o núcleo e cada app, a versão instalada, a mais nova e os passos.
3. Para o núcleo, "Atualizar até a versão" escolhe o alvo entre as versões alcançáveis a partir da instalada (#653): a mais nova de cada linha que o caminho alcança, lida dos índices verificados, sem rede; a mais nova de todas vem marcada. De `0.3.0`, com `0.3.1`, `0.4.0`, `0.4.2` e `0.5.0` publicadas, os alvos são `0.3.1`, `0.4.2` e `0.5.0`, e escolher `0.5.0` mostra os passos `0.4.2 → 0.5.0`. Sem escolha, o alvo é a mais nova alcançável. Apps seguem sempre até a mais nova.
4. "Preparar este passo" (só no primeiro passo) baixa o pacote pelo agente e abre o plano. Confirme como no envio por arquivo: cada passo tem prévia e confirmação próprias, com autenticação recente e `updates.operate`; nada é aplicado sozinho.
5. Ao fim da operação, a página dela oferece o próximo passo rumo ao alvo escolhido; ao chegar nele, nenhum passo do núcleo é oferecido e a escolha é esquecida.

O alvo fica só na sessão de quem escolheu e nunca vale como confiança: a tela confere de novo, a cada passo, que ele continua entre os alcançáveis e que o passo pedido é o primeiro rumo a ele, e o agente revalida o passo (assinatura, linha minor, versão abaixo da instalada). Um alvo que deixou de ser alcançável (índice vencido, versão retirada, linha intermediária faltando) é recusado com aviso, nunca trocado por outra versão.

Pelo shell: `check-updates`, `update-status` (sem rede), `sources` e `fetch-release <component_id> <versão> <source_id>`.

## Passo a passo e a regra de linhas

O agente recusa atualizar o núcleo pulando uma linha minor (`version_jump_forbidden`): cada linha pode trazer migrations e mudanças que a seguinte presume aplicadas. Correções (patch) podem ser puladas. De `0.5.2`, com `0.5.3`, `0.6.0`, `0.6.4` e `0.7.1` publicadas, o caminho é `0.5.2 → 0.6.4 → 0.7.1`; de `0.9.x`, o próximo passo é `1.0.x`. Quem publica declara o mesmo em `from_versions` (a própria linha e a anterior, gerado por `skeleton-core manifest`); quem instala confere de novo. Apps seguem só o `from_versions` delas.

| Bloqueio | Significa | O que fazer |
|---|---|---|
| `missing_intermediate_release` | Falta publicado um release de uma linha intermediária. | Peça ao publicador o release da linha que falta; não force o salto. |
| `incompatible_runtime` | Nenhuma versão mais nova serve ao Python ou ao núcleo desta instalação. | Atualize o runtime (ou o núcleo, no caso de apps) primeiro. |

`allow_version_jumps: true` no arquivo do agente desliga a regra. É uma chave de **emergência**, não de rotina: um salto aplica migrations de várias linhas de uma vez, sem os pontos de parada que o publicador testou. Ligue-a só com backup verificado e por uma operação, e desligue em seguida.

## Usar um fork

- **Dono do fork:** gera a própria chave (`python -m skeleton.system.packages keygen --private-key release.key --public-key release.pub.pem`), cria o ambiente `release` do repositório com o segredo `RELEASE_SIGNING_KEY` e a variável `RELEASE_PUBLISHER` (um id próprio, diferente do oficial), publica e divulga a chave pública ([publicação automática](deployment.md#publicação-automática-nos-releases-do-github)).
- **Operador do servidor:** acrescenta a chave pública em `approved_publishers` do arquivo do agente e reinicia o agente. É ele quem decide em quem confiar. No container do agente, cuja configuração gerada aprova só a chave oficial versionada em `deploy/release/publishers/` (#656), basta definir `SKELETON_AGENT_PUBLISHER`, `SKELETON_AGENT_PUBLISHER_KEY` (o PEM do fork, montado no container) e `SKELETON_AGENT_SOURCE` (`dono/nome` do fork) antes da primeira partida; depois dela, edite `agent.json` ([primeira partida pelo agente](deployment.md#primeira-partida-pelo-agente-containers)).
- **Administrador da plataforma:** cadastra `dono/nome` do fork em "Fontes" e escolhe o publicador do fork.

Trocar o publicador do núcleo de um para outro muda a fonte e a chave que vale; não muda o que está instalado, os dados nem o histórico de migrations. O agente só aceita do novo publicador versões que o caminho permite, e o próprio agente só se atualiza a partir do publicador do núcleo instalado (ou de um de `agent_publishers`).

## Repositórios privados

Crie um token de acesso de escopo mínimo (leitura de conteúdo só dos repositórios consultados) e grave-o num arquivo do usuário do agente, modo `0600`, fora do release. Aponte-o em `"github": {"token_file": "/srv/skeleton/private/github-token"}` e marque a fonte com `"private": true` (no arquivo do agente ou na tela, que só mostra a opção quando o token está configurado). O token só vai para o host de `api_url`; nunca segue o redirecionamento do download e nunca aparece em erro, log ou tela.

## Atualizar o agente

O agente informa a própria versão em "Versões instaladas". Como ele valida e instala o núcleo, uma prévia de núcleo de linha acima da do agente é recusada com `agent_update_required`, e a tela mostra "Atualize o agente primeiro". Com `agent_root` configurado, "Atualizar agente" monta um virtualenv novo a partir do pacote de release (sem rede), testa-o com `selfcheck`, troca o link `current` e reinicia o serviço (código de saída 75 com `Restart=on-failure`). Nos containers, `agent_root` é o volume `agent_code`, e o iniciador da imagem sobe a versão nova sozinho; uma instalação de containers anterior a isso precisa de uma última imagem nova ([atualizar o agente nos containers](deployment.md#atualizar-o-agente-nos-containers-772)). Num servidor systemd sem `agent_root`, use o servidor:

```sh
"$AGENT" --config "$CONFIG" agent-install --release /srv/skeleton/artifacts/skeleton-0.7.0.release.skapp --root /opt/skeleton-agent
sudo systemctl restart skeleton-agent
"$AGENT" --config "$CONFIG" agent-rollback --root /opt/skeleton-agent    # volta à versão anterior
```

Migração de uma instalação antiga (`/opt/skeleton-agent/venv`) e o leiaute de `agent_root`: [atualizar o agente](deployment.md#atualizar-o-agente).

Num cluster (#689), "Atualizar agente" em qualquer nó chega ao primário, que confere o pacote, grava a versão que todos seguem e responde que a atualização está em rolagem. Cada outro nó com `agent_root` baixa o pacote do primário, confere de novo a assinatura e o publicador, monta a versão nova e reinicia; o primário se atualiza por último, depois de os outros nós vivos informarem a versão nova (ou de 15 minutos). Nos containers com o volume `agent_code` (#772), cada nó segue sozinho como no systemd; num container sem ele, o agente vem da imagem: atualize a imagem e rode `docker compose up -d` nesse nó. A coluna **Agente** do painel de nós mostra a versão de cada nó, e um aviso aparece quando os nós vivos estão em versões diferentes; nesse estado, nenhuma atualização começa (`agent_versions_differ`).

## Atualizar um cluster de vários nós

Num cluster (#678–#681, [alta disponibilidade](high-availability.md)), a tela de qualquer nó encaminha a atualização ao agente primário. O primário prepara o release e cada seguidor o recebe e confere antes de qualquer manutenção; depois todos os nós entram juntos na manutenção e param, o primário faz o backup e as migrations, e cada nó troca o release e confere a própria saúde antes de o tráfego reabrir. Em **Versões instaladas**, o painel **Nós do cluster** mostra o agente primário e a época, e cada nó com papel, release, fase da operação em curso e se está vivo ou sem sinal há mais de 60 s; o botão **Reiniciar os nós em rolagem** reinicia os serviços nó a nó com o mesmo release (para aplicar uma configuração nova), com reautenticação recente e CSRF. Sem primário, a página diz que as atualizações estão suspensas e o site continua no ar; com o primário inalcançável, uma operação pendente depois de uma troca de primário ou o cluster ocupado, a mensagem diz o que fazer. A prévia mostra o **modo de atualização** e, na confirmação de um plano que pode trocar nó a nó, a caixa **Forçar janela de manutenção**. Um patch sem migration, da mesma linha `major.minor` e com as mesmas apps, troca nó a nó sem janela de manutenção (a prévia mostra `rolling`); o resto usa a janela em todos os nós. O passo a passo, o que cada falha deixa e a recuperação estão em [agente em vários nós](deployment.md#agente-em-vários-nós-678).

## Apps

Apps continuam em pacotes de componente. Uma app publica o próprio índice no próprio repositório: `package build` gera o `.skapp`, e `package index --previous` acumula o índice anterior; anexe ao release do GitHub o pacote e o asset `skeleton-index.json`, com `artifact` no formato `<tag>/<arquivo>` (`--artifact-base <tag>`). Declare `migrations.from_versions` com a mesma política do núcleo: a própria linha e a anterior ([extensão](extending.md#publicar-apps-e-releases-assinados)). Depois de um passo do núcleo, uma app que deixou de ser compatível aparece na checagem de dependências da prévia, com a sugestão de versão quando as fontes têm uma. Uma app que traz temas da instância ([aparência](appearance.md#temas-trazidos-por-apps)) os leva junto: a prévia de desinstalação avisa quais temas saem do catálogo e, se um deles é o ativo, que a instância volta ao tema do núcleo (o aviso não muda o plano aprovado). Reinstalar a app devolve os temas; se ninguém ativou outro, o tema dela volta a valer.

## Avisos

O agente consulta as fontes sozinho a cada `update_check_interval` segundos (padrão 6 horas; `0` desliga; a primeira consulta, 2 minutos depois de subir). Quem tem `updates.operate` vê nas páginas de `/platform/` uma faixa com o link "Ver o passo a passo"; o atraso máximo é o intervalo do agente mais 5 minutos de cache. Nada é baixado nem aplicado sozinho. Quem tem `updates.operate` também recebe na caixa da plataforma o aviso de versão nova e o de uma operação que não terminou ([alertas da plataforma](notifications.md#alertas-da-plataforma)).

Todo deployment roda o agente (#649), que mostra as versões em "Novas versões". Fora dele (só desenvolvimento local), com o opt-in `SKELETON_UPDATE_REPOSITORY`, o job `system.update_check` consulta o release mais recente e mostra um aviso **só informativo** com o link para o release ([aviso sem agente](deployment.md#sandbox-no-coolify-369)).

## Dependências do projeto derivado

Com um pacote de release que traz o alvo do servidor, o lock e os wheels das dependências do núcleo vêm do publicador. As dependências **só do projeto** ficam em `project_requirements_lock` (com os wheels no `wheelhouse`). Antes do primeiro pacote de release, mova-as do `requirements_lock` para `project_requirements_lock`; senão a prévia as lista em "Dependências que deixam de ser instaladas". Num alvo que o pacote não traz, o agente usa o `requirements_lock` e o `wheelhouse` como antes, e um wheel faltante dá `locked_wheel_unavailable`.

## Segurança

1. Nada é instalado sem assinatura de um publicador aprovado pelo operador, no servidor. A assinatura prova procedência, não ausência de código malicioso: aprove só publicadores em quem confia.
2. A tela escolhe **onde procurar** (um `dono/nome`); o servidor escolhe **em quem confiar** (`approved_publishers`). A tela não cadastra chave, token, host, caminho nem URL.
3. Tudo o que vem da rede é dado não confiável: tamanho limitado, formato validado, nunca vira caminho ou comando sem validação.
4. Saída de rede: o agente só contata `api.github.com` (ou o `api_url` configurado), os hosts de `download_hosts` no download (padrão `*.githubusercontent.com`) e os hosts das fontes `url`; o web, só com o opt-in sem agente, `api.github.com`. Libere só esses no firewall.
5. Chaves privadas de publicação ficam fora do servidor de produção (no ambiente `release` do repositório); tokens ficam em arquivo privado do agente. Nenhum dos dois aparece em log, erro, journal ou tela.
6. Downgrade é proibido; salto de linha é recusado por padrão.
7. Toda operação exige `updates.operate`, autenticação recente e confirmação do plano. Não há aplicação automática.
8. A instalação é offline (`pip --no-index --require-hashes`).

**Rotação da chave do publicador:** gere uma chave nova, publique com outro id de publicador e troque a entrada em `approved_publishers`; a chave antiga deixa de valer quando sai do arquivo.

## Solução de problemas

| Código | O que significa | O que fazer |
|---|---|---|
| `version_jump_forbidden` | O pacote está mais de uma linha à frente do núcleo instalado. | Siga o caminho de "Novas versões", um passo por vez. |
| `agent_update_required` | O agente é de linha anterior à do núcleo de destino. | Atualize o agente primeiro. |
| `agent_self_update_unavailable` | `agent_root` não configurado. | Use `agent-install` no servidor. |
| `release_without_agent` | O pacote de release não traz o agente. | Use um pacote que traga, ou atualize o agente à mão. |
| `agent_publisher_not_allowed` | O publicador do pacote não é o do núcleo instalado nem está em `agent_publishers`. | Confirme a origem; acrescente o id em `agent_publishers` só se confiar. |
| `agent_already_current`, `agent_downgrade_forbidden` | O agente do pacote é igual ou mais antigo. | Nada a fazer; a volta é só por `agent-rollback`. |
| `agent_target_unavailable` | O pacote não traz a plataforma do interpretador do agente. | Peça ao publicador o alvo, ou instale o agente à mão. |
| `agent_build_failed` | A montagem ou o `selfcheck` do agente novo falhou; o atual continua. | Veja o log do agente no servidor. |
| `agent_rollback_unavailable` | Não há versão anterior pronta. | Instale a versão desejada com `agent-install`. |
| `agent_versions_differ` | Num cluster, os nós vivos têm agentes em versões diferentes; nenhuma atualização começa. | Atualize o agente (com `agent_root`, inclusive nos containers com o volume `agent_code`, "Atualizar agente"; sem ele, à mão em cada nó). |
| `agent_update_manual` | Num cluster, um nó sem `agent_root` (systemd antigo, ou container sem o volume `agent_code`) não segue a versão do agente sozinho. | Atualize o agente desse nó à mão (no container, a imagem nova e `docker compose up -d`). |
| `release_environment_conflict` | O mesmo núcleo chegou com outro conjunto de dependências. | O publicador precisa publicar outra versão. |
| `dependency_version_conflict` | `project_requirements_lock` fixa outra versão de uma dependência do núcleo. | Alinhe a versão com o lock do publicador. |
| `locked_wheel_unavailable` | Alvo sem ambiente no pacote e wheel faltando no `wheelhouse`. | Disponibilize o wheel ou use um pacote com o alvo. |
| `package_verification_failed` | Assinatura de publicador não aprovado, conteúdo alterado ou formato inválido. | Confira a origem do arquivo e `approved_publishers`. |
| `catalogue_publisher_mismatch` | O índice da fonte foi assinado por outro publicador que o escolhido. | Confira o publicador da fonte. |
| `catalogue_verification_failed` | O índice não passou na verificação (assinatura, formato ou validade vencida). | Índice vencido bloqueia a fonte até o publicador renovar (o workflow renova todo mês). |
| `catalogue_generation_content_changed` | O índice mudou sem aumentar a geração. | Avise o publicador: é recusado de propósito. |
| `source_index_missing` | O release mais recente ainda não tem `skeleton-index.json`. | Logo depois de publicar, aguarde alguns minutos. |
| `github_rate_limited` | O GitHub limitou as requisições deste IP. | Aguarde, ou configure um token e não use intervalo menor que 1 hora. |
| `github_release_not_found` | O repositório não tem release (ou não está acessível). | Confira `dono/nome` e, se privado, o token. |
| `github_token_missing` | Fonte privada sem token legível. | Configure `github.token_file` (0600). |
| `github_asset_missing` | O asset citado no índice não está no release. | Avise o publicador. |
| `github_asset_url_rejected` | O download apontou para endereço fora do permitido. | Confira `download_hosts`; não amplie sem motivo. |
| `catalogue_artifact_path_rejected` | O índice aponta para arquivo fora do formato `<tag>/<asset>` ou do diretório da fonte. | Avise o publicador. |
| `catalogue_unavailable` | A fonte não respondeu ou respondeu fora do formato. | Tente de novo mais tarde. |
| `invalid_source`, `source_id_reserved`, `publisher_not_approved`, `too_many_sources`, `source_not_found` | Cadastro de fonte recusado. | Corrija o formulário; o publicador precisa estar aprovado no servidor. |
| `invalid_managed_sources` | O `sources.json` do agente está inválido (por exemplo, publicador retirado). | Corrija ou apague a entrada no arquivo do agente. |

**O agente não volta depois de se atualizar:** rode `agent-rollback` e `sudo systemctl restart skeleton-agent`; depois investigue o log do serviço.

## Clientes nativos (#606)

Um cliente nativo (aplicativo de desktop instalado com o Velopack) se atualiza a partir do **próprio servidor do núcleo**, não de um repositório público: o operador diz de onde vêm as releases, o servidor sincroniza, verifica a assinatura e serve feed e pacotes com os bytes exatos. O download é anônimo por desenho, porque um cliente velho demais para entrar ainda precisa se atualizar; o conteúdo não tem dado pessoal. O código fica em `skeleton/system/native_clients/`.

**Configuração.** `SKELETON_NATIVE_CLIENTS_CONFIG` aponta para um JSON privado do operador (modo 0600, fora do repositório e do SQLite da instância); vazio, a distribuição fica desligada:

```json
{
  "trust_store": "trust.json",
  "clients": {
    "acme-desktop": {"repository": "acme/desktop", "token_file": "acme-github-token",
                     "channels": ["stable", "beta"], "publisher": "acme", "enabled": true}
  }
}
```

`trust_store` é o mesmo formato de trust store dos pacotes (publicador → chave pública Ed25519); caminhos relativos partem do diretório da configuração. `token_file` guarda um token do GitHub com leitura das releases (dispensável em repositório público). O publicador precisa estar no trust store, senão a configuração é recusada. `enabled: false` para a sincronização e a entrega daquele cliente; os clientes instalados continuam na versão que têm (rollback).

**Protocolo de uma release.** Cada GitHub Release do repositório traz, por sistema (`<os>`, como `win`), o feed do Velopack `releases.<os>.json`, os arquivos que ele lista e o envelope `releases.<os>.sig.json`:

```json
{"payload": {"client_id": "acme-desktop", "channel": "stable", "feed_sha256": "<sha256 do feed>",
             "signed_at": "2026-11-20T00:00:00Z", "expires_at": "2026-12-20T00:00:00Z"},
 "signature": "<base64 Ed25519 do JSON canônico de payload>"}
```

O JSON canônico é o dos pacotes (`canonical_json`: chaves ordenadas, sem espaços, UTF-8). O vetor fixo `tests/fixtures/native_clients/vector.json` (chave pública, feed, payload canônico e envelope) serve para o cliente conferir a mesma verificação.

**Sincronização.** `python manage.py native_client_releases sync --client-id acme-desktop` (sem `--client-id`, todos os clientes ativos) e o job periódico `native_clients.sync`, registrado a cada `SKELETON_NATIVE_CLIENTS_SYNC_HOURS` horas (padrão 1) quando a configuração existe. Para cada release, da mais antiga para a mais nova, e cada envelope:

1. lê o feed e o envelope (até 1 MiB cada), só por `skeleton.common.outbound_http`, no host fixo da API do GitHub; o download dos arquivos segue o redirecionamento uma vez, só para `*.githubusercontent.com`, sem o token;
2. verifica o envelope: assinatura do publicador configurado, `client_id`, canal configurado, validade (`expires_at` futura) e o SHA-256 do feed; recusa `signed_at` que não seja mais novo que o do feed já publicado no mesmo canal e sistema (anti-rollback); o mesmo feed de novo é só ignorado;
3. baixa cada arquivo listado que ainda não está guardado, calculando o SHA-256 enquanto grava, e recusa hash ou tamanho diferentes do feed (até `SKELETON_NATIVE_CLIENT_MAX_FILE_BYTES`, padrão 1 GiB);
4. só então guarda feed e envelope e publica a release no canal.

Os bytes ficam no armazenamento privado do núcleo (`STORAGES["skeleton_files"]`), endereçados pelo SHA-256 verificado (`native-clients/<cliente>/<xx>/<sha256>`), e nunca são reescritos. Cada feed publicado ou recusado deixa uma linha `sk_system.NativeClientSync` com o código (`invalid_signature`, `untrusted_publisher`, `expired`, `stale_signature`, `feed_hash_mismatch`, `file_hash_mismatch`, `file_missing`, `invalid_feed`, `invalid_envelope`, `wrong_client`, `unknown_channel`), auditada pelos triggers; uma falha da fonte deixa uma linha `failed` e não publica nada.

**Entrega, sem autenticação**, fora de `/api/v1/` (que só aceita JSON e limita o corpo):

| Rota | Conteúdo |
|---|---|
| `GET /clients/<client_id>/<canal>/releases.<os>.json` | O feed vivo do canal e sistema: o mais novo não retirado e com envelope não vencido |
| `GET /clients/<client_id>/<canal>/releases.<os>.sig.json` | O envelope desse feed |
| `GET /clients/<client_id>/<canal>/<arquivo>` | Um arquivo listado por um feed vivo do canal |

As respostas trazem os bytes verificados, `ETag` (o SHA-256), `Content-Length`, `X-Content-Type-Options: nosniff` e `Cache-Control` (`public, max-age=60, must-revalidate` nos feeds; `public, max-age=31536000, immutable` nos arquivos); `If-None-Match` responde 304. O resto é 404. Configure o Velopack do cliente com a URL `https://<servidor>/clients/<client_id>/<canal>/`. Um envelope vencido tira o feed do ar até o publicador assinar de novo: a expiração e o anti-rollback impedem servir um feed antigo indefinidamente.

**Política de versão.** `GET /api/v1/clients/<client_id>/policy?version=<v>&os=<os>&channel=<canal>`, anônimo, responde `{"latest_version", "minimum_version", "update_required"}`: `update_required` é verdadeiro quando `version` está abaixo da versão mínima do canal. Versão inválida é 400; cliente ou canal desconhecido, 404.

**Administração.** Em `/platform/native-clients/`, o administrador da plataforma e o operador de releases (`updates.operate`) veem o que cada canal serve e o histórico de sincronização, definem a versão mínima de cada canal e retiram uma versão (o canal volta ao feed vivo anterior daquele sistema, e os arquivos só dela deixam de ser servidos). Cada POST exige CSRF, autenticação recente e a revisão mostrada; as mudanças entram na auditoria. Origem, tokens e chaves nunca aparecem na tela.

**Retenção.** Nesta versão nada é apagado: os instaladores ocupam o armazenamento até o operador limpá-los fora do núcleo. Planeje o espaço pelo número de versões por canal.

## Limites desta versão

- Não há aplicação automática; cada passo tem a sua janela de manutenção.
- Num servidor systemd sem `agent_root`, o agente não se atualiza sozinho; nos containers, só a partir da versão que traz o volume `agent_code`.
- Alvos sem ambiente no pacote continuam com o lock e o wheelhouse do operador.
- Apps do projeto e apps first-party não têm fonte própria.
- GitHub Enterprise Server não é testado (as chaves `api_url` e `download_hosts` existem).
- O aviso sem agente é só informativo; não há e-mail de aviso.
- Índice vencido bloqueia a fonte até o publicador renovar.

## Roteiro de verificação manual

Num servidor de teste Linux com systemd e PostgreSQL, com um repositório de teste (ou fork) e uma chave de teste:

1. Publique dois releases de teste em linhas diferentes pelo workflow "Artefatos de release".
2. Instale a primeira versão com o agente no leiaute `agent_root` (`agent-install`).
3. Envie pela tela o pacote de release da versão seguinte; confira o plano (`core_update`, dependências, migrations), confirme e veja a operação concluir.
4. Cadastre em "Fontes" o `dono/nome` do repositório de teste; "Verificar agora"; confira o caminho com dois passos e o botão só no primeiro.
5. Tente enviar direto o pacote da segunda linha; confira a recusa com o texto de salto de versão.
6. Atualize o agente pela tela; confira a versão nova em "Versões instaladas".
7. Conclua os dois passos; confira que a página da operação oferece o passo seguinte e que a faixa de aviso some no fim. Com três releases em linhas seguidas, escolha em "Atualizar até a versão" a do meio, conclua os passos até ela e confira que, ao chegar, nenhum passo do núcleo é oferecido (#653).
8. Rode `agent-rollback` e reinicie; confira a versão anterior do agente.
9. No sandbox (`compose.sandbox.yaml`, [sandbox no Coolify](deployment.md#sandbox-no-coolify-369)) recriado do zero: confira que a primeira partida instalou o release publicado mais novo, que `/platform/updates/` mostra o agente, o core e a fonte oficial, que a agenda, o livro contábil e a app de exemplo aparecem instaladas e que a cobrança sobe sem `skeleton.E037`; depois de um release novo, escolha-o como alvo e conclua os passos.
10. No sandbox, atualize o agente pela tela e confira que o container reinicia sozinho na versão nova e que `docker compose up -d --force-recreate agent` a mantém; com `SKELETON_AGENT_USE_IMAGE=1`, confira o agente da imagem; apague o virtualenv da versão do volume e confira, depois de 3 partidas, o agente da imagem e `agent.entry_fallback` no log (#772).
11. Confira nos logs do agente e do web que nenhum token, chave ou URL de download assinada aparece.
