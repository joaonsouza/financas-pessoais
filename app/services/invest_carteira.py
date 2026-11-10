"""Resumo da carteira de investimentos (posição, saldo, alocação vs meta) e sugestão de aporte."""
from collections import defaultdict
from dataclasses import dataclass, field
from bisect import bisect_right
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from app.models import Ativo, Conta, CotacaoCache, Meta, MetaClasse, OperacaoInvest, SerieIndicador
from app.models.investimento import (
    ALOCACOES, AtivoDesejado, MetaAtivoCarteira, MetaClasseCarteira, alocacao_padrao,
)
from app.models.investimento import Carteira as CarteiraModelo
from app.models.caixinha import Caixinha
from app.models.config import Config, get_config, set_config
from app.services.invest_conta import quantidade_por_local
from app.services.invest_posicao import Posicao, posicao
from app.services.invest_renda_fixa import valor_atualizado, valor_atualizado_detalhe, valor_na_data_para_posicao
from app.services.mercado.cotacoes import cotacao_do_ativo
from app.services.mercado.tesouro import preco_de_mercado

UM = Decimal(1)
CEM = Decimal(100)
SIMBOLO_CAMBIO = "USDBRL=X"
CLASSES_FRACIONADAS = ("cripto", "exterior")


@dataclass(frozen=True)
class LinhaAtivo:
    ativo: Ativo
    quantidade: Decimal
    preco_medio_brl: Decimal  # centavos por unidade
    custo: int
    cotacao_brl: Decimal | None  # centavos por unidade; None sem cotação (saldo cai para o custo)
    variacao_dia_pct: Decimal | None
    saldo: int
    pct_classe: Decimal
    pct_carteira: Decimal
    meta_classe_pct: Decimal
    meta_ativo_pct: Decimal
    meta_carteira_pct: Decimal  # classe × ativo / 100
    rent_pct: Decimal | None
    rent_brl: int
    lucro_realizado: int
    desatualizada: bool = False
    preco_manual: bool = False
    estimado: bool = False
    sem_cotacao: bool = False
    ideal_classe_pct: Decimal = Decimal(0)  # % ideal dentro da alocação (o meta_pct digitado)
    ideal_pct: Decimal = Decimal(0)  # % ideal na carteira inteira
    # centavos que faltam (negativo: passa) para o ativo chegar à meta dentro da classe; base: o maior entre o saldo
    # da classe e o alvo dela (numa classe ainda vazia, mostra quanto falta em cada ativo, não zero)
    falta_classe: int = 0
    preco_compra: Decimal | None = None  # Tesouro por títulos: PU de compra do dia (centavos por título)

    @property
    def comprar(self) -> bool:
        return self.pct_carteira < self.ideal_pct

    @property
    def variacao_pct(self) -> Decimal | None:
        """Preço atual contra o preço médio."""
        if self.cotacao_brl is None or not self.preco_medio_brl:
            return None
        return (self.cotacao_brl / self.preco_medio_brl - UM) * CEM


@dataclass(frozen=True)
class LinhaClasse:
    classe: str  # chave de ALOCACOES (o bloco agrupa pela exposição real, não pelo tipo do papel)
    ativos: list[LinhaAtivo]
    saldo: int
    custo: int
    pct_atual: Decimal
    meta_pct: Decimal
    lucro_realizado: int
    falta: int = 0  # centavos para a classe chegar à meta na carteira (negativo: passa)

    @property
    def rent_brl(self) -> int:
        return self.saldo - self.custo

    @property
    def n_ativos(self) -> int:
        return len(self.ativos)

    @property
    def variacao_pct(self) -> Decimal | None:
        """Saldo contra o custo das posições abertas."""
        return None if self.custo <= 0 else Decimal(self.rent_brl) / self.custo * CEM

    @property
    def rent_pct(self) -> Decimal | None:
        """Como a variação, somando o lucro já realizado em vendas e resgates."""
        return None if self.custo <= 0 else Decimal(self.rent_brl + self.lucro_realizado) / self.custo * CEM


@dataclass(frozen=True)
class Carteira:
    classes: list[LinhaClasse]
    patrimonio: int
    aplicado: int
    lucro_nao_realizado: int
    lucro_realizado: int
    por_corretora: dict[str, int] = field(default_factory=dict)  # por nome do local, para exibir
    cambio: Decimal | None = None
    por_local: dict = field(default_factory=dict)  # (conta_id, caixinha_id | None) | None -> centavos
    # na consolidada (e na carteira escolhida na meta): posições guardadas na reserva de emergência. Ficam fora de classes, patrimônio e metas
    # (não entram na alocação nem no rebalanceamento), mas contam no patrimônio total e nos locais
    reserva: list[LinhaAtivo] = field(default_factory=list)
    reserva_alvo: int = 0  # a meta da reserva de emergência (o alvo dela), para comparar com o saldo
    # posições nas caixinhas de reserva de oportunidade (da carteira; na consolidada, de todas): fora da alocação
    # como a reserva, mas o saldo conta como disponível no rebalanceamento
    oportunidade: list[LinhaAtivo] = field(default_factory=list)

    @property
    def reserva_saldo(self) -> int:
        return sum(a.saldo for a in self.reserva)

    @property
    def oportunidade_saldo(self) -> int:
        return sum(a.saldo for a in self.oportunidade)

    @property
    def reserva_abaixo_da_meta(self) -> bool:
        """A reserva ainda não chegou à meta dela: ela vem antes de tudo na tela."""
        return bool(self.reserva) and self.reserva_alvo > 0 and self.reserva_saldo < self.reserva_alvo

    @property
    def reserva_pct(self) -> Decimal | None:
        return pct(self.reserva_saldo, self.reserva_alvo) if self.reserva_alvo > 0 else None

    @property
    def patrimonio_com_reserva(self) -> int:
        return self.patrimonio + self.reserva_saldo + self.oportunidade_saldo

    @property
    def ativos(self) -> list[LinhaAtivo]:
        return [a for c in self.classes for a in c.ativos]

    @property
    def rent_pct(self) -> Decimal | None:
        return None if self.aplicado <= 0 else Decimal(self.lucro_nao_realizado) / self.aplicado * CEM


