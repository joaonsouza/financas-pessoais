"""Geração de insights determinísticos com base no histórico e tendências."""
from dataclasses import dataclass
from datetime import date
from typing import Any

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
from app.importers.normalizacao import normalizar_descricao
from app.models import Categoria, Lancamento
from app.services.desempenho.base import (
    carregar_totais_periodo,
    meses_media_referencia,
    totais_do_mes,
)
from app.services.desempenho.previsao import calcular_previsao
from app.services.mes_caixa import com_status, no_mes
from app.services.assinaturas import total_assinaturas


@dataclass(frozen=True)
class Insight:
    chave: str
    tipo: str        # "positivo" | "negativo" | "neutro"
    titulo: str
    texto: str
    valor: int | None
    acao_url: str
    peso: int        # para ordenação (maior peso aparece primeiro)


def _reais(centavos: int) -> str:
    return formatar_reais(centavos)


def gerar_insights(s: Session, competencia: str, hoje: date, previsao=None) -> list[Insight]:
    insights: list[Insight] = []
    totais_mes = totais_do_mes(s, competencia)
    meses_ref = meses_media_referencia(s, competencia, n=3)
    tem_media = len(meses_ref) > 0
    n_media = len(meses_ref) or 1

    # Carrega categorias
    cats = {c.id: c for c in s.scalars(select(Categoria)).all()}

    # 1 e 2. Categorias acima / abaixo da média (raiz)
    # Gasto por categoria raiz no mês atual
    linhas_mes = s.execute(
        select(Lancamento.categoria_id, func.sum(Lancamento.valor))
        .where(
            Lancamento.tipo == "despesa",
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
            Lancamento.meta_id.is_(None),
            com_status(("efetivado", "pendente")),
            no_mes(competencia),
        )
        .group_by(Lancamento.categoria_id)
    ).all()
    gastos_raiz_mes: dict[int | None, int] = {}
    for cid, val in linhas_mes:
        cat = cats.get(cid)
        raiz_id = cat.pai_id if cat and cat.pai_id else cid
        gastos_raiz_mes[raiz_id] = gastos_raiz_mes.get(raiz_id, 0) - int(val)

    if tem_media:
        # Gastos por categoria raiz nos meses de referência
        gastos_ref: dict[int | None, list[int]] = {}
        for m in meses_ref:
            linhas_m = s.execute(
                select(Lancamento.categoria_id, func.sum(Lancamento.valor))
                .where(
                    Lancamento.tipo == "despesa",
                    Lancamento.ignorar.is_(False),
                    Lancamento.status != "cancelado",
                    Lancamento.meta_id.is_(None),
                    com_status(("efetivado", "pendente")),
                    no_mes(m),
                )
                .group_by(Lancamento.categoria_id)
            ).all()
            por_raiz_m: dict[int | None, int] = {}
            for cid, val in linhas_m:
                cat = cats.get(cid)
                raiz_id = cat.pai_id if cat and cat.pai_id else cid
                por_raiz_m[raiz_id] = por_raiz_m.get(raiz_id, 0) - int(val)
            for r_id in set(list(gastos_raiz_mes.keys()) + list(por_raiz_m.keys())):
                gastos_ref.setdefault(r_id, []).append(por_raiz_m.get(r_id, 0))

        for raiz_id, val_atual in gastos_raiz_mes.items():
            if raiz_id is None:
                continue
            hist = gastos_ref.get(raiz_id, [0])
            media_cat = sum(hist) // len(hist) if hist else 0
            if media_cat > 0:
                diff = val_atual - media_cat
                pct = (diff * 100) / media_cat
                nome_cat = cats[raiz_id].nome if raiz_id in cats else "Outros"
                # Acima da média: >= 25% E >= R$ 50
                if pct >= 25.0 and diff >= 5000:
                    insights.append(Insight(
                        chave=f"cat_acima:{raiz_id}",
                        tipo="negativo",
                        titulo=f"{nome_cat} acima da média",
                        texto=f"Gasto de {_reais(val_atual)} está {round(pct)}% acima da sua média ({_reais(media_cat)}).",
                        valor=diff,
                        acao_url=f"/lancamentos?mes={competencia}&categoria={raiz_id}",
                        peso=diff,
                    ))
                # Abaixo da média: <= -25% E <= -R$ 50
                elif pct <= -25.0 and diff <= -5000:
                    queda = -diff
                    insights.append(Insight(
                        chave=f"cat_abaixo:{raiz_id}",
                        tipo="positivo",
                        titulo=f"{nome_cat} em queda",
                        texto=f"Gasto de {_reais(val_atual)} caiu {abs(round(pct))}% em relação à média ({_reais(media_cat)}).",
                        valor=queda,
                        acao_url=f"/lancamentos?mes={competencia}&categoria={raiz_id}",
                        peso=queda,
                    ))

    # 3. Gasto novo (descrição normalizada no mês atual >= R$ 100 ausente nos 3 meses anteriores)
    if tem_media:
        inicio_ref = min(meses_ref)
        fim_ref = max(meses_ref)
        descricoes_anteriores = set()
        for d in s.scalars(
            select(Lancamento.descricao)
            .where(
                Lancamento.tipo == "despesa",
                Lancamento.ignorar.is_(False),
                Lancamento.status != "cancelado",
                com_status(("efetivado", "pendente")),
                Lancamento.data >= primeiro_dia(inicio_ref),
                Lancamento.data <= ultimo_dia(fim_ref),
            )
        ).all():
            norm = normalizar_descricao(d)
            if norm:
                descricoes_anteriores.add(norm)

        # Gastos no mês agrupados por descrição normalizada
        gastos_atuais = s.scalars(
            select(Lancamento)
            .where(
                Lancamento.tipo == "despesa",
                Lancamento.ignorar.is_(False),
                Lancamento.status != "cancelado",
                no_mes(competencia),
            )
        ).all()
        por_desc: dict[str, tuple[str, int]] = {}
        for l in gastos_atuais:
            norm = normalizar_descricao(l.descricao)
            if norm and norm not in descricoes_anteriores:
                val = abs(l.valor)
                exemplo, tot = por_desc.get(norm, (l.descricao, 0))
                por_desc[norm] = (exemplo, tot + val)

        for norm, (exemplo, tot) in por_desc.items():
            if tot >= 10000:  # >= R$ 100
                insights.append(Insight(
                    chave=f"novo_gasto:{norm[:20]}",
                    tipo="neutro",
                    titulo="Novo gasto no mês",
                    texto=f"{exemplo}: {_reais(tot)} (não aparecia nos meses anteriores).",
                    valor=tot,
                    acao_url=f"/lancamentos?mes={competencia}&q={exemplo}",
                    peso=tot // 2,
                ))

    # 4. Maior compra do mês (>= R$ 100)
    maior_compra = s.scalar(
        select(Lancamento)
        .where(
            Lancamento.tipo == "despesa",
            Lancamento.ignorar.is_(False),
            Lancamento.status != "cancelado",
            Lancamento.meta_id.is_(None),
            no_mes(competencia),
        )
        .order_by(Lancamento.valor.asc())  # mais negativo
        .limit(1)
    )
    if maior_compra is not None and abs(maior_compra.valor) >= 10000:
        val_compra = abs(maior_compra.valor)
        desc = maior_compra.descricao
        if maior_compra.parcela_num and maior_compra.parcela_total:
            desc += f" ({maior_compra.parcela_num}/{maior_compra.parcela_total})"
        insights.append(Insight(
            chave=f"maior_compra:{maior_compra.id}",
            tipo="neutro",
            titulo="Maior compra do mês",
            texto=f"{desc} somou {_reais(val_compra)}.",
            valor=val_compra,
            acao_url=f"/lancamentos?mes={competencia}&q={maior_compra.descricao}",
            peso=val_compra // 2,
        ))

    # 5. Assinaturas
    mensal_ass, anual_ass = total_assinaturas(s)
    if mensal_ass > 0:
        pct_rec = round(mensal_ass * 100 / totais_mes.receitas_projetado, 1) if totais_mes.receitas_projetado > 0 else None
        texto_ass = f"Assinaturas ativas somam {_reais(mensal_ass)}/mês ({_reais(anual_ass)}/ano)"
        if pct_rec:
            texto_ass += f", representando {pct_rec}% da receita do mês"
        insights.append(Insight(
            chave="total_assinaturas",
            tipo="neutro",
            titulo="Comprometimento com assinaturas",
            texto=texto_ass + ".",
            valor=mensal_ass,
            acao_url="/lancamentos/assinaturas",
            peso=mensal_ass,
        ))

    # 6. Parcelas terminando no horizonte (usa cálculo da previsão)
    prev = previsao or calcular_previsao(s, hoje, competencia, horizonte_meses=12)
    if prev.liberacao_por_mes:
        primeiro_mes_liberacao = sorted(prev.liberacao_por_mes.keys())[0]
        val_liberado = prev.liberacao_por_mes[primeiro_mes_liberacao]
        rotulo_lib = nome_competencia(primeiro_mes_liberacao)
        insights.append(Insight(
            chave=f"liberacao_parcelas:{primeiro_mes_liberacao}",
            tipo="positivo",
            titulo="Alívio financeiro à frente",
            texto=f"Em {rotulo_lib} terminam parcelamentos que liberam {_reais(val_liberado)}/mês no seu orçamento.",
            valor=val_liberado,
            acao_url=f"/desempenho?mes={competencia}#previsao",
            peso=val_liberado,
        ))

    # 7. Tendência da taxa de poupança (3 meses seguidos subindo ou caindo)
    if len(meses_ref) >= 2:
        sequencia_meses = meses_ref + [competencia]
        totais_seq = carregar_totais_periodo(s, sequencia_meses[0], sequencia_meses[-1])
        taxas = [totais_seq[m].taxa_poupanca_projetado for m in sequencia_meses]
        if all(t is not None for t in taxas) and len(taxas) >= 3:
            ultimas_3 = [t for t in taxas[-3:] if t is not None]
            if len(ultimas_3) == 3:
                if ultimas_3[0] < ultimas_3[1] < ultimas_3[2]:
                    insights.append(Insight(
                        chave="taxa_poupanca_alta",
                        tipo="positivo",
                        titulo="Taxa de poupança em alta",
                        texto=f"Sua taxa de poupança sobe há 3 meses seguidos: {ultimas_3[0]}% → {ultimas_3[1]}% → {ultimas_3[2]}%.",
                        valor=None,
                        acao_url=f"/planejamento?mes={competencia}",
                        peso=80000,
                    ))
                elif ultimas_3[0] > ultimas_3[1] > ultimas_3[2]:
                    insights.append(Insight(
                        chave="taxa_poupanca_queda",
                        tipo="negativo",
                        titulo="Taxa de poupança em queda",
                        texto=f"Sua taxa de poupança recua há 3 meses seguidos: {ultimas_3[0]}% → {ultimas_3[1]}% → {ultimas_3[2]}%.",
                        valor=None,
                        acao_url=f"/planejamento?mes={competencia}",
                        peso=80000,
                    ))

    # 8. Sem categoria (>= 5% das despesas totais)
    sem_cat_total = gastos_raiz_mes.get(None, 0)
    if sem_cat_total > 0 and totais_mes.despesas_projetado > 0:
        pct_sem_cat = (sem_cat_total * 100) / totais_mes.despesas_projetado
        if pct_sem_cat >= 5.0 and sem_cat_total >= 5000:
            insights.append(Insight(
                chave="gastos_sem_categoria",
                tipo="negativo",
                titulo="Gastos sem categoria",
                texto=f"{_reais(sem_cat_total)} ({round(pct_sem_cat)}% das despesas) ainda não foram categorizados.",
                valor=sem_cat_total,
                acao_url=f"/lancamentos?mes={competencia}&categoria=sem",
                peso=sem_cat_total,
            ))

    # Ordena pelo peso decrescente
    insights.sort(key=lambda i: (-i.peso, i.titulo))
    return insights
