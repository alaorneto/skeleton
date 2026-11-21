title:	Documentar a exportação em CSV
state:	OPEN
author:	dono
labels:	documentation
number:	61
--
## Contexto
O comando `report export --format csv` saiu na 2.4.0 sem documentação; o suporte responde à mão como usar.

## Objetivo
Quem usa o relatório sabe exportar o CSV sem perguntar.

## Documentos afetados
- [ ] `docs/reports.md` — seção "Exportar em CSV"

## Público
Quem usa o relatório mensal e o suporte.

## Critérios de aceite
- [ ] `docs/reports.md` explica o comando e as opções `--format` e `--month`
- [ ] O exemplo de saída confere com a saída real de `report export --format csv --month 2026-10`
- [ ] Há uma captura de tela do arquivo aberto na planilha

## Verificação
Rodar o comando do exemplo e comparar com o texto; ver o arquivo renderizado no GitHub.

## Definição de pronto
- [ ] Critérios de aceite verificados
- [ ] Commits com `Refs #61`
- [ ] Aprovado pelo responsável
