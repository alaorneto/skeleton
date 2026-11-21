# Plano: exportação do relatório mensal

## Contexto
O relatório mensal só existe na tela. O financeiro copia os números à mão para a planilha e erra; além disso, o total mensal arredonda cada pedido antes de somar e fica alguns centavos acima do extrato do banco.

## Abordagem
1. Novo subcomando `report export --format csv`, em `reports/export.py`, com as colunas data, cliente e total, separador `;` e decimal com vírgula. Documentar em `docs/reports.md`.
2. Corrigir `monthly_total` em `reports/monthly.py` para somar os valores exatos e arredondar só o total. Independe do item 1.
3. Agendar o envio do CSV por e-mail no dia 1 de cada mês (`reports/schedule.py`, configuração `report.email_to`). Usa o exportador do item 1.

## Itens do GitHub
| Tipo | Título | Critérios de aceite | Plano de testes | Documentação |
|---|---|---|---|---|
| feature | Exportar o relatório mensal em CSV | `report export --format csv --month 2026-10` grava `relatorio-2026-10.csv` com data, cliente e total; mês sem pedidos gera só o cabeçalho; formato desconhecido é recusado com mensagem | unitário do formatador; integração do subcomando com um banco de teste | `docs/reports.md` |
| bug | Somar os valores exatos no total mensal | o total de outubro bate com o extrato (R$ 10.482,17); cada pedido não é mais arredondado antes da soma | teste de regressão com 3 pedidos de R$ 0,005 | Não se aplica — o comportamento documentado já era esse |
| feature | Enviar o relatório mensal por e-mail | no dia 1, às 6h, o CSV do mês anterior é enviado para `report.email_to`; sem a configuração, nada é enviado e o log diz por quê | unitário do agendador com relógio falso | `docs/reports.md`, seção de configuração |

Dependências: o envio por e-mail depende da exportação; a correção do total é independente.
