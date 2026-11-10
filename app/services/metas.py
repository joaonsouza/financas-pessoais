"""Metas, reserva de emergência e o aporte mensal das metas ("pague primeiro", spec §6.6, §6.7)."""
from dataclasses import dataclass
from datetime import date
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, dia_no_mes, somar_meses, somar_meses_data
from app.models import Caixinha, Categoria, Conta, Lancamento, Meta, MovimentoCaixinha, Orcamento, Provisao, Recorrencia
from app.services.caixinhas import (
    agendado_caixinha, caixinhas_da_conta, criar_caixinha, divisao_conta, excluir_caixinha, guardar_na_caixinha,
    movimentos_caixinha, resgatar_da_caixinha, saldo_caixinha,
)
from app.services.lancamentos import criar_lancamento
from app.services.mes_caixa import com_status, entre_meses, no_mes
from app.services.recorrencia import editar_recorrencia, encerrar_recorrencia, gerar_pendentes
from app.services.resumo_mes import totais_mes
from app.services.saldos import saldo_atual

MESES_BASE_ESSENCIAL = 3
DEGRAU_INICIAL = 100_000  # R$ 1.000
NOME_META_REPOR = "Repor reserva"
PRAZO_REPOR_MESES = 6
NOME_CATEGORIA_OBJETIVOS = "Objetivos"
DIAS_EXPIRA_EM_BREVE = 90


@dataclass(frozen=True)
class ProgressoMeta:
    atual: int
    alvo: int
    meses_restantes: int
    necessario_mes: int
    pct: int

    @property
    def concluido(self) -> bool:
        return self.alvo > 0 and self.atual >= self.alvo

    @property
    def falta(self) -> int:
        return max(self.alvo - self.atual, 0)


def categoria_objetivos(s: Session, tipo: str = "despesa") -> Categoria:
    """Categoria raiz "Objetivos" (despesa ao guardar, receita ao retirar); criada na primeira vez."""
    cat = s.scalar(select(Categoria).where(
        Categoria.nome == NOME_CATEGORIA_OBJETIVOS, Categoria.tipo == tipo, Categoria.pai_id.is_(None)))
    if cat is None:
        cat = Categoria(nome=NOME_CATEGORIA_OBJETIVOS, tipo=tipo, icone="target", cor="#10b981")
        s.add(cat)
        s.flush()
    return cat


def _aportes(s: Session, meta: Meta, status: tuple[str, ...]) -> int:
    """Guardado pelos lançamentos ligados à meta: despesa (saída) soma, receita (resgate) desconta."""
    return -int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.meta_id == meta.id, Lancamento.tipo.in_(("despesa", "receita")),
        Lancamento.status.in_(status), Lancamento.ignorar.is_(False))))


def caixinhas_da_meta(s: Session, meta: Meta) -> list[Caixinha]:
    return list(s.scalars(select(Caixinha).where(Caixinha.meta_id == meta.id, Caixinha.ativa).order_by(Caixinha.id)))


def caixinha_da_meta(s: Session, meta: Meta, conta_id: int, hoje: date, inicial: int = 0,
                     do_livre: bool = False) -> Caixinha:
    """A caixinha da meta na conta; cria (ou adota uma de mesmo nome ainda sem dono) na primeira vez."""
    for c in caixinhas_da_meta(s, meta):
        if c.conta_id == conta_id:
            return c
    for c in caixinhas_da_conta(s, conta_id):
        if c.meta_id is None and c.nome.casefold() == meta.nome.casefold():
            c.meta_id = meta.id
            s.flush()
            return c
    return criar_caixinha(s, s.get(Conta, conta_id), meta.nome, hoje, inicial=inicial, do_livre=do_livre,
                          meta_id=meta.id, icone=meta.icone, cor=meta.cor)


def _atual_da_meta(s: Session, meta: Meta) -> int:
    """Em caixinhas, o guardado é o saldo delas (inclui o que você remanejar para elas); fora do app,
    o valor já guardado informado mais o que entrou e saiu pelos lançamentos ligados à meta."""
    caixinhas = caixinhas_da_meta(s, meta)
    if caixinhas:
        return sum(saldo_caixinha(s, c) for c in caixinhas)
    return (meta.valor_manual or 0) + _aportes(s, meta, ("efetivado",))


