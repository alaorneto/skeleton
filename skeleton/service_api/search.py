"""SearchService: the global search over the tables marked with ``TableSpec.global_search`` (#583)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser

from .base import ServiceAPI, service_method


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A readable record: its identifier and its title (the table's title field, never ``__str__``)."""

    sysid: UUID
    label: str


@dataclass(frozen=True, slots=True)
class SearchGroup:
    """The best hits of one table; ``has_more`` says the table's own search has more."""

    table_key: str
    label: str
    hits: tuple[SearchHit, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class SearchResults:
    query: str
    groups: tuple[SearchGroup, ...]


class SearchService(ServiceAPI):
    """One term over every table marked for the global search that the actor reads."""

    @service_method(read_only=True, since="0.2.0")
    def search(self, *, actor: AbstractBaseUser, query: str, limit: int = 5) -> SearchResults:
        """Search the marked tables of the current organization, grouped by table.

        Autorização: leitura de cada tabela (TablePermission, escopos do token e app ativa) e de
        cada registro (``authorized_queryset``); uma tabela ilegível não gera grupo.
        Erros: InvalidInput para ``query`` fora de 2 a 200 caracteres ou ``limit`` fora de 1 a 20;
        Forbidden para ator divergente; ContextRequired sem organização no contexto.
        Efeitos: nenhum.
        """
        from skeleton.records.search import global_search
        from skeleton.system.search import clean_term

        groups = global_search(user=actor, term=query, limit=limit)
        return SearchResults(
            query=clean_term(query),
            groups=tuple(
                SearchGroup(
                    table_key=group.table_key,
                    label=group.label,
                    hits=tuple(SearchHit(sysid=hit.sysid, label=hit.label) for hit in group.hits),
                    has_more=group.has_more,
                )
                for group in groups
            ),
        )
