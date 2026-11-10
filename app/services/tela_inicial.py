"""Blocos do dashboard: quais aparecem e em que ordem (guardado em config["tela_inicial"])."""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Config, get_config, set_config

CHAVE = "tela_inicial"

# id -> (título, onde fica: "topo" ocupa a largura toda; "esquerda"/"direita" são as colunas no PC)
BLOCOS = {
    "resumo": ("Saldo, receitas, despesas e cartão", "topo"),
    "indicadores": ("Teto de gastos, meta de economia e reserva", "topo"),
    "balanco": ("Balanço mensal", "esquerda"),
    "pendencias": ("Pendências e a pagar", "esquerda"),
    "contas": ("Minhas contas", "esquerda"),
    "cartoes": ("Cartões de crédito", "direita"),
    "investimentos": ("Investimentos", "direita"),
    "objetivos": ("Objetivos", "direita"),
    "categorias": ("Despesas por categoria", "direita"),
}

COLUNAS = {"topo": "No topo", "esquerda": "Coluna da esquerda", "direita": "Coluna da direita"}


@dataclass(frozen=True)
class Bloco:
    id: str
    titulo: str
    coluna: str
    visivel: bool


def blocos_tela_inicial(s: Session) -> list[Bloco]:
    """Na ordem salva, cada um na coluna salva (ou na padrão). Bloco que não está salvo (novo no app) entra
    visível, antes do primeiro bloco salvo que vem depois dele na ordem padrão (senão, no fim)."""
    salvos = [b for b in get_config(s, CHAVE, []) if b.get("id") in BLOCOS]
    itens = [(b["id"], bool(b.get("visivel", True)),
              b.get("coluna") if b.get("coluna") in COLUNAS else BLOCOS[b["id"]][1]) for b in salvos]
    padrao = list(BLOCOS)
    for bid in padrao:
        if any(i[0] == bid for i in itens):
            continue
        depois = set(padrao[padrao.index(bid) + 1:])
        pos = next((k for k, i in enumerate(itens) if i[0] in depois), len(itens))
        itens.insert(pos, (bid, True, BLOCOS[bid][1]))
    return [Bloco(bid, BLOCOS[bid][0], coluna, visivel) for bid, visivel, coluna in itens]


def salvar_tela_inicial(s: Session, ordem: list[str] | None, visiveis: set[str] | None,
                        colunas: dict[str, str] | None = None) -> None:
    """`ordem=None` volta ao padrão. Blocos fora de `ordem` vão para o fim, escondidos.
    `colunas` (id -> topo/esquerda/direita) muda onde o bloco fica; sem ela, vale a padrão."""
    if ordem is None:
        if (item := s.get(Config, CHAVE)) is not None:
            s.delete(item)
            s.flush()
        return
    visiveis, colunas = visiveis or set(), colunas or {}
    ids = list(dict.fromkeys(i for i in ordem if i in BLOCOS))
    ids += [bid for bid in BLOCOS if bid not in ids]
    set_config(s, CHAVE, [{"id": bid, "visivel": bid in visiveis,
                           "coluna": colunas[bid] if colunas.get(bid) in COLUNAS else BLOCOS[bid][1]}
                          for bid in ids])
