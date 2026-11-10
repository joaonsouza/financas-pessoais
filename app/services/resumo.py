"""Boletim do mês da casa: o essencial para mandar no grupo (spec 2026-10-13-resumo-do-mes)."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.periodo import competencia_de, nome_competencia, primeiro_dia, somar_meses, ultimo_dia
from app.models import Conta, Fatura, Lancamento, Meta
from app.services.faturas import melhor_cartao_hoje, total_fatura
from app.services.indicadores_curso import Alerta, alertas
from app.services.mes_caixa import com_status, data_caixa
from app.services.metas import reserva
from app.services.orcamento import teto_mes
from app.services.parcelas import parcelas_no_gasto_do_mes
from app.services.resumo_mes import totais_mes
from app.services.saldos import saldo_liquido, saldos_contas

MAX_ITENS = 3
PROJETADO = ("efetivado", "pendente")


@dataclass(frozen=True)
class PraGastar:
    teto: int
    restante: int          # negativo = passou do teto
    pct_usado: int
    por_dia: int | None    # só no mês atual ou futuro, e com saldo
    fase: str              # atual | passado | futuro
    automatico: bool = False  # teto calculado das receitas, não salvo
    parcelas: int = 0         # quanto do gasto do mês é parcela (mesma regra do teto)

    @property
    def gasto(self) -> int:
        return self.teto - self.restante


@dataclass(frozen=True)
class ReservaResumo:
    nome: str
    atual: int
    alvo: int
    pct: int
    meses_que_aguento: float | None


@dataclass(frozen=True)
class CartaoMes:
    nome: str
    total: int
    vencimento: date


@dataclass(frozen=True)
class GastoReceita:
    gasto: int
    receita: int
    pct: int                       # gasto ÷ receita, 0 sem receita
    saldo: int                     # receita − gasto; negativo = sai da reserva
    reserva_atual: int | None      # da reserva de emergência, se houver
    pct_reserva: int | None        # quanto do saldo negativo a reserva cobre

    @property
    def frase_reserva(self) -> str | None:
        if self.saldo >= 0 or self.reserva_atual is None:
            return None
        if self.reserva_atual == 0:
            return "a reserva está zerada"
        if -self.saldo > self.reserva_atual:
            return f"mais do que a reserva tem: {formatar_reais(self.reserva_atual)}"
        return f"{self.pct_reserva}% da reserva de {formatar_reais(self.reserva_atual)}"


@dataclass(frozen=True)
class Comparacao:
    atual: int
    anterior: int
    pct: int               # variação em %, positiva = gastou mais
    ate_dia: int | None    # None = meses inteiros
    mes_anterior: str      # "setembro"


@dataclass(frozen=True)
class ResumoMes:
    competencia: str
    hoje: date
    gasto_receita: GastoReceita
    pra_gastar: PraGastar | None
    saldo_previsto: int
    melhor_cartao: dict | None
    cartoes: list[CartaoMes]
    cartoes_total: int
    reserva: ReservaResumo | None
    outras_reservas: int | None
    alertas: list[Alerta]
    comparacao: Comparacao | None


def _fase(competencia: str, hoje: date) -> str:
    atual = competencia_de(hoje)
    return "atual" if competencia == atual else ("passado" if competencia < atual else "futuro")


def _pra_gastar(s: Session, competencia: str, hoje: date, fase: str) -> PraGastar | None:
    teto = teto_mes(s, competencia)
    if teto is None:
        return None
    restante = teto.limite - teto.projetado
    dias = None
    if fase == "atual":
        dias = (ultimo_dia(competencia) - hoje).days + 1
    elif fase == "futuro":
        dias = ultimo_dia(competencia).day
    por_dia = restante // dias if dias and restante > 0 else None
    pct = teto.projetado * 100 // teto.limite if teto.limite else 0
    return PraGastar(teto.limite, restante, pct, por_dia, fase, teto.automatico,
                     parcelas_no_gasto_do_mes(s, competencia))


def _meta_reserva(s: Session) -> Meta | None:
    return s.scalar(select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa).order_by(Meta.id).limit(1))


def _reserva_resumo(s: Session, meta: Meta | None, hoje: date) -> ReservaResumo | None:
    if meta is None:
        return None
    r = reserva(s, meta, hoje)
    pct = min(r.atual * 100 // r.alvo, 100) if r.alvo else 0
    return ReservaResumo(meta.nome, r.atual, r.alvo, pct, r.meses_que_aguento)


def _gasto_receita(s: Session, competencia: str, res: ReservaResumo | None) -> GastoReceita:
    t = totais_mes(s, competencia)
    gasto, receita = t.despesas_projetado, t.receitas_projetado
    saldo = receita - gasto
    atual = res.atual if res else None
    pct_reserva = -saldo * 100 // atual if saldo < 0 and atual else None
    return GastoReceita(gasto, receita, gasto * 100 // receita if receita > 0 else 0, saldo, atual, pct_reserva)


def _cartoes_do_mes(s: Session, competencia: str) -> list[CartaoMes]:
    faturas = s.execute(select(Fatura, Conta).join(Conta, Conta.id == Fatura.cartao_id).where(
        Conta.ativa, Fatura.data_vencimento.between(primeiro_dia(competencia), ultimo_dia(competencia)))
        .order_by(Fatura.data_vencimento, Conta.nome))
    itens = [CartaoMes(c.nome, total_fatura(s, f), f.data_vencimento) for f, c in faturas]
    return [i for i in itens if i.total > 0]


def _outras_reservas(saldos, meta: Meta | None, res: ReservaResumo | None) -> int | None:
    """Contas fora do saldo líquido (investimentos, caixinhas), sem contar a reserva duas vezes."""
    fora = [x for x in saldos if not x.entra_saldo_liquido]
    if not fora:
        return None
    total = sum(x.atual for x in fora)
    if meta is not None and res is not None and meta.conta_id in {x.conta_id for x in fora}:
        total -= res.atual
    return total if total > 0 else None


def _despesas_entre(s: Session, inicio: date, fim: date) -> int:
    return -int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.tipo == "despesa", com_status(PROJETADO), Lancamento.ignorar.is_(False),
        data_caixa().between(inicio, fim))))


def _comparacao(s: Session, competencia: str, hoje: date, fase: str) -> Comparacao | None:
    if fase == "futuro":
        return None
    anterior = somar_meses(competencia, -1)
    if fase == "atual":
        dia = hoje.day
        fim_atual = hoje
        fim_anterior = primeiro_dia(anterior).replace(day=min(dia, ultimo_dia(anterior).day))
    else:
        dia, fim_atual, fim_anterior = None, ultimo_dia(competencia), ultimo_dia(anterior)
    gasto_anterior = _despesas_entre(s, primeiro_dia(anterior), fim_anterior)
    if gasto_anterior <= 0:
        return None
    gasto_atual = _despesas_entre(s, primeiro_dia(competencia), fim_atual)
    pct = round((gasto_atual - gasto_anterior) * 100 / gasto_anterior)
    return Comparacao(gasto_atual, gasto_anterior, pct, dia, nome_competencia(anterior).split()[0].lower())


def resumo_do_mes(s: Session, competencia: str, hoje: date) -> ResumoMes:
    fase = _fase(competencia, hoje)
    saldos = saldos_contas(s, hoje, competencia, tipos=("corrente", "investimento"))
    meta_reserva = _meta_reserva(s)
    res = _reserva_resumo(s, meta_reserva, hoje)
    cartoes = _cartoes_do_mes(s, competencia)
    return ResumoMes(
        competencia=competencia, hoje=hoje,
        gasto_receita=_gasto_receita(s, competencia, res),
        pra_gastar=_pra_gastar(s, competencia, hoje, fase),
        saldo_previsto=saldo_liquido(saldos, "previsto_mes"),
        melhor_cartao=melhor_cartao_hoje(s, hoje) if fase == "atual" else None,
        cartoes=cartoes, cartoes_total=sum(c.total for c in cartoes),
        reserva=res, outras_reservas=_outras_reservas(saldos, meta_reserva, res),
        alertas=[a for a in alertas(s, competencia, hoje) if a.nivel in ("perigo", "atencao")][:MAX_ITENS],
        comparacao=_comparacao(s, competencia, hoje, fase),
    )


def _linhas_teto(p: PraGastar, rs) -> list[str]:
    """Para o grupo da família: o limite do mês, quanto já gastamos, quanto disso é parcela e a situação."""
    linhas = [f"📏 Limite de gastos do mês: {rs(p.teto)} (gastamos {p.pct_usado}%)",
              f"🧾 Só de parcelas: {rs(p.parcelas)}"]
    if p.fase == "passado":
        linhas.append(f"🚨 Fechamos o mês {rs(-p.restante)} acima do limite" if p.restante < 0
                      else f"✅ Fechamos o mês {rs(p.restante)} abaixo do limite")
    elif p.restante < 0:
        linhas.append(f"🚨 Passamos do limite em {rs(-p.restante)}")
    elif p.restante > 0:
        dia = f" ({rs(p.por_dia)} por dia)" if p.por_dia else ""
        linhas.append(f"✅ Ainda dá para gastar {rs(p.restante)}{dia}")
    else:
        linhas.append("⚠️ Chegamos exatamente no limite")
    return linhas


def texto_whatsapp(r: ResumoMes) -> str:
    """O boletim em texto, com a formatação do WhatsApp (*negrito*); os blocos vazios somem."""
    rs = formatar_reais
    linhas = [f"🏠 *Casa · {nome_competencia(r.competencia)}*"]
    g, p = r.gasto_receita, r.pra_gastar
    if g.receita > 0:
        linhas.append(f"💰 Gastos: *{rs(g.gasto)}* de {rs(g.receita)} de receita ({g.pct}%)")
    else:
        linhas.append(f"💰 Gastos: *{rs(g.gasto)}* (sem receita lançada no mês)")
    if g.saldo >= 0:
        if g.receita > 0 or g.gasto > 0:
            linhas.append(f"✅ Sobram {rs(g.saldo)}")
    else:
        frase = f" ({g.frase_reserva})" if g.frase_reserva else ""
        linhas.append(f"⚠️ Faltam {rs(-g.saldo)}" + (f" → sai da reserva{frase}" if g.reserva_atual is not None else ""))
    if p:
        linhas += _linhas_teto(p, rs)
    linhas.append(f"🏦 Nas contas no fim do mês: {rs(r.saldo_previsto)}")
    if r.cartoes_total > 0 or r.melhor_cartao:
        hoje_use = ""
        if r.melhor_cartao:
            hoje_use = f" · hoje use o {r.melhor_cartao['cartao'].nome} (vence {r.melhor_cartao['vencimento']:%d/%m})"
        linhas.append(f"💳 Cartões no mês: {rs(r.cartoes_total)}{hoje_use}")
    if r.reserva:
        res = r.reserva
        meses = f" · cobre {str(res.meses_que_aguento).replace('.', ',')} meses" if res.meses_que_aguento is not None else ""
        linhas.append(f"🛟 Reserva: {rs(res.atual)} de {rs(res.alvo)} ({res.pct}%){meses}")
    if r.outras_reservas:
        linhas.append(f"📈 Investimentos e outras caixinhas: {rs(r.outras_reservas)}")
    if r.alertas:
        linhas += [f"⚠️ {a.titulo}" for a in r.alertas]
    elif (p is None or p.restante >= 0) and g.saldo >= 0:
        linhas.append("✅ Tudo em dia")
    c = r.comparacao
    if c:
        seta = "↑" if c.pct > 0 else ("↓" if c.pct < 0 else "=")
        periodo = f"Gasto até dia {c.ate_dia}" if c.ate_dia else "Gasto no mês"
        linhas.append(f"📊 {periodo}: {rs(c.atual)} ({seta} {abs(c.pct)}% vs {c.mes_anterior})")
    return "\n".join(linhas)
