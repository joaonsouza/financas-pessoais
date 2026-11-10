from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Categoria, get_config, set_config

NOME_CATEGORIA_ASSINATURAS = "Assinaturas"

FIXAS = [
    "Aluguel", "Condomínio/IPTU", "Energia", "Água", "Gás", "Internet/Celular", "Transporte fixo",
    "Escola", "Plano de saúde", "Seguros", NOME_CATEGORIA_ASSINATURAS, "Outros fixos",
]
VARIAVEIS = [
    "Mercado", "Feira/Açougue/Padaria", "Comer fora/Delivery", "Combustível/Apps", "Farmácia", "Roupas",
    "Lazer", "Presentes/Ajuda a familiares", "Cuidados pessoais", "Casa/Manutenção", "Outros variáveis",
]
DIVIDAS = ["Parcelas de empréstimo/financiamento"]
RECEITAS = ["Salário", "Renda extra", "Rendimentos", "Outras receitas"]
VARIAVEIS_ESSENCIAIS = {"Mercado", "Feira/Açougue/Padaria"}
# inflação pessoal: contas de preço fixo e de consumo (média de 12 meses); o resto não entra
INFLACAO_PADRAO = {
    **dict.fromkeys(("Aluguel", "Condomínio/IPTU", "Internet/Celular", "Escola", "Plano de saúde", "Seguros",
                     NOME_CATEGORIA_ASSINATURAS, "Faxina", "Transporte fixo", "Outros fixos"), "fixo"),
    **dict.fromkeys(("Energia", "Água", "Gás"), "consumo"),
}
CHAVE_INFLACAO_MIGRADA = "categorias_inflacao_migradas"

PALETA = [
    "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16", "#22c55e", "#10b981", "#14b8a6",
    "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1", "#8b5cf6", "#a855f7", "#d946ef", "#ec4899",
]


def semear_categorias(s: Session) -> int:
    if s.scalar(select(func.count(Categoria.id))):
        return 0
    novas: list[Categoria] = []
    for nome in FIXAS:
        novas.append(Categoria(nome=nome, tipo="despesa", grupo="fixo", natureza="necessidade", essencial=True,
                               inflacao=INFLACAO_PADRAO.get(nome)))
    for nome in VARIAVEIS:
        essencial = nome in VARIAVEIS_ESSENCIAIS
        novas.append(Categoria(nome=nome, tipo="despesa", grupo="variavel",
                               natureza="necessidade" if essencial else "desejo", essencial=essencial))
    for nome in DIVIDAS:
        novas.append(Categoria(nome=nome, tipo="despesa", grupo="divida", natureza="necessidade", essencial=False))
    for nome in RECEITAS:
        novas.append(Categoria(nome=nome, tipo="receita"))
    for i, categoria in enumerate(novas):
        categoria.cor = PALETA[i % len(PALETA)]
    s.add_all(novas)
    s.flush()
    return len(novas)


def migrar_inflacao_categorias(s: Session) -> None:
    """Uma vez só: marca pelo nome as categorias de despesa da inflação pessoal. Depois, quem manda é a tela."""
    if get_config(s, CHAVE_INFLACAO_MIGRADA, False):
        return
    for c in s.scalars(select(Categoria).where(Categoria.tipo == "despesa", Categoria.inflacao.is_(None))):
        c.inflacao = INFLACAO_PADRAO.get(c.nome)
    set_config(s, CHAVE_INFLACAO_MIGRADA, True)
    s.flush()
