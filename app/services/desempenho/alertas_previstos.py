"""Geração de alertas preditivos e forecast de riscos financeiros."""
from datetime import date, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.periodo import (
    competencia_de,
    nome_competencia,
    primeiro_dia,
    somar_meses,
    ultimo_dia,
)
from app.models import Conta, Fatura, Lancamento
from app.services.desempenho.base import carregar_totais_periodo
from app.services.desempenho.previsao import calcular_previsao
from app.services.faturas import total_fatura
from app.services.indicadores_curso import Alerta
from app.services.orcamento import teto_mes
from app.services.saldos import saldo_liquido_em


def _reais(centavos: int) -> str:
    return formatar_reais(centavos)


def alertas_previstos(
    s: Session,
    competencia: str,
    hoje: date,
    horizonte_meses: int = 12,
    previsao: Any | None = None,
) -> list[Alerta]:
    """Gera alertas prospectivos com base no forecast de saldo e faturas."""
    alertas: list[Alerta] = []
    prev = previsao or calcular_previsao(s, hoje, competencia, horizonte_meses=horizonte_meses)

    # 1. saldo_negativo_agendado: nos próximos 60 dias
    dia_negativo: date | None = None
    saldo_negativo: int = 0
    for pt in prev.curva_30_dias:
        if pt.data > hoje and pt.saldo < 0:
            dia_negativo = pt.data
            saldo_negativo = pt.saldo
            break
    if dia_negativo is None:
        for i in range(31, 61):
            d = hoje + timedelta(days=i)
            saldo_d = saldo_liquido_em(s, d)
            if saldo_d < 0:
                dia_negativo = d
                saldo_negativo = saldo_d
                break
    if dia_negativo is not None:
        comp_neg = competencia_de(dia_negativo)
        alertas.append(Alerta(
            chave=f"saldo_negativo_agendado:{comp_neg}",
            nivel="perigo",
            titulo="Saldo previsto negativo em breve",
            texto=f"Em {dia_negativo.strftime('%d/%m')} o saldo líquido agendado cai para {_reais(saldo_negativo)}.",
            acao_url=f"/desempenho?mes={competencia}#previsao",
        ))

    # 2. saldo_negativo_estimado: fim de mês < 0 no estimado (quando o agendado for >= 0)
    for p in prev.pontos_mensais:
        if p.competencia >= competencia:
            if p.saldo_agendado is not None and p.saldo_agendado >= 0:
                if p.saldo_estimado is not None and p.saldo_estimado < 0:
                    alertas.append(Alerta(
                        chave=f"saldo_negativo_estimado:{p.competencia}",
                        nivel="atencao",
                        titulo=f"Risco de déficit em {p.rotulo}",
                        texto=f"Com os gastos variáveis estimados, o mês fecha em {_reais(p.saldo_estimado)}.",
                        acao_url=f"/desempenho?mes={competencia}#previsao",
                    ))
                    break  # Alerta do primeiro mês com risco

    # 3. ritmo_teto: no mês atual a partir do dia 5
    if competencia == competencia_de(hoje) and hoje.day >= 5:
        teto = teto_mes(s, competencia)
        if teto is not None and teto.limite > 0:
            tot = prev.pontos_mensais[-1]  # ou pegar o tot do mês
            dias_totais = ultimo_dia(competencia).day
            # Variáveis avulsas já realizadas até hoje
            gastos_ate_hoje = s.scalar(
                select(func.coalesce(func.sum(Lancamento.valor), 0))
                .where(
                    Lancamento.tipo == "despesa",
                    Lancamento.ignorar.is_(False),
                    Lancamento.status != "cancelado",
                    Lancamento.meta_id.is_(None),
                    Lancamento.data.between(primeiro_dia(competencia), hoje),
                )
            ) or 0
            gastos_ate_hoje = -gastos_ate_hoje
            ritmo_linear = int((gastos_ate_hoje / hoje.day) * dias_totais)
            if ritmo_linear > teto.limite and teto.projetado > 0:
                acima = ritmo_linear - teto.limite
                alertas.append(Alerta(
                    chave="ritmo_teto",
                    nivel="atencao",
                    titulo="Ritmo de gastos acima do teto",
                    texto=f"No ritmo atual, o mês fecha em {_reais(ritmo_linear)}, {_reais(acima)} acima do teto de {_reais(teto.limite)}.",
                    acao_url=f"/planejamento?mes={competencia}",
                ))

    # 4. fatura_alta: próxima fatura >= 20% e >= R$ 100 acima da média das 3 anteriores
    cartoes = s.scalars(select(Conta).where(Conta.tipo == "cartao", Conta.ativa)).all()
    for cartao in cartoes:
        proxima = s.scalar(
            select(Fatura).where(
                Fatura.cartao_id == cartao.id,
                Fatura.competencia >= competencia,
            ).order_by(Fatura.competencia).limit(1)
        )
        if proxima is not None:
            # 3 faturas anteriores fechadas/pagas
            anteriores = s.scalars(
                select(Fatura).where(
                    Fatura.cartao_id == cartao.id,
                    Fatura.competencia < proxima.competencia,
                ).order_by(Fatura.competencia.desc()).limit(3)
            ).all()
            if len(anteriores) >= 1:
                media_fat = sum(total_fatura(s, f) for f in anteriores) // len(anteriores)
                tot_prox = total_fatura(s, proxima)
                if media_fat > 0:
                    diff = tot_prox - media_fat
                    pct = (diff * 100) / media_fat
                    if pct >= 20.0 and diff >= 10000:
                        alertas.append(Alerta(
                            chave=f"fatura_alta:{cartao.id}:{proxima.competencia}",
                            nivel="atencao",
                            titulo=f"Fatura alta: {cartao.nome}",
                            texto=f"A fatura de {proxima.competencia} ({_reais(tot_prox)}) está {round(pct)}% acima da média anterior ({_reais(media_fat)}).",
                            acao_url=f"/cartoes/{cartao.id}",
                        ))

    # 5. mes_pesado: mês futuro com despesas agendadas > receitas agendadas
    for p in prev.pontos_mensais:
        if p.competencia > competencia:
            if p.despesas_agendadas > p.receitas_agendadas and p.despesas_agendadas > 0:
                diff = p.despesas_agendadas - p.receitas_agendadas
                alertas.append(Alerta(
                    chave=f"mes_pesado:{p.competencia}",
                    nivel="info",
                    titulo=f"Mês pesado à frente: {p.rotulo}",
                    texto=f"Compromissos agendados superam as receitas em {_reais(diff)}.",
                    acao_url=f"/desempenho?mes={p.competencia}#previsao",
                ))
                break  # mostra só o primeiro

    return alertas


def central_alertas(s: Session, competencia: str, hoje: date, previsao: Any | None = None) -> list[Alerta]:
    """Retorna todos os alertas (do curso + preditivos), filtrando os dispensados e ordenando por gravidade.

    `previsao` (12 meses) já calculada evita refazer a previsão, que é a parte cara."""
    from app.models import alertas_dispensados
    from app.services.indicadores_curso import alertas as alertas_curso

    atuais = alertas_curso(s, competencia, hoje)
    preditivos = alertas_previstos(s, competencia, hoje, previsao=previsao)

    dispensados = set(alertas_dispensados(s, competencia))
    todos = [a for a in atuais + preditivos if a.chave not in dispensados]

    # Elimina possíveis duplicidades de chave
    vistos = set()
    unicos = []
    for a in todos:
        if a.chave not in vistos:
            vistos.add(a.chave)
            unicos.append(a)

    ordem = {"perigo": 0, "atencao": 1, "info": 2}
    return sorted(unicos, key=lambda a: ordem.get(a.nivel, 99))
