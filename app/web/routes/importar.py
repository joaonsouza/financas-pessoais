import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.base import LeitorErro, ResultadoLeitura, detectar_leitor
from app.importers.pdf_ia import ler_com_ia
from app.models import Categoria, Conta, Importacao, PerfilCsv
from app.services.armazem_importacao import Armazem, Entrada
from app.services.categorizacao import criar_regra_de_correcao
from app.services.assinaturas import assinatura_da_sugestao, sugestoes_de_inclusao
from app.services.ia import criar_cliente, obter_chave, obter_modelo
from app.services.importacao import (
    Escolhas,
    PagamentoSemFatura,
    ROTULOS_STATUS,
    aplicar_importacao,
    desfazer_importacao,
    propor_importacao,
)
from app.services.openfinance.pluggy import ClientePluggy, OpenFinanceErro, criar_cliente, obter_chaves
from app.services.openfinance.sincronizar import (
    PropostaPorConta,
    StatusItem,
    itens_configurados,
    propor_para_conta,
    propor_sincronizacao,
    registrar_sincronizacao,
    status_dos_itens,
)
from app.web.deps import get_hoje, get_session
from app.web.templating import render

router = APIRouter(prefix="/importar")

MAX_CAMPOS_FORM = 20000  # uma revisão tem vários campos por linha
DIAS_ALERTA_IMPORTACAO = 7


def _armazem(request: Request) -> Armazem:
    if not hasattr(request.app.state, "armazem_importacao"):
        request.app.state.armazem_importacao = Armazem()
    return request.app.state.armazem_importacao


def _cliente_ia(request: Request):
    """Cliente injetável (testes usam um falso); em produção, o SDK com a chave do keyring."""
    return getattr(request.app.state, "cliente_ia", None) or criar_cliente()


def _contas(s: Session) -> list[Conta]:
    return list(s.scalars(select(Conta).where(Conta.ativa).order_by(Conta.nome)))


@dataclass
class SituacaoCartao:
    conta: Conta
    ultima: datetime | None
    dias: int | None
    atrasado: bool  # nunca importou ou última importação há mais de DIAS_ALERTA_IMPORTACAO dias


def _situacao_cartoes(s: Session, contas: list[Conta], hoje: date) -> list[SituacaoCartao]:
    ultimas = dict(s.execute(
        select(Importacao.conta_id, func.max(Importacao.criada_em)).where(~Importacao.desfeita)
        .group_by(Importacao.conta_id)).all())
    situacoes = []
    for c in contas:
        if c.tipo != "cartao":
            continue
        ultima = ultimas.get(c.id)
        dias = (hoje - ultima.date()).days if ultima else None
        situacoes.append(SituacaoCartao(c, ultima, dias, dias is None or dias > DIAS_ALERTA_IMPORTACAO))
    return situacoes


def _tela_inicial(request: Request, s: Session, hoje: date, *, erro: str | None = None,
                  token: str | None = None, entrada: Entrada | None = None, pode_ia: bool = False,
                  escolher_conta: list[Conta] | None = None, feita: Importacao | None = None,
                  status_code: int = 200):
    contas = _contas(s)
    por_id = {c.id: c for c in s.scalars(select(Conta))}
    historico = list(s.scalars(select(Importacao).order_by(Importacao.criada_em.desc(), Importacao.id.desc()).limit(20)))
    return render(request, "importar/index.html", {
        "contas": contas, "contas_por_id": por_id, "historico": historico, "erro": erro,
        "cartoes": _situacao_cartoes(s, contas, hoje),
        "token": token, "entrada": entrada, "pode_ia": pode_ia, "escolher_conta": escolher_conta,
        "feita": feita, "sugestoes": sugestoes_de_inclusao(s, hoje)[:6],
        "ia_configurada": obter_chave() is not None,
        "of_configurado": obter_chaves() is not None and bool(itens_configurados(s)),
    }, status_code=status_code)


