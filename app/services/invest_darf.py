"""DARF mensal de renda variável (bolsa) e cripto: precisa pagar? Quanto, com qual código e até quando.

Regras de 2026 (a MP 1.303/2025 caducou; valem as anteriores), todas as carteiras no mesmo CPF:
- comum (15%, código 6015): ações, ETFs de renda variável e BDRs. Ações isentas se as vendas de ações do mês
  somarem até R$ 20 mil (o lucro delas fica isento; o prejuízo continua abatendo depois). ETF e BDR não têm isenção.
- FII (20%, código 6015, na mesma guia da comum): sem isenção; o prejuízo só abate lucro de FII.
- cripto (15%, código 4600): isenta se as vendas do mês somarem até R$ 35 mil. Acima, 15% sobre as vendas com
  ganho (regime de ganho de capital: prejuízo não abate nada). Faixas acima de R$ 5 milhões ficam de fora.
- fora: ETF de renda fixa (IR retido na fonte), renda fixa e exterior (declaração anual).
Imposto abaixo de R$ 10 não gera guia: acumula, por código, para o mês seguinte. Vencimento: último dia útil do mês
seguinte (sem feriados). Multa e juros de atraso ficam com o Sicalc, onde a guia é emitida.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ativo
from app.models.investimento import _ETFS_RENDA_FIXA, DarfPago
from app.services.invest_carteira import operacoes_por_ativo, posicao_do_ativo, series_indicadores

ISENCAO_ACOES = 2_000_000  # centavos de vendas de ações no mês
ISENCAO_CRIPTO = 3_500_000
MINIMO_GUIA = 1000
CODIGO_BOLSA, CODIGO_CRIPTO = "6015", "4600"
ROTULOS = {CODIGO_BOLSA: "Bolsa (ações, ETFs, FIIs)", CODIGO_CRIPTO: "Cripto"}
SICALC = "https://sicalc.receita.economia.gov.br/sicalc/principal"
_SUFIXOS_BDR = ("32", "33", "34", "35", "39")


def categoria(ativo: Ativo) -> str | None:
    """"acao" (conta para a isenção de R$ 20 mil), "comum" (ETF/BDR), "fii", "cripto" ou None (fora do DARF)."""
    codigo = (ativo.codigo or "").strip().upper().removesuffix(".SA")
    if ativo.classe == "acao":
        return "comum" if codigo.endswith(_SUFIXOS_BDR) else "acao"
    if ativo.classe == "etf":
        return None if codigo in _ETFS_RENDA_FIXA or ativo.alocacao == "renda_fixa" else "comum"
    if ativo.classe == "fii":
        return "fii"
    if ativo.classe == "cripto":
        return "cripto"
    return None


def vencimento(competencia: str) -> date:
    """Último dia útil (seg-sex) do mês seguinte à competência AAAA-MM."""
    ano, mes = int(competencia[:4]), int(competencia[5:])
    ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    dia = (date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)) - timedelta(days=1)
    while dia.weekday() >= 5:
        dia -= timedelta(days=1)
    return dia


@dataclass(frozen=True)
class Venda:
    data: date
    categoria: str
    vendido: int  # centavos: valor bruto da venda (é o que conta para as isenções)
    resultado: int  # lucro (ou prejuízo) da venda, já sem as taxas


@dataclass
class LinhaDarf:
    competencia: str  # AAAA-MM
    codigo: str
    vendido: int
    resultado: int
    imposto: int  # do mês, antes do acumulado
    valor: int  # da guia (com o acumulado de meses abaixo de R$ 10); 0 sem guia
    situacao: str  # pagar | pago | isento | prejuizo | acumula
    vencimento: date
    vencido: bool = False
    pago_em: date | None = None
    rotulo: str = field(init=False)

    def __post_init__(self):
        self.rotulo = ROTULOS[self.codigo]


def vendas(s: Session) -> list[Venda]:
    """Cada venda de ativo que entra no DARF, com o resultado dela (diferença do lucro realizado antes e depois)."""
    cdi, ipca = series_indicadores(s)
    por_ativo = operacoes_por_ativo(s, reserva="todas", oportunidade="todas")
    ativos = {a.id: a for a in s.scalars(select(Ativo).where(Ativo.id.in_(list(por_ativo))))}
    lista = []
    for ativo_id, ops in por_ativo.items():
        cat = categoria(ativos[ativo_id])
        if cat is None:
            continue
        for k, op in enumerate(ops):
            if op.tipo != "venda":
                continue
            antes = posicao_do_ativo(ativos[ativo_id], ops[:k], cdi, ipca).lucro_realizado_brl if k else 0
            depois = posicao_do_ativo(ativos[ativo_id], ops[:k + 1], cdi, ipca).lucro_realizado_brl
            bruto = Decimal(op.quantidade) * op.preco_unitario * (Decimal(op.cambio) if op.cambio else 1)
            lista.append(Venda(op.data, cat, int(bruto.quantize(Decimal(1), rounding=ROUND_HALF_UP)), depois - antes))
    return sorted(lista, key=lambda v: v.data)


def _pct(valor: int, aliquota: str) -> int:
    return int((Decimal(valor) * Decimal(aliquota)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def apuracao(s: Session, hoje: date) -> list[LinhaDarf]:
    """Uma linha por mês e código com venda que entra no DARF, do mês mais antigo ao mais recente."""
    por_mes: dict[str, list[Venda]] = defaultdict(list)
    for v in vendas(s):
        por_mes[f"{v.data.year:04d}-{v.data.month:02d}"].append(v)
    pagos = {(p.competencia, p.codigo): p.pago_em for p in s.scalars(select(DarfPago))}
    prejuizo = {"comum": 0, "fii": 0}
    acumulado = {CODIGO_BOLSA: 0, CODIGO_CRIPTO: 0}
    linhas: list[LinhaDarf] = []

    def fechar(competencia: str, codigo: str, vendido: int, resultado: int, imposto: int, sem_imposto: str) -> None:
        total = imposto + acumulado[codigo]
        venc = vencimento(competencia)
        if imposto == 0:
            situacao, valor = sem_imposto, 0
        elif total < MINIMO_GUIA:
            situacao, valor, acumulado[codigo] = "acumula", 0, total
        else:
            situacao, valor, acumulado[codigo] = "pagar", total, 0
        pago_em = pagos.get((competencia, codigo)) if situacao == "pagar" else None
        linhas.append(LinhaDarf(competencia, codigo, vendido, resultado, imposto, valor,
                                "pago" if pago_em else situacao, venc,
                                vencido=situacao == "pagar" and not pago_em and venc < hoje, pago_em=pago_em))

    for competencia in sorted(por_mes):
        do_mes = por_mes[competencia]
        bolsa = [v for v in do_mes if v.categoria in ("acao", "comum", "fii")]
        if bolsa:
            acoes = [v for v in bolsa if v.categoria == "acao"]
            isento = sum(v.vendido for v in acoes) <= ISENCAO_ACOES
            lucro_acoes = sum(v.resultado for v in acoes)
            base_comum = sum(v.resultado for v in bolsa if v.categoria == "comum") + (
                min(lucro_acoes, 0) if isento else lucro_acoes)
            tributavel = {}
            for chave, base in (("comum", base_comum), ("fii", sum(v.resultado for v in bolsa if v.categoria == "fii"))):
                if base > 0:
                    abate = min(prejuizo[chave], base)
                    prejuizo[chave] -= abate
                    tributavel[chave] = base - abate
                else:
                    prejuizo[chave] += -base
                    tributavel[chave] = 0
            imposto = _pct(tributavel["comum"], "0.15") + _pct(tributavel["fii"], "0.20")
            sem_imposto = "isento" if isento and lucro_acoes > 0 else "prejuizo"  # sem imposto: lucro isento ou prejuízo
            fechar(competencia, CODIGO_BOLSA, sum(v.vendido for v in bolsa), sum(v.resultado for v in bolsa),
                   imposto, sem_imposto)
        cripto = [v for v in do_mes if v.categoria == "cripto"]
        if cripto:
            vendido = sum(v.vendido for v in cripto)
            ganhos = sum(v.resultado for v in cripto if v.resultado > 0)
            imposto = 0 if vendido <= ISENCAO_CRIPTO else _pct(ganhos, "0.15")
            fechar(competencia, CODIGO_CRIPTO, vendido, sum(v.resultado for v in cripto), imposto,
                   "isento" if ganhos > 0 else "prejuizo")
    return linhas


def alternar_pago(s: Session, competencia: str, codigo: str, hoje: date) -> None:
    """Marca a guia do mês como paga (hoje) ou desfaz."""
    linha = s.get(DarfPago, (competencia, codigo))
    if linha is None:
        s.add(DarfPago(competencia=competencia, codigo=codigo, pago_em=hoje))
    else:
        s.delete(linha)
    s.flush()
