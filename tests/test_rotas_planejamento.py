import json
import re
from datetime import date

from sqlalchemy import select

from app.services.metas import sincronizar_caixinha_meta
from app.models import AjusteMes, Categoria, Lancamento, Meta, Orcamento, Recorrencia, get_config
from app.services.categorias_padrao import semear_categorias
from tests.factories import nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)


def _cenario(db):
    semear_categorias(db)
    conta = nova_conta(db, "Nubank")
    novo_lancamento(db, conta, date(2026, 10, 1), 500000, descricao="Salário")
    mercado = db.scalar(select(Categoria).where(Categoria.nome == "Mercado"))
    aluguel = db.scalar(select(Categoria).where(Categoria.nome == "Aluguel"))
    novo_lancamento(db, conta, date(2026, 10, 2), -90000, categoria=mercado, descricao="Mercado")
    novo_lancamento(db, conta, date(2026, 10, 3), -120000, categoria=aluguel, descricao="Aluguel")
    db.commit()
    return conta, mercado, aluguel


def test_menu_tem_as_novas_telas_depois_de_cartoes(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/").text
    assert texto.index("</i>Cartões") < texto.index("</i>Planejamento") < texto.index("</i>Desempenho")
    assert "</i>Objetivos" not in texto
    for caminho in ("/planejamento", "/objetivos", "/desempenho"):
        assert client.get(caminho).status_code == 200


def test_planejamento_renderiza_com_os_dados(client, db):
    _cenario(db)
    texto = client.get("/planejamento?mes=2026-10").text
    assert "Orçamento do mês" in texto and "Painel 50/30/20" in texto and "De cada R$ 100" not in texto
    assert "R$ 5.000,00" in texto            # receitas
    assert "Provisões anuais" not in texto and "Ajuste do mês" in texto


def test_definir_limite_e_faixas_na_tela(client, db):
    _, mercado, _ = _cenario(db)
    r = client.post("/planejamento/limite", data={"mes": "2026-10", "categoria_id": str(mercado.id), "limite": "1.000,00"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/planejamento?mes=2026-10"
    texto = client.get("/planejamento?mes=2026-10").text
    assert "R$ 900,00 de R$ 1.000,00 (90%)" in texto and "atenção: acima de 80%" in texto
    client.post("/planejamento/limite", data={"mes": "2026-10", "categoria_id": str(mercado.id), "limite": "0"})
    db.expire_all()
    assert db.scalar(select(Orcamento)) is None


def test_teto_e_copiar_do_mes_anterior(client, db):
    _cenario(db)
    client.post("/planejamento/limite", data={"mes": "2026-09", "categoria_id": "", "limite": "2000,00"})
    texto = client.get("/planejamento?mes=2026-10").text
    assert "Copiar do mês anterior" in texto
    client.post("/planejamento/copiar", data={"mes": "2026-10"})
    texto = client.get("/planejamento?mes=2026-10").text
    assert "R$ 2.100,00 de R$ 2.000,00 (105%)" in texto and "limite atingido" in texto


def test_metas_503020_validam_soma_100(client, db):
    _cenario(db)
    r = client.post("/planejamento/metas-503020", data={"mes": "2026-10", "necessidades": "50", "desejos": "30", "terceira": "30"})
    assert r.status_code == 422 and "somar 100%" in r.text
    assert 'value="30"' in r.text  # mantém o digitado
    assert client.post("/planejamento/metas-503020", data={"mes": "2026-10", "necessidades": "60", "desejos": "20", "terceira": "20"},
                       follow_redirects=False).status_code == 303
    db.expire_all()
    assert get_config(db, "metas_503020") == [60, 20, 20]


def test_ajuste_do_mes_um_por_mes(client, db):
    _cenario(db)
    dados = {"mes": "2026-10", "descricao": "Cortar delivery", "valor_liberado": "200,00", "destino": "Reserva"}
    client.post("/planejamento/ajuste", data=dados)
    client.post("/planejamento/ajuste", data={**dados, "valor_liberado": "250,00"})
    db.expire_all()
    (ajuste,) = db.scalars(select(AjusteMes)).all()
    assert (ajuste.descricao, ajuste.valor_liberado, ajuste.destino) == ("Cortar delivery", 25000, "Reserva")
    assert "Cortar delivery" in client.get("/planejamento?mes=2026-10").text
    client.post("/planejamento/ajuste", data={"mes": "2026-10", "descricao": ""})
    db.expire_all()
    assert db.scalar(select(AjusteMes)) is None


def _reserva(db):
    semear_categorias(db)
    corrente = nova_conta(db, "Corrente", saldo=0)
    reserva_conta = nova_conta(db, "Reserva", saldo=250000)
    aluguel = db.scalar(select(Categoria).where(Categoria.nome == "Aluguel"))
    for mes in (7, 8, 9):
        novo_lancamento(db, corrente, date(2026, mes, 10), -100000, categoria=aluguel)
    db.commit()
    return corrente, reserva_conta


def test_objetivos_criar_reserva_e_ver_degraus(client, db):
    _, reserva_conta = _reserva(db)
    r = client.post("/planejamento/metas", data={"nome": "Reserva", "tipo": "reserva_emergencia", "conta_id": str(reserva_conta.id),
                                        "multiplicador_reserva": "3"}, follow_redirects=False)
    assert r.status_code == 303
    texto = client.get("/planejamento").text   # a reserva fica no Planejamento
    assert "Meses de cobertura" in texto and "2,5" in texto
    assert texto.count('id="reserva"') == 1
    assert "R$ 3.000,00" in texto             # alvo = 1.000 × 3
    assert "Próximo degrau" in texto and "3 meses" in texto
    assert "média dos últimos 3 meses completos" in texto


def test_objetivos_pague_primeiro_cria_recorrencia_e_excluir_encerra(client, db):
    corrente, destino = _reserva(db)
    client.post("/planejamento/metas", data={
        "nome": "Viagem", "tipo": "objetivo", "horizonte": "ano", "valor_alvo": "5.000,00", "prazo": "2027-04-05",
        "conta_id": str(destino.id), "valor_manual": "", "pagar_primeiro": "on", "conta_origem_id": str(corrente.id),
        "dia_pagar": "6", "valor_mensal_pagar": "500,00"})
    db.expire_all()
    meta = db.scalar(select(Meta).where(Meta.nome == "Viagem"))
    rec = db.scalar(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id))
    assert (rec.valor, rec.dia, rec.ativa) == (50000, 6, True)
    assert len(db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id, Lancamento.valor < 0)).all()) == 3
    texto = client.get("/objetivos").text
    assert "Viagem" in texto and "Guarda R$ 500,00/mês" in texto and "Neste ano" in texto
    client.post(f"/planejamento/metas/{meta.id}/excluir")
    db.expire_all()
    assert db.scalar(select(Meta).where(Meta.nome == "Viagem")) is None
    assert db.get(Recorrencia, rec.id).ativa is False