def agendado_da_meta(s: Session, meta: Meta) -> int:
    """Aportes ainda pendentes (o "pague primeiro" dos próximos meses, por exemplo)."""
    caixinhas = caixinhas_da_meta(s, meta)
    if caixinhas:
        return sum(agendado_caixinha(s, c) for c in caixinhas)
    return _aportes(s, meta, ("pendente",))


def progresso_meta(s: Session, meta: Meta, hoje: date) -> ProgressoMeta:
    atual = _atual_da_meta(s, meta)
    alvo = reserva(s, meta, hoje).alvo if meta.tipo == "reserva_emergencia" else meta.valor_alvo
    if meta.prazo:
        c1, c2 = competencia_de(hoje), competencia_de(meta.prazo)
        meses = max(1, (int(c2[:4]) - int(c1[:4])) * 12 + int(c2[5:]) - int(c1[5:]))
    else:
        meses = 1
    return ProgressoMeta(
        atual=atual, alvo=alvo, meses_restantes=meses,
        necessario_mes=max(0, ceil((alvo - atual) / meses)),
        pct=atual * 100 // alvo if alvo > 0 else 0,
    )


@dataclass(frozen=True)
class EconomiaMes:
    meta: int
    realizado: int
    projetado: int

    @property
    def atingida_realizado(self) -> bool:
        return self.realizado >= self.meta

    @property
    def atingida_projetado(self) -> bool:
        return self.projetado >= self.meta


def economia_do_mes(s: Session, competencia: str) -> EconomiaMes | None:
    """Balanço do mês (receitas − despesas) contra a meta de economia mensal ativa."""
    meta = s.scalar(select(Meta).where(Meta.tipo == "economia_mensal", Meta.ativa).order_by(Meta.id).limit(1))
    if meta is None:
        return None
    totais = totais_mes(s, competencia)
    return EconomiaMes(meta.valor_alvo, totais.balanco_efetivado + aportes_do_mes(s, competencia, ("efetivado",)),
                       totais.balanco_projetado + aportes_do_mes(s, competencia, ("efetivado", "pendente")))


def aportes_do_mes(s: Session, competencia: str, status: tuple[str, ...]) -> int:
    """Despesas para objetivos menos resgates no mês: o balanço do mês os conta como gasto,
    mas para a economia do mês são dinheiro guardado."""
    return -int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.meta_id.is_not(None), Lancamento.tipo.in_(("despesa", "receita")),
        com_status(status), Lancamento.ignorar.is_(False),
        no_mes(competencia))))


def _ids_essenciais(s: Session) -> list[int]:
    categorias = list(s.scalars(select(Categoria)))
    por_id = {c.id: c for c in categorias}
    return [c.id for c in categorias
            if c.essencial or (c.pai_id is not None and por_id[c.pai_id].essencial)]


def gasto_essencial(s: Session, hoje: date) -> tuple[int, str]:
    """(valor mensal em centavos, base): "3_meses" | "n_meses" | "previsto" (spec §6.7)."""
    atual = competencia_de(hoje)
    ids = _ids_essenciais(s)
    primeira = s.scalar(select(func.min(Lancamento.data)).where(Lancamento.ignorar.is_(False)))
    disponiveis = 0
    if primeira is not None:
        c = competencia_de(primeira)
        disponiveis = min(MESES_BASE_ESSENCIAL, max(0, (int(atual[:4]) - int(c[:4])) * 12 + int(atual[5:]) - int(c[5:])))
    if disponiveis:
        inicio = somar_meses(atual, -disponiveis)
        total = -int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
            Lancamento.tipo == "despesa", com_status(("efetivado",)), Lancamento.ignorar.is_(False),
            Lancamento.categoria_id.in_(ids),
            entre_meses(inicio, somar_meses(atual, -1)),
        )))
        base = "3_meses" if disponiveis == MESES_BASE_ESSENCIAL else "n_meses"
        return total // disponiveis, base
    # Por categoria vale o maior entre recorrências e orçamento, para não contar a mesma despesa duas vezes.
    recorrentes = dict(s.execute(
        select(Recorrencia.categoria_id, func.sum(Recorrencia.valor))
        .where(Recorrencia.ativa, Recorrencia.tipo == "despesa", Recorrencia.categoria_id.in_(ids))
        .group_by(Recorrencia.categoria_id)
    ).all())
    orcados = dict(s.execute(
        select(Orcamento.categoria_id, func.sum(Orcamento.limite))
        .where(Orcamento.competencia == atual, Orcamento.categoria_id.in_(ids))
        .group_by(Orcamento.categoria_id)
    ).all())
    total = sum(max(int(recorrentes.get(c, 0)), int(orcados.get(c, 0))) for c in recorrentes.keys() | orcados.keys())
    return total, "previsto"


