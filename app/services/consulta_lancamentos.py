from dataclasses import dataclass
from datetime import date
from itertools import groupby

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.periodo import primeiro_dia, ultimo_dia
from app.models import Categoria, Conta, Fatura, Lancamento, Meta, Recorrencia, Tag
from app.services.mes_caixa import no_mes


@dataclass
class FiltrosLancamento:
    conta_id: int | None = None
    categoria_id: int | None = None
    sem_categoria: bool = False  # só os lançamentos sem categoria (para categorizar)
    tipo: str | None = None
    status: str | None = None
    tag: str | None = None
    busca: str | None = None
    ajustes: bool = False  # ajustes de saldo só aparecem com o tipo Ajuste ou quando pedidos


def listar_lancamentos(s: Session, competencia: str, filtros: FiltrosLancamento | None = None) -> list[Lancamento]:
    f = filtros or FiltrosLancamento()
    # mês de caixa, como os totais: a compra no cartão entra no mês em que a fatura vence
    consulta = select(Lancamento).where(
        no_mes(competencia),
        Lancamento.status != "cancelado",
    )
    if f.conta_id:
        consulta = consulta.where(Lancamento.conta_id == f.conta_id)
    if f.categoria_id:
        filhas = select(Categoria.id).where(Categoria.pai_id == f.categoria_id)
        consulta = consulta.where(or_(Lancamento.categoria_id == f.categoria_id, Lancamento.categoria_id.in_(filhas)))
    elif f.sem_categoria:
        consulta = consulta.where(Lancamento.categoria_id.is_(None))
    if f.tipo:
        consulta = consulta.where(Lancamento.tipo == f.tipo)
    elif not f.ajustes:
        consulta = consulta.where(Lancamento.tipo != "ajuste")
    if f.status:
        consulta = consulta.where(Lancamento.status == f.status)
    if f.tag:
        consulta = consulta.where(Lancamento.tags.any(Tag.nome == f.tag.strip().lower()))
    if f.busca:
        consulta = consulta.where(Lancamento.descricao.ilike(f"%{f.busca.strip()}%"))
    return list(s.scalars(consulta.order_by(Lancamento.data.desc(), Lancamento.id.desc())))


@dataclass
class LinhaFatura:
    """Uma fatura de cartão como linha única na lista de lançamentos (no mês do vencimento)."""
    fatura_id: int
    cartao_id: int
    cartao_nome: str
    competencia: str
    data: date
    valor: int  # centavos, negativo
    qtd: int
    status_fatura: str
    eh_fatura: bool = True
    tipo: str = "despesa"
    ignorar: bool = False

    @property
    def status(self) -> str:
        return "efetivado" if self.status_fatura == "paga" else "pendente"


@dataclass
class LinhaTransferencia:
    """Uma transferência entre contas numa linha só (origem → destino), em vez de saída + entrada."""
    id: int  # a perna de saída: editar, apagar e efetivar agem na transferência inteira
    data: date
    valor: int  # centavos, negativo: o que sai da conta de origem
    descricao: str
    origem_id: int
    origem_nome: str
    destino_id: int | None
    destino_nome: str
    status: str
    recorrente: bool
    aporte_meta_id: int | None  # aporte mensal de uma meta "como despesa": a linha leva à meta
    aporte_meta_nome: str | None = None
    eh_fatura: bool = False
    eh_transferencia: bool = True
    tipo: str = "transferencia"
    ignorar: bool = False
    categoria_id: None = None

    @property
    def conta_id(self) -> int:
        return self.origem_id