@dataclass(frozen=True)
class Sugestao:
    ativo: Ativo
    valor: int  # centavos BRL
    quantidade: Decimal | None  # None na renda fixa (ou sem cotação); no Tesouro por títulos, os títulos


def _centavo(valor: Decimal, modo=ROUND_HALF_UP) -> int:
    return int(valor.quantize(UM, rounding=modo))


def pct(parte: int | Decimal, todo: int | Decimal) -> Decimal:
    return Decimal(0) if not todo else Decimal(parte) / Decimal(todo) * CEM


def cambio_atual(s: Session) -> Decimal | None:
    linha = s.scalar(select(CotacaoCache).where(CotacaoCache.simbolo == SIMBOLO_CAMBIO)
                     .order_by(CotacaoCache.data.desc()).limit(1))
    return None if linha is None else linha.fechamento


def series_indicadores(s: Session) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    cdi: dict[date, Decimal] = {}
    ipca: dict[date, Decimal] = {}
    for r in s.scalars(select(SerieIndicador).order_by(SerieIndicador.data)):
        (cdi if r.serie == "cdi" else ipca)[r.data] = r.valor_pct
    return cdi, ipca


def locais_da_reserva(s: Session) -> tuple[set[int], set[int]]:
    """Onde fica a reserva de emergência: (ids das caixinhas da meta, ids das contas dela para lotes sem caixinha)."""
    metas = list(s.scalars(select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa)))
    if not metas:
        return set(), set()
    caixinhas = set(s.scalars(select(Caixinha.id).where(Caixinha.meta_id.in_([m.id for m in metas]), Caixinha.ativa)))
    return caixinhas, {m.conta_id for m in metas if m.conta_id}


def eh_da_reserva(op: OperacaoInvest, caixinhas: set[int], contas: set[int]) -> bool:
    """O lote da operação fica numa caixinha da reserva (ou, sem caixinha, na conta da reserva)."""
    if op.custodia_caixinha_id is not None:
        return op.custodia_caixinha_id in caixinhas
    return op.custodia_conta_id in contas


def locais_da_oportunidade(s: Session) -> set[int]:
    """Caixinhas de reserva de oportunidade (as que também são da reserva de emergência ficam com a emergência)."""
    da_emergencia, _ = locais_da_reserva(s)
    return set(s.scalars(select(Caixinha.id).where(Caixinha.oportunidade, Caixinha.ativa))) - da_emergencia


def operacoes_por_ativo(s: Session, ate: date | None = None, carteira: CarteiraModelo | None = None,
                        reserva: str = "excluir", oportunidade: str = "excluir") -> dict[int, list[OperacaoInvest]]:
    """Operações por ativo; com `carteira`, só as dela (na padrão entram também as sem carteira).

    `reserva`: "excluir" (padrão: a reserva de emergência não entra na carteira), "so" (só as dela) ou "todas".
    `oportunidade`: o mesmo para as caixinhas de reserva de oportunidade.
    """
    consulta = select(OperacaoInvest).order_by(OperacaoInvest.data, OperacaoInvest.id)
    if ate is not None:
        consulta = consulta.where(OperacaoInvest.data <= ate)
    if carteira is not None:
        consulta = consulta.where(or_(OperacaoInvest.carteira_id == carteira.id, OperacaoInvest.carteira_id.is_(None))
                                  if carteira.padrao else OperacaoInvest.carteira_id == carteira.id)
    caixinhas, contas = locais_da_reserva(s) if reserva != "todas" else (set(), set())
    da_oportunidade = locais_da_oportunidade(s) if oportunidade != "todas" else set()
    resultado: dict[int, list[OperacaoInvest]] = defaultdict(list)
    for op in s.scalars(consulta):
        if reserva != "todas" and eh_da_reserva(op, caixinhas, contas) != (reserva == "so"):
            continue
        if oportunidade != "todas" and (op.custodia_caixinha_id in da_oportunidade) != (oportunidade == "so"):
            continue
        resultado[op.ativo_id].append(op)
    return resultado


def metas_por_classe(s: Session) -> dict[str, Decimal]:
    """Meta de cada alocação (`MetaClasse.classe` guarda a chave de ALOCACOES)."""
    metas = {c: Decimal(0) for c in ALOCACOES}
    for m in s.scalars(select(MetaClasse)):
        if m.classe in metas:
            metas[m.classe] = m.meta_pct
    return metas


# ---------- carteiras ----------

def carteiras(s: Session) -> list[CarteiraModelo]:
    """A padrão primeiro, depois as outras por nome."""
    return list(s.scalars(select(CarteiraModelo).order_by(CarteiraModelo.padrao.desc(), CarteiraModelo.nome)))


def carteira_padrao(s: Session) -> CarteiraModelo | None:
    return s.scalar(select(CarteiraModelo).where(CarteiraModelo.padrao).limit(1))


def garantir_carteira_padrao(s: Session) -> CarteiraModelo:
    """Idempotente: a carteira padrão ("Minha carteira") recebe as operações sem carteira."""
    padrao = carteira_padrao(s)
    if padrao is None:
        padrao = CarteiraModelo(nome="Minha carteira", padrao=True)
        s.add(padrao)
        s.flush()
    return padrao


def contas_da_carteira(carteira: CarteiraModelo) -> set[int]:
    return {int(x) for x in (carteira.contas or "").split(",") if x.strip().isdigit()}


