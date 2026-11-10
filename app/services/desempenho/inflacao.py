"""Inflação pessoal: quanto subiram as contas fixas, medido nos lançamentos, lado a lado com o IPCA.

Item = um mesmo gasto ao longo dos meses (recorrência, assinatura ou a mesma descrição). Preço fixo: último
pagamento; consumo: média dos pagamentos dos 12 meses. Índice mensal encadeado, ponderado pelo gasto do mês anterior.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import NOMES_MESES, competencia_de, primeiro_dia, somar_meses, ultimo_dia
from app.importers.normalizacao import normalizar_descricao
from app.models import Assinatura, Categoria, Lancamento, Recorrencia, SerieIndicador

FIXO, CONSUMO = "fixo", "consumo"
MESES_MEDIA_CONSUMO = 12
MESES_ATE_SAIR = 2  # tanto tempo sem pagamento depois do último e o item sai da cesta
MIN_COMPARAVEIS = 2
JANELA_MESES = 12
UM, ZERO = Decimal(1), Decimal(0)


@dataclass(frozen=True)
class Pagamento:
    data: date
    valor: int  # centavos, positivo


@dataclass
class Item:
    chave: str
    nome: str
    tipo: str  # fixo | consumo
    pagamentos: list[Pagamento]  # por data

    @property
    def primeiro_mes(self) -> str:
        return competencia_de(self.pagamentos[0].data)

    @property
    def ultimo_mes(self) -> str:
        return competencia_de(self.pagamentos[-1].data)


def _compacto(texto: str) -> str:
    return normalizar_descricao(texto).replace(" ", "")


def _tipos_por_categoria(s: Session) -> dict[int, str]:
    """Categoria de despesa -> fixo | consumo; subcategoria sem marcação herda da mãe."""
    cats = {c.id: c for c in s.scalars(select(Categoria).where(Categoria.tipo == "despesa"))}
    tipos = {}
    for c in cats.values():
        tipo = c.inflacao or (cats[c.pai_id].inflacao if c.pai_id in cats else None)
        if tipo:
            tipos[c.id] = tipo
    return tipos


def itens_da_cesta(s: Session, hoje: date) -> list[Item]:
    """Lançamentos das categorias marcadas, agrupados: recorrência, senão assinatura, senão descrição compacta."""
    tipos = _tipos_por_categoria(s)
    if not tipos:
        return []
    recorrencias = {r.id: r for r in s.scalars(select(Recorrencia))}
    reserva = {r.id for r in recorrencias.values()
               if r.meta_id or r.caixinha_id or r.origem_meta_id or r.origem_provisao_id}
    assinaturas: dict[int, list[Assinatura]] = {}
    for a in s.scalars(select(Assinatura)):
        assinaturas.setdefault(a.conta_id, []).append(a)
    grupos: dict[str, dict] = {}
    for lanc in s.scalars(select(Lancamento).where(
            Lancamento.tipo == "despesa", Lancamento.status == "efetivado", Lancamento.ignorar.is_(False),
            Lancamento.valor < 0, Lancamento.data <= hoje, Lancamento.categoria_id.in_(tipos))
            .order_by(Lancamento.data, Lancamento.id)):
        if lanc.recorrencia_id in reserva:
            continue  # aporte em meta ou caixinha: reserva, não preço
        compacto = _compacto(lanc.descricao)
        casadas = [a for a in assinaturas.get(lanc.conta_id, []) if compacto.startswith(a.padrao.replace(" ", ""))]
        if lanc.recorrencia_id:
            chave, nome = f"rec:{lanc.recorrencia_id}", recorrencias[lanc.recorrencia_id].descricao
        elif casadas:
            a = max(casadas, key=lambda a: len(a.padrao.replace(" ", "")))
            chave, nome = f"ass:{a.id}", a.nome
        else:
            chave, nome = f"desc:{compacto}", lanc.descricao
        grupo = grupos.setdefault(chave, {"pagamentos": []})
        grupo["nome"], grupo["tipo"] = nome, tipos[lanc.categoria_id]  # o pagamento mais recente manda
        grupo["pagamentos"].append(Pagamento(lanc.data, -lanc.valor))
    itens = [Item(chave, g["nome"], g["tipo"], g["pagamentos"]) for chave, g in grupos.items()]
    return [it for it in itens if len({competencia_de(p.data) for p in it.pagamentos}) >= 2]


def preco_no_mes(item: Item, mes: str) -> Decimal | None:
    """Centavos. Fixo: último pagamento até o fim do mês; consumo: média dos 12 meses. None fora da cesta."""
    if mes < item.primeiro_mes or mes > somar_meses(item.ultimo_mes, MESES_ATE_SAIR):
        return None
    fim = ultimo_dia(mes)
    if item.tipo == CONSUMO:
        inicio = primeiro_dia(somar_meses(mes, -(MESES_MEDIA_CONSUMO - 1)))
        valores = [p.valor for p in item.pagamentos if inicio <= p.data <= fim]
        return Decimal(sum(valores)) / len(valores) if valores else None
    anteriores = [p for p in item.pagamentos if p.data <= fim]
    return Decimal(anteriores[-1].valor) if anteriores else None


def gasto_no_mes(item: Item, mes: str) -> Decimal | None:
    """Centavos pagos no mês; mês sem pagamento usa o preço (anuidade entre as cobranças)."""
    pagos = [p.valor for p in item.pagamentos if competencia_de(p.data) == mes]
    return Decimal(sum(pagos)) if pagos else preco_no_mes(item, mes)


@dataclass
class MesInflacao:
    competencia: str
    pessoal: Decimal | None  # fração (0,0123 = 1,23%); None = dados insuficientes
    ipca: Decimal | None     # fração; None = ainda não publicado
    comparaveis: int


@dataclass
class ResumoItem:
    nome: str
    tipo: str
    preco: Decimal                                # centavos, no fim do período
    variacao: Decimal | None                      # no período
    ultimo_reajuste: tuple[str, Decimal] | None   # (competência, variação); só preço fixo
    peso: Decimal | None                          # no último mês
    contribuicao: Decimal                         # soma de peso × variação nos meses do período
    meses_media: int | None                       # consumo: meses com pagamento na média


@dataclass
class InflacaoPessoal:
    meses: list[MesInflacao]
    acumulado_pessoal: Decimal | None = None
    acumulado_ipca: Decimal | None = None
    ipca_ate: str | None = None
    rotulo: str = ""
    meses_sem_dados: int = 0
    itens: list[ResumoItem] = field(default_factory=list)


def _mes_curto(comp: str) -> str:
    return f"{NOMES_MESES[int(comp[5:]) - 1][:3].lower()}/{comp[2:4]}"


def _comparacao(itens: list[Item], mes: str) -> tuple[Decimal | None, int, dict[str, Decimal]]:
    """(inflação do mês, itens comparáveis, contribuição de cada item)."""
    anterior = somar_meses(mes, -1)
    pares = []
    for it in itens:
        agora, antes = preco_no_mes(it, mes), preco_no_mes(it, anterior)
        if agora is not None and antes:
            pares.append((it.chave, agora / antes - UM, gasto_no_mes(it, anterior)))
    if len(pares) < MIN_COMPARAVEIS:
        return None, len(pares), {}
    total = sum(gasto for *_, gasto in pares)
    contribuicao = {chave: gasto / total * variacao for chave, variacao, gasto in pares}
    return sum(contribuicao.values()), len(pares), contribuicao


def _acumulado(taxas) -> Decimal:
    total = UM
    for taxa in taxas:
        total *= UM + taxa
    return total - UM


def _ipca_por_mes(s: Session, inicio: str, fim: str) -> dict[str, Decimal]:
    linhas = s.scalars(select(SerieIndicador).where(
        SerieIndicador.serie == "ipca", SerieIndicador.data >= primeiro_dia(inicio),
        SerieIndicador.data <= ultimo_dia(fim)))
    return {competencia_de(linha.data): Decimal(linha.valor_pct) / 100 for linha in linhas}


def _ultimo_reajuste(item: Item, fim: str) -> tuple[str, Decimal] | None:
    achado, anterior, mes = None, None, item.primeiro_mes
    while mes <= fim:
        preco = preco_no_mes(item, mes)
        if preco is not None:
            if anterior and preco != anterior:
                achado = (mes, preco / anterior - UM)
            anterior = preco
        mes = somar_meses(mes, 1)
    return achado


def _meses_na_media(item: Item, mes: str) -> int:
    inicio = somar_meses(mes, -(MESES_MEDIA_CONSUMO - 1))
    pagos = {competencia_de(p.data) for p in item.pagamentos}
    return len({m for m in pagos if inicio <= m <= mes})


def _resumo_itens(itens: list[Item], meses: list[str], contribuicao: dict[str, Decimal]) -> list[ResumoItem]:
    fim = meses[-1]
    base = [somar_meses(meses[0], -1), *meses]  # o mês antes do período é a base da variação
    gasto_fim = {it.chave: gasto_no_mes(it, fim) for it in itens if preco_no_mes(it, fim) is not None}
    total_fim = sum(gasto_fim.values())
    resumo = []
    for it in itens:
        precos = [p for p in (preco_no_mes(it, m) for m in base) if p is not None]
        if not precos:
            continue
        resumo.append(ResumoItem(
            nome=it.nome, tipo=it.tipo, preco=precos[-1],
            variacao=precos[-1] / precos[0] - UM if len(precos) > 1 else None,
            ultimo_reajuste=_ultimo_reajuste(it, fim) if it.tipo == FIXO else None,
            peso=gasto_fim[it.chave] / total_fim if it.chave in gasto_fim else None,
            contribuicao=contribuicao.get(it.chave, ZERO),
            meses_media=_meses_na_media(it, fim) if it.tipo == CONSUMO else None,
        ))
    return sorted(resumo, key=lambda r: r.contribuicao, reverse=True)


def inflacao_pessoal(s: Session, fim: str, hoje: date) -> InflacaoPessoal:
    """Até 12 meses fechados terminando em `fim`, a partir do primeiro mês com inflação calculável."""
    fim = min(fim, somar_meses(competencia_de(hoje), -1))
    itens = itens_da_cesta(s, hoje)
    calculos = [(mes, *_comparacao(itens, mes))
                for mes in (somar_meses(fim, -i) for i in range(JANELA_MESES - 1, -1, -1))]
    primeiro = next((i for i, (_, taxa, *_) in enumerate(calculos) if taxa is not None), None)
    if primeiro is None:
        return InflacaoPessoal(meses=[])
    calculos = calculos[primeiro:]
    ipca = _ipca_por_mes(s, calculos[0][0], fim)
    meses = [MesInflacao(mes, taxa, ipca.get(mes), n) for mes, taxa, n, _ in calculos]
    publicados = [m for m in meses if m.ipca is not None]
    contribuicao: dict[str, Decimal] = {}
    for *_, por_item in calculos:
        for chave, valor in por_item.items():
            contribuicao[chave] = contribuicao.get(chave, ZERO) + valor
    return InflacaoPessoal(
        meses=meses,
        acumulado_pessoal=_acumulado(m.pessoal or ZERO for m in meses),
        acumulado_ipca=_acumulado(m.ipca for m in publicados) if publicados else None,
        ipca_ate=publicados[-1].competencia if publicados else None,
        rotulo="12 meses" if len(meses) == JANELA_MESES else f"desde {_mes_curto(meses[0].competencia)}",
        meses_sem_dados=sum(m.pessoal is None for m in meses),
        itens=_resumo_itens(itens, [m.competencia for m in meses], contribuicao),
    )


def grafico_inflacao(r: InflacaoPessoal) -> dict:
    """Séries em % para o Chart.js (float só aqui); acumulado encadeado mês a mês."""
    def pct(v):
        return None if v is None else float(v * 100)

    acum_pessoal, acum_ipca, ap, ai = [], [], UM, UM
    for m in r.meses:
        ap *= UM + (m.pessoal or ZERO)
        acum_pessoal.append(pct(ap - UM))
        if m.ipca is not None:
            ai *= UM + m.ipca
        acum_ipca.append(pct(ai - UM) if m.ipca is not None else None)
    return {"rotulos": [_mes_curto(m.competencia) for m in r.meses],
            "pessoal": [pct(m.pessoal) for m in r.meses], "ipca": [pct(m.ipca) for m in r.meses],
            "acum_pessoal": acum_pessoal, "acum_ipca": acum_ipca}
