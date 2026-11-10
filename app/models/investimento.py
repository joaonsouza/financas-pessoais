from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.quantidade import DecimalTexto
from app.db import Base

CLASSES_INVEST = ("acao", "fii", "etf", "exterior", "cripto", "renda_fixa")
TIPOS_OPERACAO = ("compra", "venda", "aplicacao", "resgate", "ajuste_posicao", "rendimento")
INDEXADORES = ("pre", "cdi", "ipca")
# exposição real da carteira (metas, blocos, rosca, rebalanceamento); a `classe` do ativo é só o tipo do papel
ALOCACOES = {"acoes": "Renda variável", "renda_fixa": "Renda fixa", "cripto": "Alternativo"}
_ETFS_CRIPTO = frozenset({
    "HASH11", "QBTC11", "QETH11", "BITH11", "ETHE11", "BITI11", "QSOL11", "SOLH11", "DEFI11", "NFTS11", "META11",
})
_ETFS_RENDA_FIXA = frozenset({
    "IMAB11", "IMBB11", "B5P211", "B5MB11", "IB5M11", "IRFM11", "FIXA11", "LFTS11", "LFTB11", "IDKA11", "NTNS11",
    "DEBB11", "IMA-B11",
})
# para o formulário sugerir a mesma alocação que `alocacao_padrao` enquanto o código é digitado
ETFS_POR_ALOCACAO = {"cripto": sorted(_ETFS_CRIPTO), "renda_fixa": sorted(_ETFS_RENDA_FIXA)}


def alocacao_padrao(classe: str, codigo: str) -> str:
    """Alocação inicial pelo tipo do papel; ETF de cripto ou de renda fixa (por ticker) vai para a exposição real."""
    if classe in ("renda_fixa", "cripto"):
        return classe
    if classe == "etf":
        ticker = codigo.strip().upper().removesuffix(".SA")
        if ticker in _ETFS_CRIPTO:
            return "cripto"
        if ticker in _ETFS_RENDA_FIXA:
            return "renda_fixa"
    return "acoes"


def _alocacao_do_ativo(contexto) -> str:
    p = contexto.get_current_parameters()
    return alocacao_padrao(p["classe"], p["codigo"])