def carteira_da_conta(s: Session, conta_id: int | None) -> CarteiraModelo | None:
    """Carteira sugerida para um lote guardado nessa conta (a que lista a conta; senão, a padrão)."""
    lista = carteiras(s)
    return next((c for c in lista if conta_id in contas_da_carteira(c)), None) or next((c for c in lista if c.padrao), None)


def metas_da_carteira(s: Session, carteira: CarteiraModelo | None) -> tuple[dict[str, Decimal], dict[int, Decimal]]:
    """(meta de cada alocação, meta de cada ativo dentro da alocação). A padrão usa MetaClasse e Ativo.meta_pct."""
    if carteira is None or carteira.padrao:
        return metas_por_classe(s), {a.id: a.meta_pct for a in s.scalars(select(Ativo))}
    classes = {c: Decimal(0) for c in ALOCACOES}
    for m in s.scalars(select(MetaClasseCarteira).where(MetaClasseCarteira.carteira_id == carteira.id)):
        if m.classe in classes:
            classes[m.classe] = m.meta_pct
    ativos = {m.ativo_id: m.meta_pct for m in s.scalars(
        select(MetaAtivoCarteira).where(MetaAtivoCarteira.carteira_id == carteira.id))}
    return classes, ativos


def salvar_metas_carteira(s: Session, carteira: CarteiraModelo, por_classe: dict[str, Decimal],
                          por_ativo: dict[int, Decimal]) -> None:
    if carteira.padrao:
        for classe, valor in por_classe.items():
            linha = s.get(MetaClasse, classe)
            if linha is None:
                s.add(MetaClasse(classe=classe, meta_pct=valor))
            else:
                linha.meta_pct = valor
        for ativo_id, valor in por_ativo.items():
            s.get(Ativo, ativo_id).meta_pct = valor
    else:
        for classe, valor in por_classe.items():
            linha = s.get(MetaClasseCarteira, (carteira.id, classe))
            if linha is None:
                s.add(MetaClasseCarteira(carteira_id=carteira.id, classe=classe, meta_pct=valor))
            else:
                linha.meta_pct = valor
        for ativo_id, valor in por_ativo.items():
            linha = s.get(MetaAtivoCarteira, (carteira.id, ativo_id))
            if linha is None:
                s.add(MetaAtivoCarteira(carteira_id=carteira.id, ativo_id=ativo_id, meta_pct=valor))
            else:
                linha.meta_pct = valor
    s.flush()


def migrar_alocacao(s: Session) -> None:
    """Idempotente: preenche `Ativo.alocacao` vazia pelo padrão do tipo e passa as metas de tipo para alocação.

    As metas antigas por tipo (`acao`, `fii`, `etf`, `exterior`) somam na meta de `acoes` e são apagadas.
    """
    for a in s.scalars(select(Ativo).where(or_(Ativo.alocacao.is_(None), Ativo.alocacao == ""))):
        a.alocacao = alocacao_padrao(a.classe, a.codigo)
    antigas = [m for m in s.scalars(select(MetaClasse)) if m.classe not in ALOCACOES]
    if not antigas:
        return
    acoes = s.get(MetaClasse, "acoes")
    if acoes is None:
        acoes = MetaClasse(classe="acoes", meta_pct=Decimal(0))
        s.add(acoes)
    acoes.meta_pct += sum((m.meta_pct for m in antigas), Decimal(0))
    for m in antigas:
        s.delete(m)
    s.flush()


def migrar_simbolos_cripto(s: Session) -> None:
    """Idempotente: troca os pares cripto em reais (`BTC-BRL`), que o Yahoo deixou de cotar, pelos em dólar."""
    for a in s.scalars(select(Ativo).where(Ativo.classe == "cripto", Ativo.simbolo_cotacao.like("%-BRL"))):
        a.simbolo_cotacao = a.simbolo_cotacao[:-4] + "-USD"
    s.flush()


def posicao_do_ativo(ativo: Ativo, ops: list[OperacaoInvest], cdi, ipca, ate: date | None = None) -> Posicao:
    """`ate`: data até onde o rendimento embutido do ativo (se tiver) cresce a quantidade."""
    por_titulos = bool(getattr(ativo, "por_titulos", False))
    callback = valor_na_data_para_posicao(ativo, cdi, ipca) if ativo.classe == "renda_fixa" and not por_titulos else None
    rendimento = getattr(ativo, "rendimento_aa", None) if ativo.classe != "renda_fixa" else None
    return posicao(ops, callback, por_titulos, rendimento, ate)


def _cotacao_brl(s: Session, ativo: Ativo, cambio: Decimal | None, ops: list) -> tuple:
    """(centavos BRL por unidade | None, variação, desatualizada, manual)."""
    c = cotacao_do_ativo(s, ativo)
    if c is None:
        return None, None, False, False
    preco = c.preco * CEM
    if c.moeda == "USD" or ativo.moeda == "USD":
        taxa = cambio
        if taxa is None:  # sem câmbio em cache: usa o da última operação
            taxa = next((Decimal(o.cambio) for o in reversed(ops) if o.cambio), None)
        if taxa is None:
            return None, None, c.desatualizada, c.origem == "manual"
        preco = preco * taxa
    return preco, c.variacao_dia_pct, c.desatualizada, c.origem == "manual"


def _variacao_tesouro(s: Session, ativo: Ativo, do_dia) -> Decimal | None:
    """Preço de venda do dia contra o do último dia anterior com preço (None sem preço anterior)."""
    anterior = preco_de_mercado(s, ativo, do_dia.data - timedelta(days=1))
    if anterior is None or not anterior.pu_venda:
        return None
    return (Decimal(do_dia.pu_venda) / Decimal(anterior.pu_venda) - UM) * CEM


