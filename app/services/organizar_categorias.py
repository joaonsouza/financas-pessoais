"""Hierarquia das categorias com ajuda da IA: pais (Moradia, Alimentação...) e subcategorias, cada subcategoria
com o tipo (fixo, variável ou dívida) e a natureza (necessidade ou desejo).

A IA só propõe. Nada muda até a pessoa revisar e aplicar (`aplicar_hierarquia`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import LeitorErro
from app.models import Categoria
from app.services.categorias_padrao import PALETA
from app.services.ia import MODELO_PADRAO, chamar_parse

GRUPOS = ("fixo", "variavel", "divida")
NATUREZAS = ("necessidade", "desejo")


@dataclass
class ItemProposta:
    categoria_id: int
    nome: str
    pai: str | None  # nome do pai; None = continua na raiz
    grupo: str
    natureza: str
    motivo: str = ""


def candidatas(s: Session) -> list[Categoria]:
    """Categorias de despesa que podem virar subcategoria: ativas, na raiz e sem filhas."""
    com_filhas = set(s.scalars(select(Categoria.pai_id).where(Categoria.pai_id.is_not(None))))
    return [c for c in s.scalars(select(Categoria).where(
        Categoria.tipo == "despesa", Categoria.ativa, Categoria.pai_id.is_(None)).order_by(Categoria.nome))
        if c.id not in com_filhas]


def _modelo_saida(ids: list[int]):
    from pydantic import create_model

    item = create_model(
        "ItemHierarquia",
        categoria_id=(Literal[tuple(ids)], ...),  # type: ignore[valid-type]
        pai=(str | None, ...),
        grupo=(Literal["fixo", "variavel", "divida"], ...),
        natureza=(Literal["necessidade", "desejo"], ...),
        motivo=(str, ...),
    )
    return create_model("HierarquiaIA", itens=(list[item], ...))


INSTRUCOES = (
    "Organize as categorias de despesa de finanças pessoais de uma família brasileira em uma hierarquia de "
    "DOIS níveis. Para cada categoria listada, escolha o PAI (um nome curto de grupo, como Moradia, "
    "Alimentação, Transporte, Saúde, Educação, Lazer, Pessoal, Dívidas) ou null se ela já é um grupo por si só "
    "e deve ficar na raiz. Reaproveite os mesmos nomes de pai para categorias do mesmo grupo; prefira poucos pais "
    "(5 a 10).\n"
    "Defina também, para cada categoria: grupo = fixo (valor e data quase constantes todo mês: aluguel, "
    "energia, internet, escola, plano de saúde, assinaturas), variavel (oscila conforme o uso: mercado, lazer, "
    "combustível) ou divida (parcelas de empréstimo/financiamento); e natureza = necessidade (se faltar, a "
    "casa ou a saúde sofrem: moradia, comida básica, transporte para trabalhar e combustível, tudo de saúde "
    "como farmácia, plano e consultas, seguros, educação) ou desejo "
    "(conforto e escolhas: restaurante, lazer, roupas além do básico, faxina e serviços opcionais). "
    "Dê um motivo curto (até 8 palavras) para cada uma."
)


def propor_hierarquia(s: Session, cliente, modelo: str = MODELO_PADRAO) -> list[ItemProposta]:
    cats = candidatas(s)
    if not cats:
        return []
    existentes = sorted({c.nome for c in s.scalars(select(Categoria).where(
        Categoria.tipo == "despesa", Categoria.pai_id.is_(None))) if c.id not in {x.id for x in cats}})
    lista = "\n".join(f"- {c.id}: {c.nome} (hoje: {c.grupo or 'sem tipo'}, {c.natureza or 'sem natureza'})"
                      for c in cats)
    texto = (f"{INSTRUCOES}\n\nPais que já existem (reaproveite quando fizer sentido): "
             f"{', '.join(existentes) or 'nenhum'}\n\nCategorias:\n{lista}")
    resposta = chamar_parse(
        cliente, modelo=modelo, esforco="medium", saida=_modelo_saida([c.id for c in cats]), max_tokens=16000,
        mensagens=[{"role": "user", "content": [{"type": "text", "text": texto}]}],
    )
    if resposta.stop_reason == "refusal" or resposta.parsed_output is None:
        raise LeitorErro("A IA não conseguiu organizar as categorias desta vez.")
    por_id = {c.id: c for c in cats}
    vistos: set[int] = set()
    itens = []
    for i in resposta.parsed_output.itens:
        if i.categoria_id in por_id and i.categoria_id not in vistos:
            vistos.add(i.categoria_id)
            pai = (i.pai or "").strip() or None
            itens.append(ItemProposta(i.categoria_id, por_id[i.categoria_id].nome, pai, i.grupo, i.natureza,
                                      i.motivo))
    return sorted(itens, key=lambda x: ((x.pai or "~").casefold(), x.nome.casefold()))


@dataclass
class ResultadoAplicacao:
    movidas: int
    pais_criados: int
    atributos: int


def _pai_por_nome(s: Session, nome: str, criados: dict[str, Categoria]) -> tuple[Categoria, bool]:
    chave = nome.casefold()
    if chave in criados:
        return criados[chave], False
    existente = next((c for c in s.scalars(select(Categoria).where(
        Categoria.tipo == "despesa", Categoria.pai_id.is_(None))) if c.nome.casefold() == chave), None)
    if existente is not None:
        criados[chave] = existente
        return existente, False
    total = len(list(s.scalars(select(Categoria.id))))
    pai = Categoria(nome=nome, tipo="despesa", cor=PALETA[total % len(PALETA)])
    s.add(pai)
    s.flush()
    criados[chave] = pai
    return pai, True


def aplicar_hierarquia(s: Session, itens: list[ItemProposta]) -> ResultadoAplicacao:
    """Cria os pais que faltam, move cada categoria para o pai e grava tipo e natureza.
    Um pai nunca vira filho de outro (só dois níveis) e uma categoria não vira filha de si mesma."""
    for i in itens:
        if i.grupo not in GRUPOS or i.natureza not in NATUREZAS:
            raise ValueError("tipo ou natureza inválidos")
    criados: dict[str, Categoria] = {}
    filhos: set[int] = set()  # já movidas para dentro de um pai
    pais_usados: set[int] = set()
    movidas = pais = atributos = 0
    for i in itens:
        cat = s.get(Categoria, i.categoria_id)
        if cat is None or cat.tipo != "despesa" or cat.pai_id is not None:
            continue
        if (cat.grupo, cat.natureza) != (i.grupo, i.natureza):
            cat.grupo, cat.natureza = i.grupo, i.natureza
            atributos += 1
        if not i.pai:
            continue
        pai, novo = _pai_por_nome(s, i.pai.strip(), criados)
        if pai.id == cat.id or pai.id in filhos or cat.id in pais_usados or pai.pai_id is not None:
            continue  # evita ciclo e terceiro nível
        pais += novo
        cat.pai_id = pai.id
        filhos.add(cat.id)
        pais_usados.add(pai.id)
        movidas += 1
    s.flush()
    return ResultadoAplicacao(movidas, pais, atributos)
