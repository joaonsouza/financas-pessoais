"""Orquestra provedores + cache: cotações (15 min), histórico mensal incremental e séries do BCB."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Ativo, CotacaoCache, OperacaoInvest, ProventoCache, SerieIndicador, get_config, set_config
from app.services.mercado import ErroMercado
from app.services.mercado.bcb import Bcb
from app.services.mercado.brapi import Brapi, token_brapi
from app.services.mercado.yahoo import Yahoo

VALIDADE_CACHE = timedelta(minutes=15)
CHAVE_DESATUALIZADAS = "invest_cotacoes_desatualizadas"
SUFIXO_MENSAL = ":mensal"
CLASSES_B3 = ("acao", "fii", "etf")
CLASSES_PROVENTOS = ("acao", "fii", "etf", "exterior")  # as que pagam dividendos, JCP ou rendimentos
CHAVE_PROVENTOS = "invest_proventos_em"  # dia da última busca de proventos (uma por dia basta)
# câmbio (pré-preenche operações), Ibovespa e índice global (VT) para comparar a rentabilidade
SIMBOLOS_EXTRAS = {"USDBRL=X": "BRL", "^BVSP": "BRL", "VT": "USD"}


@dataclass
class ResultadoCotacoes:
    atualizados: list[str] = field(default_factory=list)
    falhas: list[str] = field(default_factory=list)
    desatualizados: set[int] = field(default_factory=set)  # ids de ativos com cotação desatualizada


@dataclass(frozen=True)
class CotacaoAtivo:
    preco: Decimal  # na moeda do ativo, em unidades (não centavos)
    moeda: str
    variacao_dia_pct: Decimal | None
    data: date
    origem: str  # "cotacao" | "manual"
    desatualizada: bool


def moeda_cotacao(ativo: Ativo) -> str:
    """Moeda em que o símbolo é cotado: a do ativo, ou dólar nos pares `-USD` (cripto com operações em reais)."""
    return "USD" if ativo.moeda == "USD" or (ativo.simbolo_cotacao or "").upper().endswith("-USD") else ativo.moeda


def ids_desatualizados(s: Session) -> set[int]:
    return set(get_config(s, CHAVE_DESATUALIZADAS, []))


def _primeiro_dia_mes(d: date) -> date:
    return d.replace(day=1)


def _upsert(s: Session, simbolo: str, dia: date, fechamento: Decimal, moeda: str, fonte: str,
            agora: datetime, variacao: Decimal | None = None) -> None:
    linha = s.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == simbolo, CotacaoCache.data == dia))
    if linha is None:
        linha = CotacaoCache(simbolo=simbolo, data=dia)
        s.add(linha)
    linha.fechamento, linha.moeda, linha.fonte = fechamento, moeda, fonte
    linha.atualizado_em, linha.variacao_dia_pct = agora, variacao
    s.flush()


def _cache_fresco(s: Session, simbolo: str, agora: datetime) -> bool:
    linha = s.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == simbolo, CotacaoCache.data == agora.date()))
    return (linha is not None and not linha.fonte.endswith(SUFIXO_MENSAL)
            and agora - linha.atualizado_em < VALIDADE_CACHE)


def _buscar_atual(simbolo: str, b3: bool, yahoo: Yahoo, brapi: Brapi | None):
    try:
        return yahoo.cotacao_atual(simbolo), "yahoo"
    except ErroMercado:
        if brapi is None or not b3:
            raise
    return brapi.cotacao_atual(simbolo), "brapi"


def _primeira_operacao(s: Session, ativo_ids: list[int] | None) -> date | None:
    consulta = select(func.min(OperacaoInvest.data))
    if ativo_ids is not None:
        consulta = consulta.where(OperacaoInvest.ativo_id.in_(ativo_ids))
    return s.scalar(consulta)


def _completar_historico(s: Session, simbolo: str, moeda: str, desde: date | None, agora: datetime,
                         forcar: bool, yahoo: Yahoo) -> None:
    """Baixa o fechamento mensal que falta (só meses já encerrados)."""
    if desde is None:
        return
    hoje = agora.date()
    inicio_mes_atual = _primeiro_dia_mes(hoje)
    if not forcar:
        ultimo = s.scalar(select(func.max(CotacaoCache.data)).where(
            CotacaoCache.simbolo == simbolo, CotacaoCache.fonte.like(f"%{SUFIXO_MENSAL}")))
        if ultimo is not None:
            if _primeiro_dia_mes(ultimo) >= _primeiro_dia_mes(inicio_mes_atual - timedelta(days=1)):
                return  # já tem o último mês encerrado
            desde = max(desde, _primeiro_dia_mes(_primeiro_dia_mes(ultimo) + timedelta(days=32)))
    try:
        pontos = yahoo.historico_mensal(simbolo, desde, hoje)
    except ErroMercado:
        return  # histórico é complemento: falha não derruba a página
    for dia, fechamento in pontos:
        if dia < inicio_mes_atual:
            _upsert(s, simbolo, dia, fechamento, moeda, "yahoo" + SUFIXO_MENSAL, agora)


def _completar_proventos(s: Session, agora: datetime, forcar: bool, yahoo: Yahoo) -> None:
    """Proventos por cota de todo papel que paga e já teve operação (inclusive os vendidos: contam na rentabilidade
    passada), desde a primeira operação, uma vez por dia. Posição encerrada com cache não muda mais: não busca de
    novo. Falha não derruba nada."""
    hoje = agora.date()
    if not forcar and get_config(s, CHAVE_PROVENTOS) == hoje.isoformat():
        return
    por_simbolo: dict[str, list[Ativo]] = {}
    for a in s.scalars(select(Ativo).where(Ativo.classe.in_(CLASSES_PROVENTOS), Ativo.simbolo_cotacao != "")):
        if a.simbolo_cotacao:
            por_simbolo.setdefault(a.simbolo_cotacao, []).append(a)
    com_cache = set(s.scalars(select(ProventoCache.simbolo).distinct()))
    buscou = False
    for simbolo, lista in por_simbolo.items():
        if not forcar and simbolo in com_cache and not any(a.ativo for a in lista):
            continue
        desde = _primeira_operacao(s, [a.id for a in lista])
        if desde is None:
            continue
        try:
            pontos = yahoo.proventos(simbolo, desde, hoje)
        except ErroMercado:
            continue
        buscou = True
        for dia, valor in pontos:
            linha = s.scalar(select(ProventoCache).where(ProventoCache.simbolo == simbolo,
                                                         ProventoCache.data_ex == dia))
            if linha is None:
                linha = ProventoCache(simbolo=simbolo, data_ex=dia)
                s.add(linha)
            linha.valor, linha.atualizado_em = valor, agora
        s.flush()
    if buscou:
        set_config(s, CHAVE_PROVENTOS, hoje.isoformat())


def atualizar_cotacoes(s: Session, ativos: list[Ativo], agora: datetime, forcar: bool = False, *,
                       yahoo: Yahoo | None = None, brapi: Brapi | None = None) -> ResultadoCotacoes:
    """Atualiza preço atual (cache de 15 min) e completa o histórico mensal. Erros de rede mantêm o cache."""
    yahoo = yahoo or Yahoo()
    if brapi is None and (token := token_brapi()):
        brapi = Brapi(token)
    resultado = ResultadoCotacoes()
    por_simbolo: dict[str, list[Ativo]] = {}
    for a in ativos:
        if a.ativo and a.simbolo_cotacao:
            por_simbolo.setdefault(a.simbolo_cotacao, []).append(a)

    desatualizados = ids_desatualizados(s)
    for simbolo, lista in por_simbolo.items():
        if not forcar and _cache_fresco(s, simbolo, agora):
            continue
        try:
            (preco, moeda, variacao), fonte = _buscar_atual(simbolo, lista[0].classe in CLASSES_B3, yahoo, brapi)
        except ErroMercado:
            resultado.falhas.append(simbolo)
            desatualizados |= {a.id for a in lista}
            continue
        _upsert(s, simbolo, agora.date(), preco, moeda, fonte, agora, variacao)
        resultado.atualizados.append(simbolo)
        desatualizados -= {a.id for a in lista}
    for simbolo in SIMBOLOS_EXTRAS:
        if forcar or not _cache_fresco(s, simbolo, agora):
            try:
                preco, moeda_prov, variacao = yahoo.cotacao_atual(simbolo)
                _upsert(s, simbolo, agora.date(), preco, moeda_prov, "yahoo", agora, variacao)
                resultado.atualizados.append(simbolo)
            except ErroMercado:
                resultado.falhas.append(simbolo)

    # histórico mensal: por ativo desde a primeira operação; extras desde a primeira operação da carteira
    for simbolo, lista in por_simbolo.items():
        desde = _primeira_operacao(s, [a.id for a in lista])
        _completar_historico(s, simbolo, moeda_cotacao(lista[0]), desde, agora, forcar, yahoo)
    if por_simbolo:
        desde_geral = _primeira_operacao(s, None)
        if desde_geral is not None:  # um mês antes: base da rentabilidade do primeiro mês (Ibovespa, câmbio)
            desde_geral = _primeiro_dia_mes(_primeiro_dia_mes(desde_geral) - timedelta(days=1))
        for simbolo, moeda in SIMBOLOS_EXTRAS.items():
            _completar_historico(s, simbolo, moeda, desde_geral, agora, forcar, yahoo)

    _completar_proventos(s, agora, forcar, yahoo)
    resultado.desatualizados = {a.id for a in ativos} & desatualizados
    set_config(s, CHAVE_DESATUALIZADAS, sorted(desatualizados))
    s.commit()
    return resultado


def cotacao_do_ativo(s: Session, ativo: Ativo) -> CotacaoAtivo | None:
    """Preço vigente: o manual vale quando é mais recente que a última cotação; senão a cotação em cache."""
    linha = None
    if ativo.simbolo_cotacao:
        linha = s.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == ativo.simbolo_cotacao)
                         .order_by(CotacaoCache.data.desc()).limit(1))
    if ativo.preco_manual is not None and ativo.preco_manual_em is not None and (
            linha is None or ativo.preco_manual_em >= linha.atualizado_em.date()):
        return CotacaoAtivo(Decimal(ativo.preco_manual) / 100, ativo.moeda, None, ativo.preco_manual_em, "manual", False)
    if linha is None:
        return None
    return CotacaoAtivo(linha.fechamento, linha.moeda, linha.variacao_dia_pct, linha.data, "cotacao",
                        ativo.id in ids_desatualizados(s))


def atualizar_series(s: Session, hoje: date, *, bcb: Bcb | None = None, forcar: bool = False) -> bool:
    """Completa CDI (diário) e IPCA (mensal) desde a primeira operação. Retorna False se o BCB falhou."""
    desde_geral = _primeira_operacao(s, None)
    if desde_geral is None:
        return True
    bcb = bcb or Bcb()
    ok = True
    for nome, buscar in (("cdi", bcb.cdi), ("ipca", bcb.ipca)):
        ultimo = None if forcar else s.scalar(select(func.max(SerieIndicador.data)).where(SerieIndicador.serie == nome))
        # desde onde a série já foi buscada: operação mais antiga que isso (histórico importado) refaz a busca
        chave_desde = f"serie_{nome}_desde"
        desde_salvo = get_config(s, chave_desde, None) or (
            None if ultimo is None else _primeiro_dia_mes(s.scalar(
                select(func.min(SerieIndicador.data)).where(SerieIndicador.serie == nome))).isoformat())
        if desde_salvo is not None and _primeiro_dia_mes(desde_geral) < date.fromisoformat(desde_salvo):
            ultimo = None
        inicio = _primeiro_dia_mes(desde_geral) if ultimo is None else ultimo + timedelta(days=1)
        if inicio > hoje:
            continue
        try:
            pontos = buscar(inicio, hoje)
        except ErroMercado:
            ok = False
            continue
        if ultimo is None:
            set_config(s, chave_desde, inicio.isoformat())
        existentes = {r.data: r for r in s.scalars(select(SerieIndicador).where(SerieIndicador.serie == nome))}
        for dia, valor in pontos:
            if dia in existentes:
                existentes[dia].valor_pct = valor
            else:
                s.add(SerieIndicador(serie=nome, data=dia, valor_pct=valor))
        s.flush()
    s.commit()
    return ok
