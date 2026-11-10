"""Placar do mês com base nos 6 métodos fundamentais do curso."""
import re
from dataclasses import dataclass
from datetime import date
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.periodo import competencia_de, primeiro_dia, ultimo_dia
from app.models import Conta, Fatura, Lancamento, Meta
from app.services.desempenho.base import totais_do_mes
from app.services.indicadores_curso import painel_503020
from app.services.metas import economia_do_mes
from app.services.orcamento import teto_mes

_RE_ENCARGO_LIMITE = re.compile(r"JUROS\s+LIMITE|\bIOF\b")


@dataclass(frozen=True)
class ItemPlacar:
    id: str
    titulo: str
    status: str       # "atingido" | "nao_atingido" | "indisponivel"
    parcial: bool     # True se o mês ainda está em andamento
    detalhe: str
    acao_url: str


@dataclass(frozen=True)
class PlacarMes:
    competencia: str
    atingidos: int
    total_avaliados: int
    itens: list[ItemPlacar]


def calcular_placar(s: Session, competencia: str, hoje: date) -> PlacarMes:
    mes_atual = competencia_de(hoje)
    eh_mes_atual_ou_futuro = competencia >= mes_atual
    totais = totais_do_mes(s, competencia)

    itens: list[ItemPlacar] = []

    # 1. Gastei menos do que ganhei (sobra >= 0)
    sobra = totais.sobra_projetado if eh_mes_atual_ou_futuro else totais.sobra_efetivado
    rec = totais.receitas_projetado if eh_mes_atual_ou_futuro else totais.receitas_efetivado
    if rec <= 0 and (totais.despesas_projetado == 0 and totais.despesas_efetivado == 0):
        itens.append(ItemPlacar(
            id="sobra",
            titulo="Gastei menos do que ganhei",
            status="indisponivel",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Sem movimentações no mês",
            acao_url=f"/lancamentos?mes={competencia}",
        ))
    else:
        status_sobra = "atingido" if sobra >= 0 else "nao_atingido"
        detalhe_sobra = f"Sobra de {formatar_reais(sobra)}" if sobra >= 0 else f"Déficit de {formatar_reais(-sobra)}"
        itens.append(ItemPlacar(
            id="sobra",
            titulo="Gastei menos do que ganhei",
            status=status_sobra,
            parcial=eh_mes_atual_ou_futuro,
            detalhe=detalhe_sobra,
            acao_url=f"/lancamentos?mes={competencia}",
        ))

    # 2. Fiquei dentro do teto
    teto = teto_mes(s, competencia)
    if teto is None or teto.limite <= 0:
        itens.append(ItemPlacar(
            id="teto",
            titulo="Fiquei dentro do teto de gastos",
            status="indisponivel",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Sem teto definido para o mês",
            acao_url=f"/planejamento?mes={competencia}",
        ))
    else:
        gasto_teto = teto.projetado if eh_mes_atual_ou_futuro else teto.efetivado
        atingido_teto = gasto_teto <= teto.limite
        itens.append(ItemPlacar(
            id="teto",
            titulo="Fiquei dentro do teto de gastos",
            status="atingido" if atingido_teto else "nao_atingido",
            parcial=eh_mes_atual_ou_futuro,
            detalhe=f"{formatar_reais(gasto_teto)} de {formatar_reais(teto.limite)} ({round(gasto_teto * 100 / teto.limite)}%)",
            acao_url=f"/planejamento?mes={competencia}",
        ))

    # 3. Bati a meta de economia
    econ = economia_do_mes(s, competencia)
    if econ is None or econ.meta <= 0:
        itens.append(ItemPlacar(
            id="economia",
            titulo="Bati a meta de economia mensal",
            status="indisponivel",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Sem meta de economia cadastrada",
            acao_url=f"/planejamento?mes={competencia}#objetivos",
        ))
    else:
        atingido_econ = econ.atingida_projetado if eh_mes_atual_ou_futuro else econ.atingida_realizado
        val_econ = econ.projetado if eh_mes_atual_ou_futuro else econ.realizado
        itens.append(ItemPlacar(
            id="economia",
            titulo="Bati a meta de economia mensal",
            status="atingido" if atingido_econ else "nao_atingido",
            parcial=eh_mes_atual_ou_futuro,
            detalhe=f"{formatar_reais(val_econ)} de {formatar_reais(econ.meta)}",
            acao_url=f"/planejamento?mes={competencia}#objetivos",
        ))

    # 4. 50/30/20 dentro da meta (desvio máximo de 5 p.p. por fatia)
    painel = painel_503020(s, competencia)
    if painel.receitas <= 0:
        itens.append(ItemPlacar(
            id="503020",
            titulo="Método 50/30/20 equilibrado",
            status="indisponivel",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Sem receitas cadastradas para compor o método",
            acao_url=f"/planejamento?mes={competencia}#painel-503020",
        ))
    else:
        # Tolerância de 5 pontos percentuais acima da meta para Necessidades e Desejos
        necessidades = painel.fatias[0]
        desejos = painel.fatias[1]
        desvio_nec = necessidades.pct_real - necessidades.pct_meta
        desvio_des = desejos.pct_real - desejos.pct_meta
        equilibrado = (desvio_nec <= 5.0) and (desvio_des <= 5.0)
        itens.append(ItemPlacar(
            id="503020",
            titulo="Método 50/30/20 equilibrado",
            status="atingido" if equilibrado else "nao_atingido",
            parcial=eh_mes_atual_ou_futuro,
            detalhe=f"Nec: {necessidades.pct_real}% (meta {necessidades.pct_meta}%) · Des: {desejos.pct_real}% (meta {desejos.pct_meta}%)",
            acao_url=f"/planejamento?mes={competencia}#painel-503020",
        ))

    # 5. Sem juros/IOF de cheque especial e sem fatura parcial
    faturas_parciais = s.scalar(
        select(func.count(Fatura.id))
        .where(Fatura.status == "paga_parcial", Fatura.competencia <= competencia)
    ) or 0
    encargos = s.scalars(
        select(Lancamento).join(Conta, Lancamento.conta_id == Conta.id).where(
            Conta.tipo != "cartao",
            Lancamento.valor < 0,
            Lancamento.status != "cancelado",
            Lancamento.ignorar.is_(False),
            Lancamento.data.between(primeiro_dia(competencia), ultimo_dia(competencia)),
        )
    ).all()
    encargos_total = -sum(l.valor for l in encargos if _RE_ENCARGO_LIMITE.search((l.descricao or "").upper()))

    if faturas_parciais == 0 and encargos_total == 0:
        itens.append(ItemPlacar(
            id="sem_encargos",
            titulo="Sem juros de cartão ou cheque especial",
            status="atingido",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Nenhum encargo ou rotativo registrado ✓",
            acao_url=f"/cartoes",
        ))
    else:
        problemas = []
        if faturas_parciais > 0:
            problemas.append(f"{faturas_parciais} fatura(s) parcial(is)")
        if encargos_total > 0:
            problemas.append(f"{formatar_reais(encargos_total)} em juros/IOF")
        itens.append(ItemPlacar(
            id="sem_encargos",
            titulo="Sem juros de cartão ou cheque especial",
            status="nao_atingido",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="; ".join(problemas),
            acao_url=f"/cartoes",
        ))

    # 6. Reserva não diminuiu no mês
    meta_reserva = s.scalar(
        select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa).order_by(Meta.id).limit(1)
    )
    if meta_reserva is None:
        itens.append(ItemPlacar(
            id="reserva",
            titulo="Reserva de emergência preservada",
            status="indisponivel",
            parcial=eh_mes_atual_ou_futuro,
            detalhe="Sem meta de reserva de emergência cadastrada",
            acao_url=f"/planejamento?mes={competencia}#reserva",
        ))
    else:
        # Verifica resgates da reserva no mês (despesa é aporte; receita é saque/retirada)
        resgates = s.scalar(
            select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
                Lancamento.meta_id == meta_reserva.id,
                Lancamento.tipo == "receita",
                Lancamento.status != "cancelado",
                Lancamento.ignorar.is_(False),
                Lancamento.data.between(primeiro_dia(competencia), ultimo_dia(competencia)),
            )
        ) or 0
        if resgates > 0:
            itens.append(ItemPlacar(
                id="reserva",
                titulo="Reserva de emergência preservada",
                status="nao_atingido",
                parcial=eh_mes_atual_ou_futuro,
                detalhe=f"Houve resgate de {formatar_reais(resgates)} da reserva",
                acao_url=f"/planejamento?mes={competencia}#reserva",
            ))
        else:
            itens.append(ItemPlacar(
                id="reserva",
                titulo="Reserva de emergência preservada",
                status="atingido",
                parcial=eh_mes_atual_ou_futuro,
                detalhe="Nenhum resgate no mês",
                acao_url=f"/planejamento?mes={competencia}#reserva",
            ))

    avaliados = [i for i in itens if i.status in ("atingido", "nao_atingido")]
    atingidos = [i for i in avaliados if i.status == "atingido"]

    return PlacarMes(
        competencia=competencia,
        atingidos=len(atingidos),
        total_avaliados=len(avaliados),
        itens=itens,
    )