def _variacao_na_curva(ativo: Ativo, ops: list, cdi: dict[date, Decimal], ipca, hoje: date) -> Decimal | None:
    """Renda fixa na curva: o que rendeu do penúltimo ao último dia útil da série do CDI (até `hoje`), sobre o que
    já estava aplicado na véspera; o aporte do dia não é rendimento. None sem dois dias úteis ou sem saldo na véspera."""
    dias = sorted(cdi)
    i = bisect_right(dias, hoje)
    if i < 2:
        return None
    vespera, ultimo = dias[i - 2], dias[i - 1]
    ja_aplicado = [o for o in ops if o.data <= vespera]
    antes = valor_atualizado(ativo, ja_aplicado, cdi, ipca, vespera)
    if antes <= 0:
        return None
    return (Decimal(valor_atualizado(ativo, ja_aplicado, cdi, ipca, ultimo)) / antes - UM) * CEM


def resumo_carteira(s: Session, hoje: date, carteira: CarteiraModelo | None = None) -> Carteira:
    """Uma carteira, com as metas dela; sem `carteira`, a consolidada: todas as operações, com as metas de cada
    carteira ponderadas pelo patrimônio dela (`metas_ponderadas`)."""
    ops_oportunidade = operacoes_por_ativo(s, carteira=carteira, oportunidade="so")
    if carteira is not None:
        # a reserva de emergência aparece também na carteira escolhida na meta dela
        mostra_reserva = s.scalar(select(Meta.id).where(Meta.tipo == "reserva_emergencia", Meta.ativa,
                                                        Meta.carteira_id == carteira.id).limit(1)) is not None
        return _montar(s, hoje, operacoes_por_ativo(s, carteira=carteira), *metas_da_carteira(s, carteira),
                       carteira=carteira, ops_oportunidade=ops_oportunidade,
                       ops_reserva=operacoes_por_ativo(s, reserva="so") if mostra_reserva else None)
    return _montar(s, hoje, operacoes_por_ativo(s), *metas_ponderadas(s, hoje),
                   ops_reserva=operacoes_por_ativo(s, reserva="so"), ops_oportunidade=ops_oportunidade)


def metas_ponderadas(s: Session, hoje: date) -> tuple[dict[str, Decimal], dict[int, Decimal]]:
    """Metas da consolidada: o alvo em reais de cada carteira (patrimônio dela × meta dela), somado e dividido pelo
    patrimônio total. Com todas as carteiras na meta, a consolidada também fica. Carteira sem metas entra com a
    alocação que tem hoje (não gera desvio). Sem patrimônio em nenhuma: as metas da padrão."""
    alvo_classe: dict[str, Decimal] = defaultdict(Decimal)
    alvo_ativo: dict[int, Decimal] = defaultdict(Decimal)
    total = 0
    for c in carteiras(s):
        r = resumo_carteira(s, hoje, c)
        total += r.patrimonio
        sem_metas = not any(x.meta_pct for x in r.classes)
        for classe in r.classes:
            alvo_classe[classe.classe] += Decimal(classe.saldo if sem_metas else r.patrimonio * classe.meta_pct / CEM)
        for a in r.ativos:
            alvo_ativo[a.ativo.id] += Decimal(a.saldo if sem_metas else r.patrimonio * a.ideal_pct / CEM)
    if not total:
        return metas_da_carteira(s, carteira_padrao(s))
    metas = {k: alvo_classe[k] / total * CEM for k in ALOCACOES}
    por_classe = {a.id: a.alocacao for a in s.scalars(select(Ativo).where(Ativo.id.in_(list(alvo_ativo))))}
    metas_ativo = {i: v / alvo_classe[por_classe[i]] * CEM if alvo_classe[por_classe[i]] else Decimal(0)
                   for i, v in alvo_ativo.items()}
    return metas, metas_ativo


def ativos_desejados(s: Session, carteira: CarteiraModelo | None) -> set[int]:
    """Ids dos ativos marcados como "ainda não comprei" na carteira; sem `carteira` (consolidada), em qualquer uma."""
    consulta = select(AtivoDesejado.ativo_id)
    if carteira is not None:
        consulta = consulta.where(AtivoDesejado.carteira_id == carteira.id)
    return set(s.scalars(consulta))


def carteiras_do_desejo(s: Session, ativo: Ativo) -> set[int]:
    return set(s.scalars(select(AtivoDesejado.carteira_id).where(AtivoDesejado.ativo_id == ativo.id)))


def marcar_desejado(s: Session, ativo: Ativo, ids_carteira: set[int]) -> None:
    """Deixa o ativo desejado exatamente nessas carteiras (adiciona as que faltam, tira as outras)."""
    atuais = carteiras_do_desejo(s, ativo)
    for cid in ids_carteira - atuais:
        s.add(AtivoDesejado(carteira_id=cid, ativo_id=ativo.id))
    if atuais - ids_carteira:
        s.execute(delete(AtivoDesejado).where(AtivoDesejado.ativo_id == ativo.id,
                                              AtivoDesejado.carteira_id.in_(atuais - ids_carteira)))
    s.flush()


def _tem_posicao(s: Session, ativo: Ativo, carteira: CarteiraModelo, cdi, ipca) -> bool:
    ops = operacoes_por_ativo(s, carteira=carteira, reserva="todas", oportunidade="todas").get(ativo.id, [])
    return bool(ops) and posicao_do_ativo(ativo, ops, cdi, ipca).quantidade > 0


def carteiras_sem_posicao(s: Session, ativo: Ativo) -> set[int]:
    """Carteiras em que o ativo aparece sem ter posição nelas: marcado como desejado ou com meta diferente de zero."""
    cdi, ipca = series_indicadores(s)
    ids = carteiras_do_desejo(s, ativo)
    for c in carteiras(s):
        if c.id not in ids and metas_da_carteira(s, c)[1].get(ativo.id) and not _tem_posicao(s, ativo, c, cdi, ipca):
            ids.add(c.id)
    return ids


