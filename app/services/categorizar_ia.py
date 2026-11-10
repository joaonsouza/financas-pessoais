"""Categorizar com IA os lançamentos sem categoria, com revisão antes de aplicar.

Por descrição (normalizada): primeiro os métodos locais (regra, histórico, loja conhecida); a IA completa só o
que sobrar. A IA enxerga cada categoria com o pai e com o tipo/natureza (Moradia › Energia, fixo, necessidade) e,
quando nenhuma serve, propõe uma categoria nova (com pai, tipo e natureza). Nada é gravado até a pessoa aplicar
(`aplicar_categorizacao`), que cria o que faltar e também a regra para o futuro.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import LeitorErro
from app.importers.mascaramento import mascarar
from app.importers.normalizacao import normalizar_descricao
from app.models import Categoria, Lancamento
from app.services.categorizacao import MIN_PADRAO_REGRA, Categorizador, criar_regra_de_correcao
from app.services.ia import MODELO_PADRAO, chamar_parse
from app.services.organizar_categorias import GRUPOS, NATUREZAS, _pai_por_nome

LOTE_IA = 120  # descrições por chamada


@dataclass(frozen=True)
class NovaSugestao:
    nome: str
    pai: str | None
    grupo: str
    natureza: str

    @property
    def rotulo(self) -> str:
        nome = f"{self.pai} › {self.nome}" if self.pai else self.nome
        return f"{nome} ({self.grupo}, {self.natureza})"


@dataclass
class LinhaCategorizacao:
    chave: str  # descrição normalizada
    descricao: str  # um exemplo, como veio
    tipo: str  # despesa | receita
    qtd: int
    total: int  # centavos, positivo
    categoria_id: int | None = None
    origem: str | None = None  # regra | historico | dica | loja | ia
    nova: NovaSugestao | None = None  # categoria que a IA propõe criar quando nenhuma existente serve


@dataclass
class Proposta:
    linhas: list[LinhaCategorizacao]
    aviso: str | None = None


def rotulo_categoria(c: Categoria, por_id: dict[int, Categoria]) -> str:
    """"Moradia › Energia (fixo, necessidade)": o que a IA precisa para decidir bem."""
    pai = por_id.get(c.pai_id) if c.pai_id else None
    nome = f"{pai.nome} › {c.nome}" if pai else c.nome
    atributos = ", ".join(x for x in (c.grupo, c.natureza) if x)
    return f"{nome} ({atributos})" if atributos else nome


def categorias_usaveis(s: Session, tipo: str) -> list[Categoria]:
    """Categorias que recebem lançamento: ativas do tipo e sem filhas (o pai só agrupa)."""
    com_filhas = set(s.scalars(select(Categoria.pai_id).where(Categoria.pai_id.is_not(None))))
    return [c for c in s.scalars(select(Categoria).where(Categoria.ativa, Categoria.tipo == tipo)
                                 .order_by(Categoria.nome)) if c.id not in com_filhas]


def _agrupar(s: Session) -> list[LinhaCategorizacao]:
    grupos: dict[tuple[str, str], LinhaCategorizacao] = {}
    for l in s.scalars(select(Lancamento).where(
            Lancamento.categoria_id.is_(None), Lancamento.tipo.in_(("despesa", "receita")),
            Lancamento.status != "cancelado", Lancamento.ignorar.is_(False))):
        chave = normalizar_descricao(l.descricao)
        if not chave:
            continue
        g = grupos.setdefault((l.tipo, chave), LinhaCategorizacao(chave, l.descricao, l.tipo, 0, 0))
        g.qtd += 1
        g.total += abs(l.valor)
    return sorted(grupos.values(), key=lambda g: (-g.total, g.chave))


# ---------- IA: escolher entre as existentes ou propor uma nova ----------

def _modelo_saida(ids: list[int], com_nova: bool):
    from pydantic import create_model

    restrito = Literal[tuple(ids)] | None  # type: ignore[valid-type]
    campos = {"descricao": (str, ...), "categoria_id": (restrito, ...)}
    if com_nova:
        nova = create_model("NovaCategoria", nome=(str, ...), pai=(str | None, ...),
                            grupo=(Literal["fixo", "variavel", "divida"], ...),
                            natureza=(Literal["necessidade", "desejo"], ...))
        campos["nova"] = (nova | None, ...)
    item = create_model("ItemCategorizacao", **campos)
    return create_model("CategorizacaoIA", itens=(list[item], ...))


def _pedido(categorias: list, descricoes: dict[str, str], pais: list[str], com_nova: bool) -> str:
    lista = "\n".join(f"- {c.id}: {c.nome}" for c in categorias)
    pedido = "\n".join(f"- {d}" for d in descricoes)
    texto = (
        "Classifique cada descrição de lançamento bancário brasileiro em UMA das categorias abaixo, "
        "usando só os ids listados. Prefira SEMPRE uma categoria existente. "
        "Devolva a descrição exatamente como foi enviada.\n"
    )
    if com_nova:
        texto += (
            "Só se nenhuma categoria existente servir de verdade, devolva categoria_id nulo e preencha 'nova' com "
            "uma categoria nova: nome curto (ex.: Pets, Academia), pai (use um destes pais se couber: "
            f"{', '.join(pais) or 'nenhum ainda'}; ou um nome de grupo novo, ou nulo), grupo (fixo, variavel ou "
            "divida) e natureza (necessidade ou desejo). Use o MESMO nome de categoria nova para descrições "
            "parecidas. Se não houver como saber o que é, deixe categoria_id e nova nulos.\n"
        )
    else:
        texto += "Se não houver correspondência razoável, devolva categoria_id nulo.\n"
    return f"{texto}\nCategorias:\n{lista}\n\nDescrições:\n{pedido}"


def _classificar(cliente, descricoes: list[str], categorias: list, pais: list[str], modelo: str,
                 com_nova: bool) -> dict[str, tuple[int | None, NovaSugestao | None]]:
    ids = [c.id for c in categorias]
    enviadas = {mascarar(d): d for d in descricoes}
    resposta = chamar_parse(
        cliente, modelo=modelo, esforco="low", saida=_modelo_saida(ids, com_nova), max_tokens=16000,
        mensagens=[{"role": "user", "content": [{"type": "text", "text": _pedido(categorias, enviadas, pais, com_nova)}]}],
    )
    if resposta.stop_reason == "refusal" or resposta.parsed_output is None:
        raise LeitorErro("A IA não conseguiu sugerir categorias desta vez.")
    resultado = {}
    for item in resposta.parsed_output.itens:
        original = enviadas.get(item.descricao)
        if original is None:
            continue
        categoria_id = item.categoria_id if item.categoria_id in ids else None
        nova = getattr(item, "nova", None)
        sugestao = None
        if categoria_id is None and nova is not None and nova.nome.strip() \
                and nova.grupo in GRUPOS and nova.natureza in NATUREZAS:
            sugestao = NovaSugestao(nova.nome.strip(), (nova.pai or "").strip() or None, nova.grupo, nova.natureza)
        resultado[original] = (categoria_id, sugestao)
    return resultado


def propor_categorizacao(s: Session, cliente=None, modelo: str = MODELO_PADRAO) -> Proposta:
    linhas = _agrupar(s)
    todas = {c.id: c for c in s.scalars(select(Categoria))}
    pais = sorted({c.nome for c in todas.values() if c.tipo == "despesa" and c.pai_id is None})
    aviso = None
    for tipo in ("despesa", "receita"):
        do_tipo = [g for g in linhas if g.tipo == tipo]
        local = Categorizador(s, tipo)
        for g in do_tipo:
            sg = local.sugerir(g.descricao)
            if sg.categoria_id:
                g.categoria_id, g.origem = sg.categoria_id, sg.origem
        faltam = [g for g in do_tipo if g.categoria_id is None]
        usaveis = categorias_usaveis(s, tipo)
        if cliente is None or not faltam:
            continue
        rotulos = [SimpleNamespace(id=c.id, nome=rotulo_categoria(c, todas)) for c in usaveis]
        for i in range(0, len(faltam), LOTE_IA):
            lote = faltam[i:i + LOTE_IA]
            try:
                da_ia = _classificar(cliente, [g.descricao for g in lote], rotulos, pais, modelo,
                                     com_nova=tipo == "despesa")
            except LeitorErro as exc:
                aviso = str(exc)  # o que a IA já devolveu vale; o resto fica para escolha manual
                break
            for g in lote:
                categoria_id, nova = da_ia.get(g.descricao, (None, None))
                if categoria_id is not None:
                    g.categoria_id, g.origem = categoria_id, "ia"
                elif nova is not None:
                    g.nova, g.origem = nova, "ia"
    return Proposta(linhas, aviso)


# ---------- aplicar ----------

def garantir_categoria(s: Session, nova: NovaSugestao, criadas: dict) -> Categoria:
    """A categoria sugerida: a que já existe (mesmo nome sob o mesmo pai) ou uma nova, com o pai que faltar.
    `criadas` junta o que já foi resolvido nesta aplicação para as linhas parecidas caírem na mesma categoria."""
    chave = (nova.nome.casefold(), (nova.pai or "").casefold())
    if chave in criadas:
        return criadas[chave]
    pai = _pai_por_nome(s, nova.pai, {})[0] if nova.pai else None
    if pai is not None and pai.pai_id is not None:
        pai = None  # nunca um terceiro nível
    existente = s.scalar(select(Categoria).where(
        Categoria.tipo == "despesa", Categoria.nome == nova.nome,
        Categoria.pai_id.is_(None) if pai is None else Categoria.pai_id == pai.id))
    if existente is None:
        total = len(list(s.scalars(select(Categoria.id))))
        from app.services.categorias_padrao import PALETA
        existente = Categoria(nome=nova.nome, tipo="despesa", pai_id=pai.id if pai else None, grupo=nova.grupo,
                              natureza=nova.natureza, cor=PALETA[total % len(PALETA)])
        s.add(existente)
        s.flush()
    criadas[chave] = existente
    return existente


def aplicar_categorizacao(s: Session, escolhas: list[tuple[str, str, int | NovaSugestao]]) -> tuple[int, int, int]:
    """escolhas = (tipo, chave, categoria_id ou NovaSugestao). Cria as categorias novas, categoriza os lançamentos
    de mesma descrição ainda sem categoria e cria a regra (descrições curtas demais ficam sem regra).
    Devolve (lançamentos, regras, categorias criadas)."""
    por_chave: dict[tuple[str, str], int] = {}
    criadas: dict = {}
    antes = set(s.scalars(select(Categoria.id)))
    for tipo, chave, destino in escolhas:
        if isinstance(destino, NovaSugestao):
            if tipo != "despesa":
                raise ValueError("só despesas podem ganhar categoria nova")
            if destino.grupo not in GRUPOS or destino.natureza not in NATUREZAS or not destino.nome.strip():
                raise ValueError("categoria nova inválida")
            destino = garantir_categoria(s, destino, criadas).id
        categoria = s.get(Categoria, destino)
        if categoria is None or categoria.tipo != tipo:
            raise ValueError("categoria incompatível com o tipo do lançamento")
        por_chave[(tipo, chave)] = destino
    if not por_chave:
        return 0, 0, 0
    lancamentos = regras = 0
    exemplo: dict[tuple[str, str], str] = {}
    for l in s.scalars(select(Lancamento).where(
            Lancamento.categoria_id.is_(None), Lancamento.tipo.in_(("despesa", "receita")),
            Lancamento.status != "cancelado")):
        alvo = (l.tipo, normalizar_descricao(l.descricao))
        if alvo in por_chave:
            l.categoria_id = por_chave[alvo]
            exemplo.setdefault(alvo, l.descricao)
            lancamentos += 1
    for alvo, categoria_id in por_chave.items():
        if len(alvo[1]) >= MIN_PADRAO_REGRA:
            criar_regra_de_correcao(s, exemplo.get(alvo, alvo[1]), categoria_id)
            regras += 1
    s.flush()
    return lancamentos, regras, len(set(s.scalars(select(Categoria.id))) - antes)