def _tela_revisao(request: Request, s: Session, token: str, entrada: Entrada, *,
                  erros: dict[int, str] | None = None, previo: dict | None = None, status_code: int = 200):
    proposta = entrada.proposta
    categorias = list(s.scalars(select(Categoria).where(Categoria.ativa).order_by(Categoria.nome)))
    eh_pdf = entrada.conteudo[:5] == b"%PDF-"
    return render(request, "importar/revisao.html", {
        "token": token, "proposta": proposta, "entrada": entrada, "rotulos": ROTULOS_STATUS,
        "cat_despesa": [c for c in categorias if c.tipo == "despesa"],
        "cat_receita": [c for c in categorias if c.tipo == "receita"],
        "pode_ia": eh_pdf and proposta.resultado.origem != "pdf_ia" and bool(proposta.avisos),
        "erros": erros or {}, "previo": previo,
    }, status_code=status_code)


def _ler(request: Request, s: Session, entrada: Entrada, conta: Conta | None, ia: bool) -> ResultadoLeitura:
    if ia:
        if entrada.conteudo[:5] != b"%PDF-":
            raise LeitorErro("A leitura por IA só vale para arquivos PDF.")
        return ler_com_ia(entrada.conteudo, _cliente_ia(request), obter_modelo(s))
    leitor = detectar_leitor(entrada.nome, entrada.conteudo)
    if leitor is None:
        raise LeitorErro("Não reconheci o formato deste arquivo.")
    if leitor.nome == "csv_generico":
        perfil = s.scalar(select(PerfilCsv).where(PerfilCsv.conta_id == conta.id)) if conta else None
        if perfil is None:
            raise LeitorErro("CSV de layout desconhecido: escolha a conta; se ela ainda não tem "
                             "mapeamento de colunas salvo, este arquivo não pode ser lido.")
        return leitor.ler(entrada.conteudo, perfil, "cartao" if conta.tipo == "cartao" else "corrente")
    return leitor.ler(entrada.conteudo)


def _conta_do_arquivo(s: Session, resultado: ResultadoLeitura) -> list[Conta]:
    """Contas do tipo certo; se o identificador já foi aprendido, só a que combina."""
    tipo_cartao = resultado.tipo_conta == "cartao"
    do_tipo = [c for c in _contas(s) if (c.tipo == "cartao") == tipo_cartao]
    if resultado.identificador_conta:
        certas = [c for c in do_tipo if c.identificador_externo == resultado.identificador_conta]
        if certas:
            return certas
    return do_tipo


def _processar(request: Request, s: Session, hoje: date, token: str, entrada: Entrada, *,
               conta_id: int | None, ia: bool, ia_categorias: bool):
    conta = s.get(Conta, conta_id) if conta_id else None
    pode_ia = entrada.conteudo[:5] == b"%PDF-"
    try:
        if entrada.resultado is None or ia:
            entrada.resultado = _ler(request, s, entrada, conta, ia)
        resultado = entrada.resultado
        if conta is None:
            candidatas = _conta_do_arquivo(s, resultado)
            aprendida = [c for c in candidatas if c.identificador_externo
                         and c.identificador_externo == resultado.identificador_conta]
            if len(aprendida) == 1 or len(candidatas) == 1:
                conta = (aprendida or candidatas)[0]
            else:
                return _tela_inicial(request, s, hoje, token=token, entrada=entrada, pode_ia=pode_ia,
                                     escolher_conta=candidatas or [],
                                     erro=None if candidatas else "Não há conta ou cartão do tipo deste arquivo. "
                                     "Cadastre um antes de importar.")
        cliente = _cliente_ia(request) if ia_categorias else None
        entrada.proposta = propor_importacao(
            s, resultado, conta, hoje, nome_arquivo=entrada.nome, hash_arquivo=entrada.hash_arquivo,
            cliente_ia=cliente, modelo_ia=obter_modelo(s))
    except (LeitorErro, ValueError) as exc:
        return _tela_inicial(request, s, hoje, erro=str(exc), token=token, entrada=entrada, pode_ia=pode_ia,
                             status_code=422)
    except Exception as exc:  # falha de rede/API da IA etc.: mensagem clara, sem derrubar a tela
        return _tela_inicial(request, s, hoje, erro=f"Não foi possível ler o arquivo: {exc}", token=token,
                             entrada=entrada, pode_ia=pode_ia, status_code=422)
    return RedirectResponse(f"/importar/{token}", status_code=303)


@router.get("", response_class=HTMLResponse)
def importar(request: Request, feita: int | None = None, s: Session = Depends(get_session),
             hoje: date = Depends(get_hoje)):
    return _tela_inicial(request, s, hoje, feita=s.get(Importacao, feita) if feita else None)


