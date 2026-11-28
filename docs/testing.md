# Testes, fixtures e evidência

Este documento explica como verificar o projeto hoje, em ambiente descartável: todo comando e todo caminho citado existe no repositório atual. Os resultados de cada rodada, com os comandos da época, ficam registrados na [evidência histórica](testing-evidence.md). Um resultado antigo não certifica alterações posteriores nem uma instalação de outro projeto.

Antes de interpretar os resultados, vale alinhar três termos recorrentes:

- **Fixture**: o ambiente ou conjunto controlado de dados usado pelo teste.
- **Smoke**: um ensaio curto que exercita o fluxo real de ponta a ponta.
- **Assertion**: a condição verificada pelo teste para aprovar ou reprovar a execução.

Mocks e transportes controlados ajudam a testar respostas adversas, mas não provam que um serviço externo realmente autorizou ou executou o fluxo. Consulte [autorização e contexto](concepts.md#autorização-e-contexto) e [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) antes de ler os relatórios.

## Como escolher e interpretar a prova

| Pergunta | Prova necessária | O que não substitui essa prova |
|---|---|---|
| Contratos de registros e serviços funcionam? | A suíte inteira em PostgreSQL com a role runtime (`pytest tests`) e seus testes negativos. | Um teste que só exercita o ORM não prova a RLS: as assertions de isolamento usam a role runtime. |
| O runtime realmente isola organizações no banco? | PostgreSQL real, migrations aplicadas pela role owner e assertions com role runtime restrita. | Consulta como owner ou teste restrito ao ORM. |
| O login corporativo percorre o protocolo? | Navegador, IdP real, callback e sessão final, além de testes negativos de assinatura e estado. | `force_login`, página de login respondendo 200 ou JWT gerado localmente. |
| Atualização e recuperação funcionam? | Releases e pacotes reais, processos reiniciados, banco e restore no backend escolhido. | Renderizar o Compose, manter um Dockerfile ou testar apenas o planejamento. |
| O gov.br está homologado? | Credenciamento, login e logout no ambiente governamental autorizado. | Keycloak ou tokens RS256 locais. |

**Ordem para repetir qualquer verificação:** provisione dependências e serviços → confirme que o banco é descartável → carregue somente os secrets de fixture → execute o comando da suíte → registre backend, versões, comando e resultado observado → encerre os serviços descartáveis. Nunca exponha secrets para demonstrar que o ambiente existe.

## Integração contínua

O workflow `.github/workflows/ci.yml` executa, a cada push para `main` e em cada pull request, os procedimentos deste documento que não dependem de serviços externos. Um job verde vale como evidência apenas para o commit, o backend e as versões em que rodou.

| Job | O que prova | O que não prova |
|---|---|---|
| `lint` | Hooks do pre-commit em todos os arquivos (exceto Ruff e `commit-msg`, instalado com `--require-hashes` de `requirements/ci-tools.txt`), Ruff 0.17.4 com as regras de `pyproject.toml` (inclusive `DTZ`, `BLE` e `S`) e `ruff format --check`. | `E501` e `PLC0415`, que seguem fora da linha de base. |
| `assets` | `npm ci --ignore-scripts` sobre o lock de `frontend/` e `npm run check`: garante que `site.css` e `vendor/` correspondem às fontes e às versões fixadas de Tailwind, Flowbite e IBM Plex Sans. | Aparência no navegador; ver [interface e design](ui-design.md). |
| `locks` | `uv` 0.12.7 regenera `requirements.txt`, `requirements-dev.txt` e `requirements/ci-tools.txt` com os comandos `uv pip compile` de "Atualizar locks" do `README.md`, exigindo `git diff --exit-code`: os locks correspondem aos `.in` e foram gerados pelo comando oficial (cabeçalho, `pip` e `setuptools` fixados e hashes). | Que as versões resolvidas sejam as mais novas ou livres de vulnerabilidades (ver `build`), ou que `pyproject.toml` declare as mesmas faixas que `requirements.in`. |
| `typecheck` | `mypy` estrito com `django-stubs` nos módulos listados em `[tool.mypy] files`, incluindo `tools/workflow`, `tools/dev_postgres.py`, todo o agente de deployment, os serviços de acesso e identidade, os adapters, os tokens, os webhooks e a agenda (com `SKELETON_BUNDLED_APPS=calendar` para o plugin do Django carregar os modelos da agenda). Roda sempre simulando Linux (`platform = "linux"`), inclusive em execuções locais no Windows. | Os demais módulos, importados em silêncio até serem anotados. |
| `tests` | Dezesseis jobs paralelos, oito por versão do Python (3.12 e 3.14): seis fatias da suíte do núcleo (`pytest tests -n 4 --shard 1/6` a `6/6`) e duas das suítes da agenda e do livro contábil (`pytest tests/contrib -n 4 --shard 1/2` e `2/2`, com `SKELETON_BUNDLED_APPS=calendar,ledger`), todas com cobertura e equilibradas pelo custo dos testes. Provisiona o banco `ci_skeleton_test` e um banco por worker (`ci_gw0_skeleton_test` a `ci_gw3_skeleton_test`) via `deploy/postgres/bootstrap.sql`, de onde a role runtime herda `lock_timeout` e `idle_in_transaction_session_timeout` (um cluster local criado anteriormente precisa dos dois `ALTER ROLE` de [deployment](deployment.md#provisionamento-postgresql)). Valida lock com hashes e `pip check`, roda `manage.py check` e `makemigrations --check` na primeira fatia do núcleo e na primeira das apps, aplica migrations como `skeleton_owner` uma vez por job, roda assertions como `skeleton_runtime` e executa `tools/smoke_postgres.py` no Python 3.14. Juntas, as fatias cobrem a suíte inteira (`tests/unit/test_suite_shards.py`). | PostgreSQL de produção, backup e restore ou SSO real. O container de serviço roda sem `fsync` e não serve de prova de durabilidade. |
| `coverage` | Combina a cobertura de todas as fatias de cada Python (`coverage combine`) e publica o relatório e o artifact `coverage-postgres-py<versão>`, englobando o núcleo e as apps first-party (cujo código integra o pacote). Reprova abaixo do piso `fail_under` de `pyproject.toml` (81%, medido em 82% nos dois Pythons); as fatias rodam com `--cov-fail-under=0`. | Qualidade das assertions — cobertura mede apenas linhas executadas. |
| `deploy-check` | `check --deploy` com os settings de produção e valores descartáveis. | Conectividade real com banco, Redis ou SMTP. |
| `build` | Wheel do núcleo, wheel do agente instalado em um venv limpo com o lock (`pip check` e `--help`), `pip-audit` instalado com `--require-hashes` de `requirements/ci-tools.txt` em venv isolado sobre os dois locks, `docker build` e estágio `project-assets` sem `/project-source`. | Que a imagem sobe corretamente em produção. |
| `ci` | Falha se qualquer job acima falhou ou foi cancelado. É o check único e estável exigido na proteção de branch, independentemente da matriz. | Nada além dos outros jobs. |

Todos os jobs de `ci.yml` e `release-artifacts.yml` definem `timeout-minutes` acima da duração normal (cada fatia de `tests` é dimensionada para menos de 8 minutos e tem limite de 25; `compose-stack` leva cerca de 4 e tem limite de 30), e cada passo de `apt-get` tem limite de 10 minutos. O `apt` roda com `APT_OPTIONS` (`Acquire::Retries=3`, timeouts de rede de 30 s e até 120 s de espera pelo lock do `dpkg`), com uma segunda tentativa completa antes de falhar. Assim, um runner preso em um espelho do `apt` falha em minutos, em vez de deixar o check `CI` pendente por até 6 horas — basta reexecutar os jobs que falharam com `gh run rerun <id> --failed`. Já um job que estoura o tempo sem travar no `apt` indica que a suíte cresceu: divida-a em mais fatias (veja [Escrever testes que mantêm a suíte rápida](#escrever-testes-que-mantêm-a-suíte-rápida)) antes de aumentar o limite.

A suíte unitária também protege a documentação e os templates:

- `tests/unit/test_documentation_references.py`: valida links, âncoras, caminhos citados, comandos `manage.py`, o catálogo de serviços do núcleo e as fronteiras de código.
- `test_docs_app_snippets.py`: valida os exemplos de código de apps na documentação.
- `test_check_reference.py`: valida a tabela de referência dos checks.
- `test_template_safe_filter.py`: recusa `|safe` e `autoescape off` em componentes e fora da allowlist.
- `test_form_errors.py`: recusa acesso a `.message_dict` sem guarda em `skeleton/`.
- `test_parse_revision.py`: fixa o contrato único de parsing de `If-Match`.

Ao criar pacote, serviço, comando, check ou documento novo, atualize o catálogo correspondente no mesmo PR.

Quando o job `locks` falha, o log exibe o diff produzido pela regeneração. Regenere os locks com os mesmos comandos e a mesma versão do `uv` e commite o resultado no branch do PR. Com `-o`, o `uv` usa o lock existente como preferência e evita atualizar pacotes de carona. Um falso positivo raro ocorre quando o PyPI acrescenta um wheel novo a uma versão já travada (gerando um novo hash); a correção é a mesma. Nos PRs de pip abertos pelo Dependabot, o workflow descrito a seguir faz esse ajuste automaticamente.

### Locks dos PRs do Dependabot

O Dependabot altera apenas os arquivos `.in` ou regenera os locks no formato `pip-compile` (sem fixar `pip`/`setuptools` e mantendo extras nos nomes), sem opção para executar o comando oficial do README. O workflow `.github/workflows/dependabot-locks.yml` corrige esses PRs automaticamente com um commit no próprio branch:

1. **`regenerate`** (com `contents: read` e sem secrets): parte dos locks da merge-base do PR e executa `tools/workflow/dependabot_locks.py regenerate`. O script repete os dois `uv pip compile` do README (`uv` 0.12.7) com `--upgrade-package` restrito ao que o PR alterou: a versão de cada pacote mudado nos locks e o nome de cada linha nova nos `.in`. `--no-build` impede a execução de código de build de sdists, e `--custom-compile-command` grava o cabeçalho oficial para que o job `locks` não veja diff. Os locks resultantes são publicados como artifact.
2. **`commit`** (executado apenas quando há diff): valida o artifact usando o script da branch **base** (`check-artifact`: apenas os dois arquivos, cabeçalho oficial, quebras de linha LF, toda entrada `pacote==versão` com `--hash=sha256`, sem `--index-url`, `--find-links`, URL direta ou `-e`). Em seguida, copia os arquivos para o branch do PR e faz push usando o token de um GitHub App. Esse job não executa código do PR e aborta o push se o branch tiver mudado durante a execução.

O workflow só dispara quando o autor do PR e o ator do evento são `dependabot[bot]` e o branch pertence a este repositório — por isso o push feito pelo GitHub App não dispara nova regeneração. O uso de um GitHub App é necessário porque commits feitos com `GITHUB_TOKEN` não disparam o CI, deixando o PR sem checks no novo commit.

**Configuração (feita uma vez pelo responsável pelo repositório):**

1. Crie um GitHub App com permissão de repositório **Contents: Read and write** e nenhuma outra, sem webhook, instalado apenas neste repositório.
2. Gere uma chave privada para o app.
3. Em *Settings → Secrets and variables → Dependabot*, cadastre `LOCKS_APP_CLIENT_ID` (o Client ID do app) e `LOCKS_APP_PRIVATE_KEY` (o conteúdo do `.pem`). Workflows disparados pelo Dependabot só conseguem ler secrets dessa seção.

Sem esses secrets, o job `commit` falha com uma mensagem que aponta para esta seção, e a correção volta a ser manual. Como o Dependabot deixa de rebasear um PR que recebeu commit de outro autor, caso a `main` altere os locks antes do merge, comente `@dependabot recreate` no PR: o Dependabot recria o branch e o workflow regenera os locks novamente. Esse fluxo foi reproduzido contra um PR real do Dependabot, produzindo byte a byte o mesmo lock gerado manualmente.

**Detalhes do PostgreSQL no job `tests`.** O job concede `CREATE ON DATABASE` ao owner da fixture em cada banco, para que `reset_schema()` e `restore_schema()` possam recriar o schema `skeleton`. As migrations são aplicadas de verdade uma vez por job: `SKELETON_TEST_TEMPLATE_CACHE` aponta para um diretório vazio do próprio runner, o primeiro worker migra e os outros três restauram o template gerado por ele. Nenhum template é reaproveitado de outra execução ou de outro job. Cada teste parte desse template, recriado ou reaproveitado (veja [Isolamento da suíte](#isolamento-da-suíte)), enquanto `tools/smoke_postgres.py` continua executando `migrate` de verdade.

O container do PostgreSQL recebe `fsync=off`, `synchronous_commit=off`, `full_page_writes=off` e `max_locks_per_transaction=2048` via `POSTGRES_INITDB_ARGS`: o banco é descartável, e a tabela de locks precisa comportar quatro workers recriando o schema simultaneamente. As senhas são exclusivas do CI e valem apenas para o container de serviço. SSO real, gov.br e o agente de deployment em host não rodam no CI. Para alterar o número de fatias de uma suíte, adicione ou remova entradas `slug` e `include` na matriz e atualize o denominador `N` de `shard` em todas as fatias daquela suíte — nenhuma lista manual de arquivos ou tempos precisa ser mantida, e pacotes novos com testes entram automaticamente nas fatias.

Fora de Linux e macOS, os testes marcados com `posix` (verificação de artefatos assinados, que depende de `os.O_NOFOLLOW`) e os módulos do agente (que usam `fcntl`) são pulados com motivo explícito, sem falhar na coleta. Apenas a verificação de artefato assinado exige POSIX: validação de manifesto, versionamento e grafo de dependências rodam em qualquer sistema (`tests/unit/test_manifest_validation.py`, `tests/unit/test_versioning.py`, `tests/unit/test_dependencies.py`, `tests/unit/test_package_robustness.py`). A fixture de renderização do Compose é pulada quando o CLI `docker` não está disponível.

**Controles do agente de deployment.** Seis módulos fixam os controles de segurança e recuperação do agente, sem banco, rede, Docker nem comando privilegiado. Eles rodam apenas em Linux e macOS (no Windows aparecem como pulados e não contam como executados). As fábricas compartilhadas — configuração completa com arquivos privados, serviço sem `__init__` com journal real, operação vinculada a um plano e requisição HTTP crua — ficam em `tests/unit/agent_helpers.py`.

| Módulo | O que prova |
|---|---|
| `test_agent_control_api.py` | O `ControlHandler` real sobre um par de sockets: token Bearer (ausente, errado, arquivo curto ou legível por outros), autenticação antes da leitura do corpo, respostas 411/413/415, rejeição de `Transfer-Encoding`, JSON que não é objeto, campos desconhecidos por rota, query string, rota desconhecida e corpo de erro restrito a código e `details`. |
| `test_agent_status_tickets.py` | Tickets HMAC (operação, validade de 5 minutos, assinatura, tamanho, revogação por época e chave privada) e página de status (apenas o resumo, HTML escapado, cabeçalhos, prefixo e ausência de URL sem `status_http`). |
| `test_agent_config.py` | `private_file`, chaves e caminhos obrigatórios, `project_paths`, comandos com executável absoluto, separação entre runtime e owner (`MIGRATION_DATABASE_URL` fora do runtime e do build, DSN compartilhado, exigência de PostgreSQL, `backup_env_file` apontando para o mesmo banco e role), token e `read_environment` sem shell nem expansão. |
| `test_agent_releases.py` | `parse_lock` (somente `==` com hash, marcadores e duplicatas), `copy_project` sem symlinks e sem `.env`, `.venv`, SQLite nem apps gerenciadas, detecção de drift, classificação de donos, `freeze` e arquivos especiais. |
| `test_agent_recovery.py` | `reconcile_interrupted`, regras de `recover` (`abort`, `restore` e `complete`, reconhecimento de escritas, alteração do plano de migrations, manutenção preservada e erro no journal) e validação do backup antes da restauração. |
| `test_agent_catalogue.py` | Download do catálogo: HTTPS sem credenciais nem fragmento, mesma origem, sem redirecionamento, limite de tamanho, falha de rede, fonte local sem escape de diretório e isolamento para que uma fonte inválida não derrube as demais. |

## PostgreSQL local de desenvolvimento (#145)

Toda a suíte roda em PostgreSQL, exatamente como no CI. O script `tools/dev_postgres.py` sobe um servidor local descartável sem depender de Docker:

```sh
python tools/dev_postgres.py start                 # inicializa (na primeira vez), sobe e provisiona
eval "$(python tools/dev_postgres.py env)"          # POSIX; no PowerShell: python tools/dev_postgres.py env --shell powershell | Invoke-Expression
SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -n auto
python tools/dev_postgres.py stop
```

O servidor usa o pacote `pgserver` (binários do PostgreSQL distribuídos em wheel). Como o `pgserver` publica wheels apenas para CPython 3.12, o script o executa via `uv` em um ambiente isolado com Python 3.12, fora do virtualenv do projeto e sem alterar os locks.

Por padrão, os dados ficam em `var/dev-postgres` (ignorado pelo Git). Quando `SKELETON_TEST_POSTGRES_DATA=<pasta>` está definida, eles vão para `<pasta>/<nome do checkout>-<hash do caminho>`, mantendo um servidor por checkout ou worktree sob a mesma pasta raiz (veja [O que limita a suíte inteira](#o-que-limita-a-suíte-inteira)).

Ao rodar `start`, o script:

- provisiona `local_skeleton_test` com `deploy/postgres/bootstrap.sql` e senhas locais, no mesmo layout do CI;
- cria um banco por worker paralelo (`local_gw0_skeleton_test`, `local_gw1_...`, um por CPU ou conforme `--workers N`), sem tocar nos bancos que já existem;
- configura o servidor para velocidade em ambiente descartável: `fsync`, `synchronous_commit` e `full_page_writes` desligados, `max_locks_per_transaction=2048` e `shared_buffers=512MB` (reiniciando uma vez na mesma porta se um dos dois últimos mudar);
- copia os dados de fuso horário do pacote `tzdata` quando o build do `pgserver` não os inclui, e exporta `SKELETON_TEST_PG_DUMP` para o template de schema.

Por rodar sem garantias de durabilidade, esse servidor nunca deve armazenar dados importantes. Além disso, duas execuções da suíte não podem usar os mesmos bancos ao mesmo tempo, pois cada teste recria ou limpa o schema `skeleton` do seu worker.

A suíte de SSO real (`tests/sso`, com Keycloak e navegador) só é coletada quando `SSO_FIXTURE_ENV_PATH` está definida; sem ela, `pytest tests` a ignora. Os testes marcados com `needs_example_app` são pulados sem `SKELETON_ENABLE_EXAMPLE_APP=true`; com `SKELETON_TEST_REQUIRE_EXAMPLE_APP=1` (ativo no CI), a ausência da app gera erro de uso e interrompe a execução antes do início.

### Rodar rápido e rodar completo

| Para | Comando | Tempo |
|---|---|---|
| O arquivo ou teste em que você está trabalhando | `python -m pytest tests/integration/test_x.py -q -x` (`-k nome` escolhe um teste; `-x` para na primeira falha) | Um teste com banco: 2 a 3 s com o template em cache; mais 11 a 20 s quando o código mudou e a suíte migra novamente. |
| Só o que falhou na última vez | `python -m pytest tests -q -n auto --lf` | Proporcional aos testes que falharam. |
| Testes sem banco | `python -m pytest tests/unit -q -n auto` | Menos de 1 minuto. |
| Suíte inteira, obrigatória antes do PR | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -n auto` | 7 a 10 minutos na estação de referência sozinha na máquina (7 min 13 s e 9 min 37 s com `-n 8`; 9 min 49 s com `-n 16` em 2026-11-23; veja [O que limita a suíte inteira](#o-que-limita-a-suíte-inteira)). |
| Suíte inteira com cobertura, como no CI | O mesmo comando acima com `--cov` | O mesmo tempo, somado ao custo da medição. |
| Agenda | `SKELETON_BUNDLED_APPS=calendar python -m pytest tests/contrib/calendar -q -n auto` | Proporcional aos testes com banco (267 na agenda): alguns minutos. |

Sem `-n`, a suíte roda em um único processo e um único banco — ideal para depurar com `--pdb` ou inspecionar a saída de um teste. O comando rápido serve para iterar durante o desenvolvimento, mas não substitui a suíte inteira antes do PR, pois não comprova que os demais módulos continuam intactos.

**Como a paralelização funciona.** A opção `-n` vem do `pytest-xdist` (presente no lock de desenvolvimento) e distribui os testes entre N processos. Cada worker recebe um banco exclusivo, com nome derivado das duas URLs de teste (`local_skeleton_test` vira `local_gw3_skeleton_test` no worker `gw3`), e devolve o schema ao template antes de cada teste, como no modo serial. A migração roda uma única vez por execução: o primeiro worker migra e os demais aguardam para ler o template gerado por ele. O `tests/conftest.py` reescreve as duas variáveis de ambiente dentro de cada worker, garantindo que settings, `disposable_database_pair()`, testes que leem o ambiente e subprocessos enxerguem o mesmo banco. As fixtures continuam sem criar banco nem role.

Antes de iniciar os workers, a suíte verifica se o owner consegue abrir o banco de cada worker e se a tabela de locks do servidor suporta restaurações simultâneas. Cada restauração segura cerca de 2.100 locks até o commit, enquanto o padrão do PostgreSQL (6.400 no total) comporta apenas dois workers. Se faltar algum requisito, a suíte para com erro de uso indicando o que provisionar. Em um PostgreSQL fora de `tools/dev_postgres.py`, crie os bancos `<prefixo>_gwN_skeleton_test` seguindo o banco base (bootstrap, senhas e `GRANT CREATE ON DATABASE`) e eleve `max_locks_per_transaction`.

**Template em cache entre execuções.** Migrar o schema custa entre 11 e 20 segundos. Com `-n`, uma trava de arquivo no diretório do cache garante que apenas um worker migre enquanto os demais aguardam o template pronto. Se o processo detentor da trava morrer, ela é liberada; se a trava não puder ser usada, ou após 10 minutos de espera, cada worker migra por conta própria. Quando nada que afete o schema mudou desde a execução anterior, o template salvo em `var/test-template-cache` é restaurado sem migrar (veja [Isolamento da suíte](#isolamento-da-suíte)). Editar apenas testes preserva o cache; editar código do núcleo, de uma app, settings ou trocar de servidor força uma nova migração. Definir `SKELETON_TEST_TEMPLATE_CACHE=0` desliga o cache (útil para depurar migrations, fazendo cada worker migrar); qualquer outro valor altera o diretório de cache.

Para escrever testes novos sem degradar o tempo da suíte nem quebrar a execução paralela, consulte [Escrever testes que mantêm a suíte rápida](#escrever-testes-que-mantêm-a-suíte-rápida).

A opção `--shard I/N` (`tools/suite_shards.py`) executa apenas uma fatia dos arquivos de teste. As fatias são equilibradas por custo estimado: cada teste que pede `skeleton_db` equivale a 25 testes sem banco, o peso de um arquivo é a soma de seus testes e os arquivos são distribuídos, do mais pesado ao mais leve, sempre para a fatia mais leve até o momento. Como o peso vem da própria coleta, não há arquivo manual de tempos para manter. Esse recurso existe para o CI distribuir a carga entre máquinas; localmente, `-n` é suficiente.

Os tempos medidos estão registrados em [Suíte de testes abaixo de 10 minutos de novo](testing-evidence.md#suíte-de-testes-abaixo-de-10-minutos-de-novo-epic-758-756-757-760) e, na rodada anterior, em [Suíte de testes em menos de 10 minutos](testing-evidence.md#suíte-de-testes-em-menos-de-10-minutos-epic-199).

### O que limita a suíte inteira

Cada teste com banco parte do schema `skeleton` migrado, e recriar esse schema inteiro tem custo alto. Em 2026-11-23, o schema tinha 107 tabelas, 518 índices e 63 tabelas TOAST — cerca de 760 arquivos criados e 760 removidos no diretório de dados do servidor a cada recriação. O núcleo contava com cerca de 2.000 testes com banco, e a agenda somada ao livro contábil, com 365. Enquanto todos os testes recriavam o schema, o tempo total era governado pela taxa de recriações do servidor, que depende do disco e não da quantidade de núcleos.

Hoje, a suíte só recria o schema quando o teste anterior do mesmo worker alterou sua estrutura; nos demais casos, remove as linhas gravadas e valida o estado resultante contra o template (veja [Isolamento da suíte](#isolamento-da-suíte)). Na estação de referência, sozinha na máquina, a suíte do núcleo (4.157 testes, cerca de 2.070 com banco) levou 7 min 13 s e 9 min 37 s com `-n 8` em duas medições de 2026-11-23: 91% dos testes com banco reaproveitaram o schema, com mediana de 0,16 a 0,43 s contra 2,6 a 2,8 s de uma recriação completa. Com 16 workers o tempo não caiu (9 min 49 s), pois o gargalo passou a ser a CPU. Já as apps first-party (365 testes com banco) caíram de 5 min 05 s para 1 min 41 s. No CI o ganho é menor porque o runner Linux, com `fsync` desligado e sem antivírus, já recriava o schema rapidamente.

Alguns fatores continuam impactando diretamente o tempo local:

- **Recriar continua caro e limitado pelo disco.** Na estação de referência (Windows 11, 8 núcleos e 16 threads, NTFS com antivírus em tempo real), uma recriação isolada leva de 1,3 a 1,4 s, e o servidor satura em 2,5 a 3 recriações por segundo tanto com 8 quanto com 16 workers. Como qualquer teste que executa DDL (tabela, índice, policy, trigger ou grant) obriga o teste seguinte do worker a recriar o schema, restrinja DDL de prova aos testes que realmente dependem dele.
- **Execuções simultâneas competem pela mesma máquina,** mesmo usando checkouts e servidores separados. Rodar a suíte inteira em paralelo em várias sessões (de pessoas ou agentes) chegava a estender a execução para 30 a 60 minutos. Durante o desenvolvimento, rode apenas os arquivos afetados e reserve a suíte inteira para uma execução por vez. Encerre servidores de checkouts inativos com `tools/dev_postgres.py stop`.
- **O antivírus em tempo real inspeciona cada arquivo criado.** Nas medições, durante as recriações, o antivírus consumiu mais CPU do que o próprio PostgreSQL. O ganho mais expressivo para recriações vem de manter o diretório de dados fora dessa inspeção — em uma pasta excluída da verificação em tempo real, em um volume de desenvolvimento ou em sistema de arquivos Linux. A variável `SKELETON_TEST_POSTGRES_DATA` centraliza os servidores de todos os checkouts e worktrees na mesma pasta, permitindo cobrir todos com uma única exclusão. Essa decisão cabe a quem administra a máquina: nenhum script do projeto altera configurações de antivírus, e o ganho específico de cada alternativa não foi medido aqui.
- **As garantias de isolamento não mudam.** Todo teste continua partindo do template, sem herdar linhas, sequências nem estrutura de outro teste, e as provas continuam em PostgreSQL com a role runtime (veja [O que não fazer para ganhar tempo](#o-que-não-fazer-para-ganhar-tempo)). Definir `SKELETON_TEST_SCHEMA_REUSE=0` força a recriação do schema em todos os testes. No CI, a suíte também é dividida entre máquinas por fatias.

### Migrar e conferir um banco local

Os comandos `manage.py` abaixo atuam sobre o banco configurado nos settings e nas variáveis de ambiente — e não sobre os bancos isolados da suíte. Execute-os somente em um banco local descartável:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate python manage.py migrate
python manage.py sync_table_catalog
python manage.py check_skeleton_schema
```

Para o ensaio manual após migrar:

- Execute o bootstrap informando a senha via `getpass` ou com um secret descartável forte; repita o bootstrap e o `sync_table_catalog` para confirmar que os UUIDs são preservados e a senha não é redefinida.
- Suba o `runserver` e exercite no navegador o login real, perfil, dashboard, convites, papéis e grupos, a app opt-in Notes e a auditoria.
- Use o cookie e o CSRF dessa sessão real no smoke de API — nunca `force_login` como prova de autenticação. Ao encerrar, confirme o estado final no navegador e feche as abas abertas.

## Isolamento da suíte

O isolamento impede que um teste apague dados de outro teste ou de uma instalação real. Nesse desenho, **runtime** é a role restrita usada nas verificações de aplicação e RLS, enquanto **owner** é a role privilegiada usada exclusivamente para reconstruir o schema da fixture. Um banco não se torna seguro apenas pelo nome: as URLs, o sufixo do banco e o marcador de reset precisam atender simultaneamente a todas as barreiras abaixo.

O arquivo `tests/conftest.py` fornece a fixture `skeleton_db`, usando `pytest-django` apenas para carregar settings e desbloquear explicitamente o ORM. Não utilize criação ou flush automáticos, `django_db`, `TransactionTestCase` com flush nem `--reuse-db` como atalho em schema protegido. O `pytest-xdist` é suportado apenas com um banco provisionado por worker, para que dois processos nunca compartilhem o mesmo schema.

No PostgreSQL, `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) devem apontar para o **mesmo banco descartável**, cujo nome termine em `_skeleton_test` e seja diferente de qualquer `DATABASE_URL` ou `MIGRATION_DATABASE_URL` normal. Além disso, é obrigatório definir `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`.

Com `pytest -n`, cada worker utiliza o banco `<prefixo>_gwN_skeleton_test`, derivado dessas duas URLs por `skeleton/testing/database.py` (reexportado por `tools/integration_database.py`). Como o sufixo é preservado, todas as barreiras continuam válidas. Um worker que não trocar de banco é recusado por `disposable_database_pair()`, e a ausência de qualquer banco de worker interrompe a suíte antes de executar testes (veja [Rodar rápido e rodar completo](#rodar-rápido-e-rodar-completo)).

Antes de cada teste, a fixture encerra conexões pendentes e, usando a role owner, devolve **somente o schema `skeleton`** ao estado do template — recriando o schema ou, quando o teste anterior apenas gravou linhas, limpando essas linhas e conferindo o resultado contra o template. Em seguida, todas as verificações de aplicação e RLS voltam a usar a role runtime. A fixture não cria bancos nem roles e não toca em dados fora do schema; operações de DDL e reset são serializadas. Se faltar DSN, sufixo ou marcador, corrija o ambiente: a ausência de qualquer requisito nunca autoriza reset automático nem desativação da barreira.

As barreiras (`disposable_database_pair`, `reset_schema` e a verificação do owner) vivem em `skeleton/testing/database.py`, distribuídas no wheel, e são reexportadas por `tools/integration_database.py`. Esse mesmo módulo fornece a fixture `skeleton_db` para projetos derivados: ela migra uma vez por processo, captura o schema migrado como template via `pg_dump`, restaura-o antes de cada teste, devolve a conexão à role runtime e mantém apps fechadas por padrão; em workers do `pytest-xdist`, seleciona automaticamente o banco do worker antes do primeiro teste. A suíte do núcleo mantém sua própria fixture sobre essa base, acrescentando cache do template entre execuções, reaproveitamento do schema entre testes e o comportamento de `closed_app_defaults`. As variáveis da fixture distribuída estão detalhadas em [Fixture distribuída em paralelo e com template](#fixture-distribuída-em-paralelo-e-com-template).

**Template da sessão.** O schema de cada teste vem da fixture `postgres_template`, que aplica as migrations uma única vez e captura estrutura e dados do schema com `pg_dump --schema=skeleton` rodando como `skeleton_owner`. Quando precisa recriar o schema, `restore_schema()` apaga o schema, recria-o e executa o SQL capturado em uma única transação como owner — substituindo o custo anterior de cerca de 4 s por teste para rodar `migrate`. Durante a restauração, `GRANT CREATE` é concedido temporariamente a `skeleton_audit_writer` para transferir a ownership das funções de auditoria, repetindo o que faz `InstallRecordInfrastructure`. A transação do owner roda com `synchronous_commit=off`, pois um schema de fixture perdido em queda é simplesmente recriado no teste seguinte.

Esse template não equivale a `--reuse-db`: nenhum teste herda dados de outro. `tests/integration_postgres/test_schema_template.py` compara o schema restaurado com o migrado — tabelas, colunas, constraints, índices, triggers, funções, policies, flags de RLS, owners, ACLs, sequências e dados — e comprova que a verificação falha se faltar uma policy, trigger, grant, `FORCE RLS` ou linha. Na comparação de ACLs, consideram-se os privilégios efetivos (uma ACL `NULL` equivale ao `acldefault()` do owner), porque um `REVOKE` pode gravar uma ACL explícita idêntica ao padrão que o `pg_dump` omite e o restore recria como `NULL`. Da mesma forma, expressões de `CHECK` e índices com predicado ou expressão passam por uma verificação de ida e volta — o fingerprint recria cada `CHECK` (como `NOT VALID`) e cada índice em uma transação revertida e compara o texto devolvido pelo PostgreSQL, neutralizando casts movidos sem mudança semântica (como `IN` sobre `varchar`).

**Reaproveitamento do schema entre testes.** Como recriar o schema gera cerca de 760 criações e 760 remoções de arquivos no servidor, mas a imensa maioria dos testes apenas grava linhas sem tocar na estrutura, a fixture do núcleo só recria o schema quando necessário (`template_schema()` em `tools/integration_database.py`). Logo após uma restauração completa, ela registra o estado do template: linhas semeadas pelas migrations, valor de cada sequência, flags de RLS, estado dos triggers e um marcador de catálogo. Antes do teste seguinte do mesmo worker, como `skeleton_owner`, ela executa quatro passos:

1. **Confere o marcador do catálogo.** Para cada catálogo do schema (tabelas, colunas, defaults, constraints, índices, triggers, policies, funções, tipos, valores de enum, sequências, regras, privilégios padrão e dependências do próprio schema), compara a contagem de linhas, o máximo e a soma de `xmin`. Como todo DDL insere, altera ou remove entradas de catálogo com o ID da transação que o executou, qualquer divergência dispara a recriação completa do schema.
2. **Apaga as linhas em uma única transação.** Nas tabelas com conteúdo gravado, suspende temporariamente `FORCE ROW LEVEL SECURITY` e os triggers de usuário, remove as linhas em um único comando (preservando, nas tabelas semeadas, apenas as linhas do template) e devolve todas as sequências aos valores do template.
3. **Confere e recompõe linhas semeadas.** Compara contagem e MD5 das tabelas semeadas com o template. Se um teste alterou ou removeu alguma linha semeada (como na preparação que abre apps first-party em `sk_system_appavailability`), a fixture reescreve as linhas originais a partir da cópia feita após a restauração e compara novamente; qualquer diferença desfaz a transação e recria o schema.
4. **Religa e confirma.** Religa os triggers e `FORCE ROW LEVEL SECURITY` na mesma transação e, após o commit, verifica se as flags de RLS e o estado de todos os triggers correspondem exatamente ao template.

Diante de qualquer erro ou incerteza — trava mantida por conexão deixada aberta (a limpeza aguarda 5 s e desiste sem deixar estado parcial), queda da conexão do owner, tabela particionada ou tabela de outro dono — a fixture recorre à restauração completa. A cada 25 reaproveitamentos (`SKELETON_TEST_SCHEMA_REUSE_VERIFY=N`; `0` desliga), o schema passa pela comparação integral de fingerprint contra o template e todas as tabelas são lidas sem RLS; qualquer diferença caracteriza defeito no reaproveitamento e falha a execução em vez de recriar em silêncio. Para investigar se um teste falha apenas quando acompanhado de outros, rode com `SKELETON_TEST_SCHEMA_REUSE=0`, que volta a recriar o schema a cada teste.

Ao final, toda execução imprime o resumo consolidado entre workers e os motivos de cada recriação de schema mantido:

```text
skeleton_db schemas: 1 migrated, 7 from the template cache, 190 recreated, 1870 reused
skeleton_db recreated a kept schema because: the structure of the schema changed (175); ...
```

Se um arquivo de teste novo elevar o número de recriações, o motivo aparece nesse resumo. Quando o schema não puder ser reaproveitado de forma alguma (tabela de outro dono ou particionada, trigger desligado ou linhas semeadas sem chave primária simples), a primeira linha indica o impedimento.

Esse mecanismo não é `--reuse-db`, flush nem `TRUNCATE`: nenhum teste herda linha, sequência ou estrutura de outro, nenhum teste precisa limpar dados manualmente e toda a limpeza roda fora do teste, como owner, validada contra o template. `tests/integration_postgres/test_schema_reuse.py` comprova que:

- após gravar organização, papéis, auditoria e linhas extras em tabela semeada, o schema reaproveitado mantém o fingerprint exato do migrado, sem linhas residuais (lido sem RLS) e com sequências restauradas;
- RLS e auditoria continuam ativos no teste seguinte;
- cada um de 26 tipos de mudança estrutural altera o marcador do catálogo;
- linhas semeadas alteradas ou removidas (em uma ou duas tabelas) retornam ao estado exato do template, e qualquer cópia inexata força recriação em vez de passar;
- policy removida, `FORCE ROW LEVEL SECURITY` suspenso ou schema recriado externamente são recusados sem limpeza e redirecionados para restauração completa;
- uma trava de outra conexão aborta o reaproveitamento sem deixar flags ou triggers alterados.

A fixture distribuída (`skeleton.testing.database.skeleton_db`) não faz reaproveitamento e continua recriando o schema a cada teste.

**Cache em disco do template.** Entre execuções locais, o template pode ser lido de `var/test-template-cache` (ignorado pelo Git). A chave é um SHA-256 de tudo o que molda o schema migrado: todos os arquivos de código e dados de `skeleton/`, `config/`, `applications/` e `examples/`, além de `tools/integration_database.py` e `tests/conftest.py`, `INSTALLED_APPS`, `MIGRATION_MODULES`, `DJANGO_SETTINGS_MODULE`, as variáveis `SKELETON_*` do projeto (excluindo `SKELETON_TEST_*`, URLs e worker), as versões das bibliotecas instaladas e as versões do PostgreSQL e do `pg_dump`. O arquivo de cache guarda apenas o SQL do dump e o fingerprint; entradas com outra chave, formato desconhecido, malformadas ou contendo meta-comandos do `psql` são ignoradas e provocam nova migração. A chave é propositalmente ampla: editar fora de `tests/` custa uma migração, enquanto esquecer uma entrada custaria um schema desatualizado. No CI, nenhum cache vem de fora: cada job usa um diretório vazio no runner, migra uma vez de verdade e compartilha o template apenas entre os workers do mesmo job. O cache nunca reaproveita dados entre testes.

O uso do template exige `pg_dump` com versão igual ou superior à do servidor (no CI, fornecido por `postgresql-client`). Para indicar outro binário, use `SKELETON_TEST_PG_DUMP`. Com `SKELETON_TEST_POSTGRES_TEMPLATE=0`, a fixture volta a migrar em cada teste (útil para depurar migrations). Sem `pg_dump` e sem essa variável, a suíte do núcleo falha com erro de uso para não ficar lenta em silêncio; já a fixture distribuída, pensada para projetos que podem não ter o cliente PostgreSQL instalado, faz fallback para migrar a cada teste.

**Hasher de teste e SSO.** O perfil de teste (`skeleton.conf.profiles.test_settings`) usa `skeleton.testing.hashers.FastPBKDF2PasswordHasher`: PBKDF2-SHA256 com 1.000 iterações, preservando o algoritmo e o formato de hash da produção (onde cada senha custava cerca de 0,3 s, pesando em testes que criam várias identidades). Esse hasher nunca deve entrar nos settings de uma instalação real. Já os testes de SSO no navegador usam fixture de sessão com banco isolado e servidor real, preservando commits até o fim do cenário. Não use `TestCase` com bloco `atomic` externo para provar `COMMIT` ou `SET LOCAL`: testes transacionais exigem commits reais.

### Fixture distribuída em paralelo e com template

A fixture `skeleton.testing.database.skeleton_db` reconhece as mesmas variáveis de ambiente da suíte do núcleo:

| Variável | Efeito |
|---|---|
| `TEST_DATABASE_URL`, `TEST_MIGRATION_DATABASE_URL`, `SKELETON_TEST_ALLOW_SCHEMA_RESET=1` | Banco descartável, role runtime e role owner ([Isolamento da suíte](#isolamento-da-suíte)); obrigatórias. |
| `PYTEST_XDIST_WORKER` (definida pelo `pytest-xdist`) | O worker `gwN` usa `<base>_gwN_skeleton_test`, derivado das duas URLs; a fixture reescreve as duas variáveis no processo e subprocessos herdam o banco do worker. Se o banco do worker não abrir, a execução para com erro de uso. |
| `SKELETON_TEST_PG_DUMP` | Caminho do `pg_dump` usado para o template (padrão: o do `PATH`). Sem `pg_dump`, cada teste migra (mais devagar, sem erro). |
| `SKELETON_TEST_POSTGRES_TEMPLATE=0` | Desliga o template e migra em cada teste (para depurar migrations). |

Na fixture distribuída, o template tem escopo por processo: cada worker migra uma vez e restaura antes de cada teste, sem cache entre execuções nem reaproveitamento de schema entre testes (exclusivo da suíte do núcleo). `skeleton.testing.database.SCHEMA_STATS` contabiliza as migrações e restaurações do processo. Para executar com `pytest -n N`, provisione um banco por worker além do banco base:

```sh
python -m skeleton.testing.database provision --database ci_skeleton_test --workers 4
python -m pytest -n 4 -p skeleton.testing.database
```

Importar a fixture no `conftest.py` já é suficiente para os workers funcionarem. O plugin opcional `-p skeleton.testing.database` verifica uma única vez, antes de subir qualquer worker, se todos os bancos existem e se a tabela de locks comporta as restaurações simultâneas. `tests/integration/test_distributed_database_fixture.py` exercita a fixture em um projeto externo (um `conftest.py` mínimo sobre `skeleton.testing.host.settings`) confirmando uma migração e uma restauração por teste, e `tests/unit/test_integration_database.py` cobre o módulo do kit.

### Projeto hospedeiro para apps em repositório próprio

Uma app desenvolvida em repositório próprio não possui o pacote `config` de um projeto derivado. Para rodar sua suíte, o wheel distribui o projeto hospedeiro mínimo `skeleton.testing.host`: seu `settings.py` aplica `test_settings` sobre os defaults do núcleo e substitui referências a `config.*` (`ROOT_URLCONF` passa a ser `skeleton.testing.host.urls`, com as rotas do núcleo, e WSGI/ASGI apontam para `skeleton.testing.host.wsgi` e `asgi`). A raiz do projeto é o diretório atual ou o valor de `SKELETON_PROJECT_ROOT`, e a app é carregada via `SKELETON_DEV_APPS` ([extending](extending.md#carregar-uma-app-em-desenvolvimento-skeleton_dev_apps)):

```sh
export DJANGO_SETTINGS_MODULE=skeleton.testing.host.settings
export SKELETON_DEV_APPS=$PWD
python -m pytest tests
```

O `pyproject.toml` gerado por `skeleton_startapp --standalone` ativa o plugin `skeleton.testing.host.pytest_plugin`. Sem ele, o pytest importaria `<checkout>/tests/test_x.py` como `<pacote>.tests.test_x` — criando uma segunda cópia da app ao lado de `applications.<pacote>` e registrando os modelos em duplicidade.

**Provisionar o banco pelo wheel.** O wheel inclui uma cópia byte a byte de `deploy/postgres/bootstrap.sql` em `skeleton/testing/bootstrap.sql` (conferida por `tests/unit/test_testing_provision.py`) e o subcomando de provisionamento para servidores descartáveis ([Isolamento da suíte](#isolamento-da-suíte)):

```sh
export SKELETON_TEST_ADMIN_DATABASE_URL=postgres://postgres:<senha>@127.0.0.1:5432/postgres
export SKELETON_TEST_OWNER_PASSWORD=<senha do owner> SKELETON_TEST_RUNTIME_PASSWORD=<senha do runtime>
python -m skeleton.testing.database provision --database ci_skeleton_test
```

Com `--workers N`, o comando também provisiona `<base>_gw0_skeleton_test` até `<base>_gwN-1_skeleton_test`. Ele cria o banco se ainda não existir, aplica o SQL de bootstrap, concede `GRANT CREATE ON DATABASE` a `skeleton_owner` e, quando as variáveis de senha estão definidas, ajusta as senhas das duas roles. A operação é idempotente e recusa nomes de banco que não sejam identificadores simples terminados em `_skeleton_test`. A URL administrativa vem de `--admin-url` ou de `SKELETON_TEST_ADMIN_DATABASE_URL` e nunca é impressa nem gravada; em caso de falha do driver, exibe apenas o tipo do erro. O teste de integração do subcomando roda somente quando `SKELETON_TEST_ADMIN_DATABASE_URL` está definida.

O fluxo completo de uma app em repositório próprio, incluindo o CI gerado, está em [extending](extending.md#desenvolver-uma-app-em-repositório-próprio). O projeto hospedeiro serve exclusivamente a testes e nunca deve ser usado como entrypoint de produção. `tests/unit/test_dev_apps.py` comprova que ele inicializa o Django e resolve as URLs do núcleo sem importar `config`.

## PostgreSQL runtime real

**Pré-requisitos:** servidor PostgreSQL em execução, banco descartável criado e provisionado pelo operador, URLs das roles runtime e owner apontando para esse banco e marcador de reset definido. Os comandos abaixo usam o arquivo privado da instalação de integração (`. .private/runtime/services.env`) ou, localmente, `eval "$(python tools/dev_postgres.py env)"`; em outra máquina, provisione equivalentes antes de carregá-los. Nenhum desses exemplos contém secrets de produção.

```sh
set -a
. .private/runtime/services.env
set +a
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration/test_app_extensions.py \
  tests/integration/test_access_lifecycle_regressions.py \
  tests/integration/test_audit_findings.py \
  tests/accounts/test_lifecycle_findings.py \
  tests/accounts/test_social_hardening.py
.venv/bin/python tools/smoke_postgres.py
```

O `smoke_postgres.py` reseta **somente** o schema `skeleton` do banco de fixture validado; trata-se de uma ferramenta permanente de verificação de segurança, não de um healthcheck de produção. Nunca copie secrets para `stdout` ou argumentos de linha de comando. Se `DATABASE_URL` ou `MIGRATION_DATABASE_URL` coincidirem com as URLs de teste, a barreira bloqueia a execução: separe os ambientes normal e de teste em vez de desativar a proteção.

Alternativa via Compose, usando secrets privados completos e banco descartável previamente criado e provisionado:

```sh
docker compose --profile integrations up -d postgres redis keycloak
# Operador aplica bootstrap.sql ao DB fixture novo; senhas externas ao SQL.
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py migrate
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py sync_table_catalog
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_skeleton_schema
```

Os dois primeiros comandos usam `MIGRATION_DATABASE_URL` (role owner); o terceiro usa `DATABASE_URL` (role runtime) e os requisitos do perfil de produção. Ter imagens fixadas no Compose não prova que os serviços realmente subiram.

**Assertions esperadas no runtime PostgreSQL:**

- **Privilégios e estrutura:** role runtime sem `SUPERUSER`, `BYPASSRLS`, ownership ou `SET ROLE` privilegiado; policies, RLS, `FORCE ROW LEVEL SECURITY` e grants exatos.
- **Isolamento por contexto:** sem contexto de tenant, `SELECT` retorna 0 linhas e `INSERT` falha com `42501`; a organização A lê apenas A, `UPDATE` em B (incluindo soft delete) afeta 0 linhas e `INSERT` em B falha com `42501`; `DELETE` físico é recusado com `42501` por ausência de grant (proteção mais estrita do que filtragem por RLS).
- **Integridade e transações:** FK entre A e B falha com `23503`; guard contra reatribuição de tenant ativo; GUC de UUID malformado falha com `22P02`; `COMMIT` e `ROLLBACK` devolvem o contexto ao estado vazio; duas conexões intercaladas não cruzam tenants.
- **Auditoria e descoberta:** descoberta da própria `Membership` restrita a `SELECT`; runtime bloqueado em `TRUNCATE`, `ALTER` e em `INSERT`/`UPDATE`/`DELETE` na auditoria (`42501`).
- **Guards e negativos de deploy:** o guard de identidade é testado separadamente como owner (nunca usado como prova de RLS), e testes negativos de deploy removem triggers ou inserem policies permissivas exigindo falha específica.

## Kit do portal

O módulo `skeleton.testing.external` monta cenários e valida contratos do portal de pessoas externas:

- `external_scenario(slug)` cria as organizações A e B com o portal habilitado, além de administração de A, membro comum, pessoa externa de A, administradora de A que também é externa e uma pessoa de fora.
- `acting_external(user, organization)` abre o contexto externo; `grant_external_access` e `enable_portal` ajustam o cenário.
- `assert_external_contract(service_class, method, scenario=..., call=lambda actor: ..., unlinked=..., disabled=...)` prova, para um método `allow_external` de uma app, que ele declara a marcação, funciona no portal correto, recusa outro ator, nega acesso em contexto de membro sem acesso externo, não devolve dados de A a uma pessoa externa de outra organização e responde indisponível com a app desligada.

## Pessoas, organizações e grupos em fixtures

O módulo `skeleton.testing.people` cria pessoas, organizações, vínculos e grupos usando os mesmos serviços de provisionamento de `build_service_scenario`, sempre em contexto de sistema de fixture. Assim, testes de uma app em repositório próprio podem permanecer inteiramente em `skeleton.testing.*` sem disparar o aviso `W019`. Todas as funções recebem argumentos keyword-only e devolvem o registro criado:

| Helper | Cria |
|---|---|
| `make_person(email=..., password=None, first_name="", last_name="")` | Identidade ativa com email verificado e primário; a senha padrão é `FIXTURE_PASSWORD`, sorteada por processo. |
| `make_organization(owner=..., slug=..., name=...)` | Organização em que `owner` é o administrador. |
| `make_role(organization=..., code=..., name=...)` | Papel próprio da organização; códigos dos papéis embutidos são recusados. |
| `add_member(organization=..., user=..., role_code="member")` | Vínculo com um papel embutido ou criado via `make_role`; chamar novamente com outro papel acrescenta o novo papel. Papel inexistente gera `LookupError`. |
| `make_group(organization=..., name=..., members=(), code=None, admin=None)` | Grupo com membros já vinculados (qualquer outra pessoa gera `LookupError` antes de criar o grupo). Com `admin`, cria como esse administrador (`access.manage` e auditoria atribuída a ele); sem `admin`, roda em contexto de sistema de fixture. |

As permissões de tabela de um papel são atribuídas via `grant_table_actions` (seção seguinte). `tests/integration/people_kit_scenario.py` monta duas organizações com quatro pessoas e três grupos cada usando apenas esses helpers, e `tests/integration/test_people_kit.py` valida o resultado e confirma que a análise de fronteira de importação não aponta alertas no módulo.

## Permissões de tabela em fixtures

`skeleton.testing.applications.grant_table_actions(organization_id, role_code=..., table_key=..., actions=...)` concede ações de tabela a um papel, em contexto de sistema de fixture e após sincronizar o catálogo. Esse helper atende kits de apps — como `skeleton.contrib.ledger.testing.enable_ledger` — que precisam dar acesso administrativo ao livro contábil sem conceder permissão a quem testa o registro de origem.

Bibliotecas first-party não possuem disponibilidade nem ativação próprias: ficam efetivas enquanto houver uma app efetiva dependendo delas. Nos testes da própria biblioteca, `skeleton.testing.applications.library_consumer()` registra temporariamente, durante o bloco, uma app de teste (`test_library_consumer`) que depende de todas as bibliotecas instaladas e a abre — `apps_open_by_default()` faz isso automaticamente sempre que há bibliotecas instaladas. Nesse modo, `activate_app(<biblioteca>, organização)` ativa a app de teste, `open_app(<biblioteca>)` não grava nada e `build_service_scenario` junto de `application_disabled` ligam e desligam essa app de teste. Apps consumidoras não precisam desses helpers: ativar a própria app já torna a biblioteca efetiva.

## Kit do selo

`skeleton.testing.sealing.assert_sealed_contract(model, make_record=..., seal=..., acting=..., foreign=None, make_child=None, extra=None)` verifica, sob a role runtime, o contrato completo de um modelo selável de uma app:

- colunas congeladas e o próprio selo não podem ser alterados nem pelo modelo nem por SQL;
- colunas livres continuam editáveis;
- exclusão é recusada (exceto quando `seal_allows_delete` está ativo);
- criação de filhos (`make_child`) após o selo é recusada;
- anexos ficam congelados conforme a política e entram no resumo;
- `verify_seal` é verdadeiro logo após selar;
- a organização retornada por `foreign()` não lê nem alcança o registro.

Os callables `acting()` e `foreign()` abrem os contextos das organizações A e B (por exemplo, `scenario.as_member` e `lambda: scenario.acting(scenario.admin_b, scenario.b)` de `build_service_scenario`). Todos os desvios encontrados são reportados juntos em um único `AssertionError`.

Quando uma coluna de `seal_fields` também está em `immutable_fields` (caso de uma FK fixada na criação, como `corrects` em [extending](extending.md#registros-que-não-mudam-depois-de-um-ato)), a barreira de imutabilidade dispara antes do selo: apenas para essas colunas o kit aceita `ImmutableRecord` no modelo e a mensagem `immutable record identity` do guard como recusa válida. Em qualquer outra verificação, `ImmutableRecord` é relatado como problema, nunca derrubando o teste de forma inesperada. Colunas `NOT NULL` não alteram esse comportamento, pois o guard executa antes da constraint. `tests/integration/test_sealing_kit.py` valida o kit sobre um modelo de fixture do núcleo, um modelo de teste da app de exemplo, uma tabela com FK imutável selada (e FK `NOT NULL` selada) e um guard instalado sem o selo (reprovado pelo kit).

## Kit de tabelas derivadas

`skeleton.testing.derived.assert_derived_access_contract(child_model=..., make_anchor=..., make_child=..., scenario=...)` verifica, sob a role runtime, o contrato de uma [tabela derivada](access-control.md#tabelas-derivadas):

- sem concessão em uma âncora privada, o filho é invisível;
- concessão de leitura na âncora permite ler o filho, mas não alterá-lo;
- concessão de escrita permite alterá-lo;
- o filho pertencente a outra organização é invisível;
- o filho de uma âncora com tombstone desaparece, inclusive para `restore`;
- sem permissão de tabela na âncora, o filho é recusado até mesmo para o dono;
- com a app desativada, o acesso é recusado;
- `manage_access` diretamente no filho é sempre recusado.

`make_anchor(organization=..., owner=...)` deve criar uma âncora com `visibility="private"`, e `make_child(anchor)` cria o filho (bem como os elos intermediários em cadeias maiores). O parâmetro `scenario` vem de `build_service_scenario`, que concede permissões na tabela âncora (`scenario.anchor_model`) e faz `create` criar âncoras. Todos os problemas são agregados em um único `AssertionError`.

Os helpers `grant_anchor(scenario, anchor, action=..., user=... | group=...)` e `create_group(scenario, name, members)` configuram concessões e grupos pelos serviços de gestão de acesso do núcleo (como faria um administrador), evitando que os testes da app precisem importar `skeleton.access.models`. `tests/integration/test_derived_kit.py` exercita o kit nas tabelas da [receita](extending.md#contêiner-e-tabelas-derivadas), com um e com dois saltos.

## OIDC e SAML reais

**OIDC** e **SAML** são protocolos de login entre a aplicação e um provedor de identidade (IdP). Para provar a integração, o navegador precisa percorrer os redirecionamentos reais e encerrar o fluxo com uma sessão Django válida — receber claims isoladas não substitui esse percurso. Consulte [login e protocolos](concepts.md#login-e-protocolos).

**Pré-requisitos:** Keycloak em execução com os dois realms importados, endpoints exatos acessíveis, dependências de SAML e XML instaladas e banco de fixture isolado. Gere os arquivos privados antes de iniciar o Django e só então execute a suíte.

Os realms `tests/sso/realm-skeleton-a.json` e `realm-skeleton-b.json` trazem usuários de fixture, cliente OIDC confidencial com PKCE S256 e SAML com response e assertion assinadas. O Django deve rodar em `http://127.0.0.1:8000` e o IdP em `http://127.0.0.1:8080/realms/skeleton-a` e `skeleton-b` (HTTP é permitido apenas em `DEBUG` local). Não substitua por aliases que alterem issuer ou callback no navegador.

```sh
.venv/bin/python tools/prepare_sso_fixtures.py --output .private/sso-fixtures
. .private/sso-fixtures/environment.sh
.venv/bin/python -m playwright install chromium
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests/sso
```

O script preparador consulta apenas discovery, metadata e certificados **públicos** nos realms ativos; gera chaves e segredos de SP em diretório privado, cria `providers.json`, `environment.json` e `environment.sh` com permissão `0600` e imprime somente os caminhos gerados. O `source` de `environment.sh` deve ocorrer **antes de iniciar o Django**, pois o parser lê os segredos das variáveis de ambiente (`SSO_PROVIDER_CONFIG_PATH` e `SSO_FIXTURE_ENV_PATH` indicam os arquivos). Não copie chaves privadas do IdP nem altere o trust store. Papéis, vínculos e convites são criados pelos serviços na fixture — nunca use claims arbitrárias para promovê-los.

Durante o teste, o navegador completa o redirecionamento, o login no IdP e o callback real, consultando a API pela sessão final para verificar: organização correta, ausência de `is_staff`/`is_superuser` e reexecução sem duplicar `User`, `Profile` ou `Membership`. Os testes negativos adulteram trocas reais da fixture (sem substituir o validador) para cobrir:

- **OIDC:** `state`, replay, `issuer`, `audience`, assinatura, expiração e PKCE;
- **SAML:** assinatura, `destination`, `audience`, expiração, `InResponseTo`, respostas não solicitadas e replay;
- **Vínculo de identidade:** mera coincidência de email não vincula contas; sem binding/JIT ou sem email confiável, nenhuma afiliação é concedida.

A integração com GitHub exige app OAuth, credenciais reais e callback documentado; caso contrário, registre-a como não exercitada.

**Resultado esperado:** o login retorna à aplicação, cria ou reutiliza a identidade correta e recusa todos os casos adversos sem conceder vínculo indevido. **Limite:** serviço ou segredo ausente indica pré-requisito pendente, nunca teste aprovado; essa fixture corporativa não substitui testes com GitHub real nem homologação no gov.br.

## Escrever testes que mantêm a suíte rápida

Quase todo o tempo da suíte é gasto em preparo, não na execução das assertions. Na linha de base do epic [#199](https://github.com/alaorneto/skeleton/issues/199), 1.320 s eram `setup` contra 246 s nas funções de teste, pois cada teste com `skeleton_db` recriava o schema inteiro. Portanto, o custo de um teste novo depende principalmente do que ele exige preparar.

Custos medidos em 2026-11-12 (Windows 11, execução serial, PostgreSQL local de `tools/dev_postgres.py`):

| Peça | Custo por teste |
|---|---|
| Teste sem banco (`tests/unit`) | Milissegundos |
| `skeleton_db` (restaurar o schema e abrir as apps) | 0,8 a 1,1 s |
| `build_security_fixture()` (quatro identidades, duas organizações, papéis, catálogo e notas) | 0,47 s |
| Uma identidade via `create_identity()` | 0,014 s |
| Subprocesso `python` com `django.setup()` | 2 s |
| `call_command("migrate")` dentro de um teste | 4 a 8 s |

Em 2026-11-23, com cerca de 5.000 testes e 107 tabelas no schema, esses valores subiram: recriar o schema levava 1,3 a 1,4 s em série e 2,8 s com 8 workers; `build_security_fixture()` levava 0,66 s; migrar, de 11 a 20 s. O preparo representava 93% do tempo de um teste com banco, sendo 76% a 83% apenas a restauração. Cada nova tabela, índice ou tabela TOAST entra nessa conta em toda recriação. Com o reaproveitamento de schema, `skeleton_db` só paga a recriação quando o teste anterior do worker altera a estrutura; nos demais casos, devolver o schema ao template leva de 0,15 a 0,45 s, conforme a carga da máquina.

### Custo

1. **Use banco só quando a prova depender dele.** Lógica pura (validação, parsing, formatação, cálculo, montagem de mensagem ou payload) pertence a `tests/unit`, sem `skeleton_db`. O que depende de RLS, triggers, auditoria, CAS, isolamento A/B ou da role runtime continua em PostgreSQL com a role runtime. Nunca substitua essa prova por mock do ORM, SQLite ou conexão owner para ganhar velocidade: o [checklist do `AGENTS.md`](../AGENTS.md#checklist-mínimo-de-nova-tabela) continua obrigatório.
2. **Um preparo para vários casos de leitura.** Com `skeleton_db`, cada variação de `@pytest.mark.parametrize` paga todo o preparo do schema e do cenário. Quando os casos apenas leem o mesmo cenário, agrupe-os em um único teste usando a fixture `subtests` do pytest 9: o preparo executa uma vez e cada caso aparece nomeado separadamente no relatório, inclusive com `-n`. Bons candidatos são matrizes de atores e ações de default deny, listas de rotas que devem responder 404 para outra organização e listas de campos proibidos em uma resposta. Mantenha `parametrize` ou testes separados quando um caso escreve e o seguinte exige o banco intacto.

   ```python
   def test_outra_organizacao_recebe_404_em_todas_as_rotas(skeleton_db, client, subtests):
       cenario = build_security_fixture()  # pago uma vez
       client.force_login(cenario.outsider)
       for rota in (f"/o/{cenario.a.pk}/", f"/o/{cenario.a.pk}/audit/", f"/api/v1/o/{cenario.a.pk}/catalog/"):
           with subtests.test(rota=rota):
               assert client.get(rota).status_code == 404
   ```

3. **Monte o cenário mínimo.** Crie apenas o que as assertions realmente utilizam. Para um teste com um ator em uma organização, `create_identity()` e `create_organization()` bastam. Reserve `build_security_fixture()` para a matriz A/B completa e `build_service_scenario()` para o kit da API de serviços. Nunca crie fixtures de escopo `session` ou `module` com linhas no banco: cada teste precisa partir do template limpo, como exige o [isolamento da suíte](#isolamento-da-suíte).
4. **Não migre dentro de um teste.** O template já fornece o schema migrado. Para DDL de prova, use a fixture `owner_schema_editor`. A migração real já é coberta onde é o objeto do teste (`tests/integration/test_scaffold_end_to_end.py`, `tools/smoke_postgres.py`), e `SKELETON_TEST_POSTGRES_TEMPLATE=0` serve para depurar migrations. Lembre-se de que qualquer teste com DDL força o teste seguinte a recriar o schema (segundos em vez de décimos de segundo) — evite espalhar DDL onde não é indispensável.
5. **Abra subprocesso só quando um processo novo for parte da prova.** Bons exemplos são importações em processo limpo (`test_observability_optional.py`) ou um comando de ponta a ponta. Para executar comandos Django no mesmo processo, prefira `call_command`.
6. **Não espere o relógio.**
   - Passagem de tempo: passe `now=` aos serviços que aceitam ou substitua `timezone` com `monkeypatch` (`test_api_tokens.py`, `test_ics_feed.py`).
   - Retentativa e backoff: injete a agenda ou o relógio em vez de dormir.
   - Outra thread ou transação: sincronize com `threading.Barrier` ou `Event` com timeout (`test_jobs_worker.py`, `test_admin_concurrency.py`), aguardando o evento que comprova o estado (como a aquisição do lock) em vez de um intervalo fixo.
7. **Mantenha o hasher de fixture.** O perfil de teste já utiliza PBKDF2 com 1.000 iterações (`skeleton.testing.hashers`). Só restaure o hasher de produção em testes dedicados ao próprio hashing.
8. **Sem rede externa.** Use servidores locais em `127.0.0.1` e transportes falsos (`test_outbound_http.py`, `test_webhook_delivery.py`, `test_file_scanning.py`).

### Execução paralela

Com `-n`, cada worker roda em um processo com banco exclusivo, e os testes de um mesmo arquivo podem ser distribuídos entre workers diferentes em qualquer ordem. Todo teste novo deve respeitar estas condições:

- **Porta 0, nunca fixa:** use `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` e leia a porta efetiva em `server.server_address[1]`. A única porta fixa tolerada hoje é a 8000 em `test_rd09_rendered_web_health_uses_permitted_production_host`, porque o healthcheck renderizado do Compose aponta para ela. Não introduza novas portas fixas.
- **Arquivos apenas em `tmp_path` ou `tmp_path_factory`:** nunca grave em caminhos fixos do repositório, do usuário ou do sistema.
- **Estado global restaurado ao final:** use `monkeypatch`, `override_settings`, blocos `try/finally` ao alterar registros (eventos, jobs, apps) e signals conectados com `dispatch_uid` e desconectados ao fim do teste.
- **Zero dependência de ordem:** nenhum teste pode presumir que outro rodou antes ou depois dele.
- **Banco pelas variáveis, nunca por nome fixo:** use `disposable_database_pair()` ou `TEST_DATABASE_URL` e `TEST_MIGRATION_DATABASE_URL`, que já apontam para o banco do worker (inclusive em subprocessos).
- **Rode `-n auto` antes do PR:** se um teste só passa em série, ele compartilha estado e deve ser corrigido — não o marque como exceção nem o isole para rodar à parte.

### Medir

- **Arquivo novo:** execute `python -m pytest tests/<caminho>/test_novo.py -q --durations=10`. Um `call` acima de 1 s ou um `setup` acima de 2 s exige uma justificativa no PR explicando o que está sendo provado e por que não pode ficar mais rápido. Hoje excedem esses limiares o scaffold de ponta a ponta (cerca de 30 s) e os testes de concorrência do PostgreSQL (2 a 3 s cada).
- **Suíte inteira:** `python -m pytest tests -q -n auto --durations=25` lista os testes mais lentos.
- **CI:** quando o job mais lento ultrapassar 8 minutos, aumente o número de fatias daquela suíte em vez de cortar testes. Basta ajustar os itens `slug` e `include` da matriz em `.github/workflows/ci.yml`, mantendo o mesmo denominador `N` em `shard` para toda a suíte. As fatias se equilibram automaticamente pelo custo, incorporando arquivos e pacotes novos sem edição manual.

### O que não fazer para ganhar tempo

Nenhuma das práticas abaixo é aceita como otimização:

- apagar, pular ou marcar testes como lentos ou `xfail` para retirá-los do caminho obrigatório;
- substituir a prova em PostgreSQL com a role runtime por SQLite, mock do ORM ou conexão owner;
- usar `--reuse-db`, flush, `TRUNCATE`, transação revertida ou limpeza manual dentro do teste no lugar do template (a única limpeza de linhas admitida é a da fixture `skeleton_db` do núcleo, validada contra o template e com fallback para recriação);
- desligar ou espaçar a verificação integral do reaproveitamento (`SKELETON_TEST_SCHEMA_REUSE_VERIFY`) no CI, ou tratar uma falha dela como instabilidade passageira;
- reexecutar um teste instável até passar em vez de corrigir a causa raiz.

## Matrix mínima para nova tabela

Esta **matriz** cruza atores, organizações e operações para detectar vazamentos que um teste de caminho feliz não revela. A e B representam duas organizações distintas; Alice e Bob são usuários com permissões diferentes; o administrador e um usuário sem `Membership` completam o conjunto. O controle de concorrência (CAS) compara a revisão lida antes da gravação, garantindo um único vencedor entre duas escritas simultâneas com a mesma revisão. Consulte [Auditoria e concorrência](concepts.md#auditoria-e-concorrência) para os detalhes desse contrato.

- **Modelo e catálogo:** todos os modelos próprios usam UUID, herdam de uma base do núcleo, registram `TableSpec` e têm tabelas físicas catalogadas, inclusive no manifesto técnico nominal.
- **Isolamento e visibilidade (A, B, Alice, Bob, admin e sem `Membership`):** listagem, detalhe, contagem, lookup, seletor de FK, auditoria e restauração não vazam dados de outro tenant nem registros privados.
- **Contexto e fronteiras:** tenant ausente, tenant aninhado diferente, streaming ou fluxo assíncrono proibido falham fechado; toda renderização acontece dentro do contexto.
- **Autorização:** `TablePermission` obrigatória sob default deny; concessão de registro nunca pula tabela nem tenant; visibilidade de organização ou grupo concede leitura, nunca escrita.
- **Tabelas derivadas (`access_parent`):** validar com `assert_derived_access_contract` em cada tabela filha, inclusive na de maior número de saltos.
- **Revogação e atribuição em massa:** revogar papel, grupo ou concessão surte efeito na requisição seguinte; campos ocultos (`organization`, `is_staff`, `owner`) são recusados em atribuição em massa.
- **Último administrador:** protegido contra remoção ou rebaixamento, incluindo via `GroupRole` e ciclo de vida global de `User`.
- **Exclusão lógica e restauração:** tombstones permanecem invisíveis; conflito na restauração preserva linhas e `sysid` existentes; referências cross-tenant falham tanto em `INSERT` quanto em `UPDATE`.
- **Concorrência e validação de entrada:** duas escritas com a mesma revisão têm um único vencedor e a outra falha como desatualizada; o endpoint `me` compara ambas as revisões; campos desconhecidos e ausência de CSRF são negados.
- **Auditoria:** criação, `save(update_fields=...)`, `queryset.update`, `bulk_create`, vínculos, SQL direto, soft delete e restauração produzem exatamente um evento correto por alteração, com ator, revisão, tenant e origem válidos e sem jamais expor segredos ou `body` no payload.
- **Escritas restritas:** `update_fields` vazio não executa escrita; `bulk_update` e upsert com resolução de conflito são rejeitados; SQL direto com revisão válida é auditado.
- **Transações e imutabilidade da auditoria:** rollback, savepoint e handlers que escrevem antes de retornar 403 ou 500 não deixam linha nem evento; falha no writer de auditoria aborta a operação de negócio; `UPDATE` e `DELETE` na tabela de auditoria são negados.
- **Sincronização do catálogo:** idempotente, preserva `sysid` e textos existentes e não executa DDL.
- **Ciclo de vida de conta e autenticação:** convite expirado, replay, email divergente e corrida; acesso público sem `Membership`; redefinição de senha normal e recusa para tokens inválidos, contas SSO-only, inativas ou excluídas; `auth_version`; MFA e códigos de recuperação de uso único, cifra e rotação preservando a máscara; administrador sem bypass e fluxo de enrollment sem deadlock.
- **Tabelas operacionais e append-only:** tabelas operacionais com `organization` entram no inventário de RLS de `tools/postgres_assertions.py` (`ENABLE`/`FORCE` e policies); o check de schema possui testes negativos em banco para os ramos operacional (trigger de auditoria extra, RLS desligado) e append-only (policy de leitura alargada, grant de escrita ao runtime) em `tests/integration_postgres/test_schema_assurance.py`.

A aprovação exige evidência real dos backends e protocolos pertinentes: uma suíte parcial não autoriza declarar a matriz completa.

## Evidência histórica

Os resultados de cada rodada — com comando, ambiente e números medidos — ficam registrados em [testing-evidence.md](testing-evidence.md). Ao documentar uma nova rodada, adicione a nova seção no topo daquele arquivo, logo após o aviso inicial.