def definir_carteiras_sem_posicao(s: Session, ativo: Ativo, ids_carteira: set[int]) -> None:
    """Deixa o ativo desejado nessas carteiras. Nas outras em que ele não tem posição, zera a meta dele, senão a meta
    o manteria na lista."""
    marcar_desejado(s, ativo, ids_carteira)
    cdi, ipca = series_indicadores(s)
    for c in carteiras(s):
        if c.id in ids_carteira or _tem_posicao(s, ativo, c, cdi, ipca):
            continue
        if c.padrao:
            ativo.meta_pct = Decimal(0)
        elif (linha := s.get(MetaAtivoCarteira, (c.id, ativo.id))) is not None:
            linha.meta_pct = Decimal(0)
    s.flush()


CHAVE_MIGROU_DESEJADOS = "migrou_ativos_desejados"


def migrar_ativos_desejados(s: Session) -> None:
    """Uma vez só: ativos sem operação passam a ser desejados na carteira da coluna antiga `ativo.carteira_desejo_id`
    (vazia: a padrão)."""
    if get_config(s, CHAVE_MIGROU_DESEJADOS, False):
        return
    padrao = garantir_carteira_padrao(s)
    colunas = {linha[1] for linha in s.execute(text('PRAGMA table_info("ativo")'))}
    sql = ("SELECT id, carteira_desejo_id FROM ativo" if "carteira_desejo_id" in colunas
           else "SELECT id, NULL FROM ativo")
    com_operacao = set(s.scalars(select(OperacaoInvest.ativo_id).distinct()))
    existentes = {c.id for c in carteiras(s)}
    for ativo_id, carteira_id in s.execute(text(sql + " WHERE ativo = 1")):
        if ativo_id not in com_operacao:
            cid = carteira_id if carteira_id in existentes else padrao.id
            if s.get(AtivoDesejado, (cid, ativo_id)) is None:
                s.add(AtivoDesejado(carteira_id=cid, ativo_id=ativo_id))
    set_config(s, CHAVE_MIGROU_DESEJADOS, True)
    s.flush()


