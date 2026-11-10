from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.core.quantidade import q
from app.models import Ativo, Conta, CotacaoCache, Lancamento, MetaClasse, OperacaoInvest
from app.models.caixinha import Caixinha
from app.models.investimento import Carteira as CarteiraModelo
from app.models.investimento import ALOCACOES, CLASSES_INVEST, ETFS_POR_ALOCACAO, INDEXADORES
from app.services.invest_carteira import (
    carteira_da_conta, carteira_padrao, carteiras, contas_da_carteira, garantir_carteira_padrao, metas_da_carteira,
    salvar_metas_carteira, classes_na_ordem, limpar_ordem_classes, mover_classe, ordem_manual,
    cambio_atual, metas_por_classe, posicao_do_ativo, resumo_carteira, series_indicadores, sobra_aporte,
    sugerir_aporte, plano_aporte, locais_da_oportunidade, plano_oportunidade, carteiras_do_desejo, carteiras_sem_posicao, definir_carteiras_sem_posicao, marcar_desejado,
)
from app.services import invest_planilha
from app.services.caixinhas import caixinhas_da_conta
from app.services.invest_conta import Local, definir_custodia, sugestao, vinculo, vincular
from app.services.invest_historico import evolucao_mensal
from app.services.invest_darf import ROTULOS as ROTULOS_DARF, SICALC, alternar_pago, apuracao
from app.services.invest_irpf import anos_disponiveis, relatorio_bens_e_direitos
from app.services.invest_resumo import (
    CORES_CLASSE, PERIODOS, RISCO_CLASSE, acima_da_inflacao, barras_evolucao, cards_resumo, distribuicao, mercado,
    mes_atual, rentabilidade_no_periodo, variacao_do_dia,
)
from app.services.invest_operacoes import (
    ETIQUETAS_CLASSE, ROTULOS_CLASSE, ROTULOS_TIPO, TIPOS_POR_CLASSE, DadosAtivo, DadosOperacao, adicionar_ativo, excluir_ativo, excluir_operacao,
    saldo_depois, salvar_operacao, sugerir_simbolo,
)
from app.services.mercado.cotacoes import SUFIXO_MENSAL, atualizar_cotacoes, atualizar_series, cotacao_do_ativo
from app.services.invest_renda_fixa import valor_atualizado_detalhe
from app.services.mercado.tesouro import (
    atualizar_tesouro, preco_de_mercado, taxa_na_data, titulo_do_codigo, titulos_a_venda,
)
from app.web.deps import Provedores, get_agora, get_hoje, get_provedores, get_session
from app.web.templating import render

router = APIRouter(prefix="/investimentos")

CEM = Decimal(100)
ROTULOS_INDEXADOR = {"pre": "Pré-fixado (% a.a.)", "cdi": "% do CDI", "ipca": "IPCA + (% a.a.)"}


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _recarregar() -> Response:
    return Response(status_code=200, headers={"HX-Refresh": "true"})


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError as exc:
        raise ValueError("informe uma data válida") from exc


def _decimal(texto: str, rotulo: str) -> Decimal:
    try:
        return q(texto)
    except ValueError as exc:
        raise ValueError(f"{rotulo} inválido(a)") from exc


def _reais(texto: str, rotulo: str) -> int:
    try:
        return parse_reais(texto)
    except ValueError as exc:
        raise ValueError(f"{rotulo} inválido(a)") from exc


def _num(valor: float | Decimal | None) -> float | None:
    return None if valor is None else float(valor)  # float só para o JSON do Chart.js


def atualizar_mercado(s: Session, prov: Provedores, agora: datetime, hoje: date, forcar: bool = False) -> list[str]:
    """Atualiza cotações e séries; devolve as partes que falharam. Nunca levanta (a página renderiza do cache)."""
    falhas: list[str] = []
    ativos = list(s.scalars(select(Ativo).where(Ativo.ativo)))
    try:
        res = atualizar_cotacoes(s, ativos, agora, forcar, yahoo=prov.yahoo, brapi=prov.brapi)
        falhas += res.falhas
    except Exception:  # noqa: BLE001 — qualquer falha de provedor mantém o cache
        s.rollback()
        falhas.append("cotações")
    try:
        if not atualizar_series(s, hoje, bcb=prov.bcb, forcar=forcar):
            falhas.append("CDI/IPCA")
    except Exception:  # noqa: BLE001
        s.rollback()
        falhas.append("CDI/IPCA")
    try:
        if not atualizar_tesouro(s, hoje, tesouro=prov.tesouro):
            falhas.append("Tesouro Direto")
    except Exception:  # noqa: BLE001
        s.rollback()
        falhas.append("Tesouro Direto")
    return falhas


# ---------- carteiras ----------

def _carteira_escolhida(s: Session, valor: str) -> CarteiraModelo | None:
    """`?carteira=<id>`; vazio ou inválido = consolidada (None)."""
    return s.get(CarteiraModelo, int(valor)) if valor.isdigit() else None


def _contexto_carteiras(s: Session, escolhida: CarteiraModelo | None) -> dict:
    lista = carteiras(s)
    return {"carteiras": lista, "carteira_sel": escolhida,
            "consolidada": escolhida is None and len(lista) > 1,
            "contas_invest_todas": list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo == "investimento")
                                                  .order_by(Conta.nome))),
            "contas_da_carteira": {c.id: contas_da_carteira(c) for c in lista}}


@router.post("/carteiras")
async def criar_ou_editar_carteira(request: Request, s: Session = Depends(get_session)):
    """Cria (sem `id`) ou renomeia uma carteira e escolhe as contas que ela sugere."""
    form = await request.form()
    nome = _campo(form, "nome")
    if not nome:
        raise HTTPException(422, "informe o nome da carteira")
    garantir_carteira_padrao(s)
    cid = _campo(form, "id")
    carteira = s.get(CarteiraModelo, int(cid)) if cid.isdigit() else None
    if carteira is None:
        carteira = CarteiraModelo(nome=nome)
        s.add(carteira)
    carteira.nome = nome[:80]
    carteira.contas = ",".join(c for c in form.getlist("contas") if str(c).isdigit())
    s.commit()
    return RedirectResponse(f"/investimentos?carteira={carteira.id}", status_code=303)


# ---------- página principal ----------

def _grafico_rentabilidade(pontos) -> dict:
    return {
        "rotulos": [f"{p.competencia[5:]}/{p.competencia[2:4]}" for p in pontos],
        "carteira": [_num(p.rent_acum_pct) for p in pontos],
        "cdi": [_num(p.cdi_acum_pct) for p in pontos],
        "ibov": [_num(p.ibov_acum_pct) for p in pontos],
        "global": [_num(p.global_acum_pct) for p in pontos],
    }


