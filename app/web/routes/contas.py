from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.models import Caixinha, Conta, Meta
from app.services.caixinhas import (
    ajustar_caixinha, aportar, criar_caixinha, divisao_conta, objetivo_da_conta, vincular_caixinha, excluir_caixinha, excluir_movimento, guardar_na_caixinha,
    movimentos_caixinha, remanejar, editar_caixinha, resgatar_da_caixinha,
)
from app.services.invest_conciliacao import conciliacao, conciliar
from app.services.lancamentos import ajustar_saldo, ajustar_saldo_inicial
from app.services.metas import caixinhas_da_meta, plano_aporte
from app.services.ordem import criterio_ordem, proxima_ordem, reordenar
from app.services.paineis import ordem_painel, salvar_ordem_painel
from app.services.saldos import saldo_liquido, saldos_contas
from app.web.deps import get_hoje, get_session, mes_selecionado
from app.web.routes.metas import CORES
from app.web.templating import render

ICONES_CAIXINHA = ["piggy-bank", "shield-check", "house", "receipt", "plane", "car", "graduation-cap", "heart",
                   "gift", "baby", "laptop", "shopping-cart", "paw-print", "stethoscope", "target", "trending-up"]

router = APIRouter(prefix="/contas")


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _data(form, hoje: date) -> date:
    texto = _campo(form, "data")
    try:
        return date.fromisoformat(texto) if texto else hoje
    except ValueError as exc:
        raise ValueError("data inválida") from exc


def _tela(request: Request, s: Session, mes: str, hoje: date, erro: str | None = None, status_code: int = 200,
          ver_arquivadas: bool = False):
    saldos = saldos_contas(s, hoje, mes, tipos=("corrente", "investimento"))
    arquivadas = list(s.scalars(select(Conta).where(Conta.tipo != "cartao", Conta.ativa.is_(False)).order_by(Conta.nome)))
    return render(request, "contas/index.html", {
        "mes": mes, "hoje": hoje, "saldos": saldos, "erro": erro, "ordem_painel": ordem_painel(s, "contas"),
        "arquivadas": arquivadas, "ver_arquivadas": ver_arquivadas,
        "liquido_atual": saldo_liquido(saldos),
        "liquido_dia": saldo_liquido(saldos, "previsto_dia"),
        "liquido_mes": saldo_liquido(saldos, "previsto_mes"),
        # soma de todas as contas, dentro e fora do saldo líquido
        "total_atual": sum(x.atual for x in saldos),
        "total_mes": sum(x.previsto_mes for x in saldos),
        "fora_atual": sum(x.atual for x in saldos if not x.entra_saldo_liquido),
        # caixinhas de investimento contra o valor de mercado das posições guardadas nelas
        "conciliacao": conciliacao(s, hoje),
        # contas com caixinhas: resumo no cartão da conta
        "divisoes": {c.id: divisao_conta(s, c) for c in s.scalars(
            select(Conta).join(Caixinha, Caixinha.conta_id == Conta.id).where(Caixinha.ativa).distinct())},
    }, status_code=status_code)


def _voltar(mes: str) -> RedirectResponse:
    return RedirectResponse(f"/contas?mes={mes}", status_code=303)


def _obter(s: Session, conta_id: int) -> Conta:
    conta = s.get(Conta, conta_id)
    if conta is None or conta.tipo == "cartao":
        raise HTTPException(status_code=404, detail="conta não encontrada")
    return conta


@router.get("", response_class=HTMLResponse)
def listar(request: Request, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
           hoje: date = Depends(get_hoje), arquivadas: bool = False):
    return _tela(request, s, mes, hoje, ver_arquivadas=arquivadas)