def _montar(s: Session, hoje: date, ops_por: dict, metas: dict[str, Decimal],
            metas_ativo: dict[int, Decimal], ops_reserva: dict | None = None,
            carteira: CarteiraModelo | None = None, ops_oportunidade: dict | None = None) -> Carteira:
    metas = {k: metas.get(k, Decimal(0)) for k in ALOCACOES}
    cdi, ipca = series_indicadores(s)
    cambio = cambio_atual(s)
    nomes_conta = {c.id: c.nome for c in s.scalars(select(Conta))}
    nomes_caixinha = {k.id: k.nome for k in s.scalars(select(Caixinha))}

    def nome_local(chave) -> str:
        if chave is None:
            return "Sem local"
        conta, caixinha = chave
        return nomes_conta.get(conta, "?") + (f" · {nomes_caixinha.get(caixinha, '?')}" if caixinha else "")

    def calcular(a: Ativo, ops: list, meta_ativo: Decimal, desejado: bool = False):
        """(ativo, pos, saldo, cotacao, variacao, desat, manual, estimado, sem_cotacao, meta_ativo) ou None.

        `desejado`: ativo cadastrado sem nenhuma operação ("ainda não comprei"), listado para receber meta.
        """
        pos = posicao_do_ativo(a, ops, cdi, ipca, hoje)
        if pos.quantidade == 0 and meta_ativo == 0 and not desejado:
            return None
        cotacao = variacao = compra = None
        desat = manual = estimado = sem_cotacao = False
        mercado = preco_de_mercado(s, a, hoje) if a.classe == "renda_fixa" and a.por_titulos else None
        if mercado is not None:  # Tesouro: quanto o título vale hoje se vendido (preço de mercado)
            cotacao, compra = Decimal(mercado.pu_venda), Decimal(mercado.pu_compra)
            saldo = _centavo(pos.quantidade * cotacao)
            variacao = _variacao_tesouro(s, a, mercado)
        elif a.classe == "renda_fixa":
            det = valor_atualizado_detalhe(a, ops, cdi, ipca, hoje)
            saldo, estimado = det.valor, det.estimado
            variacao = _variacao_na_curva(a, ops, cdi, ipca, hoje)
        else:
            cotacao, variacao, desat, manual = _cotacao_brl(s, a, cambio, ops)
            if cotacao is None:
                saldo, sem_cotacao = pos.custo_brl, pos.quantidade > 0
            else:
                saldo = _centavo(pos.quantidade * cotacao)
        return (a, pos, saldo, cotacao, variacao, desat, manual, estimado, sem_cotacao, meta_ativo, compra)

    por_corretora: dict[str, int] = defaultdict(int)
    por_local: dict = defaultdict(int)

    def repartir(ativo_id: int, saldo: int, ops_do_conjunto: dict) -> None:
        """Reparte o saldo pelos locais na proporção da quantidade de cada um."""
        if not saldo:
            return
        locais = quantidade_por_local(ops_do_conjunto.get(ativo_id, []))
        total_q = sum(locais.values())
        if not total_q:
            locais, total_q = {None: Decimal(1)}, Decimal(1)
        for chave, q in locais.items():
            parte = _centavo(Decimal(saldo) * q / total_q)
            por_corretora[nome_local(chave)] += parte
            por_local[chave] += parte

    todos = list(s.scalars(select(Ativo).where(Ativo.ativo).order_by(Ativo.codigo)))
    desejados = ativos_desejados(s, carteira)
    brutos = [b for a in todos
              if (b := calcular(a, ops_por.get(a.id, []), metas_ativo.get(a.id, Decimal(0)),
                                desejado=a.id in desejados and not ops_por.get(a.id))) is not None]

    patrimonio = sum(b[2] for b in brutos)
    saldo_classe: dict[str, int] = defaultdict(int)
    for b in brutos:
        saldo_classe[b[0].alocacao] += b[2]

    classes: list[LinhaClasse] = []
    for classe in ALOCACOES:
        linhas = []
        da_classe = [b for b in brutos if b[0].alocacao == classe]
        base_classe = max(Decimal(saldo_classe[classe]), metas[classe] / CEM * patrimonio)
        for a, pos, saldo, cot, var, desat, manual, est, sem, meta_ativo, compra in da_classe:
            custo = pos.custo_brl
            linhas.append(LinhaAtivo(
                ativo=a, quantidade=pos.quantidade, preco_medio_brl=pos.preco_medio_brl, custo=custo,
                cotacao_brl=cot, variacao_dia_pct=var, saldo=saldo,
                pct_classe=pct(saldo, saldo_classe[classe]), pct_carteira=pct(saldo, patrimonio),
                meta_classe_pct=metas[classe], meta_ativo_pct=meta_ativo,
                meta_carteira_pct=metas[classe] * meta_ativo / CEM,
                rent_pct=None if custo <= 0 else Decimal(saldo - custo) / custo * CEM,
                rent_brl=saldo - custo, lucro_realizado=pos.lucro_realizado_brl,
                desatualizada=desat, preco_manual=manual, estimado=est, sem_cotacao=sem,
                ideal_classe_pct=meta_ativo, ideal_pct=metas[classe] * meta_ativo / CEM,
                falta_classe=_centavo(meta_ativo / CEM * base_classe) - saldo, preco_compra=compra,
            ))
            repartir(a.id, saldo, ops_por)
        if not linhas and metas[classe] == 0:
            continue
        linhas.sort(key=lambda x: (-x.meta_ativo_pct, -x.saldo, x.ativo.codigo))  # a maior meta primeiro
        classes.append(LinhaClasse(
            classe=classe, ativos=linhas, saldo=saldo_classe[classe], custo=sum(x.custo for x in linhas),
            pct_atual=pct(saldo_classe[classe], patrimonio), meta_pct=metas[classe],
            lucro_realizado=sum(x.lucro_realizado for x in linhas),
            falta=_centavo(metas[classe] / CEM * patrimonio) - saldo_classe[classe],
        ))

    def fora_da_alocacao(ops_do_conjunto: dict) -> list[LinhaAtivo]:
        """Posições listadas à parte (sem metas, % dentro do próprio conjunto), que contam nos locais."""
        linhas: list[LinhaAtivo] = []
        brutos_fora = [b for a in todos if (b := calcular(a, ops_do_conjunto.get(a.id, []), Decimal(0))) is not None]
        total = sum(b[2] for b in brutos_fora)
        for a, pos, saldo, cot, var, desat, manual, est, sem, _, _ in brutos_fora:
            custo = pos.custo_brl
            linhas.append(LinhaAtivo(
                ativo=a, quantidade=pos.quantidade, preco_medio_brl=pos.preco_medio_brl, custo=custo,
                cotacao_brl=cot, variacao_dia_pct=var, saldo=saldo,
                pct_classe=pct(saldo, total), pct_carteira=pct(saldo, total),
                meta_classe_pct=Decimal(0), meta_ativo_pct=Decimal(0), meta_carteira_pct=Decimal(0),
                rent_pct=None if custo <= 0 else Decimal(saldo - custo) / custo * CEM,
                rent_brl=saldo - custo, lucro_realizado=pos.lucro_realizado_brl,
                desatualizada=desat, preco_manual=manual, estimado=est, sem_cotacao=sem,
            ))
            repartir(a.id, saldo, ops_do_conjunto)
        return linhas

    # reserva de emergência (na consolidada e na carteira escolhida na meta): fica de fora de classes, patrimônio e metas, mas é listada e
    # conta nos locais (a conciliação das caixinhas da reserva usa `por_local`)
    reserva = fora_da_alocacao(ops_reserva or {})
    # reserva de oportunidade: o mesmo, na carteira das operações (na consolidada, de todas)
    oportunidade = fora_da_alocacao(ops_oportunidade or {})

    reserva_alvo = 0
    if reserva:
        from app.services.metas import reserva as reserva_da_meta  # import local: metas também usa a carteira
        meta_reserva = s.scalar(select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa).order_by(Meta.id).limit(1))
        if meta_reserva is not None:
            reserva_alvo = reserva_da_meta(s, meta_reserva, hoje).alvo

    aplicado = sum(c.custo for c in classes)
    return Carteira(
        classes=classes, patrimonio=patrimonio, aplicado=aplicado, lucro_nao_realizado=patrimonio - aplicado,
        lucro_realizado=sum(c.lucro_realizado for c in classes), por_corretora=dict(por_corretora), cambio=cambio,
        por_local=dict(por_local), reserva=reserva, reserva_alvo=reserva_alvo, oportunidade=oportunidade,
    )


@dataclass(frozen=True)
class AtivoPlano:
    ativo: Ativo
    pct_antes: Decimal  # % dentro da classe
    pct_depois: Decimal
    meta_pct: Decimal  # meta dentro da classe
    falta_antes: int  # centavos para a meta (com o total depois do aporte); <= 0: acima da meta
    falta_depois: int
    fora: bool  # acima da meta: não recebe


@dataclass(frozen=True)
class ClassePlano:
    classe: str
    pct_antes: Decimal  # % na carteira
    pct_depois: Decimal
    meta_pct: Decimal
    falta_antes: int
    recebeu: int
    falta_depois: int
    fora: bool  # acima da meta (ou, com uma classe escolhida, não é ela): não recebe
    ativos: list[AtivoPlano]


@dataclass(frozen=True)
class PlanoAporte:
    """As compras sugeridas e a carteira antes e depois delas (o que sobra fica fora da alocação)."""
    sugestoes: list[Sugestao]
    classes: list[ClassePlano]


