"""Assinaturas (Spotify, iCloud, streaming...): cadastro de controle. Nada aqui cria lançamento; as cobranças reais
são os lançamentos efetivados do cartão que casam com o texto de reconhecimento de cada assinatura."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, primeiro_dia, somar_meses
from app.importers.normalizacao import normalizar_descricao
from app.models import Assinatura, Conta, Lancamento
from app.models.config import get_config, set_config
from app.services.detector_recorrencia import SugestaoRecorrencia, detectar_recorrencias

CHAVE_IGNORADAS = "assinaturas_ignoradas"
TOLERANCIA_REAJUSTE_PCT = 1
RUIDO = ("PIX NO CREDITO", "MULTA", "IOF", "JUROS", "PARCELAMENTO DE COMPRA", "PAGAMENTO")


def _compacto(texto: str) -> str:
    return normalizar_descricao(texto).replace(" ", "")


@dataclass
class ItemAssinatura:
    assinatura: Assinatura
    ultima_data: date | None = None
    ultimo_valor: int | None = None
    cobrado_12m: int = 0
    meses_cobrados: int = 0
    reajuste: bool = False
    sem_cobranca: bool = False
    ainda_cobrando: bool = False


@dataclass
class PainelControle:
    ativas: list[ItemAssinatura] = field(default_factory=list)
    canceladas: list[ItemAssinatura] = field(default_factory=list)
    sugestoes: list[SugestaoRecorrencia] = field(default_factory=list)
    qtd_ativas: int = 0
    qtd_canceladas: int = 0
    custo_mensal: int = 0
    custo_anual: int = 0
    gasto_12m: int = 0


# ---------------------------------------------------------------- comandos

def _validar(s: Session, *, nome: str, valor: int, intervalo_meses: int, dia: int | None, conta_id: int) -> None:
    if not nome.strip():
        raise ValueError("informe o nome da assinatura")
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if not 1 <= intervalo_meses <= 12:
        raise ValueError("a periodicidade deve ser de 1 a 12 meses")
    if dia is not None and not 1 <= dia <= 31:
        raise ValueError("o dia deve estar entre 1 e 31")
    if s.get(Conta, conta_id) is None:
        raise ValueError("escolha o cartão")


def _sem_duplicata(s: Session, conta_id: int, padrao: str, ignorar_id: int | None = None) -> None:
    for outra in s.scalars(select(Assinatura).where(Assinatura.conta_id == conta_id)):
        if outra.id != ignorar_id and outra.padrao.replace(" ", "") == padrao.replace(" ", ""):
            raise ValueError("já existe uma assinatura com esse texto neste cartão")


def criar_assinatura(s: Session, *, nome: str, valor: int, conta_id: int, hoje: date, intervalo_meses: int = 1,
                     dia: int | None = None, padrao: str | None = None, inicio: date | None = None,
                     observacao: str | None = None) -> Assinatura:
    nome = nome.strip()
    _validar(s, nome=nome, valor=valor, intervalo_meses=intervalo_meses, dia=dia, conta_id=conta_id)
    texto = normalizar_descricao(padrao or nome)
    _sem_duplicata(s, conta_id, texto)
    a = Assinatura(nome=nome, padrao=texto, valor=valor, intervalo_meses=intervalo_meses, conta_id=conta_id,
                   dia=dia, inicio=inicio or hoje, observacao=observacao)
    s.add(a)
    s.flush()
    return a


CAMPOS_EDITAVEIS = {"nome", "valor", "conta_id", "intervalo_meses", "dia", "padrao", "observacao"}


def editar_assinatura(s: Session, a: Assinatura, **campos) -> None:
    invalidos = set(campos) - CAMPOS_EDITAVEIS
    if invalidos:
        raise ValueError(f"campos não editáveis: {sorted(invalidos)}")
    novo = {"nome": a.nome, "valor": a.valor, "conta_id": a.conta_id, "intervalo_meses": a.intervalo_meses,
            "dia": a.dia, **{k: v for k, v in campos.items() if k not in ("padrao", "observacao")}}
    novo["nome"] = str(novo["nome"]).strip()
    _validar(s, **novo)
    padrao = normalizar_descricao(campos["padrao"]) if campos.get("padrao") else a.padrao
    _sem_duplicata(s, novo["conta_id"], padrao, ignorar_id=a.id)
    for campo, valor in {**novo, "padrao": padrao, **({"observacao": campos["observacao"]} if "observacao" in campos else {})}.items():
        setattr(a, campo, valor)
    s.flush()


def cancelar_assinatura(a: Assinatura, em: date) -> None:
    a.ativa, a.cancelada_em = False, em


def reativar_assinatura(a: Assinatura) -> None:
    a.ativa, a.cancelada_em = True, None


def _chave_ignorada(descricao: str, conta_id: int) -> str:
    return f"{conta_id}:{_compacto(descricao)}"


def ignorar_sugestao(s: Session, descricao: str, conta_id: int) -> None:
    ignoradas = list(get_config(s, CHAVE_IGNORADAS, []))
    chave = _chave_ignorada(descricao, conta_id)
    if chave not in ignoradas:
        set_config(s, CHAVE_IGNORADAS, ignoradas + [chave])


def assinatura_da_sugestao(s: Session, sug: SugestaoRecorrencia, hoje: date) -> Assinatura:
    return criar_assinatura(s, nome=sug.descricao, valor=sug.valor, conta_id=sug.conta_id, hoje=hoje, dia=sug.dia,
                            inicio=primeiro_dia(sug.meses[0]))


# ---------------------------------------------------------------- leitura

def cobrancas(s: Session, assinaturas: list[Assinatura] | None = None) -> dict[int, list[Lancamento]]:
    """Cobrança real de cada assinatura (id -> lançamentos, do mais recente para o mais antigo)."""
    lista = list(assinaturas if assinaturas is not None else s.scalars(select(Assinatura)))
    resultado: dict[int, list[Lancamento]] = {a.id: [] for a in lista}
    por_conta: dict[int, list[Assinatura]] = defaultdict(list)
    for a in lista:
        por_conta[a.conta_id].append(a)
    if not por_conta:
        return resultado
    for lanc in s.scalars(select(Lancamento).where(
            Lancamento.tipo == "despesa", Lancamento.valor < 0, Lancamento.status == "efetivado",
            Lancamento.ignorar.is_(False), Lancamento.conta_id.in_(por_conta))
            .order_by(Lancamento.data.desc(), Lancamento.id.desc())):
        compacto = _compacto(lanc.descricao)
        candidatas = [a for a in por_conta[lanc.conta_id] if compacto.startswith(a.padrao.replace(" ", ""))]
        if candidatas:
            resultado[max(candidatas, key=lambda a: len(a.padrao.replace(" ", ""))).id].append(lanc)
    return resultado


def _item(a: Assinatura, lancs: list[Lancamento], hoje: date) -> ItemAssinatura:
    ate_hoje = [l for l in lancs if l.data <= hoje]
    item = ItemAssinatura(a)
    if ate_hoje:
        item.ultima_data, item.ultimo_valor = ate_hoje[0].data, -ate_hoje[0].valor
    ultimos_12m = [l for l in ate_hoje if l.data > hoje - timedelta(days=365)]
    item.cobrado_12m = sum(-l.valor for l in ultimos_12m)
    item.meses_cobrados = len({competencia_de(l.data) for l in ultimos_12m})
    if a.ativa:
        if item.ultimo_valor is not None:
            item.reajuste = abs(item.ultimo_valor - a.valor) * 100 > a.valor * TOLERANCIA_REAJUSTE_PCT
        janela = somar_meses(competencia_de(hoje), -max(2, a.intervalo_meses + 1))
        recente = any(competencia_de(l.data) >= janela for l in ate_hoje)
        item.sem_cobranca = not recente and (a.inicio is None or a.inicio <= primeiro_dia(janela))
    elif a.cancelada_em is not None:
        item.ainda_cobrando = any(l.data > a.cancelada_em for l in ate_hoje)
    return item


def sugestoes_de_inclusao(s: Session, hoje: date) -> list[SugestaoRecorrencia]:
    cartoes = {c.id for c in s.scalars(select(Conta).where(Conta.tipo == "cartao"))}
    existentes = [(a.conta_id, a.padrao.replace(" ", "")) for a in s.scalars(select(Assinatura))]
    ignoradas = set(get_config(s, CHAVE_IGNORADAS, []))
    sugestoes = []
    for sug in detectar_recorrencias(s, hoje):
        compacto = _compacto(sug.descricao)
        if sug.conta_id not in cartoes or _chave_ignorada(sug.descricao, sug.conta_id) in ignoradas:
            continue
        if normalizar_descricao(sug.descricao).startswith(RUIDO):
            continue
        if any(conta == sug.conta_id and compacto.startswith(padrao) for conta, padrao in existentes):
            continue
        sugestoes.append(sug)
    return sugestoes


def total_assinaturas(s: Session) -> tuple[int, int]:
    """(custo por mês, custo por ano) das assinaturas ativas."""
    anual = sum(a.valor * 12 // a.intervalo_meses for a in s.scalars(select(Assinatura).where(Assinatura.ativa)))
    return anual // 12, anual


def resumo_assinaturas(s: Session) -> tuple[int, int]:
    """(quantidade, centavos por mês) das assinaturas ativas."""
    quantidade = s.scalar(select(func.count(Assinatura.id)).where(Assinatura.ativa))
    return quantidade, total_assinaturas(s)[0]


def painel_assinaturas(s: Session, hoje: date) -> PainelControle:
    todas = list(s.scalars(select(Assinatura).order_by(Assinatura.nome)))
    por_ass = cobrancas(s, todas)
    painel = PainelControle()
    for a in todas:
        item = _item(a, por_ass[a.id], hoje)
        (painel.ativas if a.ativa else painel.canceladas).append(item)
    painel.qtd_ativas, painel.qtd_canceladas = len(painel.ativas), len(painel.canceladas)
    painel.custo_anual = sum(i.assinatura.valor * 12 // i.assinatura.intervalo_meses for i in painel.ativas)
    painel.custo_mensal = painel.custo_anual // 12
    painel.gasto_12m = sum(i.cobrado_12m for i in painel.ativas)
    painel.sugestoes = sugestoes_de_inclusao(s, hoje)
    return painel
