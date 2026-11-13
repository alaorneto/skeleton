# Política de segurança

## Versões suportadas

Só a versão mais recente publicada do `django-skeleton` (o `version` de `pyproject.toml` na `main`) recebe correções de segurança. O agente de deployment e as apps first-party seguem o mesmo número e a mesma regra. Projetos derivados atualizam o núcleo para receber a correção.

## Como relatar

Use **"Report a vulnerability"** na aba **Security** deste repositório (relato privado de vulnerabilidades do GitHub). O relato fica visível só para quem mantém o projeto.

**Não abra issue, PR ou discussão pública com detalhes de exploração.** Uma falha relatada em público fica exposta a todos os projetos derivados antes que a correção exista. Se não houver o botão de relato privado, abra uma issue pública só pedindo um canal de contato, sem descrever a falha.

## O que incluir

- O componente afetado (por exemplo `skeleton/access`, `skeleton/integrations`, o agente de deployment, uma app de `skeleton/contrib`).
- A versão publicada ou o commit em que a falha aparece.
- O impacto: o que um atacante consegue ler, alterar ou executar, e com que acesso inicial (anônimo, membro de uma organização, administrador de organização, operador).
- Os passos mínimos para reproduzir, de preferência num checkout local com dados de teste.

Não inclua dados pessoais reais nem segredos de produção no relato.

## O que esperar

O relato recebe confirmação de recebimento e uma avaliação em prazo razoável. Confirmada a falha, a correção entra como uma issue resumida, com os labels `bug` e `security`, sem passos de exploração, e um PR que a fecha; os detalhes técnicos são publicados depois que a versão corrigida sai. Quem relatou é creditado, se quiser.

## Escopo

Valem os controles descritos em [docs/access-control.md](docs/access-control.md), [docs/audit-soft-delete.md](docs/audit-soft-delete.md) e [docs/deployment.md](docs/deployment.md). Ficam de fora o que a arquitetura declara que não promete ([docs/architecture.md](docs/architecture.md#segurança-o-que-a-arquitetura-não-promete)), como a proteção contra quem já controla o DBA/owner do banco, o SQLite da instância e a chave mestra, ou o host do agente, e falhas que dependem de uma configuração explicitamente desaconselhada na documentação.