@dataclass(frozen=True)
class Degrau:
    rotulo: str
    valor: int
    atingido: bool


@dataclass(frozen=True)
class Reserva:
    alvo: int
    atual: int
    meses_que_aguento: float | None
    degraus: list[Degrau]
    proximo_degrau: Degrau | None
    gasto_essencial: int
    base: str
    multiplicador: int
    sugerido: int  # gasto essencial × meses; o alvo é ele, a não ser que a pessoa defina outro
    personalizado: bool


def reserva(s: Session, meta: Meta, hoje: date) -> Reserva:
    calculado, base = gasto_essencial(s, hoje)
    mult = meta.multiplicador_reserva
    personalizado = meta.valor_alvo > 0
    # alvo definido pela pessoa manda: o mês de referência vira alvo ÷ meses de cobertura,
    # e degraus e "meses que aguento" se ajustam a ele; o calculado fica só como sugestão
    essencial = ceil(meta.valor_alvo / mult) if personalizado else calculado
    atual = _atual_da_meta(s, meta)
    alvos = [("R$ 1.000", DEGRAU_INICIAL), ("1 mês", essencial), ("3 meses", 3 * essencial), ("6 meses", 6 * essencial)]
    degraus = [Degrau(rotulo, valor, atual >= valor) for rotulo, valor in alvos]
    return Reserva(
        alvo=meta.valor_alvo if personalizado else essencial * mult, atual=atual,
        meses_que_aguento=(atual * 10 + essencial // 2) // essencial / 10 if essencial > 0 else None,
        degraus=degraus, proximo_degrau=next((d for d in degraus if not d.atingido), None),
        gasto_essencial=essencial, base="alvo" if personalizado else base, multiplicador=mult,
        sugerido=calculado * mult, personalizado=personalizado,
    )


def _aporte_do_mes_caiu(s: Session, meta_id: int, competencia: str) -> bool:
    """O aporte do mês, por qualquer recorrência da meta, já foi efetivado ou cancelado."""
    return s.scalar(select(Lancamento.id).where(
        Lancamento.recorrencia_id.in_(select(Recorrencia.id).where(Recorrencia.origem_meta_id == meta_id)),
        Lancamento.competencia_recorrencia == competencia,
        Lancamento.status.in_(("efetivado", "cancelado"))).limit(1)) is not None


def _sincronizar_recorrencia(s: Session, rec: Recorrencia | None, *, tipo: str = "transferencia",
                             descricao: str, origem_id: int, destino_id: int | None, valor: int, dia: int,
                             hoje: date, origem_meta_id: int, categoria_id: int | None = None,
                             caixinha_id: int | None = None, fim: str | None = None) -> Recorrencia:
    competencia = competencia_de(hoje)
    inicio = competencia
    if rec is not None and rec.tipo != tipo:
        # a meta passou a ter (ou deixou de ter) conta: transferência <-> despesa; o tipo não se edita
        if rec.ativa:
            encerrar_recorrencia(s, rec, a_partir=competencia)
        rec = None
        if _aporte_do_mes_caiu(s, origem_meta_id, competencia):
            inicio = somar_meses(competencia, 1)  # o aporte deste mês já caiu pela antiga: a nova não o repete
    if rec is None:
        rec = Recorrencia(descricao=descricao, tipo=tipo, conta_id=origem_id, conta_destino_id=destino_id,
                          categoria_id=categoria_id, valor=valor, dia=dia, inicio=inicio,
                          caixinha_id=caixinha_id, fim=fim, origem_meta_id=origem_meta_id)
        s.add(rec)
        s.flush()
        gerar_pendentes(s, hoje)
        return rec
    rec.ativa, rec.caixinha_id = True, caixinha_id
    editar_recorrencia(s, rec, a_partir=competencia, hoje=hoje, descricao=descricao, conta_id=origem_id,
                       conta_destino_id=destino_id, categoria_id=categoria_id, valor=valor, dia=dia, fim=fim)
    return rec


def _recorrencia_da_meta(s: Session, meta: Meta) -> Recorrencia | None:
    return s.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id)
                     .order_by(Recorrencia.id.desc())).first()


