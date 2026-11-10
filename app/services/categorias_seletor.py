"""Dados do seletor de categoria: grupos por mãe, mais usadas e o rótulo de cada categoria."""
import unicodedata
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Categoria, Lancamento
from app.services.categorias_padrao import PALETA

TIPOS = ("despesa", "receita", "todas")
DIAS_MAIS_USADAS = 90
QTD_MAIS_USADAS = 5


def _chave(nome: str) -> str:
    return unicodedata.normalize("NFD", nome).encode("ascii", "ignore").decode().lower()


def opcoes_seletor(s: Session, tipo: str, hoje: date) -> dict:
    """Grupos e mais usadas só com ativas; `por_id` com todas (rótulo de uma inativa já escolhida)."""
    if tipo not in TIPOS:
        raise ValueError("tipo de categoria inválido")
    tipos = ("despesa", "receita") if tipo == "todas" else (tipo,)
    cats = list(s.scalars(select(Categoria).where(Categoria.tipo.in_(tipos))))
    por_obj = {c.id: c for c in cats}

    def rotulo(c: Categoria) -> str:
        pai = por_obj.get(c.pai_id)
        return f"{pai.nome} › {c.nome}" if pai else c.nome

    ordem = {t: i for i, t in enumerate(tipos)}
    maes = sorted((c for c in cats if c.pai_id is None and c.ativa), key=lambda c: (ordem[c.tipo], _chave(c.nome)))
    grupos = [{"id": m.id, "nome": m.nome, "cor": m.cor, "tipo": m.tipo,
               "filhas": [{"id": f.id, "nome": f.nome, "cor": f.cor}
                          for f in sorted((f for f in cats if f.pai_id == m.id and f.ativa),
                                          key=lambda f: _chave(f.nome))]}
              for m in maes]
    contagem = func.count(Lancamento.id)
    mais = s.execute(
        select(Lancamento.categoria_id, contagem)
        .where(Lancamento.categoria_id.in_([c.id for c in cats if c.ativa]), Lancamento.status == "efetivado",
               Lancamento.data > hoje - timedelta(days=DIAS_MAIS_USADAS), Lancamento.data <= hoje)
        .group_by(Lancamento.categoria_id).order_by(contagem.desc(), Lancamento.categoria_id)
        .limit(QTD_MAIS_USADAS)).all()
    return {
        "grupos": grupos,
        "mais_usadas": [cid for cid, _ in mais],
        "por_id": {str(c.id): {"nome": c.nome, "rotulo": rotulo(c), "cor": c.cor, "inativa": not c.ativa}
                   for c in cats},
    }


def criar_categoria_rapida(s: Session, nome: str, tipo: str, pai_id: int | None) -> Categoria:
    """Categoria criada pelo seletor: filha herda da mãe; principal nasce com o básico (o resto, em Configurações)."""
    nome = " ".join((nome or "").split())[:60]
    if not nome:
        raise ValueError("informe o nome da categoria")
    if tipo not in ("despesa", "receita"):
        raise ValueError("tipo de categoria inválido")
    pai = s.get(Categoria, pai_id) if pai_id is not None else None
    if pai_id is not None and (pai is None or pai.tipo != tipo or pai.pai_id is not None):
        raise ValueError("a categoria-mãe precisa ser uma categoria principal do mesmo tipo")
    irmas = select(Categoria.id).where(Categoria.tipo == tipo, func.lower(Categoria.nome) == nome.lower(),
                                       Categoria.pai_id.is_(None) if pai is None else Categoria.pai_id == pai.id)
    if s.scalar(irmas.limit(1)) is not None:
        raise ValueError(f"já existe a categoria {nome}")
    if pai is not None:
        nova = Categoria(nome=nome, tipo=tipo, pai_id=pai.id, grupo=pai.grupo, natureza=pai.natureza,
                         essencial=pai.essencial, cor=pai.cor, inflacao=pai.inflacao)
    else:
        total = s.scalar(select(func.count(Categoria.id)))
        nova = Categoria(nome=nome, tipo=tipo, cor=PALETA[total % len(PALETA)],
                         grupo="variavel" if tipo == "despesa" else None,
                         natureza="desejo" if tipo == "despesa" else None)
    s.add(nova)
    s.flush()
    return nova