def test_objetivos_pague_primeiro_sem_dados_da_erro(client, db):
    _, destino = _reserva(db)
    r = client.post("/planejamento/metas", data={"nome": "X", "tipo": "objetivo", "pagar_primeiro": "on", "conta_id": str(destino.id)})
    assert r.status_code == 422 and "pagar a meta primeiro" in r.text
    assert db.scalar(select(Meta)) is None


def test_objetivos_editar_meta(client, db):
    db.add(Meta(nome="Carro", tipo="objetivo", horizonte="sonho", valor_alvo=100000, valor_manual=0))
    nova_conta(db)
    db.commit()
    meta = db.scalar(select(Meta))
    client.post(f"/planejamento/metas/{meta.id}", data={"nome": "Carro novo", "tipo": "objetivo", "horizonte": "ano",
                                               "valor_alvo": "2.000,00", "valor_manual": "500,00", "ativa": "on"})
    db.expire_all()
    meta = db.scalar(select(Meta))
    assert (meta.nome, meta.horizonte, meta.valor_alvo, meta.valor_manual) == ("Carro novo", "ano", 200000, 50000)


def test_relatorios_renderiza_com_dados(client, db):
    _cenario(db)
    texto = client.get("/desempenho?mes=2026-10").text
    assert "Despesas por Categoria" in texto and "Mercado" in texto and "Aluguel" in texto
    assert "grafico-rd" in texto and "grafico-patrimonio" in texto and "grafico-fv" in texto
    assert '"previsao_saldo"' in texto
    assert client.get("/relatorios?mes=2026-11").status_code == 200


