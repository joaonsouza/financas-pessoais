from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dinheiro import parse_reais
from app.models import Conta, set_config
from app.services.categorias_padrao import semear_categorias
from app.services.config_inicial import PLANILHA_PADRAO, ler_config_inicial
from app.services.assinaturas import criar_assinatura
from app.web.deps import get_hoje, get_session
from app.web.templating import render

router = APIRouter(prefix="/setup")

PLANILHA = PLANILHA_PADRAO
LINHAS_EXTRAS = 3
LINHAS_CARTAO = 5


def _sugestoes():
    try:
        return ler_config_inicial(PLANILHA)
    except (OSError, KeyError, ValueError):
        return [], []


def _texto_reais(centavos: int) -> str:
    sinal = "-" if centavos < 0 else ""
    inteiro, frac = divmod(abs(centavos), 100)
    return f"{sinal}{inteiro},{frac:02d}"


def _campo(form, chave: str) -> str:
    return str(form.get(chave, "")).strip()


def _dia(form, chave: str, rotulo: str) -> int:
    texto = _campo(form, chave)
    if not texto.isdigit() or not 1 <= int(texto) <= 31:
        raise ValueError(f"{rotulo} deve ser um dia entre 1 e 31")
    return int(texto)


def _linhas_contas_do_form(form) -> list[dict]:
    return [
        {
            "incluir": bool(form.get(f"conta_incluir_{i}")),
            "nome": _campo(form, f"conta_nome_{i}"),
            "tipo": _campo(form, f"conta_tipo_{i}") or "corrente",
            "sl": bool(form.get(f"conta_sl_{i}")),
            "saldo": _campo(form, f"conta_saldo_{i}"),
        }
        for i in range(int(form.get("n_contas") or 0))
    ]


def _linhas_cartoes_do_form(form) -> list[dict]:
    return [
        {campo: _campo(form, f"cartao_{campo}_{i}") for campo in ("nome", "limite", "fechamento", "vencimento", "pagadora")}
        for i in range(LINHAS_CARTAO)
    ]


def _tela_contas(request: Request, contas: list[dict], cartoes: list[dict], data_inicio: str,
                 erro: str | None = None, status_code: int = 200):
    return render(request, "setup/contas.html", {
        "contas": contas, "cartoes": cartoes, "data_inicio": data_inicio, "erro": erro,
    }, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def passo_contas(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    if s.scalar(select(func.count(Conta.id))):
        return RedirectResponse("/", status_code=303)
    sugeridas, _ = _sugestoes()
    contas = [
        {"incluir": True, "nome": c.nome, "tipo": c.tipo, "sl": c.entra_saldo_liquido, "saldo": _texto_reais(c.saldo)}
        for c in sugeridas
    ] + [{"incluir": False, "nome": "", "tipo": "corrente", "sl": True, "saldo": ""} for _ in range(LINHAS_EXTRAS)]
    cartoes = [dict.fromkeys(("nome", "limite", "fechamento", "vencimento", "pagadora"), "") for _ in range(LINHAS_CARTAO)]
    return _tela_contas(request, contas, cartoes, hoje.isoformat())


@router.post("", response_class=HTMLResponse)
async def salvar_contas(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    linhas_contas = _linhas_contas_do_form(form)
    linhas_cartoes = _linhas_cartoes_do_form(form)
    try:
        data_inicio = date.fromisoformat(_campo(form, "data_inicio"))
        contas = []
        for linha in linhas_contas:
            if not (linha["incluir"] and linha["nome"]):
                continue
            if linha["tipo"] not in ("corrente", "investimento"):
                raise ValueError(f"tipo inválido para {linha['nome']}")
            contas.append(Conta(
                nome=linha["nome"], tipo=linha["tipo"], entra_saldo_liquido=linha["sl"],
                saldo_inicial=parse_reais(linha["saldo"] or "0"), data_saldo_inicial=data_inicio,
            ))
        if not contas:
            raise ValueError("cadastre pelo menos uma conta")
        nomes = [c.nome for c in contas] + [l["nome"] for l in linhas_cartoes if l["nome"]]
        if len(nomes) != len(set(nomes)):
            raise ValueError("há contas ou cartões com o mesmo nome")
        s.add_all(contas)
        s.flush()
        por_nome = {c.nome: c for c in contas}
        for i, linha in enumerate(linhas_cartoes):
            if not linha["nome"]:
                continue
            pagadora = por_nome.get(linha["pagadora"])
            if pagadora is None:
                raise ValueError(f"escolha a conta pagadora do {linha['nome']}")
            s.add(Conta(
                nome=linha["nome"], tipo="cartao", entra_saldo_liquido=False, saldo_inicial=0,
                data_saldo_inicial=data_inicio, limite=parse_reais(linha["limite"] or "0"),
                dia_fechamento=_dia(form, f"cartao_fechamento_{i}", "Fechamento"),
                dia_vencimento=_dia(form, f"cartao_vencimento_{i}", "Vencimento"),
                conta_pagamento_id=pagadora.id,
            ))
        semear_categorias(s)
        set_config(s, "data_inicio", data_inicio.isoformat())
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_contas(request, linhas_contas, linhas_cartoes, _campo(form, "data_inicio"),
                            erro=str(exc), status_code=400)
    return RedirectResponse("/setup/assinaturas", status_code=303)


def _tela_assinaturas(request: Request, s: Session, linhas: list[dict], erro: str | None = None,
                      status_code: int = 200):
    contas = list(s.scalars(select(Conta).order_by(Conta.tipo, Conta.nome)))
    return render(request, "setup/assinaturas.html", {"linhas": linhas, "contas": contas, "erro": erro},
                  status_code=status_code)


@router.get("/assinaturas", response_class=HTMLResponse)
def passo_assinaturas(request: Request, s: Session = Depends(get_session)):
    _, sugeridas = _sugestoes()
    linhas = [
        {"incluir": a.ativa, "nome": a.nome, "valor": _texto_reais(a.valor), "dia": "1", "conta": ""}
        for a in sugeridas
    ] + [{"incluir": False, "nome": "", "valor": "", "dia": "1", "conta": ""} for _ in range(LINHAS_EXTRAS)]
    return _tela_assinaturas(request, s, linhas)


@router.post("/assinaturas", response_class=HTMLResponse)
async def salvar_assinaturas(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    linhas = [
        {
            "incluir": bool(form.get(f"assinatura_incluir_{i}")),
            "nome": _campo(form, f"assinatura_nome_{i}"),
            "valor": _campo(form, f"assinatura_valor_{i}"),
            "dia": _campo(form, f"assinatura_dia_{i}"),
            "conta": _campo(form, f"assinatura_conta_{i}"),
        }
        for i in range(int(form.get("n_assinaturas") or 0))
    ]
    try:
        for i, linha in enumerate(linhas):
            if not (linha["incluir"] and linha["nome"]):
                continue
            conta = s.get(Conta, int(linha["conta"])) if linha["conta"].isdigit() else None
            if conta is None:
                raise ValueError(f"escolha a conta da assinatura {linha['nome']}")
            criar_assinatura(s, nome=linha["nome"], valor=parse_reais(linha["valor"]), conta_id=conta.id,
                             dia=_dia(form, f"assinatura_dia_{i}", "Dia"), hoje=hoje)
        s.commit()
    except ValueError as exc:
        s.rollback()
        return _tela_assinaturas(request, s, linhas, erro=str(exc), status_code=400)
    return RedirectResponse("/", status_code=303)
