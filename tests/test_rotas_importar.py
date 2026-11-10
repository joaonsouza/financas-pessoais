"""Telas de importação: upload, revisão, confirmação, histórico/desfazer, IA sob demanda, token com validade."""
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from app.models import Importacao, Lancamento, RegraCategoria
from app.services.armazem_importacao import Armazem, Entrada
from app.web.templating import MENU
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento
from tests.test_ia import ClienteFalso, _ia, _pdf, _resposta

OFX = (Path(__file__).parent / "fixtures" / "importacao" / "conta_sintetica.ofx").read_bytes()


def _enviar(client, conteudo=OFX, nome="extrato.ofx", **dados):
    return client.post("/importar", files={"arquivo": (nome, conteudo)}, data=dados, follow_redirects=False)


def _token(resposta) -> str:
    assert resposta.status_code == 303, resposta.text
    return resposta.headers["location"].rsplit("/", 1)[1]


def _marcadas(proposta) -> dict:
    return {f"marcada_{ln.indice}": "1" for ln in proposta.linhas if ln.marcada}


def test_menu_tem_importar_antes_de_configuracoes(client, db):
    nomes = [rotulo for rotulo, _ in MENU]
    assert nomes.index("Cartões") + 4 == nomes.index("Importar") == nomes.index("Configurações") - 1  # Planejamento, Relatórios e Investimentos no meio
    nova_conta(db)
    db.commit()
    html = client.get("/importar").text
    assert 'data-lucide="upload"' in html and "Enviar arquivo" in html and "Nenhuma importação ainda." in html


