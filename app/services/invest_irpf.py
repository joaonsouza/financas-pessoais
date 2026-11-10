"""Relatório de Bens e Direitos para a declaração anual de IRPF (estilo Investidor10).

Calcula a posição e o custo de aquisição de cada ativo em 31/12/(ano-1) e em 31/12/(ano), classifica no Grupo e
Código da Receita Federal e gera o texto de discriminação pronto para copiar no programa do IRPF.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.quantidade import fmt_qtd
from app.models import Ativo, Conta, OperacaoInvest
from app.models.investimento import _ETFS_RENDA_FIXA
from app.models.investimento import Carteira as CarteiraModelo
from app.services.invest_carteira import operacoes_por_ativo, posicao_do_ativo, series_indicadores
from app.services.invest_conta import quantidade_por_local
from app.services.invest_posicao import _centavo

_STABLECOINS = frozenset({"USDC", "USDT", "DAI", "BUSD", "USDP", "TUSD", "RLUSD", "FDUSD", "PYUSD"})
_ISENTOS_RF_RE = re.compile(r"\b(LCI|LCA|CRI|CRA|LIG|DEB[EÊ]NTURE\s+INCENTIVADA)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ClassificacaoRfb:
    grupo_cod: str
    grupo_nome: str
    codigo_cod: str
    codigo_nome: str
    localizacao_cod: str
    localizacao_nome: str
    negociado_em_bolsa: bool


@dataclass(frozen=True)
class ItemBensDireitos:
    ativo: Ativo
    rfb: ClassificacaoRfb
    corretora: str
    quantidade_anterior: Decimal
    preco_medio_anterior_brl: int  # centavos
    custo_anterior_brl: int  # centavos em 31/12/(ano-1)
    quantidade_atual: Decimal
    preco_medio_atual_brl: int  # centavos
    custo_atual_brl: int  # centavos em 31/12/(ano)
    discriminacao: str
    encerrada_no_ano: bool


@dataclass(frozen=True)
class GrupoBensDireitos:
    grupo_cod: str
    grupo_nome: str
    itens: list[ItemBensDireitos]
    custo_anterior_brl: int
    custo_atual_brl: int


@dataclass(frozen=True)
class RelatorioIrpf:
    ano: int
    ano_anterior: int
    ano_exercicio: int
    grupos: list[GrupoBensDireitos]
    custo_anterior_total: int
    custo_atual_total: int
    total_itens: int

    @property
    def variacao_custo_brl(self) -> int:
        return self.custo_atual_total - self.custo_anterior_total


def classificar_rfb(ativo: Ativo) -> ClassificacaoRfb:
    """Mapeia o ativo para o Grupo, Código e Localização da ficha de Bens e Direitos da Receita Federal."""
    classe = ativo.classe
    codigo_limpo = ativo.codigo.strip().upper().removesuffix(".SA")

    if classe == "acao":
        return ClassificacaoRfb(
            grupo_cod="03",
            grupo_nome="03 - Participações Societárias",
            codigo_cod="01",
            codigo_nome="01 - Ações (inclusive as listadas em bolsa)",
            localizacao_cod="105",
            localizacao_nome="105 - Brasil",
            negociado_em_bolsa=True,
        )

    if classe == "fii":
        return ClassificacaoRfb(
            grupo_cod="07",
            grupo_nome="07 - Fundos",
            codigo_cod="03",
            codigo_nome="03 - Fundos de Investimento Imobiliário (FII)",
            localizacao_cod="105",
            localizacao_nome="105 - Brasil",
            negociado_em_bolsa=True,
        )

    if classe == "etf":
        eh_rf = ativo.alocacao == "renda_fixa" or codigo_limpo in _ETFS_RENDA_FIXA
        return ClassificacaoRfb(
            grupo_cod="07",
            grupo_nome="07 - Fundos",
            codigo_cod="06" if eh_rf else "09",
            codigo_nome=(
                "06 - FIP, ETF de Renda Fixa e outros fundos"
                if eh_rf
                else "09 - Demais Fundos de Índice de Mercado (ETFs)"
            ),
            localizacao_cod="105",
            localizacao_nome="105 - Brasil",
            negociado_em_bolsa=True,
        )

    if classe == "exterior":
        return ClassificacaoRfb(
            grupo_cod="03",
            grupo_nome="03 - Participações Societárias",
            codigo_cod="01",
            codigo_nome="01 - Ações (inclusive as listadas em bolsa)",
            localizacao_cod="249",
            localizacao_nome="249 - Estados Unidos",
            negociado_em_bolsa=True,
        )

    if classe == "cripto":
        base_cripto = codigo_limpo.split("-")[0]
        if base_cripto == "BTC":
            cod, nome = "01", "01 - Criptoativo Bitcoin (BTC)"
        elif base_cripto in _STABLECOINS:
            cod, nome = "03", "03 - Stablecoins (Tether, USD Coin, etc.)"
        else:
            cod, nome = "02", "02 - Outras criptomoedas (altcoins, ex.: Ether, Solana, etc.)"
        return ClassificacaoRfb(
            grupo_cod="08",
            grupo_nome="08 - Criptoativos",
            codigo_cod=cod,
            codigo_nome=nome,
            localizacao_cod="105",
            localizacao_nome="105 - Brasil",
            negociado_em_bolsa=False,
        )

    # renda_fixa
    texto_rf = f"{ativo.codigo} {ativo.nome}"
    if _ISENTOS_RF_RE.search(texto_rf):
        cod, nome = (
            "03",
            "03 - Títulos isentos de tributação (LCI, LCA, CRI, CRA, LIG, Debêntures Incentivadas, etc.)",
        )
    else:
        cod, nome = (
            "02",
            "02 - Títulos públicos e privados sujeitos à tributação (Tesouro Direto, CDB, RDB e Outros)",
        )
    return ClassificacaoRfb(
        grupo_cod="04",
        grupo_nome="04 - Aplicações e Investimentos",
        codigo_cod=cod,
        codigo_nome=nome,
        localizacao_cod="105",
        localizacao_nome="105 - Brasil",
        negociado_em_bolsa=False,
    )


def _nome_corretora(ativo: Ativo, ops_ano: list[OperacaoInvest], ops_ant: list[OperacaoInvest],
                    nomes_conta: dict[int, str]) -> str:
    """Determina a instituição/corretora onde o ativo está (ou estava) custodiado."""
    for ops in (ops_ano, ops_ant):
        locais = quantidade_por_local(ops)
        contas = []
        for chave in locais:
            if chave is not None and chave[0] in nomes_conta:
                nome = nomes_conta[chave[0]]
                if nome not in contas:
                    contas.append(nome)
        if contas:
            return ", ".join(contas)
    for op in reversed(ops_ano or ops_ant):
        if op.custodia_conta_id and op.custodia_conta_id in nomes_conta:
            return nomes_conta[op.custodia_conta_id]
    if ativo.corretora_conta_id and ativo.corretora_conta_id in nomes_conta:
        return nomes_conta[ativo.corretora_conta_id]
    return ""


def _unidade(ativo: Ativo, qtd: Decimal) -> str:
    singular = qtd == Decimal(1)
    if ativo.classe == "acao":
        return "ação" if singular else "ações"
    if ativo.classe in ("fii", "etf"):
        return "cota" if singular else "cotas"
    if ativo.classe == "renda_fixa" and ativo.por_titulos:
        return "título" if singular else "títulos"
    return "unidade" if singular else "unidades"


def _resumo_taxa_rf(ativo: Ativo) -> str:
    if not ativo.indexador or ativo.taxa is None:
        return ""
    taxa_str = fmt_qtd(ativo.taxa)
    if ativo.indexador == "cdi":
        return f"{taxa_str}% do CDI"
    if ativo.indexador == "ipca":
        return f"IPCA + {taxa_str}% a.a."
    return f"Pré-fixado {taxa_str}% a.a."


def montar_discriminacao(
    ativo: Ativo,
    ano: int,
    qtd_ant: Decimal,
    custo_ant: int,
    qtd_atual: Decimal,
    pm_atual: int,
    custo_atual: int,
    corretora: str,
) -> str:
    """Gera o texto de discriminação sugerido para colar no campo 'Discriminação' do IRPF."""
    identificacao = ativo.codigo
    if ativo.nome and ativo.nome.strip().upper() != ativo.codigo.strip().upper():
        identificacao = f"{ativo.codigo} ({ativo.nome.strip()})"
    trecho_corretora = f", custódia em {corretora}" if corretora else ""

    if qtd_atual <= 0 or custo_atual <= 0:
        if ativo.classe == "renda_fixa" and not ativo.por_titulos:
            return (
                f"Aplicação em Renda Fixa {identificacao} resgatada/encerrada integralmente ao longo de {ano} "
                f"(saldo em 31/12/{ano - 1}: {formatar_reais(custo_ant)}{trecho_corretora})."
            )
        unid_ant = _unidade(ativo, qtd_ant)
        return (
            f"Posição em {identificacao} encerrada integralmente ao longo de {ano} "
            f"(em 31/12/{ano - 1}: {fmt_qtd(qtd_ant)} {unid_ant} pelo custo total de {formatar_reais(custo_ant)}"
            f"{trecho_corretora})."
        )

    if ativo.classe == "renda_fixa" and not ativo.por_titulos:
        detalhes = []
        taxa_txt = _resumo_taxa_rf(ativo)
        if taxa_txt:
            detalhes.append(taxa_txt)
        if ativo.vencimento:
            detalhes.append(f"vencimento em {ativo.vencimento.strftime('%d/%m/%Y')}")
        complemento = f" ({', '.join(detalhes)})" if detalhes else ""
        return (
            f"Aplicação em Renda Fixa: {identificacao}{complemento}{trecho_corretora}. "
            f"Custo de aquisição em 31/12/{ano}: {formatar_reais(custo_atual)}."
        )

    unid = _unidade(ativo, qtd_atual)
    return (
        f"{fmt_qtd(qtd_atual)} {unid} de {identificacao} ao preço médio de {formatar_reais(pm_atual)}, "
        f"custo total de aquisição de {formatar_reais(custo_atual)} em 31/12/{ano}{trecho_corretora}."
    )


def anos_disponiveis(s: Session, hoje: date) -> list[int]:
    """Lista de anos-calendário disponíveis para consulta (inclui sempre `hoje.year - 1` e os anos com operações)."""
    anos_ops = {d.year for d in s.scalars(select(OperacaoInvest.data))}
    ano_padrao = hoje.year - 1
    if not anos_ops:
        return [ano_padrao, hoje.year]
    menor = min(min(anos_ops), ano_padrao)
    maior = max(max(anos_ops), hoje.year)
    return list(range(maior, menor - 1, -1))


def relatorio_bens_e_direitos(
    s: Session,
    ano: int,
    carteira: CarteiraModelo | None = None,
    grupo_filtro: str = "",
) -> RelatorioIrpf:
    """Monta a ficha de Bens e Direitos para o ano-calendário `ano` (posições em 31/12/(ano-1) e 31/12/ano).

    Na visão consolidada (`carteira=None`), inclui também os títulos guardados na reserva de emergência,
    pois na declaração de IRPF todos os ativos do titular entram em Bens e Direitos.
    """
    fim_anterior = date(ano - 1, 12, 31)
    fim_ano = date(ano, 12, 31)
    reserva_modo = "todas" if carteira is None else "excluir"
    ops_ant_por = operacoes_por_ativo(s, ate=fim_anterior, carteira=carteira, reserva=reserva_modo, oportunidade="todas")
    ops_ano_por = operacoes_por_ativo(s, ate=fim_ano, carteira=carteira, reserva=reserva_modo, oportunidade="todas")

    cdi, ipca = series_indicadores(s)
    nomes_conta = {c.id: c.nome for c in s.scalars(select(Conta))}
    ativos = list(s.scalars(select(Ativo).order_by(Ativo.codigo)))

    itens: list[ItemBensDireitos] = []
    for a in ativos:
        ops_ant = ops_ant_por.get(a.id, [])
        ops_ano = ops_ano_por.get(a.id, [])
        if not ops_ant and not ops_ano:
            continue
        pos_ant = posicao_do_ativo(a, ops_ant, cdi, ipca, fim_anterior) if ops_ant else None
        pos_ano = posicao_do_ativo(a, ops_ano, cdi, ipca, fim_ano) if ops_ano else None

        qtd_ant = pos_ant.quantidade if pos_ant else Decimal(0)
        custo_ant = pos_ant.custo_brl if pos_ant and qtd_ant > 0 else 0
        pm_ant = _centavo(pos_ant.preco_medio_brl) if pos_ant and qtd_ant > 0 else 0

        qtd_atual = pos_ano.quantidade if pos_ano else Decimal(0)
        custo_atual = pos_ano.custo_brl if pos_ano and qtd_atual > 0 else 0
        pm_atual = _centavo(pos_ano.preco_medio_brl) if pos_ano and qtd_atual > 0 else 0

        if custo_ant <= 0 and custo_atual <= 0:
            continue

        rfb = classificar_rfb(a)
        if grupo_filtro and rfb.grupo_cod != grupo_filtro:
            continue

        corretora = _nome_corretora(a, ops_ano, ops_ant, nomes_conta)
        discriminacao = montar_discriminacao(
            ativo=a,
            ano=ano,
            qtd_ant=qtd_ant,
            custo_ant=custo_ant,
            qtd_atual=qtd_atual,
            pm_atual=pm_atual,
            custo_atual=custo_atual,
            corretora=corretora,
        )
        itens.append(
            ItemBensDireitos(
                ativo=a,
                rfb=rfb,
                corretora=corretora,
                quantidade_anterior=qtd_ant,
                preco_medio_anterior_brl=pm_ant,
                custo_anterior_brl=custo_ant,
                quantidade_atual=qtd_atual,
                preco_medio_atual_brl=pm_atual,
                custo_atual_brl=custo_atual,
                discriminacao=discriminacao,
                encerrada_no_ano=custo_ant > 0 and custo_atual == 0,
            )
        )

    itens.sort(key=lambda x: (x.rfb.grupo_cod, x.rfb.codigo_cod, x.encerrada_no_ano, x.ativo.codigo))

    por_grupo: dict[str, list[ItemBensDireitos]] = {}
    nomes_grupo: dict[str, str] = {}
    for item in itens:
        por_grupo.setdefault(item.rfb.grupo_cod, []).append(item)
        nomes_grupo[item.rfb.grupo_cod] = item.rfb.grupo_nome

    grupos = [
        GrupoBensDireitos(
            grupo_cod=g_cod,
            grupo_nome=nomes_grupo[g_cod],
            itens=lista,
            custo_anterior_brl=sum(i.custo_anterior_brl for i in lista),
            custo_atual_brl=sum(i.custo_atual_brl for i in lista),
        )
        for g_cod, lista in por_grupo.items()
    ]

    return RelatorioIrpf(
        ano=ano,
        ano_anterior=ano - 1,
        ano_exercicio=ano + 1,
        grupos=grupos,
        custo_anterior_total=sum(g.custo_anterior_brl for g in grupos),
        custo_atual_total=sum(g.custo_atual_brl for g in grupos),
        total_itens=len(itens),
    )
