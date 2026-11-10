"""Serviço de projeção financeira (forecast), saldo líquido futuro e compromissos."""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import (
    competencia_de,
    nome_competencia,
    primeiro_dia,
    somar_meses,
    ultimo_dia,
)
from app.models import Categoria, Conta, Lancamento, Recorrencia
from app.services.desempenho.base import meses_media_referencia, totais_do_mes
from app.services.mes_caixa import com_status, no_mes
from app.services.resumo_mes import ItemAPagar, a_pagar
from app.services.saldos import saldo_liquido_em


@dataclass(frozen=True)
class PontoSaldoMes:
    competencia: str
    rotulo: str
    saldo_realizado: int | None
    saldo_agendado: int | None
    saldo_estimado: int | None
    receitas_agendadas: int
    despesas_agendadas: int
    variaveis_estimadas: int


@dataclass(frozen=True)
class PontoCurvaDiaria:
    data: date
    rotulo: str
    saldo: int


@dataclass(frozen=True)
class CompromissoTermino:
    competencia: str
    descricao: str
    valor_mensal: int
    tipo: str  # "parcela" | "recorrencia"


@dataclass(frozen=True)
class DadosPrevisao:
    horizonte_meses: int
    media_variaveis_avulsas: int
    base_media: str
    pontos_mensais: list[PontoSaldoMes]
    pior_mes_agendado: str | None
    pior_saldo_agendado: int | None
    pior_mes_estimado: str | None
    pior_saldo_estimado: int | None
    curva_30_dias: list[PontoCurvaDiaria]
    menor_saldo_30_dias: PontoCurvaDiaria | None
    proximos_compromissos: list[ItemAPagar]
    compromissos_terminando: list[CompromissoTermino]
    liberacao_por_mes: dict[str, int]


def _rotulo_curto(comp: str) -> str:
    return nome_competencia(comp)[:3] + "/" + comp[2:4]


def media_despesas_avulsas(s: Session, competencia: str, n: int = 3) -> tuple[int, str]:
    """Média mensal de despesas variáveis avulsas nos últimos `n` meses com dados.
    Despesas avulsas = sem recorrencia_id e sem grupo_parcela_id, grupo='variavel' ou sem categoria."""
    meses = meses_media_referencia(s, competencia, n=n)
    if not meses:
        return 0, "sem histórico anterior"
    linhas = s.execute(
        select(func.coalesce(func.sum(Lancamento.valor), 0))
        .outerjoin(Categoria, Lancamento.categoria_id == Categoria.id)
        .where(
            Lancamento.tipo == "despesa",
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
            Lancamento.recorrencia_id.is_(None),
            Lancamento.grupo_parcela_id.is_(None),
            Lancamento.meta_id.is_(None),
            (Categoria.grupo == "variavel") | (Categoria.grupo.is_(None)),
            com_status(("efetivado", "pendente")),
        )
    ).all()
    # Faz por mês para ter a média real
    totais_por_mes: dict[str, int] = {}
    for m in meses:
        soma = s.scalar(
            select(func.coalesce(func.sum(Lancamento.valor), 0))
            .outerjoin(Categoria, Lancamento.categoria_id == Categoria.id)
            .where(
                Lancamento.tipo == "despesa",
                Lancamento.ignorar.is_(False),
                Lancamento.status != "cancelado",
                Lancamento.recorrencia_id.is_(None),
                Lancamento.grupo_parcela_id.is_(None),
                Lancamento.meta_id.is_(None),
                (Categoria.grupo == "variavel") | (Categoria.grupo.is_(None)),
                com_status(("efetivado", "pendente")),
                no_mes(m),
            )
        )
        totais_por_mes[m] = -int(soma or 0)
    media = sum(totais_por_mes.values()) // len(meses)
    base = f"média de {_rotulo_curto(meses[0])} a {_rotulo_curto(meses[-1])}" if len(meses) > 1 else f"base: {_rotulo_curto(meses[0])}"
    return media, base


def despesas_avulsas_do_mes(s: Session, competencia: str) -> int:
    """Despesas avulsas já existentes/projetadas na competência."""
    soma = s.scalar(
        select(func.coalesce(func.sum(Lancamento.valor), 0))
        .outerjoin(Categoria, Lancamento.categoria_id == Categoria.id)
        .where(
            Lancamento.tipo == "despesa",
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
            Lancamento.recorrencia_id.is_(None),
            Lancamento.grupo_parcela_id.is_(None),
            Lancamento.meta_id.is_(None),
            (Categoria.grupo == "variavel") | (Categoria.grupo.is_(None)),
            com_status(("efetivado", "pendente")),
            no_mes(competencia),
        )
    )
    return -int(soma or 0)