def test_fluxo_completo_upload_revisao_confirmacao_reimportacao_e_desfazer(client, db, app_teste):
    conta = nova_conta(db, "Banco Teste")
    db.commit()
    token = _token(_enviar(client, conta_id=str(conta.id)))
    revisao = client.get(f"/importar/{token}")
    assert revisao.status_code == 200
    assert "Revisar importação" in revisao.text and "Nova" in revisao.text and "Banco Teste" in revisao.text
    proposta = app_teste.state.armazem_importacao.obter(token).proposta
    # confirma com todas as linhas marcadas por padrão
    r = client.post(f"/importar/{token}/confirmar", data={**_marcadas(proposta), "criar_futuras": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/importar?feita=")
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == len(proposta.linhas) + 1  # +1 parcela futura (2/3)
    imp = db.scalars(select(Importacao)).one()
    assert imp.qtd_novas == len(proposta.linhas) and imp.conta_id == conta.id
    assert app_teste.state.armazem_importacao.obter(token) is None  # arquivo some da memória
    pagina = client.get(r.headers["location"]).text
    assert "Importação concluída" in pagina and "extrato.ofx" in pagina and "Desfazer" in pagina

    # a conta aprendeu o identificador: reimportar sem escolher a conta e tudo vem duplicado
    token2 = _token(_enviar(client))
    p2 = app_teste.state.armazem_importacao.obter(token2).proposta
    assert p2.conta.id == conta.id and [ln.status for ln in p2.linhas] == ["duplicada"] * len(p2.linhas)
    assert "já foi importado" in client.get(f"/importar/{token2}").text

    r = client.post(f"/importar/{imp.id}/desfazer", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == 0
    assert db.get(Importacao, imp.id).desfeita


def test_criar_regra_e_categoria_escolhida_na_revisao(client, db, app_teste):
    conta = nova_conta(db, "Banco Teste")
    cat = nova_categoria(db, "Compras")
    db.commit()
    token = _token(_enviar(client, conta_id=str(conta.id)))
    proposta = app_teste.state.armazem_importacao.obter(token).proposta
    idx = next(ln.indice for ln in proposta.linhas if ln.tipo == "despesa")
    dados = {**_marcadas(proposta), f"categoria_{idx}": str(cat.id), f"regra_{idx}": "1"}
    client.post(f"/importar/{token}/confirmar", data=dados, follow_redirects=False)
    db.expire_all()
    assert db.scalars(select(Lancamento).where(Lancamento.categoria_id == cat.id)).all()
    assert db.scalars(select(RegraCategoria)).one().categoria_id == cat.id


def test_desmarcar_na_revisao_nao_importa(client, db, app_teste):
    conta = nova_conta(db, "Banco Teste")
    db.commit()
    token = _token(_enviar(client, conta_id=str(conta.id)))
    client.post(f"/importar/{token}/confirmar", data={"marcada_0": "1"}, follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == 1


def test_conta_nao_descoberta_pede_a_escolha_e_continua_pelo_token(client, db, app_teste):
    a, b = nova_conta(db, "Conta A"), nova_conta(db, "Conta B")
    db.commit()
    r = _enviar(client)
    assert r.status_code == 200 and "De qual conta ou cartão" in r.text
    token = next(iter(app_teste.state.armazem_importacao._itens))
    assert _token(client.post(f"/importar/{token}/ler", data={"conta_id": str(b.id)}, follow_redirects=False)) == token
    assert app_teste.state.armazem_importacao.obter(token).proposta.conta.id == b.id


def test_arquivo_vazio_e_formato_desconhecido(client, db):
    nova_conta(db)
    db.commit()
    assert _enviar(client, b"").status_code == 422
    r = _enviar(client, b"qualquer coisa", nome="notas.txt")
    assert r.status_code == 422 and "Não reconheci o formato" in r.text
    assert 'name="ia"' not in r.text  # não é PDF: sem botão de IA


def test_tipo_de_arquivo_errado_para_a_conta_mostra_erro(client, db):
    cartao = novo_cartao(db, "Cartão X", pagadora=nova_conta(db, "Corrente"))
    db.commit()
    r = _enviar(client, conta_id=str(cartao.id))
    assert r.status_code == 422 and "cartão" in r.text


# ---------------------------------------------------------------- IA só no clique

def test_pdf_desconhecido_oferece_ia_que_so_roda_quando_o_usuario_clica(client, db, app_teste):
    corrente = nova_conta(db, "Corrente")
    cartao = novo_cartao(db, "Cartão IA", pagadora=corrente)
    db.commit()
    cliente = ClienteFalso(_resposta(_ia()))
    app_teste.state.cliente_ia = cliente
    r = _enviar(client, _pdf("Documento de um banco que o app não conhece"), nome="fatura.pdf",
                conta_id=str(cartao.id))
    assert r.status_code == 422 and "Tentar com IA" in r.text
    assert cliente.chamadas == []  # nenhuma chamada sem clique
    token = next(iter(app_teste.state.armazem_importacao._itens))
    r2 = client.post(f"/importar/{token}/ler", data={"ia": "1", "conta_id": str(cartao.id)}, follow_redirects=False)
    assert _token(r2) == token and len(cliente.chamadas) == 1
    revisao = client.get(f"/importar/{token}").text
    assert "Lido por IA" in revisao and "LOJA A" in revisao and "LOJA B" in revisao


def test_falha_da_ia_mostra_erro_claro_e_nao_derruba(client, db, app_teste):
    cartao = novo_cartao(db, "Cartão IA", pagadora=nova_conta(db, "Corrente"))
    db.commit()
    app_teste.state.cliente_ia = ClienteFalso(_resposta(None, "refusal"), _resposta(None, "refusal"))
    _enviar(client, _pdf("Documento desconhecido"), nome="f.pdf", conta_id=str(cartao.id))
    token = next(iter(app_teste.state.armazem_importacao._itens))
    r = client.post(f"/importar/{token}/ler", data={"ia": "1", "conta_id": str(cartao.id)})
    assert r.status_code == 422 and "recusou" in r.text and "Tentar com IA" in r.text


# ---------------------------------------------------------------- validade do token

def test_token_expira_depois_de_1h_e_o_arquivo_fica_so_em_memoria():
    agora = [1000.0]
    arm = Armazem(ttl=3600, relogio=lambda: agora[0])
    token = arm.guardar(Entrada(nome="a.ofx", conteudo=b"x", hash_arquivo="h"))
    agora[0] += 3599
    assert arm.obter(token) is not None
    agora[0] += 2
    assert arm.obter(token) is None


def test_revisao_com_token_expirado_ou_invalido(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/importar/token-que-nao-existe")
    assert r.status_code == 404 and "expirou" in r.text
    assert client.post("/importar/token-que-nao-existe/confirmar", data={}).status_code == 404


# ---------------------------------------------------------------- assinaturas sugeridas

def test_card_cadastrar_assinatura_cria_assinatura_e_nao_recorrencia(client, db):
    from app.models import Assinatura, Recorrencia
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db, "Banco Teste"))
    for mes in (8, 9):
        novo_lancamento(db, cartao, date(2026, mes, 9), -3990, descricao="STREAMING FICTICIO")
    db.commit()
    html = client.get("/importar").text
    assert "Cadastrar assinatura?" in html and "STREAMING FICTICIO" in html and "R$ 478,80" in html
    r = client.post("/importar/recorrencia", data={"descricao": "STREAMING FICTICIO", "conta_id": str(cartao.id)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert [a.nome for a in db.scalars(select(Assinatura))] == ["STREAMING FICTICIO"]
    assert db.scalars(select(Recorrencia)).all() == []
    assert "Cadastrar assinatura?" not in client.get("/importar").text


def test_pagamento_de_fatura_sem_fatura_re_renderiza_422_com_erro_na_linha_e_preserva_escolhas(client, db, app_teste):
    from app.importers.base import ResultadoLeitura, TransacaoBruta
    from app.services.importacao import propor_importacao
    corrente = nova_conta(db, "Banco Um")
    cartao = novo_cartao(db, "Cartão Alfa", pagadora=corrente)
    mercado = nova_categoria(db, "Mercado")
    db.commit()
    res = ResultadoLeitura(origem="ofx", banco="b", tipo_conta="corrente", identificador_conta="ID-FICT-9", transacoes=[
        TransacaoBruta(data=date(2026, 9, 10), descricao="PAGAMENTO DE FATURA", descricao_original="PAGAMENTO DE FATURA",
                       valor=-25000, tipo_sugerido="pagamento_fatura"),
        TransacaoBruta(data=date(2026, 9, 11), descricao="PADARIA FICTICIA", descricao_original="PADARIA FICTICIA",
                       valor=-1500)])
    entrada = Entrada(nome="x.ofx", conteudo=b"x", hash_arquivo="h-x")
    entrada.resultado = res
    entrada.proposta = propor_importacao(db, res, corrente, date(2026, 10, 6), nome_arquivo="x.ofx")
    token = app_teste.state.armazem_importacao.guardar(entrada) if hasattr(app_teste.state, "armazem_importacao")         else None
    if token is None:
        app_teste.state.armazem_importacao = Armazem()
        token = app_teste.state.armazem_importacao.guardar(entrada)
    r = client.post(f"/importar/{token}/confirmar", data={
        "marcada_0": "1", "marcada_1": "1", "categoria_1": str(mercado.id), "fatura_0": ""})
    assert r.status_code == 422
    assert "Escolha a fatura paga" in r.text and "Nada foi gravado" in r.text
    assert "Não é pagamento de fatura (lançar como despesa)" in r.text
    assert f"Criar fatura do cartão Cartão Alfa em 2026-09" in r.text
    assert f'name="categoria_1" value="{mercado.id}"' in r.text  # escolha preservada
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == 0 and db.scalar(select(func.count(Importacao.id))) == 0
    # a mesma tela, com "não é pagamento", grava as duas linhas
    r = client.post(f"/importar/{token}/confirmar", data={
        "marcada_0": "1", "marcada_1": "1", "fatura_0": "despesa"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(func.count(Lancamento.id))) == 2