LOTE_TESOURO = Decimal("0.01")  # o Tesouro Direto vende frações de 0,01 título
MINIMO_TESOURO = 3000  # centavos: a compra mínima é 1% do título, respeitado o mínimo de R$ 30


def _minimo_tesouro(pu: Decimal) -> Decimal:
    """Menor quantidade que o Tesouro aceita numa compra: 0,01 título, ou o que passar de R$ 30."""
    return max(LOTE_TESOURO, (MINIMO_TESOURO / pu).quantize(LOTE_TESOURO, rounding=ROUND_UP))


def _custo_tesouro(qtd: Decimal, pu: Decimal) -> int:
    return _centavo(qtd * pu, ROUND_UP)  # arredonda para cima: o plano não promete compra que o dinheiro não paga


def sugerir_aporte(carteira: Carteira, aporte: int, classe: str | None = None) -> list[Sugestao]:
    return plano_aporte(carteira, aporte, classe).sugestoes


def plano_aporte(carteira: Carteira, aporte: int, classe: str | None = None) -> PlanoAporte:
    """Regra da planilha estendida a classes (ideal × (patrimônio + aporte) − atual), comprando cota a cota.

    O dinheiro vai primeiro para a alocação com mais falta em reais e, dentro dela, para o ativo com mais falta. A
    prioridade não trava: se a cota da prioritária não cabe no que resta, tenta a próxima alocação com falta, para
    aproveitar o máximo do saldo. Dividir antes, proporcionalmente, falha com cotas caras: R$ 150 viravam R$ 120 de
    renda fixa (sem cota que coubesse) e R$ 30 de cripto (1 cota).
    Renda fixa e frações (cripto, exterior) levam o valor quebrado, até a falta do ativo. Tesouro por títulos é
    como cota: lotes de 0,01 título pelo PU de compra do dia, a primeira compra com o mínimo do Tesouro (R$ 30).
    Com `classe`, o aporte inteiro vai para essa alocação e só se reparte entre os ativos dela.
    A sobra (aporte − soma dos valores sugeridos) fica a cargo de quem chama (`sobra_aporte`).
    """
    if aporte <= 0:
        return PlanoAporte([], [])
    total = Decimal(carteira.patrimonio + aporte)
    if classe is not None:
        alvos = {c.classe: Decimal(c.saldo + aporte) for c in carteira.classes if c.classe == classe}
    else:
        alvos = {c.classe: c.meta_pct / CEM * total for c in carteira.classes}
    falta_classe = {c.classe: alvos[c.classe] - c.saldo for c in carteira.classes if c.classe in alvos}
    falta_classe_antes = dict(falta_classe)
    falta: dict[int, Decimal] = {}
    linhas: dict[int, LinhaAtivo] = {}
    classe_de: dict[int, str] = {}
    for c in carteira.classes:
        if c.classe not in alvos:
            continue
        for a in c.ativos:
            linhas[a.ativo.id], classe_de[a.ativo.id] = a, c.classe
            falta[a.ativo.id] = a.ideal_classe_pct / CEM * alvos[c.classe] - a.saldo
    falta_antes = dict(falta)
    quantidades: dict[int, Decimal] = {}
    valores: dict[int, int] = {}
    esgotados: set[int] = set()  # sem cotação, renda fixa/frações já atendidas ou sem falta
    dinheiro = aporte

    def comprar(i: int, topo: str) -> bool:
        """Uma compra no ativo `i` (cota inteira; Tesouro: um lote de 0,01 título; renda fixa/frações: o valor
        quebrado de uma vez). Comprou?"""
        nonlocal dinheiro
        a = linhas[i]
        if a.ativo.classe != "renda_fixa" and (a.cotacao_brl is None or a.cotacao_brl <= 0):
            valores.setdefault(i, 0)  # aparece como "sem cotação", sem gastar
            esgotados.add(i)
            return False
        if a.preco_compra:  # Tesouro por títulos: múltiplos de 0,01 título, a primeira compra com o mínimo
            antes = quantidades.get(i, Decimal(0))
            qtd = antes + LOTE_TESOURO if antes else _minimo_tesouro(a.preco_compra)
            valor = _custo_tesouro(qtd, a.preco_compra) - valores.get(i, 0)
            if valor > dinheiro:
                return False  # o lote não cabe no que resta
            quantidades[i] = qtd
        elif a.ativo.classe == "renda_fixa" or a.ativo.classe in CLASSES_FRACIONADAS:
            esgotados.add(i)  # leva o valor quebrado de uma vez
            parte = min(Decimal(dinheiro), falta[i], falta_classe[topo])
            if a.ativo.classe == "renda_fixa":
                valor = _centavo(parte, ROUND_DOWN)
            else:
                qtd = (parte / a.cotacao_brl).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                valor = _centavo(qtd * a.cotacao_brl, ROUND_DOWN)
                quantidades[i] = quantidades.get(i, Decimal(0)) + qtd
            if valor <= 0:
                return False
        else:
            valor = _centavo(a.cotacao_brl, ROUND_DOWN)
            if valor > dinheiro:
                return False  # a cota não cabe no que resta
            quantidades[i] = quantidades.get(i, Decimal(0)) + UM
        valores[i] = valores.get(i, 0) + valor
        falta[i] -= valor
        falta_classe[topo] -= valor
        dinheiro -= valor
        return True

    while dinheiro > 0:
        # a alocação com mais falta tem a prioridade; se nenhuma cota dela cabe, tenta a próxima (aproveitar o saldo)
        abertas = sorted((n for n, f in falta_classe.items() if f > 0),
                         key=lambda n: (-falta_classe[n], list(ALOCACOES).index(n)))
        comprou = False
        for topo in abertas:
            candidatos = sorted((i for i in linhas if classe_de[i] == topo and i not in esgotados and falta[i] > 0),
                                key=lambda i: -falta[i])
            if any(comprar(i, topo) for i in candidatos):
                comprou = True
                break
        if not comprou:
            break  # nenhuma cota cabe no que resta

    # na ordem das alocações pela falta (como os blocos) e, dentro delas, a ordem da carteira (maior meta primeiro)
    chaves = list(ALOCACOES)
    ordem = {c.classe: k for k, c in enumerate(sorted(carteira.classes, key=lambda c: (-gap_alocacao(c), chaves.index(c.classe))))}
    sugestoes: list[Sugestao] = []
    for i in sorted(valores, key=lambda i: (ordem[classe_de[i]], list(linhas).index(i))):
        a = linhas[i]
        sem_cotacao = a.ativo.classe != "renda_fixa" and (a.cotacao_brl is None or a.cotacao_brl <= 0)
        if sem_cotacao:
            sugestoes.append(Sugestao(a.ativo, 0, None))
        elif valores[i] > 0:
            sugestoes.append(Sugestao(a.ativo, valores[i], quantidades.get(i) if a.ativo.classe == "renda_fixa" else quantidades[i]))

    # antes e depois das compras: % na carteira e na classe, falta em reais
    comprado = sum(valores.values())
    classes: list[ClassePlano] = []
    for c in sorted(carteira.classes, key=lambda c: ordem[c.classe]):
        recebeu = sum(valores.get(a.ativo.id, 0) for a in c.ativos)
        f_antes = falta_classe_antes.get(c.classe, Decimal(0))
        ativos = [AtivoPlano(
            ativo=a.ativo, pct_antes=a.pct_classe,
            pct_depois=pct(a.saldo + valores.get(a.ativo.id, 0), c.saldo + recebeu), meta_pct=a.ideal_classe_pct,
            falta_antes=_centavo(falta_antes.get(a.ativo.id, Decimal(0))),
            falta_depois=_centavo(falta_antes.get(a.ativo.id, Decimal(0))) - valores.get(a.ativo.id, 0),
            fora=c.classe not in alvos or falta_antes.get(a.ativo.id, Decimal(0)) <= 0,
        ) for a in c.ativos]
        classes.append(ClassePlano(
            classe=c.classe, pct_antes=c.pct_atual, pct_depois=pct(c.saldo + recebeu, carteira.patrimonio + comprado),
            meta_pct=c.meta_pct, falta_antes=_centavo(f_antes), recebeu=recebeu,
            falta_depois=_centavo(f_antes) - recebeu, fora=c.classe not in alvos or f_antes <= 0, ativos=ativos,
        ))
    return PlanoAporte(sugestoes, classes)