class Ativo(Base):
    __tablename__ = "ativo"
    __table_args__ = (UniqueConstraint("classe", "codigo", name="uq_ativo_classe_codigo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    classe: Mapped[str] = mapped_column(String(20))  # tipo do papel: cotação, operações, imposto
    alocacao: Mapped[str] = mapped_column(String(20), default=_alocacao_do_ativo)  # exposição real: chave de ALOCACOES
    codigo: Mapped[str] = mapped_column(String(60))
    nome: Mapped[str] = mapped_column(String(120), default="")
    simbolo_cotacao: Mapped[str] = mapped_column(String(40), default="")
    moeda: Mapped[str] = mapped_column(String(3), default="BRL")
    corretora_conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    meta_pct: Mapped[Decimal] = mapped_column(DecimalTexto, default=Decimal("0"))  # % do ativo dentro da alocação
    ativo: Mapped[bool] = mapped_column(default=True)
    preco_manual: Mapped[int | None]
    preco_manual_em: Mapped[date | None]
    # % a.a. que o ativo rende sozinho (ex.: USDC na corretora): a quantidade cresce todo dia, juros compostos
    rendimento_aa: Mapped[Decimal | None] = mapped_column(DecimalTexto)
    # só renda fixa
    indexador: Mapped[str | None] = mapped_column(String(10))
    taxa: Mapped[Decimal | None] = mapped_column(DecimalTexto)
    vencimento: Mapped[date | None]
    # Tesouro Direto: operações em quantidade de títulos e saldo pelo preço de mercado (não pela curva da taxa)
    por_titulos: Mapped[bool] = mapped_column(default=False, server_default="0")


class OperacaoInvest(Base):
    __tablename__ = "operacao_invest"

    id: Mapped[int] = mapped_column(primary_key=True)
    ativo_id: Mapped[int] = mapped_column(ForeignKey("ativo.id"))
    tipo: Mapped[str] = mapped_column(String(20))
    data: Mapped[date]
    quantidade: Mapped[Decimal] = mapped_column(DecimalTexto)
    preco_unitario: Mapped[int]  # centavos na moeda do ativo
    cambio: Mapped[Decimal | None] = mapped_column(DecimalTexto)  # BRL por USD (só exterior)
    taxas: Mapped[int] = mapped_column(default=0)  # centavos BRL
    observacao: Mapped[str] = mapped_column(String(200), default="")
    # renda fixa: taxa contratada nesta aplicação (vazia = a do ativo); cada lote rende pela sua
    taxa: Mapped[Decimal | None] = mapped_column(DecimalTexto)
    # onde o lote fica (custódia): conta de investimento e, opcionalmente, a caixinha dela
    custodia_conta_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    custodia_caixinha_id: Mapped[int | None] = mapped_column(ForeignKey("caixinha.id"))
    # carteira do lote (vazia = a carteira padrão)
    carteira_id: Mapped[int | None] = mapped_column(ForeignKey("carteira.id"))
    # perna da transferência na conta que pagou/recebeu (a outra perna fica na custódia)
    lancamento_id: Mapped[int | None] = mapped_column(ForeignKey("lancamento.id"))
    # venda/resgate: quando o dinheiro cai na conta (escolhida no formulário; vazia = D+2 úteis na B3, senão no dia)
    data_liquidacao: Mapped[date | None]
    # venda/resgate com imposto retido: o que de fato caiu na conta (centavos BRL); vazio = bruto − taxas
    valor_liquido: Mapped[int | None]


class MetaClasse(Base):
    __tablename__ = "meta_classe"

    classe: Mapped[str] = mapped_column(String(20), primary_key=True)  # chave de ALOCACOES (nome da coluna é legado)
    meta_pct: Mapped[Decimal] = mapped_column(DecimalTexto, default=Decimal("0"))


class CotacaoCache(Base):
    __tablename__ = "cotacao_cache"
    __table_args__ = (UniqueConstraint("simbolo", "data", name="uq_cotacao_simbolo_data"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    simbolo: Mapped[str] = mapped_column(String(40))
    data: Mapped[date]
    fechamento: Mapped[Decimal] = mapped_column(DecimalTexto)
    moeda: Mapped[str] = mapped_column(String(3), default="BRL")
    fonte: Mapped[str] = mapped_column(String(20), default="")  # "yahoo", "brapi"; histórico: "<fonte>:mensal"
    variacao_dia_pct: Mapped[Decimal | None] = mapped_column(DecimalTexto)
    atualizado_em: Mapped[datetime]


class SerieIndicador(Base):
    __tablename__ = "serie_indicador"
    __table_args__ = (UniqueConstraint("serie", "data", name="uq_serie_data"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    serie: Mapped[str] = mapped_column(String(10))
    data: Mapped[date]
    valor_pct: Mapped[Decimal] = mapped_column(DecimalTexto)


class TaxaTesouro(Base):
    """Taxa e preço de compra de cada título do Tesouro Direto por dia (dados públicos do Tesouro Transparente)."""

    __tablename__ = "taxa_tesouro"

    titulo: Mapped[str] = mapped_column(String(60), primary_key=True)  # "Tesouro IPCA+", "Tesouro Selic"...
    vencimento: Mapped[date] = mapped_column(primary_key=True)
    data: Mapped[date] = mapped_column(primary_key=True)
    taxa_compra: Mapped[Decimal] = mapped_column(DecimalTexto)  # % a.a. (no Selic, o ágio sobre a Selic)
    pu_compra: Mapped[int]  # centavos por título
    pu_venda: Mapped[int | None]  # centavos por título: quanto o Tesouro paga na venda antecipada (preço de mercado)


class Carteira(Base):
    """Carteira com estratégia própria (metas). A padrão usa as metas de MetaClasse/Ativo.meta_pct."""

    __tablename__ = "carteira"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))
    padrao: Mapped[bool] = mapped_column(default=False)
    contas: Mapped[str] = mapped_column(String(200), default="")  # ids das contas de investimento que ela sugere


class MetaClasseCarteira(Base):
    """Meta de cada alocação nas carteiras que não são a padrão."""

    __tablename__ = "meta_classe_carteira"

    carteira_id: Mapped[int] = mapped_column(ForeignKey("carteira.id"), primary_key=True)
    classe: Mapped[str] = mapped_column(String(20), primary_key=True)
    meta_pct: Mapped[Decimal] = mapped_column(DecimalTexto, default=Decimal("0"))


class MetaAtivoCarteira(Base):
    """Meta do ativo dentro da alocação, nas carteiras que não são a padrão."""

    __tablename__ = "meta_ativo_carteira"

    carteira_id: Mapped[int] = mapped_column(ForeignKey("carteira.id"), primary_key=True)
    ativo_id: Mapped[int] = mapped_column(ForeignKey("ativo.id"), primary_key=True)
    meta_pct: Mapped[Decimal] = mapped_column(DecimalTexto, default=Decimal("0"))


class DarfPago(Base):
    """Guia de DARF marcada como paga (competência AAAA-MM e código da receita: 6015 bolsa, 4600 cripto)."""

    __tablename__ = "darf_pago"

    competencia: Mapped[str] = mapped_column(String(7), primary_key=True)
    codigo: Mapped[str] = mapped_column(String(4), primary_key=True)
    pago_em: Mapped[date]


class AtivoDesejado(Base):
    """Ativo que a carteira ainda não comprou ("ainda não comprei"), listado nela para receber meta e entrar no
    rebalanceamento. O mesmo ativo pode estar em várias carteiras."""

    __tablename__ = "ativo_desejado"

    carteira_id: Mapped[int] = mapped_column(ForeignKey("carteira.id"), primary_key=True)
    ativo_id: Mapped[int] = mapped_column(ForeignKey("ativo.id"), primary_key=True)
