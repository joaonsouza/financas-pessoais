"""Serviço de agregação dos gráficos históricos e detalhamento por categoria."""
from dataclasses import dataclass
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import nome_competencia, ultimo_dia
from app.models import Categoria, Conta, Lancamento
from app.services.desempenho.base import (
    carregar_totais_periodo,
    janela_historico,
    meses_media_referencia,
)
from app.services.mes_caixa import com_status, no_mes
from app.services.saldos import saldo_liquido_em


def _rotulo_mes(comp: str) -> str:
    return nome_competencia(comp)[:3] + "/" + comp[2:4]


@dataclass(frozen=True)
class SerieReceitasDespesas:
    rotulos: list[str]
    competencias: list[str]
    receitas_efetivado: list[float]
    receitas_pendente: list[float]
    despesas_efetivado: list[float]
    despesas_pendente: list[float]
    taxas_poupanca: list[float | None]


@dataclass(frozen=True)
class SeriePatrimonio:
    rotulos: list[str]
    competencias: list[str]
    saldo_liquido: list[float]
    investimentos: list[float]
    patrimonio_total: list[float]


@dataclass(frozen=True)
class SerieFixosVariaveis:
    rotulos: list[str]
    competencias: list[str]
    fixos: list[float]
    variaveis: list[float]
    dividas: list[float]
    media_despesas: float


@dataclass(frozen=True)
class SubcategoriaItem:
    nome: str
    total: int
    categoria_id: int | None


@dataclass(frozen=True)
class CategoriaDesempenhoItem:
    categoria_id: int | None
    nome: str
    cor: str
    total: int
    media_anterior: int
    variacao_pct: float | None
    filhas: list[SubcategoriaItem]


@dataclass(frozen=True)
class PainelCategoriasDesempenho:
    competencia: str
    itens: list[CategoriaDesempenhoItem]
    outras_total: int
    outras_qtd: int
    total_geral: int


def serie_receitas_despesas(s: Session, fim: str, n: int = 12) -> SerieReceitasDespesas:
    meses = janela_historico(s, fim, n)
    totais = carregar_totais_periodo(s, meses[0], meses[-1])
    reais = lambda v: round(v / 100, 2)

    rotulos = [_rotulo_mes(m) for m in meses]
    rec_ef = [reais(totais[m].receitas_efetivado) for m in meses]
    rec_pend = [reais(totais[m].receitas_projetado - totais[m].receitas_efetivado) for m in meses]
    desp_ef = [reais(totais[m].despesas_efetivado) for m in meses]
    desp_pend = [reais(totais[m].despesas_projetado - totais[m].despesas_efetivado) for m in meses]
    taxas = [totais[m].taxa_poupanca_projetado for m in meses]

    return SerieReceitasDespesas(
        rotulos=rotulos,
        competencias=meses,
        receitas_efetivado=rec_ef,
        receitas_pendente=rec_pend,
        despesas_efetivado=desp_ef,
        despesas_pendente=desp_pend,
        taxas_poupanca=taxas,
    )


def serie_patrimonio(s: Session, fim: str, n: int = 12) -> SeriePatrimonio:
    meses = janela_historico(s, fim, n)
    reais = lambda v: round(v / 100, 2)

    contas_invest = list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo == "investimento")))

    rotulos = [_rotulo_mes(m) for m in meses]
    liq_vals = []
    inv_vals = []
    tot_vals = []

    for m in meses:
        limite = ultimo_dia(m)
        s_liq = saldo_liquido_em(s, limite)
        s_inv = 0
        for conta in contas_invest:
            if conta.data_saldo_inicial <= limite:
                s_inv += conta.saldo_inicial + int(s.scalar(
                    select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
                        Lancamento.conta_id == conta.id,
                        Lancamento.status != "cancelado",
                        Lancamento.ignorar.is_(False),
                        Lancamento.data >= conta.data_saldo_inicial,
                        Lancamento.data <= limite,
                    )
                ) or 0)
        liq_vals.append(reais(s_liq))
        inv_vals.append(reais(s_inv))
        tot_vals.append(reais(s_liq + s_inv))

    return SeriePatrimonio(
        rotulos=rotulos,
        competencias=meses,
        saldo_liquido=liq_vals,
        investimentos=inv_vals,
        patrimonio_total=tot_vals,
    )


