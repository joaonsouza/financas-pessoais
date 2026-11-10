"""Sugestão de categoria para lançamentos importados.

Ordem: 1) regras do usuário (contém, sobre a descrição normalizada, por prioridade); 2) histórico (mesma
descrição normalizada já categorizada, a categoria mais frequente); 3) dica do banco (categoria do Itaú);
4) IA em lote, opcional; 5) nenhuma.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import LeitorErro
from app.importers.mascaramento import mascarar
from app.importers.normalizacao import normalizar_descricao
from app.models import Categoria, Lancamento, RegraCategoria
from app.services.ia import MODELO_PADRAO, chamar_parse

_FEIRA = ("PADARIA", "PANIFICADORA", "PANIFICACAO", "ACOUGUE", "FEIRA", "HORTIFRUTI", "SACOLAO", "QUITANDA")
_MERCADO = ("SUPERMERCADO", "ATACAD", "MERCADO", "HIPERMERCADO", "ASSAI")
# nomes das categorias padrão (app/services/categorias_padrao.py)
MERCADO, FEIRA, COMER_FORA = "Mercado", "Feira/Açougue/Padaria", "Comer fora/Delivery"


def _alimentacao(descricao_norm: str) -> str:
    if any(p in descricao_norm for p in _MERCADO):
        return MERCADO
    if any(p in descricao_norm for p in _FEIRA):
        return FEIRA
    return COMER_FORA


# categoria do Itaú (sem acento, sem ponto final) -> nome da categoria do app; função quando depende da descrição
MAPA_DICAS: dict[str, str | Callable[[str], str] | None] = {
    "ALIMENTACAO": _alimentacao,
    "SAUDE": "Farmácia",
    "VEICULOS": "Combustível/Apps",
    "VESTUARIO": "Roupas",
    "HOBBY": "Lazer",
    "TURISMO E ENTRETENIM": "Lazer",
    "EDUCACAO": "Escola",
    "DIVERSOS": None,
    # categorias da Pluggy (inglês e português), normalizadas; docs.pluggy.ai/docs/transaction-categories
    "GROCERIES": _alimentacao, "MANTIMENTOS": _alimentacao, "SUPERMARKETS": _alimentacao,
    "SUPERMERCADOS": _alimentacao, "BAKERY": "Feira/Açougue/Padaria", "PADARIA": "Feira/Açougue/Padaria",
    "FOOD AND DRINKS": "Comer fora/Delivery", "COMIDA E BEBIDAS": "Comer fora/Delivery",
    "RESTAURANTS": "Comer fora/Delivery", "RESTAURANTES": "Comer fora/Delivery",
    "EATING OUT": "Comer fora/Delivery", "FOOD DELIVERY": "Comer fora/Delivery",
    "PHARMACY": "Farmácia", "FARMACIA": "Farmácia", "HEALTHCARE": "Farmácia",
    "GAS STATIONS": "Combustível/Apps", "POSTOS DE GASOLINA": "Combustível/Apps",
    "AUTOMOTIVE": "Combustível/Apps", "AUTOMOTIVO": "Combustível/Apps",
    "TRANSPORTATION": "Combustível/Apps", "TRANSPORTE": "Combustível/Apps",
    "TAXI AND RIDESHARE": "Combustível/Apps",
    "CLOTHING": "Roupas", "VESTUARIO E ACESSORIOS": "Roupas",
    "LEISURE": "Lazer", "LAZER": "Lazer", "TRAVEL": "Lazer", "VIAGEM": "Lazer",
    "EDUCATION": "Escola", "INSURANCE": "Seguros", "SEGUROS": "Seguros",
    "DIGITAL SERVICES": "Assinaturas", "SERVICOS DIGITAIS": "Assinaturas",
    "STREAMING": "Assinaturas", "SHOPPING": None, "COMPRAS": None,
}


@dataclass
class Sugestao:
    categoria_id: int | None = None
    origem: str | None = None  # "regra" | "historico" | "dica" | "loja" | "ia"


# lojas e serviços comuns em faturas e extratos -> categoria padrão (último recurso, depois de regra,
# histórico e dica do banco). Trechos já normalizados (maiúsculas, sem acento); "contém" na descrição.
LOJAS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("NETFLIX", "SPOTIFY", "GOOGLE ONE", "YOUTUBE", "DISNEY", "HBO", "MAX.COM", "PRIME VIDEO", "AMAZONPRIME",
      "APPLE.COM", "ICLOUD", "DEEZER", "GLOBOPLAY", "PARAMOUNT", "CHATGPT", "OPENAI", "CLAUDE.AI", "IPTV"),
     "Assinaturas"),
    (("IFD*", "IFOOD", "RAPPI", "RESTAURANTE", "PIZZ", "LANCH", "BURGER", "MCDONALD", "OUTBACK", "SUBWAY",
      "STARBUCKS", "CAFE", " BAR ", "CHURRASC", "SUSHI", "BOBS", "HABIB", "SPOLETO"), COMER_FORA),
    (("UBER", "99APP", "99 APP", " POSTO", "AUTOPOSTO", "SHELL", "IPIRANGA", "PETROBRAS", "COPEC", "ESTAC", "PARKING",
      "ZONA AZUL", "SEM PARAR", "CONECTCAR", "VELOE"), "Combustível/Apps"),
    (("DROGA", "FARMA", "PANVEL", "PACHECO", "PAGUE MENOS", "RAIA"), "Farmácia"),
    (("SHEIN", "RENNER", "RIACHUELO", "C&A", " CEA ", "MARISA", "ZARA", "HERING", "CENTAURO", "NETSHOES",
      "DAFITI", "MELISSA", " MODA "), "Roupas"),
    (("LATAM", " AZUL ", "GOL LINHAS", "AIRBNB", "BOOKING", "HOTEL", "DECOLAR", "HOSTEL", " CVC ", " SKI",
      "RENTCAR", "RENT A CAR", "LOCALIZA", "MOVIDA", "CINEMA", "INGRESSO", "SYMPLA", "SOUVENIR"), "Lazer"),
    (("JUMBO", "UNIMARC", " LIDER ", "CARREFOUR", "PAO DE ACUCAR", "ATACADAO", "ASSAI", "SUPERMERC",
      "MERCADINHO"), MERCADO),
    (("PADARIA", "PANIFIC", "ACOUGUE", "HORTIFRUTI", "SACOLAO", "FEIRA"), FEIRA),
    (("SMART FIT", "SMARTFIT", "ACADEMIA", "BARBEARIA", "SALAO", "CABELEIR", "NATURA", "BOTICARIO"),
     "Cuidados pessoais"),
    (("ESCOLA", "COLEGIO", "FACULDADE", "UDEMY", "ALURA"), "Escola"),
    ((" VIVO ", " CLARO ", " TIM ", "INTERNET"), "Internet/Celular"),
    (("SEGURO",), "Seguros"),
)


def categoria_por_loja(descricao_norm: str) -> str | None:
    texto = f" {descricao_norm} "
    for trechos, nome in LOJAS:
        if any(t in texto for t in trechos):
            return nome
    return None


@dataclass
class Resultado:
    sugestoes: list[Sugestao]
    aviso_ia: str | None = None  # mensagem quando a IA foi pedida e falhou (o resto da sugestão vale)


class Categorizador:
    """Carrega regras, histórico e categorias uma vez e sugere para várias descrições."""

    def __init__(self, s: Session, tipo: str = "despesa"):
        categorias = list(s.scalars(select(Categoria).where(Categoria.ativa, Categoria.tipo == tipo)))
        self.categorias = categorias
        self.ids = {c.id for c in categorias}
        self.por_nome = {c.nome: c.id for c in categorias}
        regras = s.scalars(select(RegraCategoria).where(RegraCategoria.categoria_id.in_(self.ids or {0})))
        # maior prioridade primeiro; a regra mais específica (padrão mais longo) vence o empate
        self.regras = sorted(
            ((normalizar_descricao(r.padrao), r.categoria_id, r.prioridade) for r in regras if r.padrao.strip()),
            key=lambda r: (-r[2], -len(r[0])),
        )
        contagem: dict[str, Counter] = {}
        linhas = s.execute(
            select(Lancamento.descricao, Lancamento.categoria_id)
            .where(Lancamento.categoria_id.in_(self.ids or {0}), Lancamento.status != "cancelado")
        )
        for descricao, categoria_id in linhas:
            contagem.setdefault(normalizar_descricao(descricao), Counter())[categoria_id] += 1
        self.historico = {
            desc: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for desc, c in contagem.items()
        }

    def sugerir(self, descricao: str, dica: str | None = None) -> Sugestao:
        norm = normalizar_descricao(descricao)
        for padrao, categoria_id, _ in self.regras:
            if padrao and padrao in norm:
                return Sugestao(categoria_id, "regra")
        if norm in self.historico:
            return Sugestao(self.historico[norm], "historico")
        if dica:
            chave = normalizar_descricao(dica).strip(" .")
            alvo = MAPA_DICAS.get(chave)
            nome = alvo(norm) if callable(alvo) else alvo
            if nome and nome in self.por_nome:
                return Sugestao(self.por_nome[nome], "dica")
        nome = categoria_por_loja(norm)
        if nome and nome in self.por_nome:
            return Sugestao(self.por_nome[nome], "loja")
        return Sugestao()


def sugerir_categoria(s: Session, descricao: str, dica: str | None = None, tipo: str = "despesa") -> Sugestao:
    return Categorizador(s, tipo).sugerir(descricao, dica)


# ---------- IA em lote (opcional) ----------

def _modelo_saida(ids: list[int]):
    """Saída estruturada com `categoria_id` restrito aos ids oferecidos (ou nulo)."""
    from pydantic import create_model

    restrito = Literal[tuple(ids)] | None  # type: ignore[valid-type]
    item = create_model("ItemCategoria", descricao=(str, ...), categoria_id=(restrito, ...))
    return create_model("CategorizacaoIA", itens=(list[item], ...))


def categorizar_com_ia(cliente, descricoes: list[str], categorias: list[Categoria],
                       modelo: str = MODELO_PADRAO) -> dict[str, int | None]:
    """Pede à IA a categoria de cada descrição única. Devolve {descrição: categoria_id | None}.

    As descrições são mascaradas antes de sair; ids fora da lista oferecida viram None.
    """
    if not descricoes or not categorias:
        return {}
    ids = [c.id for c in categorias]
    enviadas = {mascarar(d): d for d in descricoes}
    lista = "\n".join(f"- {c.id}: {c.nome}" for c in categorias)
    pedido = "\n".join(f"- {d}" for d in enviadas)
    texto = (
        "Classifique cada descrição de lançamento bancário brasileiro em UMA das categorias abaixo, "
        "usando só os ids listados. Se não houver correspondência razoável, devolva categoria_id nulo. "
        "Devolva a descrição exatamente como foi enviada.\n\n"
        f"Categorias:\n{lista}\n\nDescrições:\n{pedido}"
    )
    resposta = chamar_parse(
        cliente, modelo=modelo, esforco="low", saida=_modelo_saida(ids), max_tokens=16000,
        mensagens=[{"role": "user", "content": [{"type": "text", "text": texto}]}],
    )
    if resposta.stop_reason == "refusal" or resposta.parsed_output is None:
        raise LeitorErro("A IA não conseguiu sugerir categorias desta vez.")
    resultado: dict[str, int | None] = {}
    for item in resposta.parsed_output.itens:
        original = enviadas.get(item.descricao)
        if original is not None:
            resultado[original] = item.categoria_id if item.categoria_id in ids else None
    return resultado


def sugerir_categorias(s: Session, itens: list[tuple[str, str | None]], tipo: str = "despesa",
                       cliente=None, modelo: str = MODELO_PADRAO) -> Resultado:
    """Sugere para uma lista de (descrição, dica). Com `cliente`, a IA completa as que ficaram sem sugestão."""
    cat = Categorizador(s, tipo)
    sugestoes = [cat.sugerir(descricao, dica) for descricao, dica in itens]
    aviso = None
    if cliente is not None:
        faltam = list(dict.fromkeys(d for (d, _), sg in zip(itens, sugestoes) if sg.categoria_id is None))
        try:
            da_ia = categorizar_com_ia(cliente, faltam, cat.categorias, modelo)
        except LeitorErro as exc:
            da_ia, aviso = {}, str(exc)
        for (descricao, _), sg in zip(itens, sugestoes):
            if sg.categoria_id is None and da_ia.get(descricao) is not None:
                sg.categoria_id, sg.origem = da_ia[descricao], "ia"
    return Resultado(sugestoes, aviso)


# ---------- aprendizado ----------

def criar_regra_de_correcao(s: Session, descricao: str, categoria_id: int, prioridade: int = 0) -> RegraCategoria:
    """Cria (ou atualiza) a regra "contém <descrição normalizada>" -> categoria, a partir de uma correção do usuário."""
    padrao = normalizar_descricao(descricao)
    if not padrao:
        raise ValueError("descrição vazia: não dá para criar a regra")
    if s.get(Categoria, categoria_id) is None:
        raise ValueError("categoria não encontrada")
    regra = s.scalar(select(RegraCategoria).where(RegraCategoria.padrao == padrao))
    if regra is None:
        regra = RegraCategoria(padrao=padrao, categoria_id=categoria_id, prioridade=prioridade, criada_de=descricao[:300])
        s.add(regra)
    else:
        regra.categoria_id, regra.prioridade = categoria_id, prioridade
    s.flush()
    return regra


MIN_PADRAO_REGRA = 5  # descrição curta demais ("PIX", "TED") viraria regra que pega tudo


def aprender_categoria(s: Session, lanc: Lancamento) -> int:
    """O usuário escolheu a categoria de um lançamento: aplica nos outros de mesma descrição ainda sem
    categoria e cria a regra para as próximas importações. Devolve quantos outros foram categorizados."""
    if lanc.tipo not in ("receita", "despesa") or lanc.categoria_id is None:
        return 0
    norm = normalizar_descricao(lanc.descricao)
    outros = [l for l in s.scalars(select(Lancamento).where(
        Lancamento.categoria_id.is_(None), Lancamento.tipo == lanc.tipo, Lancamento.id != lanc.id,
        Lancamento.status != "cancelado")) if normalizar_descricao(l.descricao) == norm]
    for l in outros:
        l.categoria_id = lanc.categoria_id
    if len(norm) >= MIN_PADRAO_REGRA:
        criar_regra_de_correcao(s, lanc.descricao, lanc.categoria_id)
    s.flush()
    return len(outros)


def categorizar_pendentes(s: Session) -> int:
    """Sugere categoria (regra, histórico, lojas conhecidas) para lançamentos sem categoria. Devolve quantos."""
    total = 0
    for tipo in ("despesa", "receita"):
        cat = Categorizador(s, tipo)
        for l in s.scalars(select(Lancamento).where(
                Lancamento.categoria_id.is_(None), Lancamento.tipo == tipo, Lancamento.status != "cancelado")):
            sugestao = cat.sugerir(l.descricao)
            if sugestao.categoria_id:
                l.categoria_id = sugestao.categoria_id
                total += 1
    s.flush()
    return total