@router.post("", response_class=HTMLResponse)
async def criar(request: Request, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session),
                hoje: date = Depends(get_hoje)):
    form = await request.form()
    try:
        nome = _campo(form, "nome")
        if not nome:
            raise ValueError("informe o nome da conta")
        if s.scalar(select(Conta.id).where(Conta.nome == nome)):
            raise ValueError(f"já existe uma conta chamada {nome}")
        tipo = _campo(form, "tipo")
        if tipo not in ("corrente", "investimento"):
            raise ValueError("tipo de conta inválido")
        moeda = _campo(form, "moeda") or "BRL"
        if moeda not in ("BRL", "USD"):
            raise ValueError("moeda inválida")
        if moeda != "BRL" and tipo != "corrente":
            raise ValueError("conta em dólar: só conta corrente")
        # conta em outra moeda fica sempre fora do saldo líquido
        s.add(Conta(nome=nome, tipo=tipo, moeda=moeda, entra_saldo_liquido=bool(form.get("sl")) and moeda == "BRL",
                    saldo_inicial=parse_reais(_campo(form, "saldo") or "0"), data_saldo_inicial=_data(form, hoje),
                    ordem=proxima_ordem(s, cartao=False)))
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, mes, hoje, erro=str(exc), status_code=400)
    return _voltar(mes)


@router.post("/ordem", status_code=204)
async def ordem(request: Request, s: Session = Depends(get_session)):
    """Ordem das contas arrastando em /contas: `ids` na ordem nova (vale em todas as telas)."""
    ids = [int(i) for i in (await request.form()).getlist("ids") if str(i).isdigit()]
    reordenar(list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo != "cartao")
                             .order_by(*criterio_ordem(Conta.id)))), ids)
    s.commit()


@router.post("/painel/ordem", status_code=204)
async def ordem_do_painel(request: Request, s: Session = Depends(get_session)):
    salvar_ordem_painel(s, "contas", (await request.form()).getlist("ids"))
    s.commit()