def test_dashboard_mostra_alertas_pendencias_e_dispensa(client, db):
    conta, _, _ = _cenario(db)
    novo_lancamento(db, conta, date(2026, 10, 25), -3000, status="pendente", descricao="Luz")
    novo_lancamento(db, conta, date(2026, 10, 26), 10000, status="pendente", descricao="Freela")
    db.commit()
    texto = client.get("/?mes=2026-10").text
    assert "Alertas do mês" in texto and "Moradia é a maior fatia" in texto
    assert "Pendências" in texto and "R$ 30,00" in texto and "R$ 100,00" in texto
    assert "De cada R$ 100" not in texto
    client.post("/planejamento/alertas/dispensar", data={"mes": "2026-10", "chave": "moradia_maior_fatia"})
    assert "Moradia é a maior fatia" not in client.get("/?mes=2026-10").text


def test_dashboard_mostra_teto_economia_e_reserva(client, db):
    _, reserva_conta = _reserva(db)
    reserva_meta = Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id)
    db.add_all([reserva_meta, Meta(nome="Economizar", tipo="economia_mensal", valor_alvo=100000)])
    db.flush()
    sincronizar_caixinha_meta(db, reserva_meta, date(2026, 10, 5))
    db.add(Orcamento(competencia="2026-10", categoria_id=None, limite=300000))
    db.commit()
    texto = client.get("/?mes=2026-10").text
    assert "Teto de gastos" in texto and "de R$ 3.000,00" in texto
    assert "Meta de economia" in texto and "de R$ 1.000,00" in texto
    assert "meses de cobertura" in texto and "2,5" in texto


def test_lancamentos_modal_mostra_checklist_so_com_reserva(client, db):
    _, reserva_conta = _reserva(db)
    assert "Saque da reserva" not in client.get("/lancamentos/novo?tipo=despesa").text
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id))
    db.commit()
    texto = client.get("/lancamentos/novo?tipo=despesa").text
    assert "Saque da reserva de emergência" in texto and 'name="checklist_reserva"' in texto
    assert "Saque da reserva" not in client.get("/lancamentos/novo?tipo=receita").text


def test_saque_pela_rota_cria_meta_repor_reserva(client, db):
    _, reserva_conta = _reserva(db)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id))
    db.commit()
    r = client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "800,00", "descricao": "Conserto", "data": "2026-10-05",
        "conta_id": str(reserva_conta.id), "efetivado": "on",
        "checklist_reserva": ["enviado", "inesperado", "necessario"]})
    assert r.status_code == 200
    db.expire_all()
    repor = db.scalar(select(Meta).where(Meta.nome == "Repor reserva"))
    assert repor.valor_alvo == 80000 and repor.prazo == date(2027, 4, 5)
    lanc = db.scalar(select(Lancamento).where(Lancamento.descricao == "Conserto"))
    assert "Inesperado: sim" in lanc.observacao and "Urgente: não" in lanc.observacao
    # a meta de reposição aparece na tela de objetivos
    assert "Repor reserva" in client.get("/objetivos").text


def test_saque_de_outra_conta_nao_cria_meta(client, db):
    corrente, _ = _reserva(db)
    r = client.post("/lancamentos", data={
        "tipo": "despesa", "valor": "10,00", "descricao": "Café", "data": "2026-10-05",
        "conta_id": str(corrente.id), "efetivado": "on"})
    assert r.status_code == 200
    assert db.scalar(select(Meta).where(Meta.nome == "Repor reserva")) is None


def test_criar_reserva_pelo_planejamento_volta_para_ele(client, db):
    _, reserva_conta = _reserva(db)
    texto = client.get("/planejamento").text
    assert "Criar reserva" in texto and "Ajuste do mês" in texto
    r = client.post("/planejamento/metas", data={"nome": "Reserva de emergência", "tipo": "reserva_emergencia",
                                        "voltar": "planejamento", "conta_id": str(reserva_conta.id),
                                        "multiplicador_reserva": "3"}, follow_redirects=False)
    assert r.headers["location"] == "/planejamento#reserva"
    texto = client.get("/planejamento").text
    assert "Meses de cobertura" in texto and "Criar reserva" not in texto


