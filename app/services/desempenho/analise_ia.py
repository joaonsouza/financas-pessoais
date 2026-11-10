"""Análise estratégica sob demanda com IA para a central de Desempenho."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.periodo import nome_competencia
from app.core.relogio import agora
from app.importers.base import LeitorErro
from app.models import get_config, set_config
from app.services.desempenho.alertas_previstos import central_alertas
from app.services.desempenho.insights import gerar_insights
from app.services.desempenho.kpis import calcular_kpis
from app.services.desempenho.placar import calcular_placar
from app.services.desempenho.previsao import calcular_previsao
from app.services.ia import chamar_parse, cliente_e_modelo_de_texto


class ItemSugestaoIA(BaseModel):
    acao: str = Field(description="Ação prática recomendada para as finanças")
    impacto_mensal_estimado: int = Field(description="Impacto mensal estimado em centavos (ex: 15000 para R$ 150,00)")


class SaidaAnaliseIA(BaseModel):
    resumo: str = Field(description="Resumo geral da saúde financeira do mês em até 3 frases")
    pontos_fortes: list[str] = Field(description="Até 3 principais pontos positivos do período", max_length=3)
    pontos_de_atencao: list[str] = Field(description="Até 3 pontos críticos que exigem cuidado", max_length=3)
    sugestoes: list[ItemSugestaoIA] = Field(description="Até 3 ações práticas de otimização financeira", max_length=3)


@dataclass(frozen=True)
class AnaliseResultado:
    resumo: str
    pontos_fortes: list[str]
    pontos_de_atencao: list[str]
    sugestoes: list[dict]
    gerado_em: str
    desatualizado: bool = False


def _preparar_dados_agregados(s: Session, competencia: str, hoje: date) -> dict[str, Any]:
    """Coleta apenas números agregados para análise, sem nomes de contas ou detalhes pessoais."""
    kpis = calcular_kpis(s, competencia)
    placar = calcular_placar(s, competencia, hoje)
    alertas = central_alertas(s, competencia, hoje)
    insights = gerar_insights(s, competencia, hoje)
    prev = calcular_previsao(s, hoje, competencia, horizonte_meses=6)

    return {
        "competencia": competencia,
        "kpis": {
            "receitas_realizado": kpis.receitas.valor_realizado,
            "receitas_projetado": kpis.receitas.valor_projetado,
            "despesas_realizado": kpis.despesas.valor_realizado,
            "despesas_projetado": kpis.despesas.valor_projetado,
            "sobra_projetado": kpis.sobra.valor_projetado,
            "taxa_poupanca": kpis.taxa_poupanca.valor_projetado,
            "guardado": kpis.guardado.valor_projetado,
        },
        "placar": {
            "atingidos": placar.atingidos,
            "total": placar.total_avaliados,
            "itens": [{"titulo": i.titulo, "status": i.status, "detalhe": i.detalhe} for i in placar.itens],
        },
        "alertas": [{"nivel": a.nivel, "titulo": a.titulo, "texto": a.texto} for a in alertas[:5]],
        "insights": [{"titulo": i.titulo, "texto": i.texto} for i in insights[:6]],
        "forecast": {
            "media_variaveis": prev.media_variaveis_avulsas,
            "pior_saldo_agendado": prev.pior_saldo_agendado,
            "pior_mes_agendado": prev.pior_mes_agendado,
            "pior_saldo_estimado": prev.pior_saldo_estimado,
            "pior_mes_estimado": prev.pior_mes_estimado,
        },
    }


def _hash_dos_dados(dados: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dados, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _do_cache(cache: dict, desatualizado: bool) -> AnaliseResultado:
    res = cache.get("resultado", {})
    return AnaliseResultado(
        resumo=res.get("resumo", ""), pontos_fortes=res.get("pontos_fortes", []),
        pontos_de_atencao=res.get("pontos_de_atencao", []), sugestoes=res.get("sugestoes", []),
        gerado_em=cache.get("gerado_em", ""), desatualizado=desatualizado,
    )


def obter_analise_ia(
    s: Session,
    competencia: str,
    hoje: date,
    gerar_nova: bool = False,
    forcar: bool = False,
    cliente=None,
    modelo: str | None = None,
    previsao: Any | None = None,
) -> AnaliseResultado | None:
    """Retorna a análise da IA, do cache ou gerando uma nova.

    Só chama a IA com `gerar_nova` ou `forcar` (o botão da página); sem eles, apenas lê o cache e marca
    `desatualizado` se os números mudaram desde a geração. Sem cache, devolve None.
    `forcar` refaz mesmo que os números sejam os mesmos do cache.
    """
    chave_cache = f"analise_ia:{competencia}"
    cache = get_config(s, chave_cache)
    dados = _preparar_dados_agregados(s, competencia, hoje) if (cache or gerar_nova or forcar) else None
    hash_atual = _hash_dos_dados(dados) if dados is not None else None

    if not (gerar_nova or forcar):
        return _do_cache(cache, cache.get("hash_dados") != hash_atual) if cache else None

    if cache and not forcar and cache.get("hash_dados") == hash_atual:
        return _do_cache(cache, False)  # mesmos números: reaproveita, sem gastar a IA

    if cliente is None or modelo is None:
        try:
            cliente, modelo = cliente_e_modelo_de_texto(s)
        except LeitorErro:
            return _do_cache(cache, True) if cache else None  # sem chave: mostra a anterior, marcada

    prompt = (
        f"Você é um consultor financeiro pessoal de alto nível analisando o desempenho financeiro de {nome_competencia(competencia)}.\n"
        "Com base nos dados agregados abaixo, produza um diagnóstico conciso, direto e prático:\n"
        f"{json.dumps(dados, indent=2, ensure_ascii=False)}\n\n"
        "Diretrizes:\n"
        "- Seja encorajador, mas realista e objetivo.\n"
        "- No resumo (máximo 3 frases), sintetize se o mês está saudável, sob pressão ou crítico.\n"
        "- Destaque até 3 pontos fortes e até 3 pontos de atenção.\n"
        "- Apresente até 3 sugestões acionáveis, informando o impacto mensal aproximado em centavos (BRL).\n"
    )

    resposta = chamar_parse(
        cliente,
        modelo=modelo,
        esforco="low",
        saida=SaidaAnaliseIA,
        max_tokens=2000,
        mensagens=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )

    if resposta.stop_reason == "refusal" or resposta.parsed_output is None:
        raise LeitorErro("A IA não conseguiu gerar o diagnóstico financeiro desta vez.")

    parsed: SaidaAnaliseIA = resposta.parsed_output
    agora_iso = agora().strftime("%d/%m/%Y às %H:%M")

    sugestoes_dict = [sug.model_dump() for sug in parsed.sugestoes]

    resultado_dict = {
        "resumo": parsed.resumo,
        "pontos_fortes": parsed.pontos_fortes,
        "pontos_de_atencao": parsed.pontos_de_atencao,
        "sugestoes": sugestoes_dict,
    }

    set_config(s, chave_cache, {
        "hash_dados": hash_atual,
        "gerado_em": agora_iso,
        "resultado": resultado_dict,
    })
    s.flush()

    return AnaliseResultado(
        resumo=parsed.resumo,
        pontos_fortes=parsed.pontos_fortes,
        pontos_de_atencao=parsed.pontos_de_atencao,
        sugestoes=sugestoes_dict,
        gerado_em=agora_iso,
        desatualizado=False,
    )
