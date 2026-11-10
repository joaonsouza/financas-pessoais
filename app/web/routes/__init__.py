from app.web.routes import (
    assinaturas, cartoes, categorias, configuracoes, contas, dashboard, desempenho, importar, investimentos, lancamentos, metas, objetivos, planejamento,
    recorrencias, relatorios, resumo, setup,
)

ROTAS = [
    dashboard.router, setup.router, recorrencias.router, assinaturas.router, lancamentos.router, contas.router, cartoes.router, metas.router, planejamento.router,
    objetivos.router, desempenho.router, categorias.router, relatorios.router, investimentos.router, importar.router, configuracoes.router, resumo.router,
]
