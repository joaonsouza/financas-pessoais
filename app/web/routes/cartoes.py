from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.core.periodo import NOMES_MESES, competencia_de, somar_meses, validar_competencia
from app.models import Categoria, Conta, Fatura, Lancamento
from app.services.assinaturas_fatura import assinaturas_da_fatura
from app.services.assinaturas import (
    assinatura_da_sugestao, ignorar_sugestao, resumo_assinaturas, sugestoes_de_inclusao,
)
from app.services.faturas import (
    ROTULOS_STATUS,
    a_detalhar,
    cartoes_ativos,
    compras_da_fatura,
    competencia_da_compra,
    datas_fatura,
    definir_total_parcial,
    desfazer_pagamento,
    eh_pagamento_manual,
    fatura_do_mes,
    fatura_existente,
    limite_disponivel,
    melhor_cartao_hoje,
    obter_ou_criar_fatura,
    pagamentos_da_fatura,
    pagar_fatura,
    reordenar_cartoes,
    resumo_cartao,
    status_exibicao,
    total_fatura,
    total_pago,
)
from app.services.parcelas import definir_teto_parcelas, visao_parcelas
from app.services.ordem import proxima_ordem
from app.services.paineis import ordem_painel, salvar_ordem_painel
from app.services.sala_vip import desfazer_voucher, situacao_vip, usar_voucher
from app.web.deps import get_hoje, get_session
from app.web.routes.contas import validar_nome_e_cor
from app.web.templating import render

router = APIRouter(prefix="/cartoes")


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _dia(texto: str, rotulo: str) -> int:
    if not texto.isdigit() or not 1 <= int(texto) <= 31:
        raise ValueError(f"{rotulo} deve ser um dia entre 1 e 31")
    return int(texto)


def _obter(s: Session, cartao_id: int) -> Conta:
    cartao = s.get(Conta, cartao_id)
    if cartao is None or cartao.tipo != "cartao":
        raise HTTPException(status_code=404, detail="cartão não encontrado")
    return cartao


def _tela_lista(request: Request, s: Session, hoje: date, erro: str | None = None, status_code: int = 200,
                ver_arquivados: bool = False, mes: str | None = None):
    cartoes = cartoes_ativos(s)
    # fatura é em reais: só conta em reais paga
    pagadoras = list(s.scalars(select(Conta).where(Conta.tipo != "cartao", Conta.ativa, Conta.moeda == "BRL")
                               .order_by(Conta.nome)))
    arquivados = list(s.scalars(select(Conta).where(Conta.tipo == "cartao", Conta.ativa.is_(False)).order_by(Conta.nome)))
    # ?mes= abre direto na aba "Por mês"; sem ele (ou inválido) a aba começa no mês de hoje
    try:
        mes_escolhido = validar_competencia(mes)
    except ValueError:
        mes, mes_escolhido = None, competencia_de(hoje)
    return render(request, "cartoes/index.html", {
        "resumos": [resumo_cartao(s, c, hoje) for c in cartoes], "pagadoras": pagadoras, "erro": erro,
        "melhor": melhor_cartao_hoje(s, hoje), "hoje": hoje,
        "arquivados": arquivados, "ver_arquivados": ver_arquivados,
        "aba_inicial": "mes" if mes else "abertas", "mes_faturas": mes_escolhido,
        "faturas_mes": [fatura_do_mes(s, c, mes_escolhido, hoje) for c in cartoes],
        # sala VIP: só os cartões com o benefício
        "vips": {c.id: v for c in cartoes if (v := situacao_vip(c, hoje))},
        "ordem_painel": ordem_painel(s, "cartoes"),
        "parcelas": visao_parcelas(s, hoje),
        "assinaturas": resumo_assinaturas(s),
    }, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def listar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje),
           arquivados: bool = False, mes: str | None = None):
    return _tela_lista(request, s, hoje, ver_arquivados=arquivados, mes=mes)


