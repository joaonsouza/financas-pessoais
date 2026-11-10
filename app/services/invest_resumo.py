"""Aba "Resumo" dos investimentos, no formato do Investidor10: cards de topo, evolução empilhada e rosca.

Convenções:
- investido = custo das posições abertas (`Carteira.aplicado`); ganho de capital = patrimônio − investido;
- rentabilidade total = Dietz encadeado de `evolucao_mensal`; sem base para ele (menos de 2 pontos mensais, ou 0%
  com ganho), a simples, patrimônio ÷ investido − 1, marcada como tal; 12M = acumulado do último ponto contra o de 12 meses
  antes (sem esse ponto, None, e a tela mostra "—");
- evolução empilhada: aplicado (base) + ganho (topo) por mês; com ganho negativo, o topo vai a 0 e a base mostra o
  patrimônio, mas os valores reais seguem na barra para a dica;
- rosca: cores fixas por alocação; nos ativos de uma alocação, cores fixas por posição (do maior para o menor) e o
  que passar da paleta vira "Outros", para nunca repetir cor.
"""
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import somar_meses
from app.models.investimento import ALOCACOES, CotacaoCache, SerieIndicador
from app.services.invest_carteira import CEM, SIMBOLO_CAMBIO, Carteira, pct
from app.services.invest_historico import SIMBOLO_IBOV, PontoMes
from app.services.mercado.cotacoes import SUFIXO_MENSAL

UM = Decimal(1)
# risco de cada alocação (rótulo, cor): verde = baixo, amarelo = médio, vermelho = alto
RISCO_CLASSE = {"renda_fixa": ("Baixo", "#16A34A"), "acoes": ("Médio", "#F59E0B"), "cripto": ("Alto", "#DC2626")}
CORES_CLASSE = {classe: cor for classe, (_, cor) in RISCO_CLASSE.items()}  # por alocação, pela cor do risco
CORES_ATIVOS = ("#2563EB", "#16A34A", "#F59E0B", "#7C3AED", "#DB2777", "#0891B2", "#EA580C", "#65A30D")
COR_OUTROS = "#94A3B8"
PERIODOS = {"12": 12, "24": 24, "tudo": None}  # meses da evolução; "tudo" = desde a primeira operação


@dataclass(frozen=True)
class CardsResumo:
    patrimonio: int
    investido: int
    variacao_pct: Decimal | None  # patrimônio sobre o investido
    ganho: int
    rent_12m_pct: Decimal | None
    rent_total_pct: Decimal | None
    rent_total_simples: bool = False  # True: patrimônio ÷ investido − 1 (o histórico não tem base para o Dietz)


@dataclass(frozen=True)
class BarraMes:
    rotulo: str  # "MM/AA"
    aplicado: int
    ganho: int  # pode ser negativo
    patrimonio: int

    @property
    def base(self) -> int:
        """Segmento de baixo: o aplicado, ou o patrimônio quando ele fica abaixo do aplicado."""
        return min(self.aplicado, self.patrimonio)

    @property
    def topo(self) -> int:
        return max(self.ganho, 0)


@dataclass(frozen=True)
class Fatia:
    rotulo: str
    valor: int
    pct: Decimal
    cor: str


def rentabilidade_12m(pontos: list[PontoMes]) -> Decimal | None:
    if len(pontos) < 13:
        return None
    return ((UM + pontos[-1].rent_acum_pct / CEM) / (UM + pontos[-13].rent_acum_pct / CEM) - UM) * CEM


def cards_resumo(carteira: Carteira, pontos: list[PontoMes]) -> CardsResumo:
    """Total pelo Dietz do histórico; sem histórico que meça (menos de 2 pontos, ou 0% com ganho), a simples."""
    total = pontos[-1].rent_acum_pct if len(pontos) >= 2 else None
    simples = total is None or (total == 0 and carteira.lucro_nao_realizado != 0)
    return CardsResumo(
        patrimonio=carteira.patrimonio, investido=carteira.aplicado, variacao_pct=carteira.rent_pct,
        ganho=carteira.lucro_nao_realizado, rent_12m_pct=rentabilidade_12m(pontos),
        rent_total_pct=carteira.rent_pct if simples else total, rent_total_simples=simples,
    )