def sincronizar_caixinha_meta(s: Session, meta: Meta, hoje: date) -> Caixinha | None:
    """Meta com conta guarda numa caixinha dela nessa conta; o "já guardado" vira o valor inicial.
    Uma meta tem caixinha numa conta só: ao trocar de conta, a antiga perde o vínculo e fica como está."""
    for c in caixinhas_da_meta(s, meta):
        if c.conta_id != meta.conta_id:
            c.meta_id = None  # trocou de conta (ou passou a fora do app): a caixinha antiga fica na conta, sem a meta
    s.flush()
    if not meta.conta_id or meta.tipo == "economia_mensal":
        return None
    nova = not any(c.conta_id == meta.conta_id for c in caixinhas_da_meta(s, meta))
    inicial, do_livre = 0, False
    if nova:
        # "já tem guardado" informado: está na caixinha do banco e soma no saldo da conta;
        # vazio: a caixinha fica com todo o Livre da conta (o caso da conta só para a meta)
        if meta.valor_manual is not None:
            inicial = meta.valor_manual
        else:
            inicial, do_livre = max(divisao_conta(s, s.get(Conta, meta.conta_id)).livre, 0), True
    caixinha = caixinha_da_meta(s, meta, meta.conta_id, hoje, inicial=inicial, do_livre=do_livre)
    caixinha.icone, caixinha.cor = meta.icone, meta.cor
    s.flush()
    return caixinha


@dataclass(frozen=True)
class PlanoAporte:
    alvo: int
    guardado: int
    prazo: date | None
    meses: int        # aportes que ainda faltam até o prazo (inclui o deste mês, se ainda não caiu)
    mensal: int
    ultima: str | None  # competência do último aporte antes do prazo

    @property
    def falta(self) -> int:
        return max(self.alvo - self.guardado, 0)

    @property
    def completo(self) -> bool:
        return self.alvo > 0 and self.guardado >= self.alvo


def _rolar_prazo(meta: Meta, hoje: date) -> None:
    """Meta que repete todo ano: passado o prazo, o próximo é no ano seguinte."""
    while meta.repete_anual and meta.prazo and meta.prazo < hoje:
        meta.prazo = somar_meses_data(meta.prazo, 12)


def plano_aporte(s: Session, meta: Meta, hoje: date) -> PlanoAporte:
    """Guardar automático: o que falta até o alvo dividido pelos meses até o prazo. O aporte deste mês que já
    caiu (efetivado) ou foi cancelado não conta mais como mês para guardar."""
    guardado = _atual_da_meta(s, meta)
    if not meta.prazo:
        return PlanoAporte(meta.valor_alvo, guardado, None, 0, 0, None)
    atual = competencia_de(hoje)
    primeira = somar_meses(atual, 1) if _aporte_do_mes_caiu(s, meta.id, atual) else atual
    comp_alvo = competencia_de(meta.prazo)
    dia = min(meta.dia_pagar or 1, 28)
    ultima = comp_alvo if dia_no_mes(comp_alvo, dia) <= meta.prazo else somar_meses(comp_alvo, -1)
    meses = max(0, (int(ultima[:4]) - int(primeira[:4])) * 12 + int(ultima[5:]) - int(primeira[5:]) + 1)
    falta = max(meta.valor_alvo - guardado, 0)
    return PlanoAporte(meta.valor_alvo, guardado, meta.prazo, meses, ceil(falta / meses) if meses else 0, ultima)


def aporte_mensal(s: Session, meta: Meta, hoje: date) -> int | None:
    """Quanto a meta guarda por mês nos lançamentos (None = sem aporte mensal)."""
    if not meta.pagar_primeiro:
        return None
    return plano_aporte(s, meta, hoje).mensal if meta.aporte_automatico else meta.valor_mensal_pagar


