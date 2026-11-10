"""Gestão de categorias: uso, mãe, juntar/excluir e regras automáticas (tela de Configurações › Categorias)."""
import unicodedata
from dataclasses import dataclass, fields
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Categoria, Favorito, Lancamento, Meta, Orcamento, Provisao, Recorrencia, RegraCategoria

DIAS_USO = 365
# o que aponta para uma categoria e só troca o `categoria_id` ao juntar (ou fica nulo ao excluir)
LIGADOS = {"lancamentos": Lancamento, "recorrencias": Recorrencia, "favoritos": Favorito, "metas": Meta,
           "provisoes": Provisao}
ROTULOS = {"lancamentos": ("lançamento", "lançamentos"), "recorrencias": ("recorrência", "recorrências"),
           "favoritos": ("favorito", "favoritos"), "regras": ("regra", "regras"),
           "orcamentos": ("limite de orçamento", "limites de orçamento"), "metas": ("meta", "metas"),
           "provisoes": ("provisão", "provisões"), "filhas": ("subcategoria", "subcategorias")}


@dataclass
class Impacto:
    lancamentos: int = 0
    recorrencias: int = 0
    favoritos: int = 0
    regras: int = 0
    orcamentos: int = 0
    metas: int = 0
    provisoes: int = 0
    filhas: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, f.name) for f in fields(self))

    def texto(self) -> str:
        """"41 lançamentos, 6 regras e 1 meta" (só o que não é zero)."""
        partes = [f"{n} {ROTULOS[f.name][0 if n == 1 else 1]}" for f in fields(self) if (n := getattr(self, f.name))]
        if len(partes) <= 1:
            return partes[0] if partes else "nada"
        return ", ".join(partes[:-1]) + " e " + partes[-1]


def uso_por_categoria(s: Session, hoje: date) -> dict[int, tuple[int, int]]:
    """(lançamentos efetivados, total em centavos) dos últimos 12 meses; a mãe soma as filhas."""
    linhas = s.execute(
        select(Lancamento.categoria_id, func.count(Lancamento.id), func.sum(func.abs(Lancamento.valor)))
        .where(Lancamento.categoria_id.is_not(None), Lancamento.status == "efetivado",
               Lancamento.data > hoje - timedelta(days=DIAS_USO), Lancamento.data <= hoje)
        .group_by(Lancamento.categoria_id)).all()
    proprio = {cid: (qtd, int(total or 0)) for cid, qtd, total in linhas}
    uso = dict(proprio)
    for cid, pai_id in s.execute(select(Categoria.id, Categoria.pai_id).where(Categoria.pai_id.is_not(None))):
        if cid in proprio:
            qtd, total = uso.get(pai_id, (0, 0))
            uso[pai_id] = (qtd + proprio[cid][0], total + proprio[cid][1])
    return uso


def impacto(s: Session, cat: Categoria) -> Impacto:
    contar = lambda modelo, coluna=None: s.scalar(  # noqa: E731
        select(func.count()).select_from(modelo).where((coluna or modelo.categoria_id) == cat.id))
    return Impacto(**{nome: contar(modelo) for nome, modelo in LIGADOS.items()},
                   regras=contar(RegraCategoria), orcamentos=contar(Orcamento), filhas=contar(Categoria, Categoria.pai_id))


def remover_categoria(s: Session, cat: Categoria, destino: Categoria | None) -> Impacto:
    """Junta `cat` em `destino` (tudo passa para ela) ou, sem destino, exclui deixando tudo sem categoria."""
    total = impacto(s, cat)
    if destino is not None:
        if destino.id == cat.id or destino.tipo != cat.tipo:
            raise ValueError("escolha outra categoria do mesmo tipo")
        if destino.pai_id == cat.id:
            raise ValueError(f"{destino.nome} é subcategoria de {cat.nome}: escolha outra")
        if total.filhas and destino.pai_id is not None:
            raise ValueError(f"as subcategorias de {cat.nome} precisam de uma categoria principal: escolha uma")
    elif total.filhas:
        raise ValueError(f"{cat.nome} tem subcategorias: mova-as antes ou junte com outra categoria principal")
    novo = destino.id if destino else None
    for modelo in LIGADOS.values():
        s.execute(update(modelo).where(modelo.categoria_id == cat.id).values(categoria_id=novo))
    if destino is None:
        for modelo in (RegraCategoria, Orcamento):
            for linha in s.scalars(select(modelo).where(modelo.categoria_id == cat.id)):
                s.delete(linha)
    else:
        padroes = set(s.scalars(select(RegraCategoria.padrao).where(RegraCategoria.categoria_id == destino.id)))
        for regra in s.scalars(select(RegraCategoria).where(RegraCategoria.categoria_id == cat.id)):
            if regra.padrao in padroes:
                s.delete(regra)  # o destino já tem a mesma regra
            else:
                regra.categoria_id = destino.id
        limites = {o.competencia: o for o in s.scalars(select(Orcamento).where(Orcamento.categoria_id == destino.id))}
        for orc in s.scalars(select(Orcamento).where(Orcamento.categoria_id == cat.id)):
            if orc.competencia in limites:
                limites[orc.competencia].limite += orc.limite
                s.delete(orc)
            else:
                orc.categoria_id = destino.id
        for filha in s.scalars(select(Categoria).where(Categoria.pai_id == cat.id)):
            filha.pai_id = destino.id
    s.flush()
    s.delete(cat)
    s.flush()
    return total


def definir_mae(s: Session, cat: Categoria, pai_id: int | None) -> None:
    if pai_id == cat.pai_id:
        return
    if pai_id is not None:
        pai = s.get(Categoria, pai_id)
        if pai is None or pai.tipo != cat.tipo or pai.pai_id is not None or pai.id == cat.id:
            raise ValueError("a categoria-mãe precisa ser uma categoria principal do mesmo tipo")
        if s.scalar(select(func.count(Categoria.id)).where(Categoria.pai_id == cat.id)):
            raise ValueError("esta categoria já tem subcategorias e não pode virar subcategoria")
    cat.pai_id = pai_id
    s.flush()


def regras_da_categoria(s: Session, categoria_id: int) -> list[RegraCategoria]:
    return list(s.scalars(select(RegraCategoria).where(RegraCategoria.categoria_id == categoria_id)
                          .order_by(RegraCategoria.padrao)))


def trocar_regra(s: Session, regra: RegraCategoria, categoria_id: int) -> None:
    atual, nova = s.get(Categoria, regra.categoria_id), s.get(Categoria, categoria_id)
    if nova is None or (atual is not None and nova.tipo != atual.tipo):
        raise ValueError("escolha uma categoria do mesmo tipo")
    regra.categoria_id = nova.id
    s.flush()


def apagar_regra(s: Session, regra: RegraCategoria) -> None:
    s.delete(regra)
    s.flush()


def _chave_nome(c: Categoria) -> tuple:
    return (not c.ativa, unicodedata.normalize("NFD", c.nome).encode("ascii", "ignore").decode().lower())


def arvore(s: Session, tipo: str) -> list[tuple[Categoria, list[Categoria]]]:
    """Mães por nome com as filhas por nome; inativas no fim de cada nível."""
    todas = list(s.scalars(select(Categoria).where(Categoria.tipo == tipo)))
    maes = sorted((c for c in todas if c.pai_id is None), key=_chave_nome)
    return [(m, sorted((f for f in todas if f.pai_id == m.id), key=_chave_nome)) for m in maes]