def barras_evolucao(pontos: list[PontoMes], meses: int | None) -> list[BarraMes]:
    """Últimos `meses` pontos (todos com None) como barras de aplicado + ganho."""
    recorte = pontos[-meses:] if meses else pontos
    return [BarraMes(rotulo=f"{p.competencia[5:]}/{p.competencia[2:4]}", aplicado=p.aplicado_acumulado,
                     ganho=p.patrimonio - p.aplicado_acumulado, patrimonio=p.patrimonio) for p in recorte]


def rentabilidade_no_periodo(pontos: list[PontoMes], meses: int | None) -> list[PontoMes]:
    """Comparação como no TradingView: carteira, CDI, Ibovespa e índice global medidos a partir do mesmo ponto, o primeiro do
    gráfico, em 0%. No recorte, o mês anterior a ele vira esse ponto e os acumulados são refeitos a partir dele; sem
    esse mês (ou "desde o início"), o ponto de partida é o mês antes da primeira operação, quando tudo valia 0%."""
    if not pontos:
        return pontos
    if not meses or len(pontos) <= meses:
        inicio = replace(pontos[0], competencia=somar_meses(pontos[0].competencia, -1), patrimonio=0,
                         aplicado_acumulado=0, rent_mes_pct=Decimal(0), rent_acum_pct=Decimal(0),
                         cdi_acum_pct=Decimal(0) if pontos[0].cdi_acum_pct is not None else None,
                         ibov_acum_pct=Decimal(0) if pontos[0].ibov_acum_pct is not None else None,
                         global_acum_pct=Decimal(0) if pontos[0].global_acum_pct is not None else None)
        return [inicio, *pontos]
    base = pontos[-meses - 1]

    def desde_a_base(valor: Decimal | None, inicio: Decimal | None) -> Decimal | None:
        if valor is None:
            return None
        return ((UM + valor / CEM) / (UM + (inicio or 0) / CEM) - UM) * CEM

    return [replace(p, rent_acum_pct=desde_a_base(p.rent_acum_pct, base.rent_acum_pct),
                    cdi_acum_pct=desde_a_base(p.cdi_acum_pct, base.cdi_acum_pct),
                    ibov_acum_pct=desde_a_base(p.ibov_acum_pct, base.ibov_acum_pct),
                    global_acum_pct=desde_a_base(p.global_acum_pct, base.global_acum_pct))
            for p in pontos[-meses - 1:]]


def distribuicao(carteira: Carteira, classe: str | None = None) -> list[Fatia]:
    """Sem classe: uma fatia por alocação (% da carteira). Com classe (chave de alocação): os ativos dela (% dela)."""
    if classe is None:
        return [Fatia(ALOCACOES[c.classe], c.saldo, pct(c.saldo, carteira.patrimonio), CORES_CLASSE[c.classe])
                for c in carteira.classes if c.saldo > 0]
    linha = next((c for c in carteira.classes if c.classe == classe), None)
    if linha is None:
        return []
    ativos = sorted((a for a in linha.ativos if a.saldo > 0), key=lambda a: -a.saldo)
    fatias = [Fatia(a.ativo.codigo, a.saldo, pct(a.saldo, linha.saldo), cor)
              for a, cor in zip(ativos, CORES_ATIVOS)]
    resto = sum(a.saldo for a in ativos[len(CORES_ATIVOS):])
    if resto:
        fatias.append(Fatia("Outros", resto, pct(resto, linha.saldo), COR_OUTROS))
    return fatias


# ---------- variação do dia e mercado ----------