def serie_fixos_variaveis_dividas(s: Session, fim: str, n: int = 12) -> SerieFixosVariaveis:
    meses = janela_historico(s, fim, n)
    totais = carregar_totais_periodo(s, meses[0], meses[-1])
    reais = lambda v: round(v / 100, 2)

    rotulos = [_rotulo_mes(m) for m in meses]
    fixos = [reais(totais[m].fixos_projetado) for m in meses]
    vars_ = [reais(totais[m].variaveis_projetado) for m in meses]
    divs = [reais(totais[m].dividas_projetado) for m in meses]
    despesas_totais = [totais[m].despesas_projetado for m in meses]
    media_desp = reais(sum(despesas_totais) // len(meses)) if meses else 0.0

    return SerieFixosVariaveis(
        rotulos=rotulos,
        competencias=meses,
        fixos=fixos,
        variaveis=vars_,
        dividas=divs,
        media_despesas=media_desp,
    )


def categorias_desempenho_mes(s: Session, competencia: str, max_itens: int = 8) -> PainelCategoriasDesempenho:
    """Despesas do mês agrupadas por categoria raiz com comparativo de média e detalhe de filhas."""
    cats = {c.id: c for c in s.scalars(select(Categoria)).all()}
    meses_ref = meses_media_referencia(s, competencia, n=3)

    # 1. Despesas no mês por categoria
    linhas = s.execute(
        select(Lancamento.categoria_id, func.sum(Lancamento.valor))
        .where(
            Lancamento.tipo == "despesa",
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
            Lancamento.meta_id.is_(None),
            com_status(("efetivado", "pendente")),
            no_mes(competencia),
        )
        .group_by(Lancamento.categoria_id)
    ).all()

    totais_raiz: dict[int | None, int] = {}
    filhas_por_raiz: dict[int | None, dict[str, tuple[int, int | None]]] = {}

    for cid, val in linhas:
        valor = -int(val)
        cat = cats.get(cid)
        raiz_id = cat.pai_id if cat and cat.pai_id else cid
        totais_raiz[raiz_id] = totais_raiz.get(raiz_id, 0) + valor

        if cat and cat.pai_id:
            filhas_por_raiz.setdefault(raiz_id, {})[cat.nome] = (
                filhas_por_raiz.get(raiz_id, {}).get(cat.nome, (0, cid))[0] + valor,
                cid,
            )
        elif cat and cat.pai_id is None:
            # Gasto direto na raiz
            chave_direto = f"(direto em {cat.nome})"
            filhas_por_raiz.setdefault(raiz_id, {})[chave_direto] = (
                filhas_por_raiz.get(raiz_id, {}).get(chave_direto, (0, cid))[0] + valor,
                cid,
            )

    # 2. Média histórica de referência por raiz
    medias_ref: dict[int | None, int] = {}
    if meses_ref:
        for m in meses_ref:
            linhas_m = s.execute(
                select(Lancamento.categoria_id, func.sum(Lancamento.valor))
                .where(
                    Lancamento.tipo == "despesa",
                    Lancamento.ignorar.is_(False),
                    Lancamento.status != "cancelado",
                    Lancamento.meta_id.is_(None),
                    com_status(("efetivado", "pendente")),
                    no_mes(m),
                )
                .group_by(Lancamento.categoria_id)
            ).all()
            for cid, val in linhas_m:
                cat = cats.get(cid)
                raiz_id = cat.pai_id if cat and cat.pai_id else cid
                medias_ref[raiz_id] = medias_ref.get(raiz_id, 0) - int(val)
        for r_id in medias_ref:
            medias_ref[r_id] = medias_ref[r_id] // len(meses_ref)

    todos_itens: list[CategoriaDesempenhoItem] = []
    total_geral = sum(totais_raiz.values())

    for raiz_id, total in totais_raiz.items():
        if total <= 0:
            continue
        cat = cats.get(raiz_id)
        nome = cat.nome if cat else "Sem categoria"
        cor = cat.cor if cat else "#9ca3af"
        med = medias_ref.get(raiz_id, 0)
        var_pct = round(((total - med) * 100) / med, 1) if med > 0 else None

        # Monta subcategorias se houver filhas ou múltiplos lançamentos
        filhas_dict = filhas_por_raiz.get(raiz_id, {})
        # Só exibe linha de filhas se houver mais de uma ou se houver subcategorias reais cadastradas
        filhas_list = []
        if len(filhas_dict) > 1 or any(cat and cat.pai_id is not None for cid in [cid for _, (_, cid) in filhas_dict.items()]):
            filhas_list = [
                SubcategoriaItem(nome=n, total=v, categoria_id=cid)
                for n, (v, cid) in sorted(filhas_dict.items(), key=lambda x: -x[1][0])
            ]

        todos_itens.append(CategoriaDesempenhoItem(
            categoria_id=raiz_id,
            nome=nome,
            cor=cor,
            total=total,
            media_anterior=med,
            variacao_pct=var_pct,
            filhas=filhas_list,
        ))

    todos_itens.sort(key=lambda i: -i.total)

    itens_topo = todos_itens[:max_itens]
    outras = todos_itens[max_itens:]
    outras_tot = sum(o.total for o in outras)

    return PainelCategoriasDesempenho(
        competencia=competencia,
        itens=itens_topo,
        outras_total=outras_tot,
        outras_qtd=len(outras),
        total_geral=total_geral,
    )


def serie_historica_categoria(s: Session, categoria_id: int | None, fim: str, n: int = 12) -> dict[str, Any]:
    """Série mensal dos últimos `n` meses para uma categoria (e suas filhas se for pai)."""
    meses = janela_historico(s, fim, n)
    reais = lambda v: round(v / 100, 2)
    cat = s.get(Categoria, categoria_id) if categoria_id else None

    # Se for pai, busca ids das filhas
    ids_filtro = [categoria_id] if categoria_id else [None]
    if cat and cat.pai_id is None:
        filhas_ids = list(s.scalars(select(Categoria.id).where(Categoria.pai_id == cat.id)))
        ids_filtro.extend(filhas_ids)

    valores = []
    for m in meses:
        soma = s.scalar(
            select(func.coalesce(func.sum(Lancamento.valor), 0))
            .where(
                Lancamento.tipo == "despesa",
                Lancamento.ignorar.is_(False),
                Lancamento.status != "cancelado",
                Lancamento.meta_id.is_(None),
                com_status(("efetivado", "pendente")),
                Lancamento.categoria_id.in_(ids_filtro) if categoria_id else Lancamento.categoria_id.is_(None),
                no_mes(m),
            )
        ) or 0
        valores.append(reais(-int(soma)))

    media = round(sum(valores) / len(valores), 2) if valores else 0.0

    return {
        "categoria_id": categoria_id,
        "nome": cat.nome if cat else "Sem categoria",
        "cor": cat.cor if cat else "#9ca3af",
        "rotulos": [_rotulo_mes(m) for m in meses],
        "competencias": meses,
        "valores": valores,
        "media": media,
    }