def _uma_linha_por_transferencia(s: Session, linhas: list) -> list:
    """Cada transferência aparece uma vez (a saída, com origem → destino); a entrada some da lista.
    Se só a entrada estiver na lista (a saída foi ignorada, por exemplo), ela fica como está; também fica
    quando a entrada está em outra situação ou data (venda liquidada em D+2: a entrada ainda pendente)."""
    pernas = [l for l in linhas if isinstance(l, Lancamento) and l.tipo == "transferencia" and l.transferencia_par_id]
    if not pernas:
        return linhas
    por_id = {l.id: l for l in pernas}
    pares = dict(s.execute(select(Lancamento.id, Lancamento.conta_id).where(
        Lancamento.id.in_([l.transferencia_par_id for l in pernas]))).all())
    nomes_conta = dict(s.execute(select(Conta.id, Conta.nome)).all())
    aportes = {rec_id: (meta_id, nome) for rec_id, meta_id, nome in s.execute(
        select(Recorrencia.id, Meta.id, Meta.nome).join(Meta, Meta.id == Recorrencia.origem_meta_id).where(
            Recorrencia.tipo == "despesa", Recorrencia.conta_destino_id.is_not(None)))}
    novas, trocados = [], set()
    for l in pernas:
        if l.valor > 0:
            saida = por_id.get(l.transferencia_par_id)
            if saida is not None and (saida.status, saida.data) == (l.status, l.data):
                trocados.add(id(l))  # a saída está na lista e representa a transferência
            continue
        destino = pares.get(l.transferencia_par_id)
        meta_id, meta_nome = aportes.get(l.recorrencia_id, (None, None))
        novas.append(LinhaTransferencia(
            l.id, l.data, l.valor, l.descricao, l.conta_id, nomes_conta.get(l.conta_id, ""), destino,
            nomes_conta.get(destino, ""), l.status, l.recorrencia_id is not None, meta_id, meta_nome))
        trocados.add(id(l))
    return [l for l in linhas if id(l) not in trocados] + novas


def _agrega_cartoes(f: FiltrosLancamento) -> bool:
    # com filtro de conta, categoria, tag ou busca o usuário procura compras específicas
    return not (f.conta_id or f.categoria_id or f.sem_categoria or f.tag or f.busca)


def _linhas_de_fatura(s: Session, competencia: str, f: FiltrosLancamento) -> list[LinhaFatura]:
    if f.tipo not in (None, "despesa"):
        return []
    consulta = (
        select(Fatura, Conta.nome,
               func.coalesce(func.sum(case((Lancamento.ignorar.is_(False), Lancamento.valor), else_=0)), 0),
               func.coalesce(func.sum(case((Lancamento.ignorar.is_(False), 1), else_=0)), 0))
        .join(Conta, Conta.id == Fatura.cartao_id)
        .outerjoin(Lancamento, and_(Lancamento.fatura_id == Fatura.id, Lancamento.conta_id == Fatura.cartao_id,
                                    Lancamento.tipo == "despesa", Lancamento.status != "cancelado"))
        .where(Fatura.data_vencimento.between(primeiro_dia(competencia), ultimo_dia(competencia)))
        .group_by(Fatura.id, Conta.nome)
    )
    linhas = []
    from app.services.faturas import a_detalhar

    for fatura, nome, soma, qtd in s.execute(consulta):
        detalhar = a_detalhar(s, fatura)  # total digitado do app do banco que ainda não virou lançamento
        if not qtd and not detalhar:
            continue
        if f.status == "efetivado" and fatura.status != "paga":
            continue
        if f.status == "pendente" and fatura.status == "paga":
            continue
        linhas.append(LinhaFatura(
            fatura_id=fatura.id, cartao_id=fatura.cartao_id, cartao_nome=nome, competencia=fatura.competencia,
            data=fatura.data_vencimento, valor=int(soma) - detalhar, qtd=qtd, status_fatura=fatura.status,
        ))
    return linhas


def linhas_da_lista(s: Session, competencia: str, filtros: FiltrosLancamento | None = None) -> list:
    """Lançamentos da tela, com as compras de cartão trocadas por uma linha por fatura e cada transferência
    numa linha só (sem a entrada). Em Todas o pagamento da fatura também sai: a linha da fatura já
    mostra se foi paga. Com filtro de conta, categoria, tag ou busca, tudo aparece separado."""
    f = filtros or FiltrosLancamento()
    lancamentos = listar_lancamentos(s, competencia, f)
    if not _agrega_cartoes(f):
        return lancamentos
    cartoes = set(s.scalars(select(Conta.id).where(Conta.tipo == "cartao")))
    linhas = [l for l in lancamentos
              if l.conta_id not in cartoes and not (f.tipo is None and l.tipo == "pagamento_fatura")]
    linhas += _linhas_de_fatura(s, competencia, f)
    linhas = _uma_linha_por_transferencia(s, linhas)
    return sorted(linhas, key=lambda l: (l.data, getattr(l, "id", 0)), reverse=True)


# coluna da tabela -> direção inicial ao clicar nela
COLUNAS_LISTA = {"situacao": "asc", "data": "desc", "descricao": "asc", "categoria": "asc", "conta": "asc", "valor": "desc"}