def calcular_previsao(s: Session, hoje: date, competencia: str, horizonte_meses: int = 12) -> DadosPrevisao:
    # 1. Ponto de início do realizado: última data_saldo_inicial das contas ativas que entram no líquido
    max_inicio = s.scalar(
        select(func.max(Conta.data_saldo_inicial))
        .where(Conta.ativa, Conta.entra_saldo_liquido, Conta.tipo != "cartao")
    )
    comp_inicio_realizado = competencia_de(max_inicio) if max_inicio else competencia

    # Meses passados a exibir (até horizonte_meses para trás, mas não antes de comp_inicio_realizado)
    meses_passados: list[str] = []
    cursor = somar_meses(competencia, -(horizonte_meses - 1))
    if cursor < comp_inicio_realizado:
        cursor = comp_inicio_realizado
    while cursor < competencia:
        meses_passados.append(cursor)
        cursor = somar_meses(cursor, 1)

    # Meses futuros (de competencia até competencia + horizonte - 1)
    meses_futuros: list[str] = []
    cursor = competencia
    for _ in range(horizonte_meses):
        meses_futuros.append(cursor)
        cursor = somar_meses(cursor, 1)

    # Média de variáveis avulsas
    media_avulsas, base_media = media_despesas_avulsas(s, competencia, n=3)

    pontos_mensais: list[PontoSaldoMes] = []

    # Meses passados: apenas saldo realizado
    for m in meses_passados:
        s_fim = saldo_liquido_em(s, ultimo_dia(m))
        tot = totais_do_mes(s, m)
        pontos_mensais.append(PontoSaldoMes(
            competencia=m,
            rotulo=_rotulo_curto(m),
            saldo_realizado=s_fim,
            saldo_agendado=s_fim,
            saldo_estimado=s_fim,
            receitas_agendadas=tot.receitas_projetado,
            despesas_agendadas=tot.despesas_projetado,
            variaveis_estimadas=0,
        ))

    # Meses futuros: saldo agendado e saldo estimado
    var_estimadas_acumuladas = 0
    pior_saldo_ag = None
    pior_mes_ag = None
    pior_saldo_est = None
    pior_mes_est = None

    for m in meses_futuros:
        s_agendado = saldo_liquido_em(s, ultimo_dia(m))
        tot = totais_do_mes(s, m)
        avulsas_ja = despesas_avulsas_do_mes(s, m)
        faltam_avulsas = max(0, media_avulsas - avulsas_ja)
        var_estimadas_acumuladas += faltam_avulsas
        s_estimado = s_agendado - var_estimadas_acumuladas

        if pior_saldo_ag is None or s_agendado < pior_saldo_ag:
            pior_saldo_ag = s_agendado
            pior_mes_ag = m

        if pior_saldo_est is None or s_estimado < pior_saldo_est:
            pior_saldo_est = s_estimado
            pior_mes_est = m

        pontos_mensais.append(PontoSaldoMes(
            competencia=m,
            rotulo=_rotulo_curto(m),
            saldo_realizado=None if m > competencia else s_agendado,
            saldo_agendado=s_agendado,
            saldo_estimado=s_estimado,
            receitas_agendadas=tot.receitas_projetado,
            despesas_agendadas=tot.despesas_projetado,
            variaveis_estimadas=faltam_avulsas,
        ))

    # 2. Curva diária de 30 dias
    curva_30: list[PontoCurvaDiaria] = []
    menor_ponto: PontoCurvaDiaria | None = None
    for i in range(31):
        dia_c = hoje + timedelta(days=i)
        s_dia = saldo_liquido_em(s, dia_c)
        pt = PontoCurvaDiaria(data=dia_c, rotulo=dia_c.strftime("%d/%m"), saldo=s_dia)
        curva_30.append(pt)
        if menor_ponto is None or s_dia < menor_ponto.saldo:
            menor_ponto = pt

    # 3. Próximos compromissos (a_pagar para 30 dias)
    compromissos_30 = a_pagar(s, hoje, dias=30)[:5]

    # 4. Compromissos terminando no horizonte
    mes_fim_horizonte = meses_futuros[-1]
    terminos: list[CompromissoTermino] = []
    liberacao: dict[str, int] = {}

    # Últimas parcelas no horizonte
    ultimas_parcelas = s.execute(
        select(Lancamento)
        .where(
            Lancamento.grupo_parcela_id.is_not(None),
            Lancamento.parcela_num == Lancamento.parcela_total,
            Lancamento.status != "cancelado",
            Lancamento.ignorar.is_(False),
            no_mes(competencia) | (Lancamento.data >= primeiro_dia(competencia)),
        )
    ).scalars().all()
    for p in ultimas_parcelas:
        c_p = competencia_de(p.data)
        if competencia <= c_p <= mes_fim_horizonte:
            val = abs(p.valor)
            terminos.append(CompromissoTermino(
                competencia=c_p,
                descricao=f"{p.descricao} ({p.parcela_num}/{p.parcela_total})",
                valor_mensal=val,
                tipo="parcela",
            ))
            liberacao[c_p] = liberacao.get(c_p, 0) + val

    # Recorrências ativas que encerram no horizonte
    recorrencias_fim = s.execute(
        select(Recorrencia)
        .where(
            Recorrencia.ativa,
            Recorrencia.tipo == "despesa",
            Recorrencia.fim.is_not(None),
            Recorrencia.fim >= competencia,
            Recorrencia.fim <= mes_fim_horizonte,
        )
    ).scalars().all()
    for r in recorrencias_fim:
        if r.fim:
            val = r.valor
            terminos.append(CompromissoTermino(
                competencia=r.fim,
                descricao=f"Recorrência: {r.descricao}",
                valor_mensal=val,
                tipo="recorrencia",
            ))
            liberacao[r.fim] = liberacao.get(r.fim, 0) + val

    terminos.sort(key=lambda t: (t.competencia, -t.valor_mensal))

    return DadosPrevisao(
        horizonte_meses=horizonte_meses,
        media_variaveis_avulsas=media_avulsas,
        base_media=base_media,
        pontos_mensais=pontos_mensais,
        pior_mes_agendado=pior_mes_ag,
        pior_saldo_agendado=pior_saldo_ag,
        pior_mes_estimado=pior_mes_est,
        pior_saldo_estimado=pior_saldo_est,
        curva_30_dias=curva_30,
        menor_saldo_30_dias=menor_ponto,
        proximos_compromissos=compromissos_30,
        compromissos_terminando=terminos,
        liberacao_por_mes=liberacao,
    )
