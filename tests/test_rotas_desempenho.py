from datetime import date
from types import SimpleNamespace
from app.models import Categoria, Conta
from app.services.desempenho.analise_ia import ItemSugestaoIA, SaidaAnaliseIA
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 15)


def test_rota_desempenho_renderiza_com_e_sem_dados(client, db):
    # Precisa de ao menos uma conta para passar o middleware de setup inicial
    conta = nova_conta(db, "Carteira", saldo=100000, inicio=date(2026, 9, 1))
    db.commit()

    # 1. Sem lançamentos adicionais
    resp = client.get("/desempenho?mes=2026-10")
    assert resp.status_code == 200
    texto = resp.text
    assert "Desempenho &amp; Forecast" in texto or "Desempenho & Forecast" in texto
    assert 'id="placar"' in texto
    assert 'id="alertas"' in texto
    assert 'id="insights"' in texto
    assert 'id="previsao"' in texto
    assert 'id="historico"' in texto
    assert 'id="patrimonio"' in texto

    # 2. Com dados
    cat = nova_categoria(db, "Alimentação")
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")
    novo_lancamento(db, conta, date(2026, 10, 5), -150000, tipo="despesa", categoria=cat)
    db.commit()

    resp2 = client.get("/desempenho?mes=2026-10")
    assert resp2.status_code == 200
    assert "Receitas" in resp2.text
    assert "Despesas" in resp2.text


def test_redirecionamento_relatorios_para_desempenho(client, db):
    nova_conta(db, "Conta")
    db.commit()

    resp = client.get("/relatorios?mes=2026-11", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/desempenho?mes=2026-11"


def test_seletor_janela_persiste_e_renderiza(client, db):
    nova_conta(db, "Conta")
    db.commit()

    resp = client.get("/desempenho?mes=2026-10&janela=6")
    assert resp.status_code == 200
    assert "6m" in resp.text


def test_drilldown_categoria(client, db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 9, 1))
    cat = nova_categoria(db, "Lazer")
    novo_lancamento(db, conta, date(2026, 10, 2), -50000, categoria=cat)
    db.commit()

    resp = client.get(f"/desempenho/categoria/{cat.id}?mes=2026-10")
    assert resp.status_code == 200
    assert "Lazer" in resp.text
    assert "grafico-drilldown-cat" in resp.text


class ClienteIAFalso:
    def __init__(self, resposta):
        self._resposta = resposta
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        return self._resposta


def test_post_analise_ia(client, db, monkeypatch):
    conta = nova_conta(db, "Conta", inicio=date(2026, 9, 1))
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")
    db.commit()

    saida_mock = SaidaAnaliseIA(
        resumo="Diagnóstico excelente gerado com sucesso.",
        pontos_fortes=["Receita sólida"],
        pontos_de_atencao=["Manter disciplina"],
        sugestoes=[ItemSugestaoIA(acao="Investir sobra", impacto_mensal_estimado=20000)],
    )
    cliente = ClienteIAFalso(SimpleNamespace(stop_reason="end_turn", parsed_output=saida_mock))
    monkeypatch.setattr("app.services.desempenho.analise_ia.cliente_e_modelo_de_texto", lambda s: (cliente, "modelo-mock"))

    resp = client.post("/desempenho/analise?mes=2026-10", data={"forcar": "1"})
    assert resp.status_code == 200
    assert "Diagnóstico excelente gerado com sucesso." in resp.text
    assert "Receita sólida" in resp.text


def test_kpi_sem_mes_anterior_nao_mostra_none(client, db):
    conta = nova_conta(db, "Carteira", saldo=100000, inicio=date(2026, 10, 1))
    novo_lancamento(db, conta, date(2026, 10, 2), 500000, tipo="receita")  # mês anterior sem dados: sem percentual
    db.commit()
    texto = client.get("/desempenho?mes=2026-10").text
    assert "None%" not in texto and "None" not in texto.split('id="placar"')[0]


def test_previsao_de_12_meses_e_calculada_uma_vez_por_pagina(client, db, monkeypatch):
    from app.services.desempenho import alertas_previstos, insights, previsao
    from app.web.routes import desempenho as rota
    nova_conta(db, "Carteira", saldo=100000, inicio=date(2026, 9, 1))
    db.commit()
    chamadas = []
    real = previsao.calcular_previsao

    def contada(*a, **k):
        chamadas.append(k.get("horizonte_meses"))
        return real(*a, **k)
    for modulo in (rota, alertas_previstos, insights):
        monkeypatch.setattr(modulo, "calcular_previsao", contada)
    assert client.get("/desempenho?mes=2026-10&janela=12").status_code == 200
    assert chamadas == [12]  # alertas e insights reaproveitam a mesma previsão


def test_desempenho_secao_inflacao_vazia(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/desempenho?mes=2026-09").text
    assert 'id="inflacao"' in texto and 'href="#inflacao"' in texto
    assert "se preenche sozinha" in texto


def test_desempenho_secao_inflacao_com_dados(client, db):
    conta = nova_conta(db)
    aluguel = nova_categoria(db, "Aluguel", inflacao="fixo")
    internet = nova_categoria(db, "Internet/Celular", inflacao="fixo")
    for mes, valor in (("2026-07", 100000), ("2026-08", 100000), ("2026-09", 110000)):
        novo_lancamento(db, conta, date.fromisoformat(f"{mes}-10"), -valor, tipo="despesa", categoria=aluguel,
                        descricao="Aluguel")
        novo_lancamento(db, conta, date.fromisoformat(f"{mes}-15"), -10000, tipo="despesa", categoria=internet,
                        descricao="Claro")
    db.commit()
    texto = client.get("/desempenho?mes=2026-09").text   # HOJE_TESTE = 05/10/2026: setembro é o último fechado
    assert "Minha inflação" in texto and "9,09%" in texto  # 1.000/1.100 × 10%
    assert "Aluguel" in texto and "10,00%" in texto
    assert "desde ago/26" in texto