@router.post("/conciliar-investimentos")
async def conciliar_investimentos(request: Request, mes: str = Depends(mes_selecionado),
                                  s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """Iguala a caixinha escolhida (ou todas) ao valor de mercado das posições dela."""
    caixinha = _campo(await request.form(), "caixinha_id")
    conciliar(s, hoje, int(caixinha) if caixinha.isdigit() else None)
    s.commit()
    return RedirectResponse(f"/contas?mes={mes}#conciliar", status_code=303)


@router.post("/{conta_id}/ajustar", response_class=HTMLResponse)
async def ajustar(request: Request, conta_id: int, mes: str = Depends(mes_selecionado),
                  s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    form = await request.form()
    try:
        novo_saldo = parse_reais(_campo(form, "novo_saldo"))
        if _campo(form, "modo") == "saldo_inicial":
            ajustar_saldo_inicial(s, conta, novo_saldo)
        else:
            ajustar_saldo(s, conta, novo_saldo, _data(form, hoje))
        s.commit()
    except ValueError as exc:
        s.rollback()
        if _campo(form, "voltar") == "conta":
            return _tela_conta(request, s, conta, hoje, erro=str(exc), status_code=400)
        return _tela(request, s, mes, hoje, erro=str(exc), status_code=400)
    # a diferença (rendimento, tarifa...) não tem caixinha: cai no Livre
    return _voltar_conta(conta) if _campo(form, "voltar") == "conta" else _voltar(mes)


def validar_nome_e_cor(s: Session, conta: Conta, form) -> None:
    nome = _campo(form, "nome")
    if not nome:
        raise ValueError("informe o nome")
    if s.scalar(select(Conta.id).where(Conta.nome == nome, Conta.id != conta.id)):
        raise ValueError(f"já existe uma conta chamada {nome}")
    cor = _campo(form, "cor")
    if cor and not (len(cor) == 7 and cor.startswith("#")):
        raise ValueError("cor inválida")
    conta.nome = nome
    conta.cor = cor or conta.cor


@router.post("/{conta_id}/editar", response_class=HTMLResponse)
async def editar(request: Request, conta_id: int, mes: str = Depends(mes_selecionado),
                 s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    form = await request.form()
    try:
        validar_nome_e_cor(s, conta, form)
        conta.entra_saldo_liquido = bool(form.get("sl")) and conta.moeda == "BRL"
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela(request, s, mes, hoje, erro=str(exc), status_code=400)
    return _voltar(mes)


@router.post("/{conta_id}/arquivar")
def arquivar(conta_id: int, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session)):
    _obter(s, conta_id).ativa = False
    s.commit()
    return _voltar(mes)


@router.post("/{conta_id}/desarquivar")
def desarquivar(conta_id: int, mes: str = Depends(mes_selecionado), s: Session = Depends(get_session)):
    _obter(s, conta_id).ativa = True
    s.commit()
    return RedirectResponse(f"/contas?mes={mes}&arquivadas=1", status_code=303)


# --- caixinhas: a divisão do dinheiro da conta por finalidade ---

def _tela_conta(request: Request, s: Session, conta: Conta, hoje: date, erro: str | None = None,
                status_code: int = 200):
    divisao = divisao_conta(s, conta)
    outras = [c for c in s.scalars(select(Conta).where(Conta.ativa, Conta.tipo != "cartao").order_by(Conta.nome))
              if c.id != conta.id]
    return render(request, "contas/detalhe.html", {
        "conta": conta, "d": divisao, "hoje": hoje, "erro": erro, "outras": outras,
        "metas": {m.id: m for m in s.scalars(select(Meta))},
        # objetivos que guardam nesta conta: uma caixinha pode fazer parte de um deles
        "objetivos_conta": list(s.scalars(select(Meta).where(Meta.conta_id == conta.id, Meta.tipo != "economia_mensal")
                                          .order_by(Meta.nome))),
        "objetivo_padrao": objetivo_da_conta(s, conta.id),
        # metas que guardam automático até o prazo: quanto falta e quanto vai por mês, na caixinha delas
        "planos": {c.id: plano_aporte(s, m, hoje)
                   for m in s.scalars(select(Meta).where(Meta.aporte_automatico, Meta.pagar_primeiro,
                                                         Meta.prazo.is_not(None)))
                   for c in caixinhas_da_meta(s, m)},
        "extratos": {l.caixinha.id: movimentos_caixinha(s, l.caixinha, 8) for l in divisao.caixinhas},
        "icones": ICONES_CAIXINHA, "cores": CORES,
        "esconder_mes_topo": True,
    }, status_code=status_code)


def _voltar_conta(conta: Conta) -> RedirectResponse:
    return RedirectResponse(f"/contas/{conta.id}", status_code=303)


def _caixinha(s: Session, conta: Conta, caixinha_id) -> Caixinha | None:
    """Vazio ou "livre" = o Livre da conta (None); senão a caixinha, que tem de ser desta conta."""
    texto = str(caixinha_id or "").strip()
    if texto in ("", "livre"):
        return None
    caixinha = s.get(Caixinha, int(texto)) if texto.isdigit() else None
    if caixinha is None or caixinha.conta_id != conta.id:
        raise HTTPException(status_code=404, detail="caixinha não encontrada")
    return caixinha


@router.get("/{conta_id}", response_class=HTMLResponse)
def detalhe(request: Request, conta_id: int, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    return _tela_conta(request, s, _obter(s, conta_id), hoje)


async def _acao_conta(request: Request, s: Session, conta: Conta, hoje: date, acao):
    form = await request.form()
    try:
        acao(form)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_conta(request, s, conta, hoje, erro=str(exc), status_code=422)
    return _voltar_conta(conta)


def _aparencia(form) -> dict:
    """Ícone e cor da caixinha; fora das listas do seletor vira None (mantém o que já tem)."""
    icone, cor = _campo(form, "icone"), _campo(form, "cor")
    return {"icone": icone if icone in ICONES_CAIXINHA else None, "cor": cor if cor in CORES else None}


def _valor(form) -> int:
    valor = parse_reais(_campo(form, "valor") or "0")
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    return valor


@router.post("/{conta_id}/caixinhas", response_class=HTMLResponse)
async def criar_caixinha_rota(request: Request, conta_id: int, s: Session = Depends(get_session),
                              hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    def criar(form):
        caixinha = criar_caixinha(s, conta, _campo(form, "nome"), hoje, inicial=parse_reais(_campo(form, "inicial") or "0"),
                                  **_aparencia(form))
        _marcar_oportunidade(conta, caixinha, form)
        meta_id = _campo(form, "meta_id")
        if meta_id.isdigit():
            vincular_caixinha(s, caixinha, int(meta_id))
    return await _acao_conta(request, s, conta, hoje, criar)


@router.post("/{conta_id}/caixinhas/{caixinha_id}/objetivo", response_class=HTMLResponse)
async def objetivo_caixinha_rota(request: Request, conta_id: int, caixinha_id: int,
                                 s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)
    return await _acao_conta(request, s, conta, hoje, lambda form: vincular_caixinha(
        s, caixinha, int(_campo(form, "meta_id")) if _campo(form, "meta_id").isdigit() else None))


@router.post("/{conta_id}/caixinhas/{caixinha_id}/editar", response_class=HTMLResponse)
async def editar_caixinha_rota(request: Request, conta_id: int, caixinha_id: int,
                               s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)

    def editar(form):
        editar_caixinha(s, caixinha, _campo(form, "nome"), **_aparencia(form))
        _marcar_oportunidade(conta, caixinha, form)
    return await _acao_conta(request, s, conta, hoje, editar)


def _marcar_oportunidade(conta: Conta, caixinha: Caixinha, form) -> None:
    """Reserva de oportunidade: só em conta de investimento (o que fica nela sai da alocação da carteira)."""
    if conta.tipo == "investimento":
        caixinha.oportunidade = _campo(form, "oportunidade") == "1"


@router.post("/{conta_id}/caixinhas/{caixinha_id}/excluir", response_class=HTMLResponse)
async def excluir_caixinha_rota(request: Request, conta_id: int, caixinha_id: int,
                                s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)
    return await _acao_conta(request, s, conta, hoje, lambda form: excluir_caixinha(s, caixinha))


@router.post("/{conta_id}/remanejar", response_class=HTMLResponse)
async def remanejar_rota(request: Request, conta_id: int, s: Session = Depends(get_session),
                         hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    return await _acao_conta(request, s, conta, hoje, lambda form: remanejar(
        s, conta, de=_caixinha(s, conta, form.get("de")), para=_caixinha(s, conta, form.get("para")),
        valor=_valor(form), data=_data(form, hoje)))


def _outra_conta(s: Session, form, conta: Conta) -> Conta:
    outra = s.get(Conta, int(_campo(form, "outra_id"))) if _campo(form, "outra_id").isdigit() else None
    if outra is None or outra.tipo == "cartao" or outra.id == conta.id:
        raise ValueError("escolha a outra conta")
    return outra


@router.post("/{conta_id}/caixinhas/{caixinha_id}/extrato/{tipo}/{movimento_id}/excluir", response_class=HTMLResponse)
async def excluir_movimento_rota(request: Request, conta_id: int, caixinha_id: int, tipo: str, movimento_id: int,
                                 s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)
    return await _acao_conta(request, s, conta, hoje, lambda form: excluir_movimento(
        s, caixinha, divisao=tipo == "divisao", movimento_id=movimento_id))


@router.post("/{conta_id}/caixinhas/{caixinha_id}/ajustar", response_class=HTMLResponse)
async def ajustar_caixinha_rota(request: Request, conta_id: int, caixinha_id: int,
                                s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)
    return await _acao_conta(request, s, conta, hoje, lambda form: ajustar_caixinha(
        s, caixinha, parse_reais(_campo(form, "novo_saldo")), _data(form, hoje)))


@router.post("/{conta_id}/caixinhas/{caixinha_id}/{acao}", response_class=HTMLResponse)
async def mover_caixinha_rota(request: Request, conta_id: int, caixinha_id: int, acao: str,
                              s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    """guardar: dinheiro de outra conta entra na caixinha; resgatar: sai dela para outra conta."""
    if acao not in ("guardar", "resgatar"):
        raise HTTPException(status_code=404)
    conta = _obter(s, conta_id)
    caixinha = _caixinha(s, conta, caixinha_id)

    def mover(form):
        data = _data(form, hoje)
        comum = dict(valor=_valor(form), data=data, efetivado=data <= hoje)
        if acao == "guardar" and _campo(form, "outra_id") == "aporte":
            # já está na caixinha do banco: só registra e soma no saldo da conta
            aportar(s, caixinha, valor=comum["valor"], data=data)
            return
        if acao == "guardar":
            pernas = guardar_na_caixinha(s, caixinha, origem=_outra_conta(s, form, conta), **comum)
        else:
            pernas = resgatar_da_caixinha(s, caixinha, destino=_outra_conta(s, form, conta), **comum)
        for lanc in pernas:
            lanc.meta_id = caixinha.meta_id  # caixinha de objetivo: o movimento aparece no histórico dele
    return await _acao_conta(request, s, conta, hoje, mover)