def _grafico_barras(barras) -> dict:
    """Base e topo desenham as barras; os valores reais vão para a dica."""
    return {
        "rotulos": [b.rotulo for b in barras],
        "base": [b.base / 100 for b in barras],
        "ganho": [b.topo / 100 for b in barras],
        "aplicado_real": [b.aplicado / 100 for b in barras],
        "ganho_real": [b.ganho / 100 for b in barras],
        "patrimonio": [b.patrimonio / 100 for b in barras],
    }


# (chave, rótulo, opcional): opcionais começam escondidas; a escolha fica no localStorage do navegador
COLUNAS_ATIVOS = (
    ("quant", "Quant.", False), ("preco_medio", "Preço médio", False), ("preco_atual", "Preço atual", False),
    ("variacao", "Variação", False), ("dia", "Variação do dia", True), ("rentabilidade", "Rentabilidade", False),
    ("saldo", "Saldo", False),
    # alocação, tudo dentro da classe: peso atual (com a meta marcada na barra), meta e quanto falta para ela
    ("na_classe", "Na classe", False), ("meta", "Meta", False), ("falta", "Falta para a meta", False),
    ("pct_carteira", "% Carteira", True), ("pct_ideal", "% Ideal", True), ("comprar", "Comprar?", True),
    ("opcoes", "Opções", False),
)
# largura (px, com o padding) de cada coluna: fixa para os blocos das classes ficarem alinhados entre si
LARGURAS_COLUNAS = {
    "quant": 80, "preco_medio": 112, "preco_atual": 112, "variacao": 80, "dia": 112, "rentabilidade": 104,
    "saldo": 120, "na_classe": 140, "meta": 100, "falta": 124, "pct_carteira": 84, "pct_ideal": 72,
    "comprar": 84, "opcoes": 100,
}


@router.post("/classes/mover")
def mover_bloco_de_classe(classe: str = "", passo: int = 0, carteira: str = "", s: Session = Depends(get_session),
                          hoje: date = Depends(get_hoje)):
    """Sobe ou desce o bloco de uma alocação na tela de investimentos (htmx recarrega a página)."""
    atual = resumo_carteira(s, hoje, _carteira_escolhida(s, carteira))
    mover_classe(s, [c.classe for c in classes_na_ordem(s, atual)], classe, passo)
    s.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/classes/ordem-automatica")
def ordem_automatica_das_classes(s: Session = Depends(get_session)):
    """Descarta a ordem escolhida: volta à automática (maior gap, o que falta para a meta, primeiro)."""
    limpar_ordem_classes(s)
    s.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.get("", response_class=HTMLResponse)
def investimentos(request: Request, periodo: str = "12", classe: str = "", rosca: str = "", carteira: str = "",
                  periodo_rent: str = "tudo", s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    periodo = periodo if periodo in PERIODOS else "12"
    periodo_rent = periodo_rent if periodo_rent in PERIODOS else "tudo"
    classe = classe if classe in ALOCACOES else ""  # chave de alocação
    rosca = rosca if rosca in ALOCACOES else ""
    escolhida = _carteira_escolhida(s, carteira)
    contexto_carteiras = _contexto_carteiras(s, escolhida)
    carteira = resumo_carteira(s, hoje, escolhida)
    carteira = replace(carteira, classes=classes_na_ordem(s, carteira))
    pontos = evolucao_mensal(s, hoje, carteira=escolhida)
    pontos_classe = evolucao_mensal(s, hoje, classe, carteira=escolhida) if classe else pontos
    ultima = pontos[-1] if pontos else None
    ultima_atualizacao = s.scalar(select(func.max(CotacaoCache.atualizado_em)).where(
        ~CotacaoCache.fonte.like(f"%{SUFIXO_MENSAL}")))
    fatias = distribuicao(carteira, rosca or None)
    cards = cards_resumo(carteira, pontos)
    indices = mercado(s, hoje)
    return render(request, "investimentos/index.html", {
        **contexto_carteiras,
        "carteira": carteira, "ultima": ultima, "ultima_atualizacao": ultima_atualizacao,
        "cards": cards, "dia": variacao_do_dia(carteira), "mercado": indices,
        "real_12m": acima_da_inflacao(cards.rent_12m_pct, indices.ipca_12m_pct),
        "no_mes": mes_atual(pontos), "composicao": distribuicao(carteira),
        "grafico": _grafico_rentabilidade(rentabilidade_no_periodo(pontos, PERIODOS[periodo_rent])),
        "barras": _grafico_barras(barras_evolucao(pontos_classe, PERIODOS[periodo])),
        "fatias": fatias,
        "alocacao": {"rotulos": [f.rotulo for f in fatias], "valores": [f.valor / 100 for f in fatias],
                     "cores": [f.cor for f in fatias]},
        "filtros": {"periodo": periodo, "classe": classe, "rosca": rosca, "periodo_rent": periodo_rent},
        "periodos": {"12": "Últimos 12 meses", "24": "Últimos 24 meses", "tudo": "Desde o início"},
        "classes": tuple(ALOCACOES), "colunas": COLUNAS_ATIVOS, "cores_classe": CORES_CLASSE,
        "riscos": {k: rotulo for k, (rotulo, _) in RISCO_CLASSE.items()}, "ordem_manual": bool(ordem_manual(s)),
        "colunas_padrao": {chave: not opcional for chave, _, opcional in COLUNAS_ATIVOS},
        "larguras_colunas": LARGURAS_COLUNAS,
        "rotulos_classe": ALOCACOES, "etiquetas_tipo": ETIQUETAS_CLASSE,
        "desatualizados": any(a.desatualizada for a in carteira.ativos),
        "sem_cotacao": any(a.sem_cotacao for a in carteira.ativos),
    })


@router.post("/atualizar")
def atualizar(s: Session = Depends(get_session), hoje: date = Depends(get_hoje), agora: datetime = Depends(get_agora),
              prov: Provedores = Depends(get_provedores)):
    atualizar_mercado(s, prov, agora, hoje, forcar=True)
    return _recarregar()


# ---------- operações ----------

def _form_padrao(s: Session, hoje: date, tipo: str = "compra", ativo_id: str = "") -> dict:
    cambio = cambio_atual(s)
    return {
        "ativo_id": ativo_id, "tipo": tipo, "classe": "acao", "codigo": "", "nome": "", "simbolo": "",
        "corretora_id": "", "indexador": "cdi", "taxa": "", "vencimento": "", "data": hoje.isoformat(),
        "quantidade": "", "preco": "", "cambio": "" if cambio is None else str(cambio).replace(".", ","),
        "taxas": "", "observacao": "", "taxa_op": "", "data_liquidacao": "", "valor_liquido": "",
        **_campos_vinculo(None),
    }


CAMPOS_VINCULO = ("custodia_conta_id", "custodia_caixinha_id", "conta_id", "caixinha_conta_id", "carteira_id")


def _campos_vinculo(v) -> dict:
    if v is None:
        return {k: "" for k in CAMPOS_VINCULO}
    return {k: "" if getattr(v, k) is None else str(getattr(v, k)) for k in CAMPOS_VINCULO}


def _local(s: Session, form, chave_conta: str, chave_caixinha: str) -> Local | None:
    """Conta + caixinha escolhidas no formulário; sem conta, None."""
    def obter(modelo, chave):
        valor = _campo(form, chave)
        if not valor.isdigit():
            return None
        obj = s.get(modelo, int(valor))
        if obj is None:
            raise ValueError("conta ou caixinha não encontrada")
        return obj
    conta = obter(Conta, chave_conta)
    return None if conta is None else Local(conta, obter(Caixinha, chave_caixinha))


def _custodia_e_pagamento(s: Session, form, ativo: Ativo, op: OperacaoInvest, hoje: date) -> None:
    definir_custodia(op, _local(s, form, "custodia_conta_id", "custodia_caixinha_id"))
    escolhida = _carteira_escolhida(s, _campo(form, "carteira_id"))
    if escolhida is None and "carteira_id" not in form:  # formulário sem o campo: a da conta onde fica
        escolhida = carteira_da_conta(s, op.custodia_conta_id)
    op.carteira_id = escolhida.id if escolhida else None
    liquidacao = _campo(form, "data_liquidacao")
    op.data_liquidacao = _data(liquidacao) if op.tipo in ("venda", "resgate") and liquidacao else None
    if ativo.corretora_conta_id is None and op.custodia_conta_id:
        ativo.corretora_conta_id = op.custodia_conta_id  # vale como corretora do ativo daqui em diante
    vincular(s, ativo, op, _local(s, form, "conta_id", "caixinha_conta_id"), hoje)


def _modal_operacao(request: Request, s: Session, form: dict, hoje: date, *, acao: str, titulo: str,
                    edicao: bool = False, erro: str | None = None, salvo: bool = False, status_code: int = 200):
    ativos = list(s.scalars(select(Ativo).where(Ativo.ativo).order_by(Ativo.classe, Ativo.codigo)))
    contas = list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo != "cartao").order_by(Conta.nome)))
    return render(request, "investimentos/_operacao.html", {
        "form": form, "hoje": hoje, "acao": acao, "titulo": titulo, "edicao": edicao, "erro": erro, "salvo": salvo,
        "ativos": ativos, "classe_do_ativo": {a.id: a.classe for a in ativos},
        "contas_pagam": [c for c in contas if c.tipo != "investimento"] + [c for c in contas if c.tipo == "investimento"],
        "contas_invest": [c for c in contas if c.tipo == "investimento"],
        "caixinhas": {c.id: [{"id": k.id, "nome": k.nome} for k in caixinhas_da_conta(s, c.id)] for c in contas},
        "sugestoes": {a.id: _campos_vinculo(sugestao(s, a)) for a in ativos},
        "carteiras_lista": carteiras(s),
        # carteira sugerida pela conta onde o lote fica (corretora X -> segunda carteira, por exemplo)
        "carteira_por_conta": {c.id: (cart.id if (cart := carteira_da_conta(s, c.id)) and not cart.padrao else "")
                               for c in contas if c.tipo == "investimento"},
        "rotulos_classe": ROTULOS_CLASSE, "rotulos_tipo": ROTULOS_TIPO, "tipos_por_classe": TIPOS_POR_CLASSE,
        "rotulos_indexador": ROTULOS_INDEXADOR, "classes": CLASSES_INVEST,
        "alocacoes": ALOCACOES, "etfs_por_alocacao": ETFS_POR_ALOCACAO,
        # títulos do Tesouro já cadastrados: o formulário busca a taxa do dia da aplicação
        "tesouro_dos_ativos": {a.id: {"titulo": titulo_do_codigo(a.codigo), "vencimento": a.vencimento.isoformat(),
                                      "por_titulos": a.por_titulos}
                               for a in ativos if a.classe == "renda_fixa" and a.vencimento and titulo_do_codigo(a.codigo)},
    }, status_code=status_code)