def aplicar_pague_primeiro(s: Session, meta: Meta, hoje: date) -> Recorrencia | None:
    """Cria/atualiza o aporte mensal da meta; com `pagar_primeiro` desligado, encerra o existente.
    Valor fixo (`valor_mensal_pagar`) ou automático (o que falta ÷ meses até o prazo; nada faltando, encerra).
    Com conta da meta, vai para a caixinha dela: transferência ou, com `aporte_como_despesa`, despesa pendente que
    vira transferência ao efetivar. Sem conta, despesa na categoria Objetivos.
    Pode rolar `meta.prazo` (repete_anual). Sem nada a guardar, devolve a recorrência existente (já encerrada) ou None."""
    rec = _recorrencia_da_meta(s, meta)
    if not meta.pagar_primeiro:
        if rec is not None and rec.ativa:
            encerrar_recorrencia(s, rec, a_partir=competencia_de(hoje))
        return None
    if not (meta.conta_origem_id and meta.dia_pagar):
        raise ValueError("para pagar a meta primeiro, informe a conta de onde sai, o dia e o valor mensal")
    if meta.conta_id == meta.conta_origem_id:
        raise ValueError("a conta de origem deve ser diferente da conta da meta")
    if meta.aporte_como_despesa and not meta.conta_id:
        raise ValueError("Para contar como despesa, escolha a conta onde o dinheiro fica.")
    fim = None
    if meta.aporte_automatico:
        if not (meta.prazo and meta.valor_alvo > 0):
            raise ValueError("Para guardar automático, informe o valor e o prazo.")
        _rolar_prazo(meta, hoje)
    elif (meta.valor_mensal_pagar or 0) <= 0:
        raise ValueError("para pagar a meta primeiro, informe a conta de onde sai, o dia e o valor mensal")
    caixinha = caixinha_da_meta(s, meta, meta.conta_id, hoje) if meta.conta_id else None
    if meta.aporte_automatico:
        plano = plano_aporte(s, meta, hoje)
        if plano.mensal <= 0:
            if rec is not None and rec.ativa:
                encerrar_recorrencia(s, rec, a_partir=competencia_de(hoje))
            return rec
        valor, fim = plano.mensal, plano.ultima
    else:
        valor = meta.valor_mensal_pagar
    comum = dict(descricao=f"Meta: {meta.nome}", origem_id=meta.conta_origem_id, valor=valor,
                 dia=min(meta.dia_pagar, 28), hoje=hoje, fim=fim, origem_meta_id=meta.id)
    if caixinha is None:
        return _sincronizar_recorrencia(s, rec, tipo="despesa", destino_id=None,
                                        categoria_id=categoria_objetivos(s).id, **comum)
    if meta.aporte_como_despesa:
        return _sincronizar_recorrencia(s, rec, tipo="despesa", destino_id=meta.conta_id,
                                        categoria_id=meta.categoria_id, caixinha_id=caixinha.id, **comum)
    return _sincronizar_recorrencia(s, rec, destino_id=meta.conta_id, caixinha_id=caixinha.id, **comum)


def recalcular_metas_automaticas(s: Session, hoje: date) -> int:
    """Na abertura do app: prazo que passou vai para o ano seguinte (metas que repetem) e o aporte automático
    acompanha o guardado. Só mexe na recorrência quando o valor, o fim ou a categoria mudam. Mudança de conta ou
    de "como despesa" não é daqui: vale ao salvar a meta, que chama `aplicar_pague_primeiro`."""
    alteradas = 0
    for meta in s.scalars(select(Meta).where(Meta.aporte_automatico, Meta.pagar_primeiro, Meta.ativa,
                                             Meta.prazo.is_not(None))):
        antes = meta.prazo
        _rolar_prazo(meta, hoje)
        rec = _recorrencia_da_meta(s, meta)
        plano = plano_aporte(s, meta, hoje)
        if plano.mensal <= 0:  # nada a guardar: só falta encerrar a recorrência que ainda corre neste mês
            mudou = rec is not None and bool(rec.ativa) and (rec.fim is None or rec.fim >= competencia_de(hoje))
        else:
            mudou = (meta.prazo != antes or rec is None or rec.valor != plano.mensal or rec.fim != plano.ultima
                     or not rec.ativa or (meta.aporte_como_despesa and rec.categoria_id != meta.categoria_id))
        if mudou:
            aplicar_pague_primeiro(s, meta, hoje)
            alteradas += 1
    s.flush()
    return alteradas


