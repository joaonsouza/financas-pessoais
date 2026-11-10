"""Cálculo dos 5 KPIs principais da tela de Desempenho com comparações históricas."""
from dataclasses import dataclass
from sqlalchemy.orm import Session

from app.core.periodo import somar_meses
from app.services.desempenho.base import (
    carregar_totais_periodo,
    meses_media_referencia,
    totais_do_mes,
    TotaisMensais,
)


@dataclass(frozen=True)
class Variacao:
    diferenca: int | float  # centavos para valor monetário, p.p. para taxa de poupança
    pct: float | None       # % em relação à base de comparação; None se base for 0
    sentido: str            # "alta" | "queda" | "neutro"
    positivo: bool          # True se a mudança for favorável ao usuário


@dataclass(frozen=True)
class ItemKPI:
    chave: str
    titulo: str
    valor_realizado: int | float | None  # centavos (int) ou percentual (float)
    valor_projetado: int | float | None
    eh_percentual: bool
    delta_anterior: Variacao | None
    delta_media: Variacao | None
    acao_url: str


@dataclass(frozen=True)
class PainelKPIs:
    competencia: str
    tem_media: bool
    meses_media: list[str]
    receitas: ItemKPI
    despesas: ItemKPI
    sobra: ItemKPI
    taxa_poupanca: ItemKPI
    guardado: ItemKPI

    @property
    def lista(self) -> list[ItemKPI]:
        return [self.receitas, self.despesas, self.sobra, self.taxa_poupanca, self.guardado]


def _calcular_variacao(atual: int, base: int, inverter_polaridade: bool = False) -> Variacao:
    diff = atual - base
    if diff == 0:
        return Variacao(diferenca=0, pct=0.0, sentido="neutro", positivo=True)
    pct = round((diff * 100) / abs(base), 1) if base != 0 else None
    subiu = diff > 0
    sentido = "alta" if subiu else "queda"
    # Se inverter_polaridade=True (ex: despesas), subir é desfavorável (positivo=False)
    positivo = not subiu if inverter_polaridade else subiu
    return Variacao(diferenca=diff, pct=pct, sentido=sentido, positivo=positivo)


def _calcular_variacao_pp(atual: float | None, base: float | None) -> Variacao | None:
    if atual is None or base is None:
        return None
    diff = round(atual - base, 1)
    if diff == 0:
        return Variacao(diferenca=0.0, pct=0.0, sentido="neutro", positivo=True)
    subiu = diff > 0
    return Variacao(
        diferenca=diff,
        pct=diff,  # para taxa, mostramos o delta em p.p.
        sentido="alta" if subiu else "queda",
        positivo=subiu,
    )


