# Atualizar o sistema base e as apps

Este guia orienta quem opera uma instalação: como atualizar o sistema base e as apps, de onde vêm as versões, como utilizar um fork e como agir quando algo falha. A referência completa do agente, das chaves de configuração e das rotas permanece em [deployment](deployment.md#agente-independente-de-deployment).

## Visão geral

O fluxo separa responsabilidades de ponta a ponta: quem publica assina os artefatos; o agente de deployment no servidor verifica e instala; e a tela da plataforma apenas solicita e exibe o andamento. Nenhuma mudança é aplicada sem que o operador examine o plano e confirme a execução, e cada passo corresponde a uma operação completa do agente.

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

## Plataforma e aplicativos

A interface divide o ponto de entrada entre plataforma e aplicativos, mas o motor de execução é único: o agente prepara o plano, o operador confere o impacto e aprova, e a execução é acompanhada na mesma tela — tanto para o núcleo quanto para uma app. Um plano do núcleo também pode incorporar apps compatíveis ("Adicionar ao plano").

| Área | No menu | O que faz |
|---|---|---|
| **Atualizações da plataforma** (`/platform/updates/`) | "Atualizações", no topo, logo abaixo de "Configuração da instância" | Gerencia o núcleo e o agente de deployment: versão instalada, versão mais nova, seletor "Atualizar até a versão", lista de passos e "Preparar atualização para X", painel de nós do cluster e detalhes técnicos recolhidos (geração, digest, inventário confirmado no banco e versões publicadas). Abas: Visão geral, Histórico, Fontes e Enviar pacote. |
| **Atualizações de aplicativos** (`/platform/updates/apps/`) | "Atualizações", no grupo Aplicativos | Lista atualizações disponíveis para apps instaladas, apps publicadas nas fontes ainda não instaladas ("Preparar instalação") e ações para apps instaladas — "Retirar o código" (preservando dados) e "Reinstalar código" (reativando dados preservados), sem exigir digitação de digest ou ID. Abas: Visão geral, Histórico e Enviar pacote. |

Ambas as áreas ficam visíveis para quem tem `updates.operate`, mesmo sem ser administrador completo. As abas não são numeradas por não formarem uma sequência fixa. A sequência existe dentro de cada plano, estruturada em quatro etapas:

1. **Escolher:** seleciona o passo, o pacote enviado ou a ação da app. A tela descreve a ação ("Atualizar Notas para 1.4.0") e exibe o botão "Conferir o impacto". Para combinar múltiplos pacotes em um único plano, use o formulário "Plano manual (avançado)" com operação, digests e IDs.
2. **Conferir o impacto:** o agente monta a prévia enquanto a página se atualiza automaticamente. O resultado abre com "O que muda": versões anteriores e novas dos componentes, indicação se o site sai do ar (conforme o modo do plano: janela de manutenção ou troca nó a nó), backup, mudanças no banco e comportamento em caso de falha. Detalhes de componentes, migrations, arquivos, recursos compartilhados e apresentação ficam recolhidos com suas contagens. "Continuar para a aprovação" grava o plano.
3. **Aprovar:** apresenta o mesmo resumo, o botão "Abra o acompanhamento" (o link privado só é pedido ao agente no clique) e "Aprovar e iniciar", que exige autenticação recente. "Descartar plano" encerra o plano sem alterar a instalação. Se o envio ao agente falhar após a aprovação — inclusive quando o operador forçou janela de manutenção (`mode="window"`) —, a retentativa reenvia a aprovação original preservando o mesmo `mode` e o mesmo UUID idempotente.
4. **Acompanhar:** mostra estado, etapa atual em texto, resultado final e, quando concluído, o próximo passo rumo ao alvo escolhido.

Por padrão, o histórico de cada área exibe apenas planos em aberto ou executados; o filtro "Descartadas, substituídas e vencidas" revela planos encerrados sem execução.

## Planos aguardando aprovação

Um plano salvo permanece no estado "Aguardando aprovação" até ser aprovado ou encerrado. Ele aparece no topo da área correspondente com os botões "Revisar e aprovar" e "Descartar". Nenhum plano fica pendente indefinidamente:

- **Substituído:** preparar um novo plano que altere os mesmos componentes (por exemplo, a mesma versão novamente) substitui o plano anterior, com aviso na tela.
- **Vencido:** quando outra operação é concluída e altera a instalação, todos os planos preparados anteriormente vencem, pois o agente recusaria aplicá-los. A tela de um plano vencido bloqueia a aprovação e oferece "Preparar de novo" para refazer o passo sobre o estado atual.
- **Descartado:** clicar em "Descartar" encerra o plano sem tocar na instalação; o mesmo passo pode ser preparado novamente a qualquer momento.

Todos os três encerramentos ficam registrados no histórico e na auditoria. Consulte os estados técnicos em [planos que não rodam](deployment.md#agente-independente-de-deployment).

O agente valida a assinatura do índice e de cada pacote contra as chaves que o operador aprovou no arquivo de configuração (`approved_publishers`); o GitHub atua apenas como entregador de bytes. A interface web nunca cadastra chaves ou tokens nem baixa arquivos diretamente — ela apenas comunica-se com o agente pelo socket de controle.

## Vocabulário

| Termo | Significado |
|---|---|
| pacote de componente | Arquivo `.skapp` com `kind` igual a `core` ou `app`. |
| pacote de release | Arquivo `.skapp` com `kind` igual a `release`: reúne em um único arquivo o pacote do core, os wheels das dependências, o lock e o wheel do agente ([formato](deployment.md#pacote-de-release)). |
| alvo | Combinação de interpretador e plataforma: `implementation_name`, `python_version`, `platform_machine` e `sys_platform` (ex.: `cp314-manylinux_2_28_x86_64`). |
| ambiente do publicador | Lock e wheels de um alvo, extraídos de um pacote de release e armazenados pelo agente. |
| índice de releases | Catálogo assinado, publicado como o asset `skeleton-index.json` do release mais recente. |
| fonte | Origem consultada pelo agente para descobrir releases: `path`, `url` ou `github` (definida no arquivo do agente ou cadastrada pela tela). |
| caminho de atualização | Sequência ordenada de versões entre a instalada e a mais recente alcançável. |
| passo | Uma operação completa do agente: plano, confirmação, manutenção, backup, migrations e cutover. |
| linha | Par `(major, minor)` de uma versão. |

## Atualizar por arquivo

1. Baixe o pacote de release (`skeleton-<versão>.release.skapp`) do release do GitHub e, se desejar, valide-o previamente com a chave pública do publicador:

   ```sh
   .venv/bin/python -m skeleton.system.packages validate skeleton-0.6.1.release.skapp \
     --trust-store /caminho/privado/publishers.json
   ```

2. Envie o arquivo em "Atualizações da plataforma" → "Enviar pacote". O agente valida o pacote completo e armazena tanto o pacote de release quanto o pacote do core embutido. A tela abre a etapa "Escolher" com "Atualizar o núcleo para X" (para um pacote de app, abre "Atualizar" se já instalada ou "Instalar" se for nova). Se o pacote não incluir o alvo deste servidor, a tela avisa que serão usados o lock e o wheelhouse configurados no agente.
3. Clique em "Conferir o impacto" para gerar a prévia. Revise "O que muda" e os detalhes de componentes, migrations, arquivos e, quando houver, "Dependências que deixam de ser instaladas".
4. Clique em "Continuar para a aprovação", abra o acompanhamento, aprove (com autenticação recente) e aguarde o estado `succeeded`.

Pelo shell do servidor, o mesmo procedimento equivale a:

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

1. Na aba "Fontes", cadastre o repositório (`dono/nome`) e escolha um dos publicadores aprovados no servidor. Quando ainda não há fonte GitHub cadastrada, o formulário sugere o repositório oficial (`alaorneto/skeleton`).
2. Clique em "Verificar agora" para consultar as fontes. "Atualizações da plataforma" exibe a última verificação, a versão instalada do núcleo, a mais nova e os passos intermediários. "Atualizações de aplicativos" mostra as informações equivalentes para cada app, além das apps publicadas ainda não instaladas.
3. No núcleo, o seletor "Atualizar até a versão" permite escolher qualquer versão alcançável a partir da instalada — listando a versão mais recente de cada linha do caminho (lida dos índices verificados, sem acesso à rede) e marcando a mais nova por padrão. Por exemplo, a partir de `0.3.0`, com `0.3.1`, `0.4.0`, `0.4.2` e `0.5.0` publicadas, os alvos são `0.3.1`, `0.4.2` e `0.5.0`; selecionar `0.5.0` mostra os passos `0.4.2 → 0.5.0`. Sem seleção explícita, o alvo é a mais recente alcançável. Apps avançam sempre até a versão mais nova.
4. Clique em "Preparar atualização para X" (disponível apenas no primeiro passo): o agente baixa o pacote e abre o plano. Confirme como no fluxo por arquivo — cada passo requer prévia e aprovação próprias, com autenticação recente e `updates.operate`; nada é aplicado automaticamente.
5. Ao término de cada passo, a tela oferece o próximo rumo ao alvo escolhido. Ao atingir o alvo, nenhum novo passo do núcleo é sugerido e a seleção é limpa.

A escolha de alvo permanece apenas na sessão do operador e não é tratada como dado confiável: a cada passo, a tela revalida que o alvo segue alcançável e que o passo solicitado é o primeiro até ele, enquanto o agente revalida assinatura, linha minor e ordem de versões. Caso um alvo deixe de ser alcançável (por índice expirado, versão removida ou falta de linha intermediária), o pedido é recusado com aviso — nunca substituído silenciosamente por outra versão.

Pelo shell, use os comandos `check-updates`, `update-status` (sem rede), `sources` e `fetch-release <component_id> <versão> <source_id>`.

## Passo a passo e a regra de linhas

O agente impede atualizações do núcleo que pulem uma linha minor (`version_jump_forbidden`), pois cada linha pode introduzir migrations e alterações que a linha seguinte pressupõe já aplicadas. Versões de correção (patch) podem ser puladas livremente.

Assim, partindo de `0.5.2` com `0.5.3`, `0.6.0`, `0.6.4` e `0.7.1` publicadas, o caminho é `0.5.2 → 0.6.4 → 0.7.1`; a partir de `0.9.x`, o passo seguinte é `1.0.x`. O publicador declara essa mesma regra em `from_versions` (a própria linha e a anterior, gerada por `skeleton-core manifest`), e o agente reconfere na instalação. Apps obedecem apenas ao próprio campo `from_versions`.

| Bloqueio | Significado | O que fazer |
|---|---|---|
| `missing_intermediate_release` | Falta publicar um release de uma linha intermediária. | Solicite ao publicador o release da linha ausente; não force o salto. |
| `incompatible_runtime` | Nenhuma versão mais nova é compatível com o Python ou com o núcleo desta instalação. | Atualize primeiro o runtime (ou o núcleo, no caso de apps). |

A chave `allow_version_jumps: true` no arquivo do agente desativa essa proteção. Trata-se de um recurso de **emergência**, não de rotina, pois aplica migrations de várias linhas de uma só vez sem passar pelos pontos de parada testados pelo publicador. Utilize-o somente com backup verificado, para uma única operação, e desligue-o logo em seguida.

## Usar um fork

- **Dono do fork:** gera uma chave própria (`python -m skeleton.system.packages keygen --private-key release.key --public-key release.pub.pem`), configura o ambiente `release` do repositório com o segredo `RELEASE_SIGNING_KEY` e a variável `RELEASE_PUBLISHER` (usando um ID próprio, diferente do oficial), publica os releases e distribui a chave pública ([publicação automática](deployment.md#publicação-automática-nos-releases-do-github)).
- **Operador do servidor:** adiciona a chave pública em `approved_publishers` no arquivo do agente e reinicia o serviço — a decisão de confiança é sempre do operador. Em deployments com container, cuja configuração inicial aprova apenas a chave oficial versionada em `deploy/release/publishers/`, basta definir `SKELETON_AGENT_PUBLISHER`, `SKELETON_AGENT_PUBLISHER_KEY` (caminho do PEM montado no container) e `SKELETON_AGENT_SOURCE` (`dono/nome` do fork) antes da primeira partida; após a primeira partida, edite `agent.json` diretamente ([primeira partida pelo agente](deployment.md#primeira-partida-pelo-agente-containers)).
- **Administrador da plataforma:** cadastra `dono/nome` do fork em "Fontes" e associa o publicador correspondente.

Trocar o publicador do núcleo altera apenas a origem e a chave confiável: o código já instalado, os dados e o histórico de migrations permanecem intactos. O agente aceita do novo publicador apenas versões compatíveis com o caminho de atualização, e o próprio agente só aceita auto-atualização vinda do publicador do núcleo instalado ou de um ID listado em `agent_publishers`.

## Repositórios privados

Crie um token de acesso com escopo mínimo (somente leitura de conteúdo nos repositórios desejados) e salve-o em um arquivo pertencente ao usuário do agente, com permissão `0600` e fora do diretório de releases.

Referencie o arquivo em `"github": {"token_file": "/srv/skeleton/private/github-token"}` e marque a fonte com `"private": true` (no arquivo do agente ou pela interface web, que só exibe essa opção quando há token configurado). O token é enviado exclusivamente ao host de `api_url`, nunca acompanha o redirecionamento de download e nunca aparece em erros, logs ou telas.

## Atualizar o agente

O agente informa sua versão no cartão "Agente de deployment" em "Atualizações da plataforma". Como cabe a ele validar e instalar o núcleo, tentar gerar prévia de um núcleo de linha superior à do agente falha com `agent_update_required`.

Quando `agent_root` está configurado, o agente se auto-atualiza a partir do pacote de release: cria um novo virtualenv sem acesso à rede, valida-o com `selfcheck`, troca o link simbólico `current` e encerra com código `75` para o supervisor reiniciá-lo (`Restart=on-failure`). Em containers, `agent_root` corresponde ao volume `agent_code` e o iniciador da imagem sobe a nova versão automaticamente; instalações em containers anteriores a esse recurso precisam de uma última atualização manual de imagem ([atualizar o agente nos containers](deployment.md#atualizar-o-agente-nos-containers-772)).

**O passo do núcleo começa pelo agente.** Em "Atualizações da plataforma", o cartão do núcleo avisa quando o próximo passo requer um agente de linha mais nova ("Este passo começa atualizando o agente de deployment (A → B)"). Com a auto-atualização ativa:

1. clicar em "Preparar atualização para X" baixa o pacote, solicita a atualização do agente (o clique registra o consentimento e apenas o digest segue para o agente) e abre uma tela de espera recarregada a cada 5 segundos;
2. apenas o agente reinicia — o site permanece no ar normalmente;
3. assim que o novo agente entra em operação (em todos os nós vivos, no caso de cluster), a página refaz a busca do passo pelo agente atualizado, que reaproveita e revalida o pacote já salvo, apresentando o plano do núcleo pronto para "Conferir o impacto" (preservando eventual escolha feita em "Atualizar até a versão").

O botão "Atualizar agente" em um plano de pacote enviado manualmente segue o mesmo fluxo e desembarca no plano do núcleo contido no pacote. A confirmação final do plano continua nas mãos do operador.

Se o novo agente não responder na versão esperada em até 30 minutos, a espera é encerrada com orientações de recuperação: em containers, o iniciador volta automaticamente ao agente da imagem após 3 partidas sem sucesso (verifique o log do serviço `agent`); em servidores systemd, execute `agent-rollback` e `systemctl restart skeleton-agent`. Caso o agente recuse a atualização (por exemplo, `agent_build_failed`), o erro é exibido em "Atualize o agente primeiro" sem deixar estado preso na sessão.

**Sem `agent_root`.** O agente expõe `agent.manual_update` no inventário, e as telas ("Atualize o agente primeiro", plano e "Atualizações da plataforma") adaptam as instruções ao ambiente:

- **Containers:** atualize a imagem do agente e recrie o serviço (`docker compose pull agent && docker compose up -d agent`; no Coolify, use Redeploy). A partir da imagem que utiliza o volume `agent_code`, o agente passa a se auto-atualizar.
- **systemd com virtualenv fixo:** a tela gera os comandos exatos de migração única para `agent_root` com os caminhos reais do servidor (executável, `agent.json`, raiz sugerida `/opt/skeleton-agent` e pacote já enviado), usando `agent-install --enable` e o drop-in do systemd ([migrar para `agent_root`](deployment.md#migrar-para-agent_root-775)). Feita a migração, passos futuros do núcleo atualizarão o agente automaticamente.
- Agentes antigos que não informam `manual_update` exibem o texto genérico de `agent-install`.

Para atualizar ou reverter manualmente em um servidor que já usa `agent_root`:

```sh
"$AGENT" --config "$CONFIG" agent-install --release /srv/skeleton/artifacts/skeleton-0.7.0.release.skapp --root /opt/skeleton-agent
sudo systemctl restart skeleton-agent
"$AGENT" --config "$CONFIG" agent-rollback --root /opt/skeleton-agent    # volta à versão anterior
```

**Em um cluster**, clicar em "Atualizar agente" em qualquer nó encaminha o pedido ao primário, que valida o pacote, grava a versão-alvo para todo o cluster e responde que a atualização entrou em rolagem. Cada outro nó com `agent_root` baixa o pacote do primário, revalida assinatura e publicador, monta o novo ambiente e reinicia; o primário se atualiza por último, após os demais nós vivos informarem a nova versão (ou após 15 minutos). Em containers com o volume `agent_code`, cada nó acompanha automaticamente como no systemd; em containers sem esse volume, o agente vem da imagem e precisa ser atualizado com uma imagem nova e `docker compose up -d`. A coluna **Agente** do painel de nós mostra a versão de cada nó e exibe um aviso quando há divergência — enquanto os nós vivos estiverem em versões diferentes, nenhuma atualização é iniciada (`agent_versions_differ`).

## Atualizar um cluster de vários nós

Em um cluster ([alta disponibilidade](high-availability.md)), a interface de qualquer nó encaminha a atualização ao agente primário. O primário prepara o release e cada seguidor recebe e valida o pacote antes de qualquer janela de manutenção. Só então todos os nós entram em manutenção e param, o primário executa o backup e as migrations, e cada nó faz a troca de release e valida sua própria saúde antes de o tráfego ser reaberto.

Em **Atualizações da plataforma**, o painel **Nós do cluster** exibe o agente primário, a época atual e, para cada nó, seu papel, release ativo, fase da operação em curso e estado de vida (sinalizando nós sem resposta há mais de 60 s). O botão **Reiniciar os nós em rolagem** reinicia os serviços nó a nó no mesmo release (útil para aplicar nova configuração), exigindo autenticação recente e CSRF. Se não houver primário, a tela informa que atualizações estão suspensas enquanto o site continua no ar; se o primário estiver inalcançável, houver operação pendente após troca de primário ou o cluster estiver ocupado, a mensagem orienta o procedimento.

A prévia indica em "O site sai do ar?" o **modo de atualização**:
- patches sem migrations, na mesma linha `major.minor` e sem alteração nas apps, executam nó a nó sem janela de manutenção (modo `rolling`), embora a tela de confirmação ofereça a opção **Forçar janela de manutenção**;
- todas as demais mudanças utilizam janela de manutenção em todos os nós.

O protocolo detalhado, o efeito de cada falha e os procedimentos de recuperação estão em [agente em vários nós](deployment.md#agente-em-vários-nós-678).

## Apps

Apps continuam distribuídas como pacotes de componente. Cada app publica seu índice em seu próprio repositório: `package build` gera o `.skapp`, e `package index --previous` acumula o índice anterior. Anexe ao release do GitHub o pacote e o asset `skeleton-index.json`, usando `artifact` no formato `<tag>/<arquivo>` (`--artifact-base <tag>`). Declare `migrations.from_versions` seguindo a mesma política do núcleo — a própria linha e a anterior ([extensão](extending.md#publicar-apps-e-releases-assinados)).

Após um passo do núcleo, qualquer app que se torne incompatível aparece na checagem de dependências da prévia, já com sugestão de versão quando disponível nas fontes.

Se uma app fornece temas da instância ([aparência](appearance.md#temas-trazidos-por-apps)), a prévia de desinstalação avisa quais temas sairão do catálogo e, caso um deles esteja ativo, informa que a instância retornará ao tema do núcleo (o aviso não altera o plano aprovado). Ao reinstalar a app, os temas retornam ao catálogo e, se nenhum outro tema tiver sido ativado nesse meio tempo, o tema dela volta a vigorar.

## Avisos

O agente consulta as fontes automaticamente a cada `update_check_interval` segundos (padrão de 6 horas; `0` desativa; primeira verificação 2 minutos após subir). Quem possui `updates.operate` vê nas páginas de `/platform/` uma faixa com o link correspondente à novidade ("Ver a atualização da plataforma" ou "Ver os aplicativos"), com atraso máximo igual ao intervalo do agente mais 5 minutos de cache. Nada é baixado nem aplicado sem intervenção. Esse mesmo perfil recebe na caixa da plataforma alertas de nova versão e de operações interrompidas ([alertas da plataforma](notifications.md#alertas-da-plataforma)).

Todo deployment suportado executa o agente e acompanha versões nas duas áreas de atualização. Fora dele (exclusivamente em desenvolvimento local), ativando `SKELETON_UPDATE_REPOSITORY`, o job `system.update_check` consulta o release mais recente e exibe um aviso **meramente informativo** com link para a página da release ([aviso sem agente](deployment.md#sandbox-no-coolify-369)).

## Dependências do projeto derivado

Quando um pacote de release contempla o alvo do servidor, o lock e os wheels das dependências do núcleo vêm do próprio publicador. Dependências exclusivas do projeto derivado ficam em `project_requirements_lock` (com os respectivos wheels em `wheelhouse`).

Antes de adotar o primeiro pacote de release, mova as dependências específicas do projeto de `requirements_lock` para `project_requirements_lock`; caso contrário, a prévia as apontará em "Dependências que deixam de ser instaladas". Em alvos não presentes no pacote, o agente recorre normalmente a `requirements_lock` e `wheelhouse`, recusando com `locked_wheel_unavailable` se faltar algum wheel.

## Segurança

1. Nada é instalado sem assinatura válida de um publicador aprovado pelo operador no servidor. A assinatura comprova procedência e integridade, não ausência de código malicioso: aprove apenas publicadores confiáveis.
2. A interface web define apenas **onde procurar** (`dono/nome`); o servidor define **em quem confiar** (`approved_publishers`). A interface nunca cadastra chaves, tokens, hosts, caminhos ou URLs.
3. Todo dado vindo da rede é tratado como não confiável: passa por limites de tamanho e validação de formato, nunca virando caminho ou comando diretamente.
4. Tráfego de saída: o agente comunica-se apenas com `api.github.com` (ou o `api_url` configurado), os hosts listados em `download_hosts` durante downloads (padrão `*.githubusercontent.com`) e os hosts das fontes `url`; o processo web só acessa `api.github.com` quando o aviso opt-in sem agente está ligado. Restrinja o firewall de saída a esses destinos.
5. Chaves privadas de publicação ficam fora da produção (no ambiente `release` do repositório) e tokens residem em arquivos privados do agente. Nenhum segredo aparece em logs, erros, journals ou telas.
6. Downgrade é proibido e saltos de linha minor são recusados por padrão.
7. Toda operação exige `updates.operate`, autenticação recente e confirmação explícita do plano — não existe aplicação automática.
8. A instalação ocorre inteiramente offline (`pip --no-index --require-hashes`).

**Rotação da chave do publicador:** gere uma nova chave, publique com um novo identificador de publicador e atualize `approved_publishers`; ao ser removida do arquivo, a chave antiga deixa de ser aceita.

## Solução de problemas

| Código | O que significa | O que fazer |
|---|---|---|
| `version_jump_forbidden` | O pacote está mais de uma linha minor à frente do núcleo instalado. | Siga o caminho indicado em "Atualizações da plataforma", um passo por vez. |
| `agent_update_required` | O agente está em uma linha anterior à do núcleo de destino. | Atualize o agente primeiro. |
| `agent_self_update_unavailable` | `agent_root` não está configurado. | Execute `agent-install` no servidor. |
| `release_without_agent` | O pacote de release não inclui o wheel do agente. | Utilize um pacote completo ou atualize o agente manualmente. |
| `agent_publisher_not_allowed` | O publicador do pacote difere do publicador do núcleo instalado e não consta em `agent_publishers`. | Verifique a procedência e adicione o ID em `agent_publishers` apenas se confiar nele. |
| `agent_already_current`, `agent_downgrade_forbidden` | A versão do agente no pacote é igual ou anterior à instalada. | Nenhuma ação necessária; reversões só ocorrem via `agent-rollback`. |
| `agent_target_unavailable` | O pacote não contempla a plataforma do interpretador do agente. | Solicite o alvo ao publicador ou instale o agente manualmente. |
| `agent_build_failed` | Falha na montagem ou no `selfcheck` do novo agente; o agente atual permanece ativo. | Consulte o log do agente no servidor. |
| `agent_rollback_unavailable` | Não existe versão anterior pronta para reversão. | Instale a versão desejada via `agent-install`. |
| `agent_config_not_writable` | `agent-install --enable` não tem permissão para gravar `agent.json`; nada foi alterado. | Execute o comando como o dono de `agent.json` (`skeleton-agent`), ou adicione `"agent_root"` manualmente e rode sem `--enable`. |
| `agent_root_conflict` | `agent-install --enable` recebeu uma raiz diferente do `agent_root` já configurado; nada foi alterado. | Informe a raiz gravada em `agent.json` ou execute sem `--enable`. |
| `agent_versions_differ` | Em um cluster, nós vivos rodam versões diferentes do agente; novas atualizações ficam bloqueadas. | Atualize o agente (via "Atualizar agente" nos nós com `agent_root`, incluindo containers com `agent_code`; manualmente nos demais). |
| `agent_update_manual` | Em um cluster, um nó sem `agent_root` (systemd antigo ou container sem `agent_code`) não acompanha a atualização sozinho. | Atualize o agente manualmente nesse nó (em containers, atualize a imagem e rode `docker compose up -d`). |
| `release_environment_conflict` | O mesmo núcleo foi recebido com outro conjunto de dependências. | O publicador precisa lançar uma nova versão. |
| `dependency_version_conflict` | `project_requirements_lock` fixa uma versão conflitante com uma dependência do núcleo. | Alinhe a versão com o lock do publicador. |
| `locked_wheel_unavailable` | Alvo ausente no pacote e wheel faltando no `wheelhouse`. | Disponibilize o wheel no `wheelhouse` ou use um pacote que inclua o alvo. |
| `package_verification_failed` | Assinatura de publicador não aprovado, arquivo adulterado ou formato inválido. | Confira a origem do pacote e a lista `approved_publishers`. |
| `catalogue_publisher_mismatch` | O índice da fonte foi assinado por outro publicador diferente do configurado. | Verifique o publicador associado à fonte. |
| `catalogue_verification_failed` | O índice falhou na verificação de assinatura, formato ou validade. | Índices vencidos bloqueiam a fonte até o publicador renovar (o workflow renova mensalmente). |
| `catalogue_generation_content_changed` | O conteúdo do índice mudou sem incrementar o número de geração. | Notifique o publicador; a recusa é proposital. |
| `source_index_missing` | O release mais recente ainda não possui `skeleton-index.json`. | Logo após uma publicação, aguarde alguns minutos até o workflow anexar o índice. |
| `github_rate_limited` | O GitHub atingiu o limite de requisições para este IP. | Aguarde, ou configure um token e evite intervalos menores que 1 hora. |
| `github_release_not_found` | O repositório não possui releases ou não está acessível. | Confira `dono/nome` e, para repositórios privados, o token configurado. |
| `github_token_missing` | Fonte privada sem arquivo de token legível. | Configure `github.token_file` com permissão `0600`. |
| `github_asset_missing` | Um asset listado no índice não existe no release. | Notifique o publicador. |
| `github_asset_url_rejected` | O download redirecionou para um destino fora da lista permitida. | Revise `download_hosts`; não amplie a lista sem justificativa. |
| `catalogue_artifact_path_rejected` | O índice aponta para um caminho fora do formato `<tag>/<asset>` ou do diretório da fonte. | Notifique o publicador. |
| `catalogue_unavailable` | A fonte não respondeu ou retornou resposta malformada. | Tente novamente mais tarde. |
| `invalid_source`, `source_id_reserved`, `publisher_not_approved`, `too_many_sources`, `source_not_found` | Cadastro de fonte recusado. | Corrija os dados no formulário; o publicador precisa estar aprovado no servidor. |
| `invalid_managed_sources` | O arquivo `sources.json` do agente está inválido (por exemplo, cita um publicador removido). | Corrija ou remova a entrada diretamente no arquivo do agente. |

**Plano marcado como "Vencida" ou "Substituída":** outro plano foi salvo para os mesmos componentes ou uma operação concluída alterou a instalação. Prepare o passo novamente pela área correspondente; o plano anterior permanece apenas no histórico ([planos aguardando aprovação](#planos-aguardando-aprovação)).

**"O plano mudou ou a instalação mudou desde a prévia" ao aprovar:** a revisão do plano ou a geração da instalação mudou. Recarregue a página do plano e, se tiver vencido, prepare uma nova atualização.

**Agente não sobe após auto-atualização:** execute `agent-rollback` e `sudo systemctl restart skeleton-agent`, e investigue o log do serviço.

## Clientes nativos (#606)

Clientes nativos (aplicativos desktop empacotados com Velopack) atualizam-se diretamente a partir do **servidor do núcleo**, em vez de consultar um repositório público: o operador configura a origem das releases, e o servidor sincroniza, valida assinaturas e serve feeds e pacotes com os bytes exatos. O download é anônimo por desenho — clientes antigos demais para autenticar ainda precisam se atualizar — e não contém dados pessoais. A implementação vive em `skeleton/system/native_clients/`.

**Configuração.** `SKELETON_NATIVE_CLIENTS_CONFIG` aponta para um arquivo JSON privado do operador (permissão `0600`, fora do repositório e do SQLite da instância); quando vazio, o recurso permanece desligado:

```json
{
  "trust_store": "trust.json",
  "clients": {
    "acme-desktop": {"repository": "acme/desktop", "token_file": "acme-github-token",
                     "channels": ["stable", "beta"], "publisher": "acme", "enabled": true}
  }
}
```

`trust_store` segue o mesmo formato dos pacotes (publicador → chave pública Ed25519), resolvendo caminhos relativos a partir do diretório do arquivo de configuração. `token_file` armazena um token do GitHub com permissão de leitura de releases (opcional para repositórios públicos). O publicador deve existir no trust store, ou a configuração é recusada. Definir `enabled: false` interrompe a sincronização e a distribuição daquele cliente, mantendo as instalações existentes na versão atual (rollback seguro).

**Protocolo de uma release.** Cada GitHub Release do repositório fornece, por sistema operacional (`<os>`, como `win`), o feed Velopack `releases.<os>.json`, os arquivos listados nele e o envelope assinado `releases.<os>.sig.json`:

```json
{"payload": {"client_id": "acme-desktop", "channel": "stable", "feed_sha256": "<sha256 do feed>",
             "signed_at": "2026-11-20T00:00:00Z", "expires_at": "2026-12-20T00:00:00Z"},
 "signature": "<base64 Ed25519 do JSON canônico de payload>"}
```

O JSON canônico segue a regra dos pacotes (`canonical_json`: chaves ordenadas, sem espaços, UTF-8). O vetor fixo em `tests/fixtures/native_clients/vector.json` (chave pública, feed, payload canônico e envelope) permite que o cliente teste e valide a mesma verificação.

**Sincronização.** Acionada manualmente por `python manage.py native_client_releases sync --client-id acme-desktop` (sem `--client-id`, sincroniza todos os clientes ativos) ou automaticamente pelo job periódico `native_clients.sync`, executado a cada `SKELETON_NATIVE_CLIENTS_SYNC_HOURS` horas (padrão `1`) quando a configuração existe. Para cada release (da mais antiga à mais nova) e cada envelope:

1. lê o feed e o envelope (limite de 1 MiB cada) exclusivamente por `skeleton.common.outbound_http`, no host fixo da API do GitHub; o download dos binários segue um único redirecionamento, apenas para `*.githubusercontent.com` e sem enviar o token;
2. valida o envelope: assinatura do publicador configurado, `client_id`, canal permitido, validade futura (`expires_at`) e SHA-256 do feed; recusa `signed_at` anterior ou igual ao do feed já publicado no mesmo canal e sistema (proteção anti-rollback); feeds repetidos são ignorados;
3. baixa os arquivos listados ainda não armazenados, calculando o SHA-256 em fluxo e rejeitando divergências de hash ou tamanho (limite de `SKELETON_NATIVE_CLIENT_MAX_FILE_BYTES`, padrão 1 GiB);
4. somente após todas as validações, grava feed e envelope e publica a release no canal.

Os binários ficam no armazenamento privado do núcleo (`STORAGES["skeleton_files"]`), endereçados por seu SHA-256 verificado (`native-clients/<cliente>/<xx>/<sha256>`), e nunca são sobrescritos. Cada feed aceito ou recusado gera uma linha auditada em `sk_system.NativeClientSync` com o código correspondente (`invalid_signature`, `untrusted_publisher`, `expired`, `stale_signature`, `feed_hash_mismatch`, `file_hash_mismatch`, `file_missing`, `invalid_feed`, `invalid_envelope`, `wrong_client`, `unknown_channel`); falhas de comunicação com a fonte registram `failed` sem publicar nada.

**Entrega anônima**, servida fora de `/api/v1/` (que aceita apenas JSON e impõe limite de corpo):

| Rota | Conteúdo |
|---|---|
| `GET /clients/<client_id>/<canal>/releases.<os>.json` | Feed vivo do canal e sistema: o mais recente não retirado e com envelope dentro da validade |
| `GET /clients/<client_id>/<canal>/releases.<os>.sig.json` | Envelope assinado correspondente ao feed |
| `GET /clients/<client_id>/<canal>/<arquivo>` | Arquivo referenciado por algum feed vivo do canal |

As respostas servem os bytes verificados com `ETag` (o SHA-256), `Content-Length`, `X-Content-Type-Options: nosniff` e `Cache-Control` (`public, max-age=60, must-revalidate` para feeds; `public, max-age=31536000, immutable` para arquivos); `If-None-Match` retorna `304`. Qualquer outro pedido retorna `404`. Aponte o Velopack do cliente para `https://<servidor>/clients/<client_id>/<canal>/`. Um envelope expirado retira o feed de circulação até que o publicador assine novamente, combinando expiração e anti-rollback para impedir que feeds antigos sejam servidos indefinidamente.

**Política de versão.** O endpoint anônimo `GET /api/v1/clients/<client_id>/policy?version=<v>&os=<os>&channel=<canal>` responde `{"latest_version", "minimum_version", "update_required"}`, marcando `update_required` como verdadeiro quando `version` está abaixo da versão mínima do canal. Versão inválida responde `400`; cliente ou canal desconhecido, `404`.

**Administração.** Em `/platform/native-clients/`, o administrador da plataforma e o operador de releases (`updates.operate`) acompanham o feed ativo de cada canal e o histórico de sincronizações, definem a versão mínima por canal e retiram versões (fazendo o canal recuar ao feed vivo anterior daquele sistema e deixando de servir arquivos exclusivos da versão retirada). Todo POST exige CSRF, autenticação recente e a revisão exibida, com registro em auditoria. Origens, tokens e chaves nunca aparecem na tela.

**Retenção.** Nesta versão, nenhum arquivo é apagado automaticamente: os instaladores permanecem no armazenamento até limpeza externa pelo operador. Dimensione o espaço considerando o número de versões mantidas por canal.

## Limites desta versão

- Não há aplicação automática de atualizações; cada passo tem sua própria janela de manutenção.
- Em servidores systemd sem `agent_root`, o agente não se auto-atualiza até a migração única (`agent-install --enable` e drop-in, executados como root); em containers, o recurso vale a partir da versão que inclui o volume `agent_code`.
- A espera automática pelo novo agente dura até 30 minutos; em um cluster, um nó cujo agente não informa `agent_version` em `GET /cluster` faz a espera aguardar até esse limite.
- Alvos ausentes no pacote de release continuam dependendo do lock e do wheelhouse mantidos pelo operador.
- Apps locais do projeto e apps first-party não possuem fonte de catálogo própria.
- GitHub Enterprise Server não é testado, embora as chaves `api_url` e `download_hosts` existam.
- O aviso sem agente é meramente informativo e não dispara e-mails.
- Índices expirados bloqueiam a fonte até que o publicador os renove.

## Roteiro de verificação manual

Em um servidor de teste Linux com systemd e PostgreSQL, usando um repositório de teste (ou fork) e uma chave própria:

1. Publique dois releases de teste em linhas minor distintas pelo workflow "Artefatos de release".
2. Instale a primeira versão configurando o agente com `agent_root` (`agent-install`).
3. Envie pela tela o pacote de release da versão seguinte; confira o plano (`core_update`, "O que muda", dependências e migrations), aprove e aguarde a conclusão.
4. Cadastre em "Fontes" o `dono/nome` do repositório de teste, clique em "Verificar agora" e confirme o caminho com dois passos e o botão habilitado apenas no primeiro.
5. Prepare o mesmo passo duas vezes até "Continuar para a aprovação" e confirme que o primeiro plano aparece como "Substituída" no histórico. Descarte o segundo e confirme "Descartada"; prepare novamente, aprove e confirme que um plano de app preparado antes passa para "Vencida".
6. Tente enviar diretamente o pacote da segunda linha e confirme a recusa por salto de versão.
7. Com um pacote contendo um agente de linha mais nova, confira o aviso no cartão do núcleo em "Atualizações da plataforma" e clique em "Preparar atualização para X": a tela de espera deve recarregar enquanto apenas o agente reinicia (com o site no ar) e desembocar no plano do núcleo; confira a nova versão no cartão do agente. Em um servidor com a unit antiga (virtualenv fixo), execute o bloco sugerido em "Atualize o agente primeiro" e confirme que o passo seguinte já atualiza o agente sozinho.
8. Conclua os dois passos e verifique que a tela da operação oferece o passo seguinte e que a faixa de aviso desaparece no final. Com três releases em linhas consecutivas, selecione a linha intermediária em "Atualizar até a versão", conclua os passos até ela e confirme que nenhum passo adicional do núcleo é sugerido ao alcançá-la.
9. Execute `agent-rollback`, reinicie o agente e confirme o retorno à versão anterior.
10. No sandbox (`compose.sandbox.yaml`, [sandbox no Coolify](deployment.md#sandbox-no-coolify-369)) recriado do zero, confirme que a primeira partida instalou o release publicado mais recente, que "Atualizações da plataforma" exibe agente, core e fonte oficial, que agenda, livro contábil e app de exemplo estão instaladas e que a cobrança sobe sem `skeleton.E037`. Após publicar um novo release, escolha-o como alvo e conclua os passos.
11. No sandbox, atualize o agente pela tela e confirme que o container reinicia automaticamente na nova versão e a preserva após `docker compose up -d --force-recreate agent`; com `SKELETON_AGENT_USE_IMAGE=1`, confirme que o agente da imagem é usado; remova o virtualenv da versão no volume e confirme que, após 3 partidas, o container recorre ao agente da imagem registrando `agent.entry_fallback` no log.
12. Em "Atualizações de aplicativos", instale uma app publicada, retire seu código e reinstale-a; confira em 390 px e em largura de desktop que nenhuma tela apresenta rolagem horizontal.
13. Verifique nos logs do agente e do web que nenhum token, chave ou URL assinada de download foi registrado.