def limpar_vinculos_orfaos(s: Session) -> None:
    """Zera referências a meta ou provisão que não existe mais. O id da meta não tem AUTOINCREMENT: uma meta nova
    pode ganhar o id de uma apagada e herdaria a recorrência, os lançamentos e a caixinha dela."""
    metas, provisoes = select(Meta.id), select(Provisao.id)
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id.not_in(metas))):
        rec.origem_meta_id = None
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.meta_id.not_in(metas))):
        rec.meta_id = None
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_provisao_id.not_in(provisoes))):
        rec.origem_provisao_id = None
    for lanc in s.scalars(select(Lancamento).where(Lancamento.meta_id.not_in(metas))):
        lanc.meta_id = None
    for caixinha in s.scalars(select(Caixinha).where(Caixinha.meta_id.not_in(metas))):
        caixinha.meta_id = None
    s.flush()


def migrar_provisoes_para_metas(s: Session, hoje: date) -> int:
    """Cada provisão vira uma meta anual que guarda automático e conta como despesa (roda a cada abertura; sem
    provisões, só limpa vínculos órfãos). A recorrência e a caixinha passam para a meta; os lançamentos ficam como
    estão. Sem vencimento (o caso antigo), aporte fixo de 1/12. Sem caixinha na conta de destino, ganha uma vazia."""
    limpar_vinculos_orfaos(s)
    migradas = 0
    for prov in list(s.scalars(select(Provisao).order_by(Provisao.id))):
        sem_prazo = prov.data_alvo is None
        meta = Meta(nome=prov.nome, tipo="objetivo", horizonte="ano", valor_alvo=prov.valor_anual,
                    prazo=prov.data_alvo, conta_id=prov.conta_destino_id, conta_origem_id=prov.conta_origem_id,
                    dia_pagar=prov.dia, pagar_primeiro=True, aporte_automatico=not sem_prazo,
                    valor_mensal_pagar=ceil(prov.valor_anual / 12) if sem_prazo else None,
                    aporte_como_despesa=True, categoria_id=prov.categoria_id, repete_anual=prov.repete_anual,
                    icone="calendar-clock")
        s.add(meta)
        s.flush()
        caixinha = s.get(Caixinha, prov.caixinha_id) if prov.caixinha_id else None
        if caixinha is not None and caixinha.ativa and caixinha.conta_id == prov.conta_destino_id:
            caixinha.meta_id = meta.id
            meta.cor = caixinha.cor
        else:
            # sem ela, `migrar_para_caixinhas` daria à meta todo o saldo da conta: começa vazia
            caixinha = caixinha_da_meta(s, meta, prov.conta_destino_id, hoje)
        for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_provisao_id == prov.id)):
            rec.origem_meta_id, rec.origem_provisao_id = meta.id, None
            if rec.tipo != "despesa":  # provisões antigas eram transferências: passam a despesa enquanto pendentes
                rec.tipo, rec.categoria_id = "despesa", prov.categoria_id
            if rec.conta_destino_id == caixinha.conta_id:
                rec.caixinha_id = caixinha.id
        s.delete(prov)
        migradas += 1
    s.flush()
    return migradas


CHECKLIST_SAQUE = (("inesperado", "Inesperado"), ("necessario", "Necessário"), ("urgente", "Urgente"))


def _repor_reserva(s: Session, lanc: Lancamento, valor: int, respostas: set[str], hoje: date) -> Meta:
    """Anota o checklist do saque na observação e cria a meta "Repor reserva" com o valor sacado."""
    checklist = "Checklist da reserva — " + "; ".join(
        f"{rotulo}: {'sim' if chave in respostas else 'não'}" for chave, rotulo in CHECKLIST_SAQUE)
    lanc.observacao = f"{lanc.observacao}\n{checklist}" if lanc.observacao else checklist
    repor = Meta(nome=NOME_META_REPOR, tipo="objetivo", horizonte="ano", valor_alvo=valor,
                 prazo=somar_meses_data(hoje, PRAZO_REPOR_MESES), valor_manual=0, icone="shield-check")
    s.add(repor)
    s.flush()
    return repor


