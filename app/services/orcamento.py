"""Orçamento mensal por categoria e teto total (spec §6.6)."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Categoria, Lancamento, Orcamento, get_metas_503020
from app.services.mes_caixa import com_status, no_mes
from app.services.resumo_mes import totais_mes

FAIXA_ATENCAO = 80
FAIXA_ESTOURADO = 100


@dataclass(frozen=True)
class LinhaOrcamento:
    categoria: Categoria | None  # None = teto total
    limite: int
    efetivado: int
    projetado: int
    pct: int
    faixa: str  # ok | atencao | estourado
    automatico: bool = False  # teto calculado das receitas, não salvo


def faixa_de(gasto: int, limite: int) -> str:
    if limite <= 0 or gasto * 100 >= FAIXA_ESTOURADO * limite:
        return "estourado"
    if gasto * 100 >= FAIXA_ATENCAO * limite:
        return "atencao"
    return "ok"


def _pct(gasto: int, limite: int) -> int:
    return gasto * 100 // limite if limite > 0 else 100


def _linha(categoria: Categoria | None, limite: int, efetivado: int, projetado: int,
           automatico: bool = False) -> LinhaOrcamento:
    # a faixa e o % usam o projetado (efetivado + pendente): avisa antes de estourar
    return LinhaOrcamento(categoria, limite, efetivado, projetado, _pct(projetado, limite),
                          faixa_de(projetado, limite), automatico)


def _soma(s: Session, competencia: str, ids: list[int], status: tuple[str, ...]) -> int:
    total = s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.tipo == "despesa",
        Lancamento.categoria_id.in_(ids),
        no_mes(competencia),
        com_status(status),
        Lancamento.ignorar.is_(False),
    ))
    return -int(total)


def gasto_categoria(s: Session, competencia: str, categoria_id: int) -> tuple[int, int]:
    """(efetivado, projetado) em centavos positivos, incluindo as subcategorias."""
    ids = [categoria_id, *s.scalars(select(Categoria.id).where(Categoria.pai_id == categoria_id))]
    return _soma(s, competencia, ids, ("efetivado",)), _soma(s, competencia, ids, ("efetivado", "pendente"))


def painel_orcamento(s: Session, competencia: str) -> list[LinhaOrcamento]:
    linhas = []
    itens = s.execute(
        select(Orcamento, Categoria).join(Categoria, Orcamento.categoria_id == Categoria.id)
        .where(Orcamento.competencia == competencia).order_by(Categoria.nome)
    ).all()
    for orc, cat in itens:
        efetivado, projetado = gasto_categoria(s, competencia, cat.id)
        linhas.append(_linha(cat, orc.limite, efetivado, projetado))
    return linhas


def teto_mes(s: Session, competencia: str) -> LinhaOrcamento | None:
    """O teto salvo do mês; sem ele, as receitas projetadas menos a terceira meta do 50/30/20 (80%, por padrão)."""
    orc = s.scalar(select(Orcamento).where(Orcamento.competencia == competencia, Orcamento.categoria_id.is_(None)))
    totais = totais_mes(s, competencia)
    if orc is not None:
        return _linha(None, orc.limite, totais.despesas_efetivado, totais.despesas_projetado)
    limite = totais.receitas_projetado * (100 - get_metas_503020(s)[2]) // 100
    if limite <= 0:
        return None
    return _linha(None, limite, totais.despesas_efetivado, totais.despesas_projetado, automatico=True)


def definir_limite(s: Session, competencia: str, categoria_id: int | None, limite: int) -> None:
    """Cria ou atualiza o limite; limite <= 0 remove."""
    filtro = Orcamento.categoria_id.is_(None) if categoria_id is None else Orcamento.categoria_id == categoria_id
    orc = s.scalar(select(Orcamento).where(Orcamento.competencia == competencia, filtro))
    if limite <= 0:
        if orc:
            s.delete(orc)
    elif orc:
        orc.limite = limite
    else:
        s.add(Orcamento(competencia=competencia, categoria_id=categoria_id, limite=limite))
    s.flush()


def copiar_orcamento(s: Session, de: str, para: str) -> int:
    """Copia os limites (e o teto) de um mês para outro sem sobrescrever; devolve quantos criou."""
    existentes = set(s.scalars(select(Orcamento.categoria_id).where(Orcamento.competencia == para)))
    criados = 0
    for orc in s.scalars(select(Orcamento).where(Orcamento.competencia == de)).all():
        if orc.categoria_id in existentes:
            continue
        s.add(Orcamento(competencia=para, categoria_id=orc.categoria_id, limite=orc.limite))
        criados += 1
    s.flush()
    return criados