def _rotulo_mes(comp: str) -> str:
    return f"{NOMES_MESES[int(comp[5:]) - 1][:3]}/{comp[2:4]}"


def _tela_parcelas(request: Request, s: Session, hoje: date, cartao_id: int | None, *,
                   erro: str | None = None, status_code: int = 200):
    cartoes = cartoes_ativos(s)
    if cartao_id not in {c.id for c in cartoes}:
        cartao_id = None
    visao = visao_parcelas(s, hoje, cartao_id)
    grafico = {
        "rotulos": [_rotulo_mes(m.competencia) for m in visao.meses],
        "valores": [m.total / 100 for m in visao.meses],
        "acima": [m.acima for m in visao.meses],
        "passado": [m.passado for m in visao.meses],
        "teto": visao.teto / 100,
    }
    compras = [{"descricao": c.descricao, "cartao": c.cartao, "atual": c.proxima_num, "total": c.total_parcelas,
                "parcela": c.valor_parcela / 100, "restantes": c.restantes, "falta": c.falta / 100,
                "fim": c.ultimo_mes, "fim_rotulo": _rotulo_mes(c.ultimo_mes)} for c in visao.compras]
    return render(request, "cartoes/parcelas.html", {
        "visao": visao, "cartoes": cartoes, "cartao_id": cartao_id, "grafico": grafico, "compras": compras,
        "erro": erro, "hoje": hoje,
    }, status_code=status_code)


@router.get("/parcelas", response_class=HTMLResponse)
def parcelas(request: Request, cartao: str | None = None, s: Session = Depends(get_session),
             hoje: date = Depends(get_hoje)):
    cartao_id = int(cartao) if cartao and cartao.isdigit() else None
    return _tela_parcelas(request, s, hoje, cartao_id)


