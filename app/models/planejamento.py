from datetime import date

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.models.config import get_config, set_config

TIPOS_META = ("economia_mensal", "objetivo", "reserva_emergencia")
HORIZONTES = ("mes", "ano", "sonho")
METAS_503020_PADRAO = [50, 30, 20]


class Orcamento(Base):
    """Limite mensal por categoria; `categoria_id` nulo = teto total de gastos do mês."""

    __tablename__ = "orcamento"
    __table_args__ = (
        UniqueConstraint("competencia", "categoria_id", name="uq_orcamento_mes_categoria"),
        Index("uq_orcamento_teto", "competencia", unique=True, sqlite_where=text("categoria_id IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    competencia: Mapped[str] = mapped_column(String(7), index=True)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))
    limite: Mapped[int]  # centavos


class AjusteMes(Base):
    """"Um ajuste por vez": no máximo um por competência."""

    __tablename__ = "ajuste_mes"

    id: Mapped[int] = mapped_column(primary_key=True)
    competencia: Mapped[str] = mapped_column(String(7), unique=True)
    descricao: Mapped[str] = mapped_column(String(200))
    valor_liberado: Mapped[int] = mapped_column(default=0)  # centavos
    destino: Mapped[str | None] = mapped_column(String(200))


class Meta(Base):
    __tablename__ = "meta"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    tipo: Mapped[str] = mapped_column(String(20))  # economia_mensal | objetivo | reserva_emergencia
    horizonte: Mapped[str] = mapped_column(String(6), default="ano")  # mes | ano | sonho
    valor_alvo: Mapped[int] = mapped_column(default=0)
    prazo: Mapped[date | None]
    conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))  # conta onde o dinheiro da meta fica
    valor_manual: Mapped[int | None]
    multiplicador_reserva: Mapped[int] = mapped_column(default=3)  # 3 | 6
    carteira_id: Mapped[int | None] = mapped_column(ForeignKey("carteira.id"))  # reserva: aparece nela além da consolidada
    pagar_primeiro: Mapped[bool] = mapped_column(default=False)
    conta_origem_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))  # de onde sai o "pague primeiro"
    dia_pagar: Mapped[int | None]
    valor_mensal_pagar: Mapped[int | None]
    ativa: Mapped[bool] = mapped_column(default=True)
    icone: Mapped[str] = mapped_column(String(30), default="target")
    cor: Mapped[str] = mapped_column(String(9), default="#10b981")
    repete_anual: Mapped[bool] = mapped_column(default=False)  # passou o prazo, ele vai para o ano seguinte
    aporte_automatico: Mapped[bool] = mapped_column(default=False)  # mensal = o que falta ÷ meses até o prazo
    aporte_como_despesa: Mapped[bool] = mapped_column(default=False)  # pendente é despesa; ao efetivar, transferência
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))  # categoria dessa despesa


class Provisao(Base):
    """Legado: despesa anual diluída por mês. Virou meta (`migrar_provisoes_para_metas`); só a migração lê esta tabela."""

    __tablename__ = "provisao"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    valor_anual: Mapped[int]
    conta_destino_id: Mapped[int] = mapped_column(ForeignKey("conta.id"))
    conta_origem_id: Mapped[int] = mapped_column(ForeignKey("conta.id"))
    dia: Mapped[int] = mapped_column(default=5)
    caixinha_id: Mapped[int | None] = mapped_column(ForeignKey("caixinha.id"))  # onde o dinheiro da provisão se junta
    data_alvo: Mapped[date | None]  # quando a despesa vence; o mensal é o que falta dividido pelos meses até lá
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))  # despesa enquanto pendente
    repete_anual: Mapped[bool] = mapped_column(default=True)  # passou o vencimento, o alvo vai para o ano seguinte


def get_metas_503020(s: Session) -> list[int]:
    return list(get_config(s, "metas_503020", METAS_503020_PADRAO))


def set_metas_503020(s: Session, valores: list[int]) -> None:
    if len(valores) != 3 or any(v < 0 for v in valores) or sum(valores) != 100:
        raise ValueError("as três metas devem somar 100%")
    set_config(s, "metas_503020", [int(v) for v in valores])


def alertas_dispensados(s: Session, competencia: str) -> list[str]:
    return list(get_config(s, "alertas_dispensados", {}).get(competencia, []))


def dispensar_alerta(s: Session, competencia: str, chave: str) -> None:
    todos = dict(get_config(s, "alertas_dispensados", {}))
    chaves = list(todos.get(competencia, []))
    if chave not in chaves:
        chaves.append(chave)
    todos[competencia] = chaves
    set_config(s, "alertas_dispensados", todos)