# ---------- Tesouro Direto: lista de títulos e taxa do dia (para preencher o formulário) ----------

@router.get("/tesouro/titulos")
def tesouro_titulos(s: Session = Depends(get_session), hoje: date = Depends(get_hoje),
                    prov: Provedores = Depends(get_provedores)):
    atualizar_tesouro(s, hoje, tesouro=prov.tesouro)
    return [{"titulo": t.titulo, "nome": t.nome, "vencimento": t.vencimento.isoformat(), "indexador": t.indexador}
            for t in titulos_a_venda(s)]


@router.get("/tesouro/taxa")
def tesouro_taxa(titulo: str, vencimento: str, data: str, anterior: bool = False, s: Session = Depends(get_session)):
    """Taxa de compra e preços do título no dia; no Selic a taxa vira % do CDI (100, o ágio fica de fora).

    `anterior`: o dia útil antes de `data`. O extrato mostra a liquidação (D+1), e o preço pago é o do dia da ordem.
    """
    try:
        dia = date.fromisoformat(data)
        ponto = taxa_na_data(s, titulo, date.fromisoformat(vencimento), dia - timedelta(days=1) if anterior else dia)
    except ValueError:
        raise HTTPException(422, "data inválida") from None
    if ponto is None:
        raise HTTPException(404, "sem taxa para esse título nessa data")
    taxa = Decimal(100) if titulo == "Tesouro Selic" else ponto.taxa_compra
    from app.web.templating import _num_input
    return {"taxa": _num_input(taxa), "pu": ponto.pu_compra, "pu_venda": ponto.pu_venda, "data": ponto.data.isoformat()}