CHAVE_ORDEM_CLASSES = "ordem_classes_investimento"


def gap_alocacao(classe: LinhaClasse) -> Decimal:
    """Quanto falta, em pontos percentuais, para a alocação chegar à meta. Só para baixo: no buy & hold não se vende
    para rebalancear, então acima da meta o gap é 0 (a alocação só deixa de receber aporte)."""
    return max(Decimal(0), classe.meta_pct - classe.pct_atual)


def ordem_manual(s: Session) -> list[str]:
    """A ordem que o usuário escolheu para os blocos (vazia = automática)."""
    return list(dict.fromkeys(c for c in get_config(s, CHAVE_ORDEM_CLASSES, []) if c in ALOCACOES))


def classes_na_ordem(s: Session, carteira: Carteira) -> list[LinhaClasse]:
    """Sem ordem escolhida, o maior gap primeiro (empate, inclusive as que têm gap 0: a ordem padrão das
    alocações); com ordem escolhida, a dela, e o que ela não cita vem no fim, por gap."""
    chaves = list(ALOCACOES)
    por_gap = sorted(carteira.classes, key=lambda c: (-gap_alocacao(c), chaves.index(c.classe)))
    manual = ordem_manual(s)
    if not manual:
        return por_gap
    posicao = {c: i for i, c in enumerate(manual)}
    return sorted(por_gap, key=lambda c: posicao.get(c.classe, len(posicao)))


def mover_classe(s: Session, atual: list[str], classe: str, passo: int) -> None:
    """Troca `classe` de lugar com a vizinha na ordem exibida `atual` (-1 sobe, 1 desce) e guarda a ordem."""
    if classe not in atual or passo not in (-1, 1):
        return
    i = atual.index(classe)
    j = i + passo
    if not 0 <= j < len(atual):
        return
    nova = list(atual)
    nova[i], nova[j] = nova[j], nova[i]
    set_config(s, CHAVE_ORDEM_CLASSES, nova + [c for c in ALOCACOES if c not in nova])


def limpar_ordem_classes(s: Session) -> None:
    """Volta para a ordem automática (maior gap primeiro)."""
    item = s.get(Config, CHAVE_ORDEM_CLASSES)
    if item is not None:
        s.delete(item)
        s.flush()


def sobra_aporte(sugestoes: list[Sugestao], aporte: int) -> int:
    return aporte - sum(x.valor for x in sugestoes)


@dataclass(frozen=True)
class PlanoOportunidade:
    """O aporte com a reserva de oportunidade: o que dá para comprar e quanto entra ou sai dela."""
    disponivel: int  # aporte + o saldo da reserva (se usado)
    comprado: int
    fica: int  # saldo da reserva depois das compras
    resgatar: int  # quanto tirar da reserva para as compras
    aplicar: int  # quanto pôr na reserva (a sobra que não fecha cota)


def plano_oportunidade(*, saldo: int, aporte: int, usar: bool, sugestoes: list[Sugestao]) -> PlanoOportunidade:
    disponivel = aporte + (saldo if usar else 0)
    comprado = sum(x.valor for x in sugestoes)
    fica = disponivel - comprado + (0 if usar else saldo)
    return PlanoOportunidade(disponivel=disponivel, comprado=comprado, fica=fica,
                             resgatar=max(0, saldo - fica), aplicar=max(0, fica - saldo))