@router.post("/parcelas/teto")
async def salvar_teto_parcelas(request: Request, s: Session = Depends(get_session),
                               hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        definir_teto_parcelas(s, parse_reais(_campo(form, "teto")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_parcelas(request, s, hoje, None, erro=str(exc), status_code=422)
    return RedirectResponse("/cartoes/parcelas", status_code=303)


@router.post("/ordem", status_code=204)
async def ordem(request: Request, s: Session = Depends(get_session)):
    """Ordem dos cartões arrastando em /cartoes: `ids` na ordem nova. Salva na hora (sem recarregar a página)."""
    form = await request.form()
    reordenar_cartoes(s, [int(i) for i in form.getlist("ids") if str(i).isdigit()])
    s.commit()


@router.post("/painel/ordem", status_code=204)
async def ordem_do_painel(request: Request, s: Session = Depends(get_session)):
    salvar_ordem_painel(s, "cartoes", (await request.form()).getlist("ids"))
    s.commit()


@router.post("/{cartao_id}/vip/usar", response_class=HTMLResponse)
def vip_usar(request: Request, cartao_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    try:
        usar_voucher(cartao, hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_lista(request, s, hoje, erro=str(exc), status_code=400)
    return RedirectResponse("/cartoes", status_code=303)


@router.post("/{cartao_id}/vip/desfazer")
def vip_desfazer(cartao_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    desfazer_voucher(_obter(s, cartao_id), hoje)
    s.commit()
    return RedirectResponse("/cartoes", status_code=303)


@router.post("/{cartao_id}/arquivar")
def arquivar(cartao_id: int, s: Session = Depends(get_session)):
    _obter(s, cartao_id).ativa = False
    s.commit()
    return RedirectResponse("/cartoes", status_code=303)


@router.post("/{cartao_id}/desarquivar")
def desarquivar(cartao_id: int, s: Session = Depends(get_session)):
    _obter(s, cartao_id).ativa = True
    s.commit()
    return RedirectResponse("/cartoes?arquivados=1", status_code=303)


@router.post("", response_class=HTMLResponse)
async def criar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        nome = _campo(form, "nome")
        if not nome:
            raise ValueError("informe o nome do cartão")
        if s.scalar(select(Conta.id).where(Conta.nome == nome)):
            raise ValueError(f"já existe uma conta chamada {nome}")
        pagadora = s.get(Conta, int(_campo(form, "pagadora_id"))) if _campo(form, "pagadora_id").isdigit() else None
        if pagadora is None or pagadora.tipo == "cartao":
            raise ValueError("escolha a conta pagadora")
        s.add(Conta(
            nome=nome, tipo="cartao", entra_saldo_liquido=False, saldo_inicial=0, data_saldo_inicial=hoje,
            limite=parse_reais(_campo(form, "limite") or "0"),
            dia_fechamento=_dia(_campo(form, "fechamento"), "Fechamento"),
            dia_vencimento=_dia(_campo(form, "vencimento"), "Vencimento"),
            conta_pagamento_id=pagadora.id, ordem=proxima_ordem(s, cartao=True),
        ))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_lista(request, s, hoje, erro=str(exc), status_code=400)
    return RedirectResponse("/cartoes", status_code=303)


# coluna -> direção inicial ao clicar nela
COLUNAS_COMPRAS = {"data": "desc", "titulo": "asc", "categoria": "asc", "valor": "desc"}


def _ordenar_compras(compras: list[Lancamento], categorias: dict[int, str], ordem: str, direcao: str) -> list[Lancamento]:
    desc = direcao == "desc"
    if ordem == "categoria":
        # sem categoria fica sempre no fim, nas duas direções
        com = [l for l in compras if l.categoria_id in categorias]
        sem = [l for l in compras if l.categoria_id not in categorias]
        com.sort(key=lambda l: (categorias[l.categoria_id].casefold(), l.data), reverse=desc)
        return com + sorted(sem, key=lambda l: l.data, reverse=True)
    chaves = {
        "data": lambda l: (l.data, l.id),
        "titulo": lambda l: (l.descricao.casefold(), l.data),
        "valor": lambda l: (-l.valor, l.data),  # valor gasto: desc = maior compra primeiro
    }
    return sorted(compras, key=chaves[ordem], reverse=desc)


def _link_fatura(competencia: str, ordem: str, direcao: str) -> str:
    params = {"fatura": competencia}
    if (ordem, direcao) != ("data", "desc"):
        params |= {"ordem": ordem, "dir": direcao}
    return "?" + urlencode(params)


def _tela_fatura(request: Request, s: Session, cartao: Conta, competencia: str, hoje: date,
                 erro: str | None = None, status_code: int = 200, ordem: str = "data", direcao: str = "desc"):
    fatura = fatura_existente(s, cartao, competencia)
    fechamento, vencimento = datas_fatura(cartao, competencia)
    total = total_fatura(s, fatura) if fatura else 0
    pago = total_pago(s, fatura) if fatura else 0
    categorias = dict(s.execute(select(Categoria.id, Categoria.nome)).all())
    compras = _ordenar_compras(compras_da_fatura(s, fatura) if fatura else [], categorias, ordem, direcao)
    colunas = {
        col: {
            "link": _link_fatura(competencia, col, ("asc" if direcao == "desc" else "desc") if col == ordem else inicial),
            "aria": ("descending" if direcao == "desc" else "ascending") if col == ordem else None,
        }
        for col, inicial in COLUNAS_COMPRAS.items()
    }
    return render(request, "cartoes/detalhe.html", {
        "cartao": cartao, "competencia": competencia, "fechamento": fechamento, "vencimento": vencimento,
        "compras": compras, "categorias_nome": categorias, "colunas": colunas,
        "link_anterior": _link_fatura(somar_meses(competencia, -1), ordem, direcao),
        "link_proxima": _link_fatura(somar_meses(competencia, 1), ordem, direcao),
        "pagamentos": pagamentos_da_fatura(s, fatura) if fatura else [],
        "total": total, "pago": pago, "restante": max(total - pago, 0),
        # total da fatura aberta digitado do app do banco; o que falta lançar aparece como "a detalhar"
        "detalhar": a_detalhar(s, fatura) if fatura else 0,
        "total_parcial": fatura.total_parcial if fatura else None,
        "total_parcial_em": fatura.total_parcial_em if fatura else None,
        # conferência com o total que o banco informou (PDF ou OFX de fatura fechada)
        "total_banco": fatura.total_informado if fatura else None,
        # modal de pagamento: datas rápidas e início do saldo de cada conta (pagamento antes dele não muda saldo)
        "hoje": hoje, "ontem": hoje - timedelta(days=1), "eh_manual": eh_pagamento_manual,
        "inicios_saldo": {str(c.id): c.data_saldo_inicial.isoformat() for c in s.scalars(
            select(Conta).where(Conta.tipo != "cartao", Conta.ativa))},
        "status_chave": (chave := status_exibicao(fatura, hoje, total) if fatura else "aberta"),
        "status": ROTULOS_STATUS[chave],
        "disponivel": limite_disponivel(s, cartao),
        "origens": list(s.scalars(select(Conta).where(Conta.tipo != "cartao", Conta.ativa, Conta.moeda == "BRL")
                                  .order_by(Conta.nome))),
        "erro": erro,
        "assinaturas": assinaturas_da_fatura(s, cartao, fatura, hoje) if fatura else None,
    }, status_code=status_code)


@router.get("/{cartao_id}", response_class=HTMLResponse)
def detalhe(request: Request, cartao_id: int, fatura: str = "", ordem: str = "data", dir: str = "desc",
            s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    try:
        competencia = validar_competencia(fatura)
    except ValueError:
        competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
    if ordem not in COLUNAS_COMPRAS or dir not in ("asc", "desc"):
        ordem, dir = "data", "desc"
    return _tela_fatura(request, s, cartao, competencia, hoje, ordem=ordem, direcao=dir)


@router.post("/{cartao_id}/assinaturas/cadastrar")
async def cadastrar_assinatura(request: Request, cartao_id: int, fatura: str = "", s: Session = Depends(get_session),
                               hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    form = await request.form()
    try:
        competencia = validar_competencia(fatura)
    except ValueError:
        competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
    descricao = _campo(form, "descricao")
    sug = next((x for x in sugestoes_de_inclusao(s, hoje)
                if x.descricao == descricao and x.conta_id == cartao.id), None)
    if sug is not None:
        try:
            assinatura_da_sugestao(s, sug, hoje)
            s.commit()
        except ValueError:
            s.rollback()
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)


@router.post("/{cartao_id}/assinaturas/ignorar")
async def ignorar_assinatura(request: Request, cartao_id: int, fatura: str = "", s: Session = Depends(get_session),
                             hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    form = await request.form()
    try:
        competencia = validar_competencia(fatura)
    except ValueError:
        competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
    ignorar_sugestao(s, _campo(form, "descricao"), cartao.id)
    s.commit()
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)


@router.post("/{cartao_id}/editar", response_class=HTMLResponse)
async def editar(request: Request, cartao_id: int, fatura: str = "", s: Session = Depends(get_session),
                 hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    form = await request.form()
    try:
        competencia = validar_competencia(fatura)
    except ValueError:
        competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
    try:
        validar_nome_e_cor(s, cartao, form)
        pagadora = s.get(Conta, int(_campo(form, "pagadora_id"))) if _campo(form, "pagadora_id").isdigit() else None
        if pagadora is None or pagadora.tipo == "cartao":
            raise ValueError("escolha a conta pagadora")
        cartao.conta_pagamento_id = pagadora.id
        cartao.limite = parse_reais(_campo(form, "limite") or "0")
        cartao.dia_fechamento = _dia(_campo(form, "fechamento"), "Fechamento")
        cartao.dia_vencimento = _dia(_campo(form, "vencimento"), "Vencimento")
        # campo oculto "0" + caixa "1": marcada chega ["0", "1"]; ausente (form antigo) não mexe
        if "sugerir_compra" in form:
            cartao.sugerir_compra = form.getlist("sugerir_compra")[-1] == "1"
        # sala VIP: vazio tira o benefício; mudar o total não mexe nos usados
        if "vip_vouchers" in form:
            texto = _campo(form, "vip_vouchers")
            if texto and not texto.isdigit():
                raise ValueError("vouchers de sala VIP: informe um número inteiro")
            cartao.vip_vouchers = int(texto) if texto else None
            data_vip = _campo(form, "vip_renova_em")
            try:
                renova = date.fromisoformat(data_vip) if data_vip else None
            except ValueError:
                raise ValueError("data de renovação da sala VIP inválida") from None
            cartao.vip_renova_em = renova if cartao.vip_vouchers is not None else None
        # faturas que ainda vão fechar passam a usar os dias novos; as já fechadas ficam como estão
        for f in s.scalars(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.status == "aberta",
                                               Fatura.data_fechamento >= hoje)):
            f.data_fechamento, f.data_vencimento = datas_fatura(cartao, f.competencia)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_fatura(request, s, cartao, competencia, hoje, erro=str(exc), status_code=400)
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)


@router.post("/{cartao_id}/pagamentos/{lanc_id}/desfazer", response_class=HTMLResponse)
def desfazer(request: Request, cartao_id: int, lanc_id: int, s: Session = Depends(get_session),
             hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    entrada = s.get(Lancamento, lanc_id)
    if entrada is None or entrada.conta_id != cartao.id or entrada.fatura_id is None:
        raise HTTPException(status_code=404, detail="pagamento não encontrado")
    competencia = s.get(Fatura, entrada.fatura_id).competencia
    try:
        desfazer_pagamento(s, entrada)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_fatura(request, s, cartao, competencia, hoje, erro=str(exc), status_code=400)
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)


@router.post("/{cartao_id}/faturas/{competencia}/parcial", response_class=HTMLResponse)
async def total_parcial(request: Request, cartao_id: int, competencia: str, s: Session = Depends(get_session),
                        hoje: date = Depends(get_hoje)):
    """Total da fatura aberta visto no app do banco; vazio apaga."""
    cartao = _obter(s, cartao_id)
    form = await request.form()
    try:
        validar_competencia(competencia)
        texto = _campo(form, "valor")
        definir_total_parcial(s, obter_ou_criar_fatura(s, cartao, competencia), parse_reais(texto) if texto else None,
                              hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_fatura(request, s, cartao, competencia, hoje, erro=str(exc), status_code=400)
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)


@router.post("/{cartao_id}/faturas/{competencia}/pagar", response_class=HTMLResponse)
async def pagar(request: Request, cartao_id: int, competencia: str, s: Session = Depends(get_session),
                hoje: date = Depends(get_hoje)):
    cartao = _obter(s, cartao_id)
    form = await request.form()
    try:
        validar_competencia(competencia)
        origem_id = _campo(form, "conta_origem_id")
        origem = s.get(Conta, int(origem_id)) if origem_id.isdigit() else None
        if origem is None or origem.tipo == "cartao":
            raise ValueError("escolha a conta de origem do pagamento")
        try:
            data = date.fromisoformat(_campo(form, "data"))
        except ValueError as exc:
            raise ValueError("data inválida") from exc
        valor = parse_reais(_campo(form, "valor"))
        if valor <= 0:
            raise ValueError("o valor deve ser maior que zero")
        fatura = obter_ou_criar_fatura(s, cartao, competencia)
        pagar_fatura(s, fatura, valor, data, conta_origem=origem, efetivado=bool(form.get("efetivado")))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_fatura(request, s, cartao, competencia, hoje, erro=str(exc), status_code=400)
    return RedirectResponse(f"/cartoes/{cartao.id}?fatura={competencia}", status_code=303)