@router.post("")
async def enviar(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form(max_fields=MAX_CAMPOS_FORM)
    arquivo = form.get("arquivo")
    conteudo = await arquivo.read() if arquivo is not None and hasattr(arquivo, "read") else b""
    if not conteudo:
        return _tela_inicial(request, s, hoje, erro="Escolha um arquivo para importar.", status_code=422)
    entrada = Entrada(nome=arquivo.filename or "arquivo", conteudo=conteudo,
                      hash_arquivo=hashlib.sha256(conteudo).hexdigest())
    token = _armazem(request).guardar(entrada)
    conta_id = str(form.get("conta_id", ""))
    return _processar(request, s, hoje, token, entrada, conta_id=int(conta_id) if conta_id.isdigit() else None,
                      ia=False, ia_categorias=bool(form.get("ia_categorias")))


@router.post("/recorrencia")
async def virar_assinatura(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    form = await request.form()
    descricao, conta_id = str(form.get("descricao", "")), int(str(form.get("conta_id", "0")) or 0)
    sug = next((x for x in sugestoes_de_inclusao(s, hoje) if x.descricao == descricao and x.conta_id == conta_id), None)
    if sug is not None:
        try:
            assinatura_da_sugestao(s, sug, hoje)
            s.commit()
        except ValueError:
            s.rollback()
    return RedirectResponse("/importar", status_code=303)


@router.post("/{importacao_id:int}/desfazer")
def desfazer(importacao_id: int, s: Session = Depends(get_session)):
    imp = s.get(Importacao, importacao_id)
    if imp is not None and not imp.desfeita:
        desfazer_importacao(s, imp)
        s.commit()
    return RedirectResponse("/importar", status_code=303)


@router.get("/{token}", response_class=HTMLResponse)
def revisao(token: str, request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    entrada = _armazem(request).obter(token)
    if entrada is None or entrada.proposta is None:
        return _tela_inicial(request, s, hoje, erro="Essa importação expirou ou não existe mais. "
                             "Envie o arquivo de novo.", status_code=404)
    return _tela_revisao(request, s, token, entrada)


@router.post("/{token}/ler")
async def ler_de_novo(token: str, request: Request, s: Session = Depends(get_session),
                      hoje: date = Depends(get_hoje)):
    """Continua um upload guardado: escolha da conta ou "Tentar com IA" (só quando o usuário clica)."""
    entrada = _armazem(request).obter(token)
    if entrada is None:
        return _tela_inicial(request, s, hoje, erro="Essa importação expirou. Envie o arquivo de novo.",
                             status_code=404)
    form = await request.form()
    conta_id = str(form.get("conta_id", ""))
    return _processar(request, s, hoje, token, entrada, conta_id=int(conta_id) if conta_id.isdigit() else None,
                      ia=form.get("ia") == "1", ia_categorias=bool(form.get("ia_categorias")))


@router.post("/{token}/confirmar")
async def confirmar(token: str, request: Request, s: Session = Depends(get_session),
                    hoje: date = Depends(get_hoje)):
    armazem = _armazem(request)
    entrada = armazem.obter(token)
    if entrada is None or entrada.proposta is None:
        return _tela_inicial(request, s, hoje, erro="Essa importação expirou. Envie o arquivo de novo.",
                             status_code=404)
    proposta = entrada.proposta
    form = await request.form(max_fields=MAX_CAMPOS_FORM)
    escolhas = Escolhas(marcadas=set(), criar_parcelas_futuras=bool(form.get("criar_futuras")))
    regras: list[tuple[str, int]] = []
    previo = {"marcadas": escolhas.marcadas, "categorias": escolhas.categorias, "faturas": {}, "regras": set(),
              "criar_futuras": escolhas.criar_parcelas_futuras}
    for ln in proposta.linhas:
        i = ln.indice
        if form.get(f"marcada_{i}"):
            escolhas.marcadas.add(i)
        if f"categoria_{i}" in form:
            valor = str(form.get(f"categoria_{i}", ""))
            escolhas.categorias[i] = int(valor) if valor.isdigit() else None
            if form.get(f"regra_{i}") and escolhas.categorias[i]:
                regras.append((ln.transacao.descricao, escolhas.categorias[i]))
        if form.get(f"regra_{i}"):
            previo["regras"].add(i)
        fatura = str(form.get(f"fatura_{i}", ""))
        previo["faturas"][i] = fatura
        if fatura.isdigit():
            escolhas.faturas[i] = int(fatura)
        elif fatura == "despesa":
            escolhas.nao_e_pagamento.add(i)
        elif fatura.startswith("criar:"):
            _, cartao_id, competencia = fatura.split(":", 2)
            if cartao_id.isdigit():
                escolhas.criar_faturas[i] = (int(cartao_id), competencia)
    try:
        proposta.conta = s.merge(proposta.conta)  # veio de outra sessão: o vínculo aprendido precisa gravar
        imp = aplicar_importacao(s, proposta, escolhas, hoje)
        for descricao, categoria_id in regras:
            criar_regra_de_correcao(s, descricao, categoria_id)
        if entrada.account_id:
            registrar_sincronizacao(s, entrada.account_id, hoje)
        s.commit()
    except PagamentoSemFatura as exc:
        s.rollback()
        return _tela_revisao(request, s, token, entrada, erros=exc.erros, previo=previo, status_code=422)
    except Exception as exc:
        s.rollback()
        return _tela_inicial(request, s, hoje, erro=f"Nada foi gravado: {exc}", status_code=500)
    armazem.remover(token)
    return RedirectResponse(f"/importar?feita={imp.id}", status_code=303)


# ---------- Open Finance (Pluggy) ----------

@dataclass
class GrupoSincronizacao:
    """Uma sincronização em andamento: uma proposta por conta da Pluggy, cada uma com seu token de revisão."""
    pcs: list[PropostaPorConta]
    status: list[StatusItem]
    tokens: dict[int, str] = field(default_factory=dict)
    feitas: dict[int, tuple[int, int, int]] = field(default_factory=dict)  # (novas, conciliadas, duplicadas)
    avisos: dict[int, str] = field(default_factory=dict)


def _cliente_pluggy(request: Request) -> ClientePluggy | None:
    """Cliente injetável (testes usam um falso); em produção, as chaves do keyring."""
    return getattr(request.app.state, "cliente_pluggy", None) or criar_cliente()


def _guardar_entrada(request: Request, grupo: GrupoSincronizacao, idx: int) -> None:
    pc = grupo.pcs[idx]
    entrada = Entrada(nome=proposta_nome(pc), conteudo=b"", hash_arquivo="", resultado=pc.resultado,
                      proposta=pc.proposta, account_id=pc.account_id)
    grupo.tokens[idx] = _armazem(request).guardar(entrada)


def proposta_nome(pc: PropostaPorConta) -> str:
    return f"Open Finance · {pc.nome_pluggy}"


def _grupo(request: Request, token: str) -> GrupoSincronizacao | None:
    entrada = _armazem(request).obter(token)
    return entrada.resultado if entrada is not None and isinstance(entrada.resultado, GrupoSincronizacao) else None


def _tela_grupo(request: Request, s: Session, token: str, grupo: GrupoSincronizacao, *,
                erro: str | None = None, status_code: int = 200):
    armazem = _armazem(request)
    return render(request, "importar/openfinance.html", {
        "token": token, "grupo": grupo, "erro": erro,
        "ativos": {i: armazem.obter(t) is not None for i, t in grupo.tokens.items()},
        "pode_todas": any(pc.proposta is not None and i not in grupo.feitas and armazem.obter(grupo.tokens.get(i, "")) is not None
                          for i, pc in enumerate(grupo.pcs)),
    }, status_code=status_code)


def _confirmar_padrao(request: Request, s: Session, grupo: GrupoSincronizacao, idx: int, hoje: date) -> None:
    """Aplica a proposta da conta com as escolhas padrão. Pagamento sem fatura fica para revisão manual."""
    armazem = _armazem(request)
    pc, token = grupo.pcs[idx], grupo.tokens.get(idx)
    entrada = armazem.obter(token) if token else None
    if pc.proposta is None or entrada is None or idx in grupo.feitas:
        return
    try:
        pc.proposta.conta = s.merge(pc.proposta.conta)  # veio de outra sessão: o vínculo aprendido precisa gravar
        imp = aplicar_importacao(s, pc.proposta, None, hoje)
        registrar_sincronizacao(s, pc.account_id, hoje)
        s.commit()
    except PagamentoSemFatura:
        s.rollback()
        grupo.avisos[idx] = "Há pagamento de fatura sem fatura definida: abra a revisão para escolher."
        return
    except Exception as exc:
        s.rollback()
        grupo.avisos[idx] = f"Nada foi gravado: {exc}"
        return
    grupo.avisos.pop(idx, None)
    grupo.feitas[idx] = (imp.qtd_novas, imp.qtd_conciliadas, imp.qtd_duplicadas)
    armazem.remover(token)


@router.post("/sincronizar")
def sincronizar_openfinance(request: Request, s: Session = Depends(get_session), hoje: date = Depends(get_hoje)):
    cliente = _cliente_pluggy(request)
    itens = itens_configurados(s)
    if cliente is None or not itens:
        return _tela_inicial(request, s, hoje, status_code=422,
                             erro="Configure as chaves e ao menos um item em Configurações › Open Finance.")
    try:
        pcs = propor_sincronizacao(s, cliente, itens, hoje)
        s.commit()  # guarda o status dos itens
    except OpenFinanceErro as exc:
        s.rollback()
        return _tela_inicial(request, s, hoje, erro=str(exc), status_code=502)
    except ValueError as exc:
        s.rollback()
        return _tela_inicial(request, s, hoje, erro=str(exc), status_code=422)
    grupo = GrupoSincronizacao(pcs=pcs, status=list(status_dos_itens(s).values()))
    for i, pc in enumerate(pcs):
        if pc.proposta is not None:
            _guardar_entrada(request, grupo, i)
    token = _armazem(request).guardar(Entrada(nome="Open Finance", conteudo=b"", hash_arquivo="", resultado=grupo))
    return RedirectResponse(f"/importar/sincronizacao/{token}", status_code=303)


@router.get("/sincronizacao/{token}", response_class=HTMLResponse)
def revisao_sincronizacao(token: str, request: Request, s: Session = Depends(get_session),
                          hoje: date = Depends(get_hoje)):
    grupo = _grupo(request, token)
    if grupo is None:
        return _tela_inicial(request, s, hoje, status_code=404,
                             erro="Essa sincronização expirou ou não existe mais. Sincronize de novo.")
    return _tela_grupo(request, s, token, grupo)


@router.post("/sincronizacao/{token}/{idx:int}/vincular")
async def vincular_conta_pluggy(token: str, idx: int, request: Request, s: Session = Depends(get_session),
                                hoje: date = Depends(get_hoje)):
    grupo = _grupo(request, token)
    if grupo is None or not 0 <= idx < len(grupo.pcs):
        return _tela_inicial(request, s, hoje, status_code=404, erro="Essa sincronização expirou. Sincronize de novo.")
    form = await request.form()
    valor = str(form.get("conta_id", ""))
    conta = s.get(Conta, int(valor)) if valor.isdigit() else None
    pc = grupo.pcs[idx]
    if conta is None or pc.resultado is None:
        return _tela_grupo(request, s, token, grupo, erro="Escolha a conta ou o cartão.", status_code=422)
    try:
        propor_para_conta(s, pc, conta, hoje)
    except ValueError as exc:
        return _tela_grupo(request, s, token, grupo, erro=str(exc), status_code=422)
    _guardar_entrada(request, grupo, idx)
    return RedirectResponse(f"/importar/sincronizacao/{token}", status_code=303)


@router.post("/sincronizacao/{token}/confirmar-todas")
def confirmar_todas_pluggy(token: str, request: Request, s: Session = Depends(get_session),
                           hoje: date = Depends(get_hoje)):
    grupo = _grupo(request, token)
    if grupo is None:
        return _tela_inicial(request, s, hoje, status_code=404, erro="Essa sincronização expirou. Sincronize de novo.")
    for i in range(len(grupo.pcs)):
        _confirmar_padrao(request, s, grupo, i, hoje)
    return RedirectResponse(f"/importar/sincronizacao/{token}", status_code=303)


@router.post("/sincronizacao/{token}/{idx:int}/confirmar")
def confirmar_uma_pluggy(token: str, idx: int, request: Request, s: Session = Depends(get_session),
                         hoje: date = Depends(get_hoje)):
    grupo = _grupo(request, token)
    if grupo is None or not 0 <= idx < len(grupo.pcs):
        return _tela_inicial(request, s, hoje, status_code=404, erro="Essa sincronização expirou. Sincronize de novo.")
    _confirmar_padrao(request, s, grupo, idx, hoje)
    return RedirectResponse(f"/importar/sincronizacao/{token}", status_code=303)