def ordenar_linhas(linhas: list, ordem: str, direcao: str, *, categorias: dict[int, str],
                   contas: dict[int, str]) -> list:
    """Ordena as linhas da tabela (lançamentos e faturas). Data desc é a ordem natural de `linhas_da_lista`."""
    desc = direcao == "desc"
    fatura = lambda l: getattr(l, "eh_fatura", False)
    data_id = lambda l: (l.data, getattr(l, "id", 0))
    if ordem == "categoria":
        # sem categoria (e faturas) ficam sempre no fim, nas duas direções
        com = [l for l in linhas if not fatura(l) and l.categoria_id in categorias]
        sem = [l for l in linhas if l not in com]
        com.sort(key=lambda l: (categorias[l.categoria_id].casefold(), data_id(l)), reverse=desc)
        return com + sorted(sem, key=data_id, reverse=True)
    chaves = {
        "data": data_id,
        "situacao": lambda l: (
            (l.status_fatura == "paga") if fatura(l) else (l.status != "pendente"), data_id(l)),
        "descricao": lambda l: (
            (f"Fatura {l.cartao_nome}" if fatura(l) else l.descricao).casefold(), data_id(l)),
        "conta": lambda l: (contas.get(l.cartao_id if fatura(l) else l.conta_id, "").casefold(), data_id(l)),
        # tamanho do valor: desc = maior valor primeiro, seja receita ou despesa
        "valor": lambda l: (abs(l.valor), data_id(l)),
    }
    return sorted(linhas, key=chaves[ordem], reverse=desc)


def agrupar_por_dia(lancamentos: list[Lancamento]) -> list[tuple[date, list[Lancamento]]]:
    return [(dia, list(itens)) for dia, itens in groupby(lancamentos, key=lambda l: l.data)]


def totais_da_lista(lancamentos: list[Lancamento]) -> tuple[int, int]:
    validos = [l for l in lancamentos if not l.ignorar]
    receitas = sum(l.valor for l in validos if l.tipo == "receita")
    despesas = -sum(l.valor for l in validos if l.tipo == "despesa")
    return receitas, despesas


@dataclass(frozen=True)
class ResumoVencidos:
    qtd: int  # lançamentos pendentes com data anterior a hoje (em todos os meses)
    despesas: int  # centavos a pagar, positivo
    receitas: int  # centavos a receber
    mais_antigo: date | None
    faturas: int  # faturas de cartão vencidas e ainda não pagas
    valor_faturas: int
    fatura_mais_antiga: date | None = None

    @property
    def tem(self) -> bool:
        return bool(self.qtd or self.faturas)

    @property
    def competencia_mais_antiga(self) -> str | None:
        datas = [d for d in (self.mais_antigo, self.fatura_mais_antiga) if d is not None]
        return f"{min(datas):%Y-%m}" if datas else None

    fatura_mais_antiga: date | None = None


def resumo_vencidos(s: Session, hoje: date) -> ResumoVencidos:
    """O que já devia ter acontecido e continua pendente, em qualquer mês: lançamentos fora de cartão (a
    transferência conta uma vez) e faturas de cartão vencidas sem pagamento completo."""
    from app.services.faturas import total_fatura

    cartoes = set(s.scalars(select(Conta.id).where(Conta.tipo == "cartao")))
    lancs = [l for l in s.scalars(select(Lancamento).where(
        Lancamento.status == "pendente", Lancamento.data < hoje, Lancamento.ignorar.is_(False),
        Lancamento.tipo.in_(("receita", "despesa", "transferencia", "pagamento_fatura"))))
        if l.conta_id not in cartoes and (l.tipo != "transferencia" or l.valor < 0)]
    despesas = -sum(l.valor for l in lancs if l.valor < 0 and l.tipo != "transferencia")
    receitas = sum(l.valor for l in lancs if l.valor > 0 and l.tipo != "transferencia")
    faturas = []
    for f in s.scalars(select(Fatura).where(Fatura.status != "paga", Fatura.data_vencimento < hoje)):
        total = total_fatura(s, f)
        if total > 0:
            faturas.append((f, total))
    return ResumoVencidos(
        qtd=len(lancs), despesas=despesas, receitas=receitas,
        mais_antigo=min((l.data for l in lancs), default=None),
        faturas=len(faturas), valor_faturas=sum(t for _, t in faturas),
        fatura_mais_antiga=min((f.data_vencimento for f, _ in faturas), default=None),
    )