@router.get("/operacoes/nova", response_class=HTMLResponse)
def nova_operacao(request: Request, tipo: str = "compra", ativo_id: str = "", quantidade: str = "", preco: str = "",
                  carteira_id: str = "", s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = _form_padrao(s, hoje, tipo, ativo_id)
    form.update({"quantidade": quantidade, "preco": preco})  # vindos do rebalanceamento
    ativo = s.get(Ativo, int(ativo_id)) if ativo_id.isdigit() else None
    if ativo is not None:
        form.update(_campos_vinculo(sugestao(s, ativo)))
    if carteira_id.isdigit():  # vindo de uma carteira (rebalancear): a operação é dela
        form["carteira_id"] = carteira_id
    return _modal_operacao(request, s, form, hoje, acao="/investimentos/operacoes", titulo="Nova operação")


def _dados_ativo_novo(form) -> DadosAtivo:
    classe = _campo(form, "classe")
    taxa = _campo(form, "taxa")
    venc = _campo(form, "vencimento")
    corretora = _campo(form, "corretora_id")
    return DadosAtivo(
        classe=classe, codigo=_campo(form, "codigo"), nome=_campo(form, "nome"),
        simbolo_cotacao=_campo(form, "simbolo"), corretora_conta_id=int(corretora) if corretora.isdigit() else None,
        indexador=_campo(form, "indexador") if classe == "renda_fixa" else None,
        taxa=_decimal(taxa, "taxa") if classe == "renda_fixa" and taxa else None,
        vencimento=_data(venc) if classe == "renda_fixa" and venc else None,
        por_titulos=classe == "renda_fixa" and _campo(form, "por_titulos") == "1",
        alocacao=_campo(form, "alocacao"),
    )


def _dados_operacao(form, ativo: Ativo, novo_ativo: bool = False) -> DadosOperacao:
    tipo = _campo(form, "tipo")
    classe = ativo.classe
    rf = classe == "renda_fixa" and not ativo.por_titulos  # Tesouro por títulos: quantidade e preço como ações
    cambio = _campo(form, "cambio")
    taxa = _campo(form, "taxa_op") or (_campo(form, "taxa") if novo_ativo else "")
    return DadosOperacao(
        taxa=_decimal(taxa, "taxa da aplicação") if classe == "renda_fixa" and taxa else None,
        tipo=tipo, data=_data(_campo(form, "data")),
        quantidade=Decimal(1) if rf else _decimal(_campo(form, "quantidade") or "0", "quantidade"),
        preco_unitario=_reais(_campo(form, "preco") or "0", "valor" if rf else "preço"),
        cambio=_decimal(cambio, "câmbio") if classe == "exterior" and cambio else None,
        taxas=_reais(_campo(form, "taxas") or "0", "taxas"), observacao=_campo(form, "observacao"),
        valor_liquido=_reais(_campo(form, "valor_liquido"), "valor líquido")
        if tipo in ("venda", "resgate") and _campo(form, "valor_liquido") else None,
    )


def _form_reexibir(form) -> dict:
    return {k: _campo(form, k) for k in (
        "ativo_id", "tipo", "classe", "codigo", "nome", "simbolo", "corretora_id", "indexador", "taxa", "vencimento",
        "data", "quantidade", "preco", "cambio", "taxas", "observacao", "taxa_op", "so_cadastrar", "alocacao",
        "data_liquidacao", "valor_liquido", *CAMPOS_VINCULO)}


@router.post("/operacoes", response_class=HTMLResponse)
async def criar_operacao(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        escolhido = _campo(form, "ativo_id")
        if escolhido.isdigit():
            ativo = s.get(Ativo, int(escolhido))
            if ativo is None:
                raise ValueError("ativo não encontrado")
        else:
            ativo = adicionar_ativo(s, _dados_ativo_novo(form))
        if _campo(form, "so_cadastrar") == "1":  # "ainda não comprei": só lista o ativo na carteira, sem operação
            escolhida = _carteira_escolhida(s, _campo(form, "carteira_id")) or garantir_carteira_padrao(s)
            marcar_desejado(s, ativo, carteiras_do_desejo(s, ativo) | {escolhida.id})
            s.commit()
            return _recarregar()
        op = salvar_operacao(s, ativo, _dados_operacao(form, ativo, novo_ativo=not escolhido.isdigit()))
        _custodia_e_pagamento(s, form, ativo, op, hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _modal_operacao(request, s, _form_reexibir(form), hoje, acao="/investimentos/operacoes",
                               titulo="Nova operação", erro=str(exc), status_code=422)
    if form.get("continuar"):
        novo = _form_padrao(s, hoje, _campo(form, "tipo"), str(ativo.id))
        novo["data"] = _campo(form, "data")
        novo.update({k: _campo(form, k) for k in CAMPOS_VINCULO})
        return _modal_operacao(request, s, novo, hoje, acao="/investimentos/operacoes", titulo="Nova operação",
                               salvo=True)
    return _recarregar()


def _obter_operacao(s: Session, op_id: int) -> OperacaoInvest:
    op = s.get(OperacaoInvest, op_id)
    if op is None:
        raise HTTPException(404, "Operação não encontrada")
    return op


def _form_da_operacao(s: Session, op: OperacaoInvest, ativo: Ativo) -> dict:
    from app.web.templating import _num_input, _reais_input
    return {
        "ativo_id": str(ativo.id), "tipo": op.tipo, "data": op.data.isoformat(),
        # rendimento: o formulário pede o saldo total depois do crédito, não a quantidade creditada
        "quantidade": _num_input(saldo_depois(s, ativo, op) if op.tipo == "rendimento" else op.quantidade),
        "preco": _reais_input(op.preco_unitario), "cambio": _num_input(op.cambio), "taxas": _reais_input(op.taxas),
        "observacao": op.observacao, "taxa_op": _num_input(op.taxa),
        "data_liquidacao": op.data_liquidacao.isoformat() if op.data_liquidacao else "",
        "valor_liquido": _reais_input(op.valor_liquido) if op.valor_liquido is not None else "",
        **_campos_vinculo(vinculo(s, op)),
    }


@router.get("/operacoes/{op_id}/editar", response_class=HTMLResponse)
def editar_operacao(request: Request, op_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    op = _obter_operacao(s, op_id)
    ativo = s.get(Ativo, op.ativo_id)
    return _modal_operacao(request, s, _form_da_operacao(s, op, ativo), hoje, acao=f"/investimentos/operacoes/{op.id}",
                           titulo=f"Editar operação · {ativo.codigo}", edicao=True)


@router.post("/operacoes/{op_id}", response_class=HTMLResponse)
async def salvar_edicao_operacao(request: Request, op_id: int, s: Session = Depends(get_session),
                                 hoje: date = Depends(get_hoje)):
    op = _obter_operacao(s, op_id)
    ativo = s.get(Ativo, op.ativo_id)
    form = await request.form()
    try:
        salvar_operacao(s, ativo, _dados_operacao(form, ativo), op)
        _custodia_e_pagamento(s, form, ativo, op, hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        reexibido = _form_reexibir(form)
        reexibido["ativo_id"] = str(ativo.id)
        return _modal_operacao(request, s, reexibido, hoje, acao=f"/investimentos/operacoes/{op_id}",
                               titulo=f"Editar operação · {ativo.codigo}", edicao=True, erro=str(exc), status_code=422)
    return _recarregar()


@router.post("/operacoes/{op_id}/excluir")
def excluir(request: Request, op_id: int, s: Session = Depends(get_session)):
    try:
        ativo_apagado = excluir_operacao(s, _obter_operacao(s, op_id))
    except ValueError as exc:
        s.rollback()
        raise HTTPException(409, str(exc)) from exc
    s.commit()
    if ativo_apagado and "/investimentos/ativos/" in request.headers.get("HX-Current-URL", ""):
        return Response(status_code=200, headers={"HX-Redirect": "/investimentos"})  # a página do ativo sumiu
    return _recarregar()


@router.get("/simbolo")
def simbolo(classe: str = "acao", codigo: str = ""):
    return {"simbolo": sugerir_simbolo(classe, codigo)}


# ---------- detalhe do ativo ----------

def _obter_ativo(s: Session, ativo_id: int) -> Ativo:
    ativo = s.get(Ativo, ativo_id)
    if ativo is None:
        raise HTTPException(404, "Ativo não encontrado")
    return ativo


@router.get("/ativos/{ativo_id}", response_class=HTMLResponse)
def detalhe_ativo(request: Request, ativo_id: int, carteira: str = "", s: Session = Depends(get_session),
                  hoje: date = Depends(get_hoje)):
    """`?carteira=<id>` (vindo da página da carteira): só as operações e a posição dela; sem, de todas."""
    ativo = _obter_ativo(s, ativo_id)
    todas = list(s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == ativo.id)
                           .order_by(OperacaoInvest.data, OperacaoInvest.id)))
    escolhida = _carteira_escolhida(s, carteira)
    # a mesma regra da carteira: na padrão entram também as operações sem carteira
    ops = todas if escolhida is None else [o for o in todas if o.carteira_id == escolhida.id
                                           or (escolhida.padrao and o.carteira_id is None)]
    cdi, ipca = series_indicadores(s)
    pos = posicao_do_ativo(ativo, ops, cdi, ipca, hoje)
    linha = next((a for a in resumo_carteira(s, hoje, escolhida).ativos if a.ativo.id == ativo.id), None)
    historico = []
    if ativo.simbolo_cotacao:
        historico = list(s.scalars(select(CotacaoCache).where(
            CotacaoCache.simbolo == ativo.simbolo_cotacao, CotacaoCache.fonte.like(f"%{SUFIXO_MENSAL}"))
            .order_by(CotacaoCache.data)))
    return render(request, "investimentos/ativo.html", {
        "ativo": ativo, "ops": list(reversed(ops)), "pos": pos, "linha": linha, "carteira_sel": escolhida,
        "cotacao": cotacao_do_ativo(s, ativo), "rotulos_classe": ROTULOS_CLASSE, "rotulos_tipo": ROTULOS_TIPO,
        "alocacoes": ALOCACOES, "carteiras_lista": carteiras(s), "desejo_ids": carteiras_sem_posicao(s, ativo),
        # encerrar é a posição inteira, em qualquer carteira
        "link_encerrar": _link_encerrar(s, ativo, todas, posicao_do_ativo(ativo, todas, cdi, ipca, hoje), hoje),
        "rotulos_indexador": ROTULOS_INDEXADOR, "hoje": hoje,
        "grafico": {"rotulos": [f"{h.data.month:02d}/{h.data.year % 100:02d}" for h in historico],
                    "valores": [_num(h.fechamento) for h in historico]},
    })


@router.post("/ativos/{ativo_id}/preco-manual")
async def preco_manual(request: Request, ativo_id: int, s: Session = Depends(get_session),
                       hoje: date = Depends(get_hoje)):
    ativo = _obter_ativo(s, ativo_id)
    form = await request.form()
    texto = _campo(form, "preco")
    if not texto:
        ativo.preco_manual, ativo.preco_manual_em = None, None
    else:
        try:
            valor = parse_reais(texto)
        except ValueError as exc:
            raise HTTPException(422, "preço inválido") from exc
        if valor <= 0:
            raise HTTPException(422, "preço inválido")
        ativo.preco_manual, ativo.preco_manual_em = valor, hoje
    s.commit()
    return RedirectResponse(f"/investimentos/ativos/{ativo_id}", status_code=303)


@router.post("/ativos/{ativo_id}/rendimento")
async def salvar_rendimento(request: Request, ativo_id: int, s: Session = Depends(get_session)):
    """% a.a. embutido no ativo (ex.: USDC na corretora); vazio ou 0 desliga."""
    ativo = _obter_ativo(s, ativo_id)
    form = await request.form()
    texto = _campo(form, "rendimento")
    try:
        valor = _decimal(texto, "rendimento") if texto else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if ativo.classe == "renda_fixa" or (valor is not None and not 0 <= valor <= 100):
        raise HTTPException(422, "rendimento inválido")
    ativo.rendimento_aa = valor or None
    s.commit()
    return RedirectResponse(f"/investimentos/ativos/{ativo_id}", status_code=303)


@router.post("/ativos/{ativo_id}/alocacao")
async def salvar_alocacao(request: Request, ativo_id: int, s: Session = Depends(get_session)):
    ativo = _obter_ativo(s, ativo_id)
    form = await request.form()
    alocacao = _campo(form, "alocacao")
    if alocacao not in ALOCACOES:
        raise HTTPException(422, "alocação inválida")
    ativo.alocacao = alocacao
    s.commit()
    return RedirectResponse(f"/investimentos/ativos/{ativo_id}", status_code=303)


def _link_encerrar(s: Session, ativo: Ativo, ops: list[OperacaoInvest], pos, hoje: date) -> str | None:
    """Modal de nova operação já com a saída da posição inteira (venda ou resgate) ao preço de hoje.

    Preço da posição toda, em qualquer carteira ou na reserva: valor atualizado (renda fixa), preço de mercado
    (Tesouro por títulos) ou a cotação na moeda do ativo; sem preço conhecido, o campo fica vazio.
    """
    if pos.quantidade <= 0:
        return None
    from app.web.templating import _num_input, _reais_input
    rf = ativo.classe == "renda_fixa" and not ativo.por_titulos
    preco = None
    if rf:
        preco = valor_atualizado_detalhe(ativo, ops, *series_indicadores(s), hoje).valor
    elif ativo.classe == "renda_fixa":
        mercado = preco_de_mercado(s, ativo, hoje)
        preco = mercado.pu_venda if mercado else None
    elif (cot := cotacao_do_ativo(s, ativo)) is not None and cot.moeda == ativo.moeda:
        preco = int((cot.preco * 100).quantize(Decimal(1)))
    params = {"ativo_id": ativo.id, "tipo": "resgate" if ativo.classe == "renda_fixa" else "venda",
              "quantidade": "" if rf else _num_input(pos.quantidade),
              "preco": _reais_input(preco) if preco else ""}
    return "/investimentos/operacoes/nova?" + urlencode(params)


@router.post("/ativos/{ativo_id}/excluir")
def excluir_ativo_rota(ativo_id: int, s: Session = Depends(get_session)):
    """Apaga o ativo com todo o histórico (operações, transferências ligadas e metas)."""
    excluir_ativo(s, _obter_ativo(s, ativo_id))
    s.commit()
    return RedirectResponse("/investimentos", status_code=303)


@router.post("/ativos/{ativo_id}/carteira")
async def salvar_carteira_desejo(request: Request, ativo_id: int, s: Session = Depends(get_session)):
    """Carteiras em que o ativo aparece mesmo sem compra nelas (as marcadas; nas desmarcadas, a meta dele zera)."""
    ativo = _obter_ativo(s, ativo_id)
    form = await request.form()
    existentes = {c.id for c in carteiras(s)}
    definir_carteiras_sem_posicao(s, ativo, {int(v) for v in form.getlist("carteira_id") if v.isdigit()} & existentes)
    s.commit()
    return RedirectResponse(f"/investimentos/ativos/{ativo_id}", status_code=303)


# ---------- metas (editadas nos blocos de "Meus ativos") ----------

def _meta_do_form(form, chave: str, atual: Decimal | None, rotulo: str) -> Decimal:
    """Campo ausente mantém o valor salvo (ativo escondido na tela); vazio vale 0."""
    if chave not in form:
        return atual or Decimal(0)
    return _decimal(_campo(form, chave) or "0", rotulo)


@router.get("/metas")
def metas():
    return RedirectResponse("/investimentos#meus-ativos", status_code=303)


@router.post("/metas", response_class=HTMLResponse)
async def salvar_metas(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    ativos = list(s.scalars(select(Ativo).where(Ativo.ativo)))
    escolhida = _carteira_escolhida(s, _campo(form, "carteira_id")) or garantir_carteira_padrao(s)
    salvas, salvas_ativo = metas_da_carteira(s, escolhida)
    try:
        por_classe = {c: _meta_do_form(form, f"classe_{c}", salvas.get(c), f"meta de {ALOCACOES[c]}")
                      for c in ALOCACOES}
        por_ativo = {a.id: _meta_do_form(form, f"ativo_{a.id}", salvas_ativo.get(a.id), f"meta de {a.codigo}")
                     for a in ativos}
        if any(v < 0 or v > 100 for v in [*por_classe.values(), *por_ativo.values()]):
            raise ValueError("as metas devem ficar entre 0 e 100%")
        soma = sum(por_classe.values())
        if soma != 100:
            raise ValueError(f"a soma das metas das alocações deve ser 100% (está em {soma}%)")
        for classe in ALOCACOES:
            doclasse = [a for a in ativos if a.alocacao == classe]
            total = sum((por_ativo[a.id] for a in doclasse), Decimal(0))
            if doclasse and por_classe[classe] > 0 and total != 100:
                raise ValueError(f"a soma das metas dos ativos de {ALOCACOES[classe]} deve ser 100% (está em {total}%)")
    except ValueError as exc:
        return render(request, "investimentos/_erro_metas.html", {"erro": str(exc)}, status_code=422)
    salvar_metas_carteira(s, escolhida, por_classe, por_ativo)
    s.commit()
    if request.headers.get("HX-Request"):
        return _recarregar()
    return RedirectResponse("/investimentos#meus-ativos", status_code=303)


# ---------- lançamentos ----------

TIPOS_LANCAMENTO = {"entrada": ("compra", "aplicacao"), "saida": ("venda", "resgate"), "ajuste": ("ajuste_posicao",),
                    "rendimento": ("rendimento",)}


def _opcoes_custodia(s: Session) -> list[tuple[str, str]]:
    """("conta:caixinha", rótulo) de cada conta de investimento ativa e de cada caixinha dela."""
    opcoes = []
    for c in s.scalars(select(Conta).where(Conta.ativa, Conta.tipo == "investimento").order_by(Conta.nome)):
        opcoes.append((f"{c.id}:", c.nome))
        opcoes += [(f"{c.id}:{k.id}", f"{c.nome} · {k.nome}") for k in caixinhas_da_conta(s, c.id)]
    return opcoes


def _chave_custodia(op: OperacaoInvest) -> str:
    return f"{op.custodia_conta_id}:{op.custodia_caixinha_id or ''}" if op.custodia_conta_id else ""


@router.post("/operacoes/{op_id}/custodia")
async def salvar_custodia(request: Request, op_id: int, s: Session = Depends(get_session)):
    """Muda só onde o lote fica (seletor da lista de lançamentos); a transferência ligada acompanha."""
    op = _obter_operacao(s, op_id)
    valor = _campo(await request.form(), "custodia")
    try:
        if not valor:
            if op.lancamento_id:
                raise ValueError("operação com conta vinculada precisa de um local; edite a operação para tirar a conta")
            definir_custodia(op, None)
        else:
            conta_id, _, caixinha_id = valor.partition(":")
            conta = s.get(Conta, int(conta_id)) if conta_id.isdigit() else None
            caixinha = s.get(Caixinha, int(caixinha_id)) if caixinha_id.isdigit() else None
            if conta is None:
                raise ValueError("conta não encontrada")
            definir_custodia(op, Local(conta, caixinha))
            if op.lancamento_id:  # a perna da transferência na custódia muda junto
                da_conta = s.get(Lancamento, op.lancamento_id)
                outra = s.get(Lancamento, da_conta.transferencia_par_id)
                outra.conta_id, outra.caixinha_id = op.custodia_conta_id, op.custodia_caixinha_id
    except ValueError as exc:
        s.rollback()
        raise HTTPException(422, str(exc)) from exc
    s.commit()
    return Response(status_code=204)


@router.get("/lancamentos", response_class=HTMLResponse)
def lancamentos(request: Request, alocacao: str = "", tipo: str = "", ativo: str = "", ano: str = "", local: str = "",
                carteira: str = "", s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    alocacao = alocacao if alocacao in ALOCACOES else ""
    tipo = tipo if tipo in TIPOS_LANCAMENTO else ""
    consulta = (select(OperacaoInvest, Ativo).join(Ativo, Ativo.id == OperacaoInvest.ativo_id)
                .order_by(OperacaoInvest.data.desc(), OperacaoInvest.id.desc()))
    todos_ativos = list(s.scalars(select(Ativo).where(Ativo.id.in_(select(OperacaoInvest.ativo_id)))
                                  .order_by(Ativo.codigo)))
    anos = sorted({d.year for d in s.scalars(select(OperacaoInvest.data))}, reverse=True)
    if alocacao:
        consulta = consulta.where(Ativo.alocacao == alocacao)
    if tipo:
        consulta = consulta.where(OperacaoInvest.tipo.in_(TIPOS_LANCAMENTO[tipo]))
    if ativo.isdigit():
        consulta = consulta.where(Ativo.id == int(ativo))
    if ano.isdigit():
        consulta = consulta.where(func.strftime("%Y", OperacaoInvest.data) == ano)
    escolhida = _carteira_escolhida(s, carteira)
    if escolhida is not None:
        consulta = consulta.where(or_(OperacaoInvest.carteira_id == escolhida.id, OperacaoInvest.carteira_id.is_(None))
                                  if escolhida.padrao else OperacaoInvest.carteira_id == escolhida.id)
    if local == "sem":
        consulta = consulta.where(OperacaoInvest.custodia_conta_id.is_(None))
    elif local:
        conta_id, _, caixinha_id = local.partition(":")
        if conta_id.isdigit():
            consulta = consulta.where(OperacaoInvest.custodia_conta_id == int(conta_id))
            if caixinha_id.isdigit():
                consulta = consulta.where(OperacaoInvest.custodia_caixinha_id == int(caixinha_id))
    linhas = []
    for op, a in s.execute(consulta):
        total = int((Decimal(op.quantidade) * op.preco_unitario * Decimal(op.cambio or 1)).quantize(Decimal(1)))
        linhas.append({"op": op, "ativo": a, "total": total, "custodia": _chave_custodia(op)})
    entradas = sum(l["total"] + (l["op"].taxas or 0) for l in linhas if l["op"].tipo in TIPOS_LANCAMENTO["entrada"])
    saidas = sum(l["total"] - (l["op"].taxas or 0) for l in linhas if l["op"].tipo in TIPOS_LANCAMENTO["saida"])
    consulta_grafico = select(OperacaoInvest, Ativo).join(Ativo, Ativo.id == OperacaoInvest.ativo_id)
    if escolhida is not None:
        consulta_grafico = consulta_grafico.where(
            or_(OperacaoInvest.carteira_id == escolhida.id, OperacaoInvest.carteira_id.is_(None))
            if escolhida.padrao else OperacaoInvest.carteira_id == escolhida.id
        )
    if ativo.isdigit():
        consulta_grafico = consulta_grafico.where(Ativo.id == int(ativo))
    if local == "sem":
        consulta_grafico = consulta_grafico.where(OperacaoInvest.custodia_conta_id.is_(None))
    elif local:
        c_id, _, cx_id = local.partition(":")
        if c_id.isdigit():
            consulta_grafico = consulta_grafico.where(OperacaoInvest.custodia_conta_id == int(c_id))
            if cx_id.isdigit():
                consulta_grafico = consulta_grafico.where(OperacaoInvest.custodia_caixinha_id == int(cx_id))
    dados_aportes = []
    for op_g, a_g in s.execute(consulta_grafico.order_by(OperacaoInvest.data, OperacaoInvest.id)):
        if op_g.tipo not in (*TIPOS_LANCAMENTO["entrada"], *TIPOS_LANCAMENTO["saida"]):
            continue
        bruto_g = int((Decimal(op_g.quantidade) * op_g.preco_unitario * Decimal(op_g.cambio or 1)).quantize(Decimal(1)))
        eh_compra = op_g.tipo in TIPOS_LANCAMENTO["entrada"]
        val_cent = bruto_g + (op_g.taxas or 0) if eh_compra else bruto_g - (op_g.taxas or 0)
        dados_aportes.append({
            "mes": f"{op_g.data.year:04d}-{op_g.data.month:02d}",
            "ano": f"{op_g.data.year:04d}",
            "classe": a_g.classe,
            "alocacao": a_g.alocacao,
            "direcao": "compra" if eh_compra else "venda",
            "valor": round(val_cent / 100, 2),
        })
    return render(request, "investimentos/lancamentos.html", {
        "linhas": linhas, "entradas": entradas, "saidas": saidas, "anos": anos, "ativos": todos_ativos,
        "filtros": {"alocacao": alocacao, "tipo": tipo, "ativo": ativo, "ano": ano, "local": local, "carteira": carteira},
        **_contexto_carteiras(s, escolhida),
        "opcoes_custodia": _opcoes_custodia(s),
        "rotulos_classe": ALOCACOES, "rotulos_tipo": ROTULOS_TIPO, "etiquetas_tipo": ETIQUETAS_CLASSE,
        "classes_ativo": ROTULOS_CLASSE, "dados_aportes": dados_aportes,
        "hoje_mes": f"{hoje.year:04d}-{hoje.month:02d}",
    })


# ---------- rebalanceamento ----------

@router.get("/rebalancear", response_class=HTMLResponse)
def rebalancear(request: Request, aporte: str = "", carteira: str = "", classe: str = "",
                usar_oportunidade: str = "", s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    escolhida = _carteira_escolhida(s, carteira)
    carteira = resumo_carteira(s, hoje, escolhida)
    classe = classe if classe in {c.classe for c in carteira.classes} else ""  # vazio ou inválida: global
    # reserva de oportunidade: o saldo dela soma no aporte (marcado por padrão; no envio, só se a caixa veio)
    tem_oportunidade = bool(locais_da_oportunidade(s))
    usar = tem_oportunidade and (not aporte.strip() or usar_oportunidade == "1")
    saldo_oportunidade = carteira.oportunidade_saldo if usar else 0
    valor = None
    erro = None
    if aporte.strip():
        try:
            valor = parse_reais(aporte)
            if valor < 0 or valor + saldo_oportunidade <= 0:
                raise ValueError
        except ValueError:
            erro, valor = "Informe um valor de aporte maior que zero.", None
    disponivel = None if valor is None else valor + saldo_oportunidade
    plano_compras = plano_aporte(carteira, disponivel, classe or None) if disponivel else None
    sugestoes = plano_compras.sugestoes if plano_compras else []
    plano = (plano_oportunidade(saldo=carteira.oportunidade_saldo, aporte=valor, usar=usar, sugestoes=sugestoes)
             if tem_oportunidade and valor is not None else None)
    pu_tesouro = {a.ativo.id: a.preco_compra for a in carteira.ativos if a.preco_compra}
    return render(request, "investimentos/rebalancear.html", {
        "efetivar": {sg.ativo.id: _url_efetivar(s, sg, escolhida, pu_tesouro.get(sg.ativo.id)) for sg in sugestoes},
        **_contexto_carteiras(s, escolhida),
        "carteira": carteira, "aporte": aporte, "valor": valor, "erro": erro, "sugestoes": sugestoes, "classe_sel": classe,
        "alocacoes": ALOCACOES,
        "sobra": sobra_aporte(sugestoes, disponivel) if disponivel else 0, "rotulos_classe": ROTULOS_CLASSE,
        "tem_oportunidade": tem_oportunidade, "usar_oportunidade": usar, "plano": plano,
        "antes_depois": plano_compras.classes if plano_compras else [],
        "por_ativo": {a.ativo.id: a for c in plano_compras.classes for a in c.ativos} if plano_compras else {},
        "cores_classe": CORES_CLASSE,
        "linhas": {a.ativo.id: a for a in carteira.ativos},
    }, status_code=422 if erro else 200)


def _url_efetivar(s: Session, sg, carteira: CarteiraModelo | None = None, pu_tesouro: Decimal | None = None) -> str:
    """Modal de operação já com o ativo, a quantidade e o preço (ou o valor, na renda fixa) da sugestão.
    Tesouro por títulos: os títulos e o PU de compra do dia."""
    from app.web.templating import _num_input, _reais_input
    if sg.ativo.classe == "renda_fixa" and sg.quantidade and pu_tesouro:
        dados = {"tipo": "aplicacao", "quantidade": _num_input(sg.quantidade), "preco": _reais_input(int(pu_tesouro))}
    elif sg.ativo.classe == "renda_fixa":
        dados = {"tipo": "aplicacao", "preco": _reais_input(sg.valor)}
    else:
        cot = cotacao_do_ativo(s, sg.ativo)  # na moeda do ativo: no exterior, o preço vai em US$
        dados = {"tipo": "compra", "quantidade": _num_input(sg.quantidade) if sg.quantidade else "",
                 "preco": _reais_input(int((cot.preco * CEM).quantize(Decimal(1)))) if cot else ""}
    if carteira is not None:
        dados["carteira_id"] = carteira.id
    return "/investimentos/operacoes/nova?" + urlencode({"ativo_id": sg.ativo.id, **dados})


# ---------- pré-preenchimento pela planilha ----------

def _linhas_planilha(s: Session, posicoes, form=None) -> list[dict]:
    existentes = {(a.classe, a.codigo) for a in s.scalars(select(Ativo))}
    linhas = []
    for i, p in enumerate(posicoes):
        existe = (p.classe, p.codigo) in existentes
        linha = {
            "i": i, "classe": p.classe, "codigo": p.codigo, "existe": existe,
            "incluir": not existe, "quantidade": _num_texto(p.quantidade), "meta": _num_texto(p.meta_pct),
            "preco": "", "indexador": "cdi", "taxa": "", "vencimento": "",
        }
        if form is not None:
            linha["incluir"] = bool(form.get(f"incluir_{i}"))
            for chave in ("quantidade", "meta", "preco", "indexador", "taxa", "vencimento"):
                linha[chave] = _campo(form, f"{chave}_{i}")
        linhas.append(linha)
    return linhas


def _num_texto(valor: Decimal) -> str:
    from app.core.quantidade import fmt_qtd
    return fmt_qtd(valor).replace(".", "")


def _tela_planilha(request: Request, s: Session, hoje: date, *, posicoes=None, form=None, erro: str | None = None,
                   status_code: int = 200):
    aviso = None
    if posicoes is None:
        try:
            posicoes = invest_planilha.ler_planilha()
        except invest_planilha.PlanilhaIndisponivel as exc:
            posicoes, aviso = [], str(exc)
    cambio = _campo(form, "cambio") if form is not None else (
        "" if (c := cambio_atual(s)) is None else str(c).replace(".", ","))
    return render(request, "investimentos/importar_planilha.html", {
        "linhas": _linhas_planilha(s, posicoes, form), "aviso": aviso, "erro": erro, "rotulos_classe": ROTULOS_CLASSE,
        "rotulos_indexador": ROTULOS_INDEXADOR, "data": _campo(form, "data") if form is not None else hoje.isoformat(),
        "cambio": cambio,
    }, status_code=status_code)


@router.get("/importar-planilha", response_class=HTMLResponse)
def importar_planilha(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    return _tela_planilha(request, s, hoje)


@router.post("/importar-planilha", response_class=HTMLResponse)
async def confirmar_planilha(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        posicoes = invest_planilha.ler_planilha()
    except invest_planilha.PlanilhaIndisponivel as exc:
        return _tela_planilha(request, s, hoje, posicoes=[], erro=str(exc), status_code=422)
    try:
        data = _data(_campo(form, "data"))
        itens = []
        for i, p in enumerate(posicoes):
            if not form.get(f"incluir_{i}"):
                continue
            rf = p.classe == "renda_fixa"
            taxa, venc, cambio = _campo(form, f"taxa_{i}"), _campo(form, f"vencimento_{i}"), _campo(form, "cambio")
            try:
                itens.append(invest_planilha.ItemImportacao(
                    classe=p.classe, codigo=p.codigo, quantidade=_decimal(_campo(form, f"quantidade_{i}") or "0", "quantidade"),
                    meta_pct=_decimal(_campo(form, f"meta_{i}") or "0", "meta"),
                    preco_medio=_reais(_campo(form, f"preco_{i}") or "0", "preço médio"),
                    cambio=_decimal(cambio, "câmbio") if p.classe == "exterior" and cambio else None,
                    indexador=_campo(form, f"indexador_{i}") if rf else None,
                    taxa=_decimal(taxa, "taxa") if rf and taxa else None,
                    vencimento=_data(venc) if rf and venc else None,
                ))
            except ValueError as exc:
                raise ValueError(f"{p.codigo}: {exc}") from exc
        if not itens:
            raise ValueError("Marque pelo menos uma posição para importar.")
        invest_planilha.importar_posicoes(s, itens, data)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_planilha(request, s, hoje, posicoes=posicoes, form=form, erro=str(exc), status_code=422)
    return RedirectResponse("/investimentos", status_code=303)


# ---------- IRPF (Bens e Direitos) ----------

GRUPOS_RFB = {
    "03": "03 - Participações Societárias",
    "04": "04 - Aplicações e Investimentos",
    "07": "07 - Fundos",
    "08": "08 - Criptoativos",
}


@router.get("/darf", response_class=HTMLResponse)
def darf(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """Preciso pagar DARF? Um mês por linha (bolsa e cripto), do mais recente ao mais antigo; a guia sai no Sicalc."""
    linhas = list(reversed(apuracao(s, hoje)))
    return render(request, "investimentos/darf.html", {
        "linhas": linhas, "a_pagar": sum(l.valor for l in linhas if l.situacao == "pagar"),
        "vencidos": sum(1 for l in linhas if l.vencido), "sicalc": SICALC, "rotulos": ROTULOS_DARF,
    })


@router.post("/darf/{competencia}/{codigo}/pago")
def darf_pago(competencia: str, codigo: str, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    if codigo not in ROTULOS_DARF or len(competencia) != 7 or competencia[4] != "-":
        raise HTTPException(404, "DARF não encontrado")
    alternar_pago(s, competencia, codigo, hoje)
    s.commit()
    return RedirectResponse("/investimentos/darf", status_code=303)


@router.get("/irpf", response_class=HTMLResponse)
def irpf(request: Request, ano: str = "", carteira: str = "", grupo: str = "",
         s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    anos = anos_disponiveis(s, hoje)
    escolhida = _carteira_escolhida(s, carteira)
    grupo = grupo if grupo in GRUPOS_RFB else ""
    if ano.isdigit() and int(ano) in anos:
        ano_sel = int(ano)
    else:
        ano_sel = hoje.year - 1
        rel_padrao = relatorio_bens_e_direitos(s, ano_sel, escolhida)
        if rel_padrao.total_itens == 0 and hoje.year in anos:
            rel_atual = relatorio_bens_e_direitos(s, hoje.year, escolhida)
            if rel_atual.total_itens > 0:
                ano_sel = hoje.year
    relatorio = relatorio_bens_e_direitos(s, ano_sel, escolhida, grupo_filtro=grupo)
    return render(request, "investimentos/irpf.html", {
        **_contexto_carteiras(s, escolhida),
        "relatorio": relatorio,
        "anos": anos,
        "ano_sel": ano_sel,
        "grupo_sel": grupo,
        "grupos_rfb": GRUPOS_RFB,
        "etiquetas_tipo": ETIQUETAS_CLASSE,
        "rotulos_classe": ROTULOS_CLASSE,
        "hoje": hoje,
    })