def calcular_kpis(s: Session, competencia: str) -> PainelKPIs:
    anterior_comp = somar_meses(competencia, -1)
    meses_media = meses_media_referencia(s, competencia, n=3)

    # Carrega todos os meses relevantes
    todos_meses = set([competencia, anterior_comp] + meses_media)
    inicio_busca = min(todos_meses)
    fim_busca = max(todos_meses)
    totais = carregar_totais_periodo(s, inicio_busca, fim_busca)

    mes_atual = totais[competencia]
    mes_ant = totais.get(anterior_comp)

    # Média dos meses de referência (usa valor projetado/total de cada mês passado para refletir o mês fechado)
    tem_media = len(meses_media) > 0
    n_media = len(meses_media) or 1
    media_rec = sum(totais[m].receitas_projetado for m in meses_media) // n_media if tem_media else 0
    media_desp = sum(totais[m].despesas_projetado for m in meses_media) // n_media if tem_media else 0
    media_sobra = sum(totais[m].sobra_projetado for m in meses_media) // n_media if tem_media else 0
    media_guard = sum(totais[m].guardado_projetado for m in meses_media) // n_media if tem_media else 0

    taxas_validas = [totais[m].taxa_poupanca_projetado for m in meses_media if totais[m].taxa_poupanca_projetado is not None]
    media_taxa = round(sum(taxas_validas) / len(taxas_validas), 1) if taxas_validas else None

    # Variações anteriores
    var_rec_ant = _calcular_variacao(mes_atual.receitas_projetado, mes_ant.receitas_projetado) if mes_ant else None
    var_desp_ant = _calcular_variacao(mes_atual.despesas_projetado, mes_ant.despesas_projetado, inverter_polaridade=True) if mes_ant else None
    var_sobra_ant = _calcular_variacao(mes_atual.sobra_projetado, mes_ant.sobra_projetado) if mes_ant else None
    var_guard_ant = _calcular_variacao(mes_atual.guardado_projetado, mes_ant.guardado_projetado) if mes_ant else None
    var_taxa_ant = _calcular_variacao_pp(mes_atual.taxa_poupanca_projetado, mes_ant.taxa_poupanca_projetado) if mes_ant else None

    # Variações em relação à média
    var_rec_med = _calcular_variacao(mes_atual.receitas_projetado, media_rec) if tem_media else None
    var_desp_med = _calcular_variacao(mes_atual.despesas_projetado, media_desp, inverter_polaridade=True) if tem_media else None
    var_sobra_med = _calcular_variacao(mes_atual.sobra_projetado, media_sobra) if tem_media else None
    var_guard_med = _calcular_variacao(mes_atual.guardado_projetado, media_guard) if tem_media else None
    var_taxa_med = _calcular_variacao_pp(mes_atual.taxa_poupanca_projetado, media_taxa) if tem_media else None

    # URLs de ação
    url_rec = f"/lancamentos?mes={competencia}&tipo=receita"
    url_desp = f"/lancamentos?mes={competencia}&tipo=despesa"
    url_sobra = f"/desempenho?mes={competencia}#previsao"
    url_taxa = f"/planejamento?mes={competencia}"
    url_guard = f"/planejamento?mes={competencia}#objetivos"

    return PainelKPIs(
        competencia=competencia,
        tem_media=tem_media,
        meses_media=meses_media,
        receitas=ItemKPI(
            chave="receitas",
            titulo="Receitas",
            valor_realizado=mes_atual.receitas_efetivado,
            valor_projetado=mes_atual.receitas_projetado,
            eh_percentual=False,
            delta_anterior=var_rec_ant,
            delta_media=var_rec_med,
            acao_url=url_rec,
        ),
        despesas=ItemKPI(
            chave="despesas",
            titulo="Despesas",
            valor_realizado=mes_atual.despesas_efetivado,
            valor_projetado=mes_atual.despesas_projetado,
            eh_percentual=False,
            delta_anterior=var_desp_ant,
            delta_media=var_desp_med,
            acao_url=url_desp,
        ),
        sobra=ItemKPI(
            chave="sobra",
            titulo="Sobra",
            valor_realizado=mes_atual.sobra_efetivado,
            valor_projetado=mes_atual.sobra_projetado,
            eh_percentual=False,
            delta_anterior=var_sobra_ant,
            delta_media=var_sobra_med,
            acao_url=url_sobra,
        ),
        taxa_poupanca=ItemKPI(
            chave="taxa_poupanca",
            titulo="Taxa de poupança",
            valor_realizado=mes_atual.taxa_poupanca_efetivado,
            valor_projetado=mes_atual.taxa_poupanca_projetado,
            eh_percentual=True,
            delta_anterior=var_taxa_ant,
            delta_media=var_taxa_med,
            acao_url=url_taxa,
        ),
        guardado=ItemKPI(
            chave="guardado",
            titulo="Guardado em metas",
            valor_realizado=mes_atual.guardado_efetivado,
            valor_projetado=mes_atual.guardado_projetado,
            eh_percentual=False,
            delta_anterior=var_guard_ant,
            delta_media=var_guard_med,
            acao_url=url_guard,
        ),
    )