@dataclass(frozen=True)
class Mercado:
    cdi_12m_pct: Decimal | None  # CDI acumulado nos últimos 12 meses
    ipca_12m_pct: Decimal | None  # IPCA dos 12 últimos meses divulgados
    ipca_ate: date | None  # mês do último IPCA divulgado
    cdi_aa_pct: Decimal | None  # taxa do CDI de hoje, ao ano (252 dias úteis)
    ibov: Decimal | None
    ibov_var_pct: Decimal | None
    dolar: Decimal | None
    dolar_var_pct: Decimal | None


@dataclass(frozen=True)
class MesAtual:
    aportes: int  # aplicado no mês (líquido de vendas pelo custo)
    rendimento: int  # o que o patrimônio mudou além dos aportes
    rent_pct: Decimal


def mes_atual(pontos: list[PontoMes]) -> MesAtual | None:
    """O mês corrente, contra o fechamento do mês anterior (sem o anterior, None)."""
    if len(pontos) < 2:
        return None
    antes, agora = pontos[-2], pontos[-1]
    aportes = agora.aplicado_acumulado - antes.aplicado_acumulado
    return MesAtual(aportes, agora.patrimonio - antes.patrimonio - aportes, agora.rent_mes_pct)


def variacao_do_dia(carteira: Carteira) -> tuple[int, Decimal] | None:
    """Quanto a carteira mudou desde o fechamento anterior, pelos ativos com variação do dia: cotados contra o
    pregão anterior, Tesouro pelo preço de venda da véspera, renda fixa na curva pelo último dia útil do CDI.
    Ativo em dólar entra pela variação dele na moeda dele (sem a do câmbio)."""
    com_variacao = [a for a in carteira.ativos if a.variacao_dia_pct is not None and a.saldo]
    if not com_variacao:
        return None
    valor = sum(round(a.saldo - Decimal(a.saldo) / (UM + a.variacao_dia_pct / CEM)) for a in com_variacao)
    ontem = sum(a.saldo for a in carteira.ativos) - valor
    return int(valor), pct(valor, ontem)


def acima_da_inflacao(rent_pct: Decimal | None, ipca_pct: Decimal | None) -> Decimal | None:
    """Rentabilidade real: o quanto rendeu além do IPCA, (1 + r) ÷ (1 + ipca) − 1."""
    if rent_pct is None or ipca_pct is None:
        return None
    return ((UM + rent_pct / CEM) / (UM + ipca_pct / CEM) - UM) * CEM


def _acumulado(valores: list[Decimal]) -> Decimal:
    total = UM
    for v in valores:
        total *= UM + v / CEM
    return (total - UM) * CEM


def _cotacao(s: Session, simbolo: str) -> tuple[Decimal | None, Decimal | None]:
    linha = s.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == simbolo,
                                                ~CotacaoCache.fonte.like(f"%{SUFIXO_MENSAL}"))
                     .order_by(CotacaoCache.data.desc()).limit(1))
    return (None, None) if linha is None else (linha.fechamento, linha.variacao_dia_pct)


def mercado(s: Session, hoje: date) -> Mercado:
    cdi = list(s.scalars(select(SerieIndicador.valor_pct).where(
        SerieIndicador.serie == "cdi", SerieIndicador.data > hoje - timedelta(days=365), SerieIndicador.data <= hoje)
        .order_by(SerieIndicador.data)))
    ipca = list(s.execute(select(SerieIndicador.data, SerieIndicador.valor_pct).where(
        SerieIndicador.serie == "ipca", SerieIndicador.data <= hoje).order_by(SerieIndicador.data.desc()).limit(12)))
    ibov, ibov_var = _cotacao(s, SIMBOLO_IBOV)
    dolar, dolar_var = _cotacao(s, SIMBOLO_CAMBIO)
    return Mercado(
        cdi_12m_pct=_acumulado(cdi) if cdi else None,
        ipca_12m_pct=_acumulado([v for _, v in ipca]) if len(ipca) == 12 else None,
        ipca_ate=ipca[0][0] if ipca else None,
        cdi_aa_pct=((UM + cdi[-1] / CEM) ** 252 - UM) * CEM if cdi else None,
        ibov=ibov, ibov_var_pct=ibov_var, dolar=dolar, dolar_var_pct=dolar_var,
    )