def test_planejamento_mostra_este_mes_e_metas(client, db):
    _, reserva_conta = _reserva(db)
    db.add_all([Meta(nome="Economizar", tipo="economia_mensal", valor_alvo=50000),
                Meta(nome="Viagem", tipo="objetivo", valor_alvo=500000, prazo=date(2027, 4, 5)),
                Meta(nome="IPTU", tipo="objetivo", valor_alvo=120000, prazo=date(2027, 2, 15), repete_anual=True,
                     valor_manual=120000)])
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    assert 'id="este-mes"' in texto and 'id="metas"' in texto
    assert texto.index('id="este-mes"') < texto.index('id="metas"')
    assert "Economizar" in texto and "Viagem" in texto and "Nova meta" in texto
    assert "Provisões anuais" not in texto
    # meta que repete todo ano nunca vai para Concluídos, mesmo com o alvo batido
    assert "IPTU" in texto and "anual" in texto
    concluidos = client.get("/planejamento?mes=2026-10&aba=concluidos").text.split('id="metas"')[1]
    iptu = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert f'href="/planejamento/metas/{iptu.id}"' not in concluidos


def test_objetivos_redireciona_para_planejamento_com_os_filtros(client, db):
    nova_conta(db)  # sem conta o app manda para o setup
    db.commit()
    r = client.get("/objetivos?aba=concluidos&ordem=alvo", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/planejamento?aba=concluidos&ordem=alvo#metas"
    assert client.get("/objetivos", follow_redirects=False).headers["location"] == "/planejamento#metas"


def test_filtros_de_metas_mantem_o_mes(client, db):
    nova_conta(db)
    db.add(Meta(nome="Viagem", tipo="objetivo", valor_alvo=500000))
    db.commit()
    texto = client.get("/planejamento?mes=2026-12").text
    assert "/planejamento?mes=2026-12&amp;aba=concluidos#metas" in texto


def test_criar_meta_automatica_anual_como_despesa_pela_tela(client, db):
    corrente, _ = _reserva(db)
    destino = nova_conta(db, "Caixinha IPTU", saldo=0)  # a conta da reserva já tem mais que o alvo, e não sobraria o que guardar
    db.commit()
    cat = db.scalar(select(Categoria).where(Categoria.nome == "Aluguel"))
    r = client.post("/planejamento/metas", data={
        "nome": "IPTU", "tipo": "objetivo", "horizonte": "ano", "valor_alvo": "1.200,00", "prazo": "2027-09-30",
        "conta_id": str(destino.id), "pagar_primeiro": "on", "conta_origem_id": str(corrente.id), "dia_pagar": "10",
        "aporte": "automatico", "repete_anual": "on", "aporte_como_despesa": "on", "categoria_id": str(cat.id)},
        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/planejamento#metas"
    db.expire_all()
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert (meta.aporte_automatico, meta.repete_anual, meta.aporte_como_despesa, meta.categoria_id) == (
        True, True, True, cat.id)
    rec = db.scalar(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id))
    assert rec.valor == 10000 and rec.aporte_como_despesa and rec.categoria_id == cat.id
    texto = client.get(f"/planejamento/metas/{meta.id}").text
    assert "Guarda automático" in texto and "todo ano" in texto


def test_meta_fora_do_app_nao_salva_como_despesa_escondido(client, db):
    corrente, _ = _reserva(db)
    cat = db.scalar(select(Categoria).where(Categoria.nome == "Aluguel"))
    # "como despesa" fica escondido fora do app: marcado antes de trocar a conta, não vale
    r = client.post("/planejamento/metas", data={
        "nome": "IPTU", "tipo": "objetivo", "valor_alvo": "1.200,00", "prazo": "2027-09-30", "conta_id": "",
        "pagar_primeiro": "on", "conta_origem_id": str(corrente.id), "dia_pagar": "10", "aporte": "automatico",
        "aporte_como_despesa": "on", "categoria_id": str(cat.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert (meta.conta_id, meta.aporte_como_despesa, meta.categoria_id) == (None, False, None)


def test_detalhe_antigo_redireciona(client, db):
    nova_conta(db)
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=100000)
    db.add(meta)
    db.commit()
    r = client.get(f"/objetivos/{meta.id}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/planejamento/metas/{meta.id}"


def _meta_com_aporte(db, **campos):
    corrente, reserva_conta = _reserva(db)
    meta = Meta(**({"nome": "Reserva", "tipo": "reserva_emergencia", "conta_id": reserva_conta.id,
                    "pagar_primeiro": True, "conta_origem_id": corrente.id, "dia_pagar": 10,
                    "valor_alvo": 300000, "prazo": date(2027, 3, 5)} | campos))
    db.add(meta)
    db.commit()
    return meta


def test_reserva_com_guardar_automatico_renderiza_o_planejamento(client, db):
    _meta_com_aporte(db, aporte_automatico=True, valor_mensal_pagar=None)
    r = client.get("/planejamento")
    assert r.status_code == 200
    reserva = r.text.split('id="reserva"')[1].split("</section>")[0]
    assert "Guarda automático: R$ 600,00 todo dia 10" in reserva   # 3.000 ÷ out a fev


def test_reserva_com_aporte_fixo_mostra_pague_primeiro(client, db):
    _meta_com_aporte(db, valor_mensal_pagar=50000)
    reserva = client.get("/planejamento").text.split('id="reserva"')[1].split("</section>")[0]
    assert "Pague primeiro: R$ 500,00 todo dia 10" in reserva


def test_reserva_sem_aporte_no_mes_nao_mostra_a_linha(client, db):
    _meta_com_aporte(db, aporte_automatico=True, valor_mensal_pagar=None, prazo=None)
    r = client.get("/planejamento")
    assert r.status_code == 200
    reserva = r.text.split('id="reserva"')[1].split("</section>")[0]
    assert "Guarda automático" not in reserva and "Pague primeiro" not in reserva


def test_controles_escondidos_nao_ficam_salvos(client, db):
    corrente, destino = _reserva(db)
    cat = db.scalar(select(Categoria).where(Categoria.nome == "Aluguel"))
    # sem "guardar todo mês": automático, anual e como despesa estavam marcados, mas escondidos
    r = client.post("/planejamento/metas", data={
        "nome": "Viagem", "tipo": "objetivo", "valor_alvo": "5.000,00", "prazo": "2027-04-05",
        "conta_id": str(destino.id), "aporte": "automatico", "repete_anual": "on", "aporte_como_despesa": "on",
        "categoria_id": str(cat.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    meta = db.scalar(select(Meta).where(Meta.nome == "Viagem"))
    assert (meta.pagar_primeiro, meta.aporte_automatico, meta.repete_anual, meta.aporte_como_despesa,
            meta.categoria_id) == (False, False, False, False, None)


def test_reserva_nao_guarda_automatico_nem_repete(client, db):
    corrente, destino = _reserva(db)
    r = client.post("/planejamento/metas", data={
        "nome": "Reserva", "tipo": "reserva_emergencia", "conta_id": str(destino.id), "pagar_primeiro": "on",
        "conta_origem_id": str(corrente.id), "dia_pagar": "10", "valor_mensal_pagar": "500,00",
        "aporte": "automatico", "repete_anual": "on"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    meta = db.scalar(select(Meta).where(Meta.nome == "Reserva"))
    assert (meta.aporte_automatico, meta.repete_anual) == (False, False)
    rec = db.scalar(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id))
    assert rec.valor == 50000


def test_formulario_esconde_automatico_fora_de_objetivo(client, db):
    _reserva(db)
    texto = client.get("/planejamento").text
    escolha = texto.split("Quanto guardar por mês")[0].rsplit("<fieldset", 1)[1]
    assert "x-show=\"tipo === 'objetivo'\"" in escolha                   # fixo ou automático: só no objetivo
    assert "x-show=\"aporte === 'fixo' || tipo !== 'objetivo'\">Valor por mês" in texto


def test_conta_so_mostra_plano_de_meta_que_guarda_automatico_ate_o_prazo(client, db):
    corrente, destino = _reserva(db)
    for nome, campos in (("Sem guardar", {"pagar_primeiro": False, "prazo": date(2027, 4, 5)}),
                         ("Sem prazo", {"pagar_primeiro": True, "prazo": None})):
        meta = Meta(nome=nome, tipo="objetivo", valor_alvo=500000, conta_id=destino.id, aporte_automatico=True,
                    conta_origem_id=corrente.id, dia_pagar=10, valor_manual=0, **campos)
        db.add(meta)
        db.flush()
        sincronizar_caixinha_meta(db, meta, HOJE)
    db.commit()
    texto = client.get(f"/contas/{destino.id}").text
    assert "Sem guardar" in texto and "Sem prazo" in texto
    assert "/mês (" not in texto


def test_erro_ao_salvar_meta_volta_ao_mes_do_formulario(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/planejamento?mes=2026-12").text
    metas = texto.split('id="metas"')[1]
    assert metas.count('<input type="hidden" name="mes" value="2026-12">') == 2   # nova meta e criar reserva
    r = client.post("/planejamento/metas", data={"nome": "", "tipo": "objetivo", "mes": "2026-12"})
    assert r.status_code == 422
    assert "/planejamento?mes=2026-12&amp;aba=concluidos#metas" in r.text
    r = client.post("/planejamento/metas", data={"nome": "", "tipo": "objetivo", "mes": "lixo"})
    assert r.status_code == 422 and "/planejamento?mes=2026-10&amp;aba=concluidos#metas" in r.text


def test_textos_falam_em_planejamento_e_aporte_mensal(client, db):
    nova_conta(db)
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=100000)
    db.add(meta)
    db.commit()
    assert "Defina uma meta de economia em Planejamento." in client.get("/").text
    detalhe = client.get(f"/planejamento/metas/{meta.id}").text
    assert "pague primeiro" not in detalhe and "só os aportes mensais futuros são cancelados" in detalhe


def test_planejamento_mostra_teto_automatico(client, db):
    _cenario(db)
    texto = client.get("/planejamento?mes=2026-10").text
    assert "automático · 80% das receitas" in texto
    assert 'placeholder="4000,00"' in texto
    assert 'id="limite-teto"' in texto and 'value=""' in texto.split('id="limite-teto"')[1].split(">")[0]


def test_salvar_teto_fixa_e_zero_volta_ao_automatico(client, db):
    _cenario(db)
    client.post("/planejamento/limite", data={"mes": "2026-10", "categoria_id": "", "limite": "3000,00"})
    texto = client.get("/planejamento?mes=2026-10").text
    assert "automático ·" not in texto and "de R$ 3.000,00" in texto
    client.post("/planejamento/limite", data={"mes": "2026-10", "categoria_id": "", "limite": "0"})
    db.expire_all()
    assert db.scalar(select(Orcamento)) is None
    assert "automático · 80% das receitas" in client.get("/planejamento?mes=2026-10").text


def test_formulario_de_limite_lista_subcategorias_e_painel_mostra_mae_filha(client, db):
    conta, mercado, _ = _cenario(db)
    feira = Categoria(nome="Feira", tipo="despesa", pai_id=mercado.id, grupo="variavel", natureza="necessidade")
    db.add(feira)
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    assert {mercado.id, feira.id} <= _permitidas_no_limite(texto)   # mãe e filha no seletor do limite
    client.post("/planejamento/limite", data={"mes": "2026-10", "categoria_id": str(feira.id), "limite": "200,00"})
    texto = client.get("/planejamento?mes=2026-10").text
    assert "Mercado › Feira" in texto
    assert feira.id not in _permitidas_no_limite(texto)  # já tem limite: sai da lista


def _permitidas_no_limite(texto: str) -> set[int]:
    """Ids que o seletor do formulário de limite aceita (`somente`); vazio se o formulário não aparece."""
    if 'id="form-limite"' not in texto:
        return set()
    form = texto[texto.index('id="form-limite"'):texto.index("Adicionar limite")]
    return set(json.loads(re.search(r'"somente": (\[[^\]]*\])', form).group(1)))

def test_teto_salvo_mostra_o_recomendado_pelo_503020(client, db):
    from app.services.orcamento import definir_limite
    conta = nova_conta(db, "Nubank", saldo=0)
    novo_lancamento(db, conta, date(2026, 10, 1), 1_330_000, descricao="Salário")
    definir_limite(db, "2026-10", None, 800_000)
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    assert "recomendado pelo 50/30/20: R$ 10.640,00 (80% das receitas)" in texto
    definir_limite(db, "2026-10", None, 0)   # sem teto salvo: o automático já é o recomendado, sem a dica
    db.commit()
    assert "recomendado pelo 50/30/20" not in client.get("/planejamento?mes=2026-10").text
