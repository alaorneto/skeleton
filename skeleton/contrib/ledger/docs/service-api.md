# API de serviços: Livro contábil

## Visão geral
`LedgerService` é o contrato em processo do livro contábil. Outras apps o usam para lançar pelos registros delas, criar e baixar títulos e consultar saldos, sempre com a autorização do ator que está no contexto. As páginas do próprio livro usam o mesmo serviço.

## Dependência
`app_id`: `org.skeleton.ledger`. Faixa recomendada: `{"org.skeleton.ledger": ">=0.5.0,<0.6.0"}` (a versão é a do núcleo). Importe só `skeleton.contrib.ledger.service_api`.

## Serviços

### `LedgerService`
Todos os métodos exigem a app ativa na organização (`Unavailable`), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden`). Nenhum faz commit nem executa efeito externo. "Livro opcional" significa: sem `book_id`, o livro padrão da organização.

#### `books(*, actor) -> tuple[BookInfo, ...]`
Livros que o ator lê, o padrão primeiro.
- **Autorização:** TablePermission read em `skeleton_ledger.book` e visibilidade dos registros.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** nenhum.

#### `create_default_book(*, actor, basis="cash", currency="BRL") -> BookInfo`
O livro padrão da organização, criado na primeira chamada.
- **Autorização:** TablePermission create em `skeleton_ledger.book` para criar.
- **Erros:** `Forbidden` sem a permissão de criar quando ainda não há livro padrão; `NotFound` se ele existe mas o ator não o lê; `InvalidInput` para regime ou moeda inválidos.
- **Efeitos:** cria o livro (moeda e regime imutáveis) na transação do chamador.

## DTOs
- `BookInfo`: `sysid`, `name`, `currency`, `basis`, `is_default`, `locked_until`, `revision` e `writable` (se o ator gere o livro).

## Histórico de versões
- 0.6.0 (próxima *minor*, com o núcleo): primeira versão (#542): `books`, `create_default_book`.