def registrar_saque_reserva(s: Session, lanc: Lancamento, respostas: set[str], hoje: date) -> Meta | None:
    """Saída da conta da reserva: sai da caixinha da reserva, anota o checklist e cria "Repor reserva"."""
    if lanc.valor >= 0:
        return None
    reserva_meta = s.scalar(select(Meta).where(
        Meta.tipo == "reserva_emergencia", Meta.ativa, Meta.conta_id == lanc.conta_id).limit(1))
    if reserva_meta is None:
        return None
    if lanc.caixinha_id is None:
        caixinha = next((c for c in caixinhas_da_meta(s, reserva_meta) if c.conta_id == lanc.conta_id), None)
        lanc.caixinha_id = caixinha.id if caixinha else None
    return _repor_reserva(s, lanc, -lanc.valor, respostas, hoje)


def guardar(s: Session, meta: Meta, *, valor: int, data: date, conta: Conta, hoje: date | None = None,
            efetivado: bool = True) -> list[Lancamento]:
    """Põe dinheiro na meta. Vira lançamento: transferência para a caixinha da meta ou, sem conta,
    despesa na categoria Objetivos (o dinheiro sai do saldo e fica guardado fora do app)."""
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if conta.tipo == "cartao":
        raise ValueError("guarde a partir de uma conta, não do cartão")
    if meta.conta_id:
        if conta.id == meta.conta_id:
            raise ValueError("o dinheiro já está nesta conta: use Remanejar na tela da conta")
        caixinha = caixinha_da_meta(s, meta, meta.conta_id, hoje or data)
        pernas = list(guardar_na_caixinha(s, caixinha, origem=conta, valor=valor, data=data, efetivado=efetivado,
                                          descricao=f"Meta: {meta.nome}"))
    else:
        pernas = [criar_lancamento(s, conta=conta, data=data, descricao=f"Meta: {meta.nome}", valor_abs=valor,
                                   tipo="despesa", categoria_id=categoria_objetivos(s).id, efetivado=efetivado)]
    for lanc in pernas:
        lanc.meta_id = meta.id
    s.flush()
    return pernas


def retirar(s: Session, meta: Meta, *, valor: int, data: date, conta: Conta, hoje: date,
            efetivado: bool = True, checklist: set[str] | None = None) -> list[Lancamento]:
    """Tira dinheiro da meta para uma conta (transferência da caixinha, ou receita na categoria Objetivos).
    Da reserva de emergência, registra o checklist e cria "Repor reserva", como no saque pela conta."""
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if conta.tipo == "cartao":
        raise ValueError("retire para uma conta, não para o cartão")
    if valor > _atual_da_meta(s, meta):
        raise ValueError("o valor é maior do que o guardado na meta")
    if meta.conta_id:
        if conta.id == meta.conta_id:
            raise ValueError("o dinheiro já está nesta conta: use Remanejar na tela da conta")
        caixinha = caixinha_da_meta(s, meta, meta.conta_id, hoje)
        pernas = list(resgatar_da_caixinha(s, caixinha, destino=conta, valor=valor, data=data, efetivado=efetivado,
                                           descricao=f"Resgate: {meta.nome}"))
    else:
        pernas = [criar_lancamento(s, conta=conta, data=data, descricao=f"Resgate: {meta.nome}", valor_abs=valor,
                                   tipo="receita", categoria_id=categoria_objetivos(s, "receita").id,
                                   efetivado=efetivado)]
    for lanc in pernas:
        lanc.meta_id = meta.id
    if meta.tipo == "reserva_emergencia":
        _repor_reserva(s, pernas[0], valor, checklist or set(), hoje)
    s.flush()
    return pernas


def historico_meta(s: Session, meta: Meta, limite: int = 100) -> list[dict]:
    """Movimentos da meta vistos por ela (positivo = guardado): extrato das caixinhas dela ou,
    fora do app, os lançamentos ligados à meta."""
    caixinhas = caixinhas_da_meta(s, meta)
    if caixinhas:
        linhas = [l | {"caixinha": c.nome if len(caixinhas) > 1 else None}
                  for c in caixinhas for l in movimentos_caixinha(s, c, limite)]
    else:
        linhas = [{"data": l.data, "descricao": l.descricao, "valor": -l.valor, "pendente": l.status == "pendente",
                   "divisao": False, "caixinha": None}
                  for l in s.scalars(select(Lancamento).where(Lancamento.meta_id == meta.id,
                                                              Lancamento.status != "cancelado"))]
    return sorted(linhas, key=lambda x: x["data"], reverse=True)[:limite]


def excluir_meta(s: Session, meta: Meta, hoje: date) -> None:
    """Encerra o aporte mensal e apaga a meta. Os lançamentos já feitos continuam (são dinheiro real). As caixinhas
    dela ficam na conta, com o saldo, sem o vínculo; a que ficou vazia e sem nada agendado sai junto."""
    meta.pagar_primeiro = False
    aplicar_pague_primeiro(s, meta, hoje)
    for lanc in s.scalars(select(Lancamento).where(Lancamento.meta_id == meta.id)):
        lanc.meta_id = None
    # o id da meta pode voltar numa meta nova (sem AUTOINCREMENT): as recorrências não podem apontar para ele
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id)):
        rec.origem_meta_id = None
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.meta_id == meta.id)):
        rec.meta_id = None
    caixinhas = caixinhas_da_meta(s, meta)
    for caixinha in caixinhas:
        caixinha.meta_id = None
    s.flush()
    s.delete(meta)
    s.flush()
    for caixinha in caixinhas:
        if saldo_caixinha(s, caixinha) == 0 and agendado_caixinha(s, caixinha) == 0:
            excluir_caixinha(s, caixinha)


def vincular_aportes_recorrentes(s: Session) -> int:
    """Liga à meta os lançamentos do "pague primeiro" gerados antes de existir o vínculo."""
    n = 0
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id.is_not(None))):
        if rec.aporte_como_despesa:
            continue  # aporte "como despesa" é despesa do mês; o guardado da meta vem da caixinha
        if s.get(Meta, rec.origem_meta_id) is None:
            continue
        for lanc in s.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id,
                                                        Lancamento.meta_id.is_(None))):
            lanc.meta_id = rec.origem_meta_id
            n += 1
    s.flush()
    return n


def _marcar_pernas_da_recorrencia(s: Session, rec: Recorrencia, caixinha: Caixinha) -> None:
    rec.caixinha_id = caixinha.id
    for lanc in s.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id,
                                                    Lancamento.conta_id == caixinha.conta_id,
                                                    Lancamento.caixinha_id.is_(None))):
        lanc.caixinha_id = caixinha.id


def migrar_para_caixinhas(s: Session, hoje: date) -> int:
    """Uma vez, nos bancos de antes das caixinhas: metas com conta passam a ter caixinha.
    Meta com conta: o guardado continua o mesmo número (o "já guardado" informado ou, sem ele, o saldo da conta)."""
    criadas = 0
    for meta in s.scalars(select(Meta).where(Meta.conta_id.is_not(None), Meta.tipo != "economia_mensal")):
        if any(c.conta_id == meta.conta_id for c in caixinhas_da_meta(s, meta)):
            continue
        conta = s.get(Conta, meta.conta_id)
        caixinha = caixinha_da_meta(s, meta, conta.id, hoje)
        for lanc in s.scalars(select(Lancamento).where(Lancamento.meta_id == meta.id, Lancamento.conta_id == conta.id,
                                                        Lancamento.caixinha_id.is_(None))):
            lanc.caixinha_id = caixinha.id
        for rec in s.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id,
                                                        Recorrencia.tipo == "transferencia")):
            _marcar_pernas_da_recorrencia(s, rec, caixinha)
        s.flush()
        alvo = meta.valor_manual if meta.valor_manual else saldo_atual(s, conta)
        falta = alvo - saldo_caixinha(s, caixinha)
        if falta > 0:
            s.add(MovimentoCaixinha(caixinha_id=caixinha.id, data=hoje, valor=falta,
                                    descricao="Valor inicial (do Livre)"))
        criadas += 1
    s.flush()
    return criadas
