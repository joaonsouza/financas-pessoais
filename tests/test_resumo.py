"""Boletim do mês: o que entra em cada bloco e o texto para o WhatsApp."""
from dataclasses import replace
from datetime import date

from sqlalchemy import select

from app.models import Conta, Meta, set_metas_503020
from app.services.faturas import obter_ou_criar_fatura
from app.services.orcamento import definir_limite
from app.services.resumo import resumo_do_mes, texto_whatsapp
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 13)


def _base(db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    lazer = nova_categoria(db, "Lazer")
    novo_lancamento(db, conta, date(2026, 10, 1), 1_000_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 5), -300_000, categoria=lazer, descricao="Show")
    return conta, lazer


def test_pra_gastar_com_teto_no_mes_atual(db):
    _base(db)
    definir_limite(db, "2026-10", None, 400_000)
    r = resumo_do_mes(db, "2026-10", HOJE)
    p = r.pra_gastar
    assert (p.teto, p.restante, p.pct_usado, p.fase) == (400_000, 100_000, 75, "atual")
    assert p.por_dia == 100_000 // 19                      # 13 a 31/10 = 19 dias, contando hoje


def test_pra_gastar_estourado_passado_futuro_e_sem_teto(db):
    _base(db)
    assert resumo_do_mes(db, "2026-11", HOJE).pra_gastar is None   # sem teto salvo e sem receitas: o bloco some
    definir_limite(db, "2026-10", None, 200_000)
    p = resumo_do_mes(db, "2026-10", HOJE).pra_gastar
    assert (p.restante, p.por_dia) == (-100_000, None)
    definir_limite(db, "2026-09", None, 50_000)
    assert resumo_do_mes(db, "2026-09", HOJE).pra_gastar.fase == "passado"
    definir_limite(db, "2026-12", None, 310_000)
    futuro = resumo_do_mes(db, "2026-12", HOJE).pra_gastar
    assert (futuro.fase, futuro.por_dia) == ("futuro", 310_000 // 31)


def test_saldo_previsto_e_melhor_cartao_so_no_mes_atual(db):
    conta, _ = _base(db)
    novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert r.saldo_previsto == 500_000 + 1_000_000 - 300_000
    assert r.melhor_cartao is not None and r.melhor_cartao["cartao"].nome == "Itaú"
    assert resumo_do_mes(db, "2026-11", HOJE).melhor_cartao is None


def _reserva(db, atual=600_000, alvo=1_200_000):
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=alvo, valor_manual=atual))
    db.flush()


def test_reserva_so_a_de_emergencia_ativa(db):
    _base(db)
    db.add(Meta(nome="Viagem", tipo="objetivo", valor_alvo=100_000, valor_manual=20_000))
    assert resumo_do_mes(db, "2026-10", HOJE).reserva is None
    _reserva(db)
    res = resumo_do_mes(db, "2026-10", HOJE).reserva
    assert (res.nome, res.atual, res.alvo, res.pct) == ("Reserva", 600_000, 1_200_000, 50)
    assert res.meses_que_aguento is not None


def test_outras_reservas_subtrai_so_a_reserva_guardada_fora_do_saldo(db):
    conta, _ = _base(db)
    cdb = nova_conta(db, "CDB", tipo="investimento", saldo=800_000, sl=False)
    nova_conta(db, "Ações", tipo="investimento", saldo=200_000, sl=False)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=1_200_000, valor_manual=600_000, conta_id=cdb.id))
    db.flush()
    assert resumo_do_mes(db, "2026-10", HOJE).outras_reservas == 800_000 + 200_000 - 600_000


def test_outras_reservas_reserva_em_conta_do_saldo_liquido_nao_subtrai(db):
    conta, _ = _base(db)
    nova_conta(db, "Ações", tipo="investimento", saldo=200_000, sl=False)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=1_200_000, valor_manual=600_000, conta_id=conta.id))
    db.flush()
    assert resumo_do_mes(db, "2026-10", HOJE).outras_reservas == 200_000


def test_outras_reservas_none_sem_contas_fora_do_saldo(db):
    _base(db)
    assert resumo_do_mes(db, "2026-10", HOJE).outras_reservas is None


def test_texto_whatsapp_outras_reservas_logo_apos_a_reserva(db):
    _base(db)
    nova_conta(db, "Ações", tipo="investimento", saldo=200_000, sl=False)
    _reserva(db)
    linhas = texto_whatsapp(resumo_do_mes(db, "2026-10", HOJE)).splitlines()
    i = next(k for k, l in enumerate(linhas) if l.startswith("🛟"))
    assert linhas[i + 1] == "📈 Investimentos e outras caixinhas: R$ 2.000,00"


def test_gastos_vs_receita_sobra(db):
    _base(db)     # receita 1.000.000, gasto 300.000
    g = resumo_do_mes(db, "2026-10", HOJE).gasto_receita
    assert (g.gasto, g.receita, g.pct, g.saldo) == (300_000, 1_000_000, 30, 700_000)
    assert g.reserva_atual is None and g.frase_reserva is None


def _estourado(db, gasto, atual=600_000):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    novo_lancamento(db, conta, date(2026, 10, 1), 100_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 5), -gasto, descricao="Obra")
    if atual is not None:
        _reserva(db, atual)
    return resumo_do_mes(db, "2026-10", HOJE).gasto_receita


def test_gastos_vs_receita_falta_com_reserva(db):
    g = _estourado(db, 160_000)           # falta 60.000 de uma reserva de 600.000
    assert (g.saldo, g.reserva_atual, g.pct_reserva) == (-60_000, 600_000, 10)
    assert g.frase_reserva == "10% da reserva de R$ 6.000,00"


def test_gastos_vs_receita_falta_maior_que_a_reserva_ou_reserva_zerada_ou_sem_reserva(db):
    assert _estourado(db, 800_000, atual=50_000).frase_reserva == "mais do que a reserva tem: R$ 500,00"


def test_gastos_vs_receita_reserva_zerada(db):
    assert _estourado(db, 160_000, atual=0).frase_reserva == "a reserva está zerada"


def test_gastos_vs_receita_falta_sem_reserva(db):
    g = _estourado(db, 160_000, atual=None)
    assert g.saldo == -60_000 and g.reserva_atual is None and g.frase_reserva is None


def test_gastos_vs_receita_sem_receita(db):
    conta = nova_conta(db, "Nubank")
    novo_lancamento(db, conta, date(2026, 10, 5), -10_000)
    g = resumo_do_mes(db, "2026-10", HOJE).gasto_receita
    assert (g.receita, g.pct) == (0, 0)


def test_cartoes_do_mes_lista_soma_e_ignora_fora_do_mes_e_zerada(db):
    conta, _ = _base(db)
    itau = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    nubank = novo_cartao(db, "Nubank Cartão", pagadora=conta, fechamento=3, vencimento=15)
    vazio = novo_cartao(db, "Vazio", pagadora=conta, fechamento=3, vencimento=20)
    f_itau = obter_ou_criar_fatura(db, itau, "2026-10")
    f_nu = obter_ou_criar_fatura(db, nubank, "2026-10")
    f_vazio = obter_ou_criar_fatura(db, vazio, "2026-10")
    f_outra = obter_ou_criar_fatura(db, itau, "2026-11")
    novo_lancamento(db, conta, date(2026, 10, 2), -30_000, fatura_id=f_itau.id)
    novo_lancamento(db, conta, date(2026, 10, 2), -20_000, fatura_id=f_nu.id)
    novo_lancamento(db, conta, date(2026, 11, 2), -99_000, fatura_id=f_outra.id)
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert [(c.nome, c.total) for c in r.cartoes] == [("Itaú", 30_000), ("Nubank Cartão", 20_000)]
    assert r.cartoes_total == 50_000 and all(c.vencimento.month == 10 for c in r.cartoes)
    assert f_vazio.id and resumo_do_mes(db, "2026-12", HOJE).cartoes == []


def test_alertas_ate_tres_so_perigo_e_atencao(db):
    conta, lazer = _base(db)
    definir_limite(db, "2026-10", lazer.id, 1_000)       # estoura o limite de Lazer
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert r.alertas and len(r.alertas) <= 3 and all(a.nivel in ("perigo", "atencao") for a in r.alertas)


def test_comparacao_ate_o_mesmo_dia_do_mes_anterior(db):
    conta, lazer = _base(db)
    novo_lancamento(db, conta, date(2026, 9, 10), -100_000, categoria=lazer)
    novo_lancamento(db, conta, date(2026, 9, 20), -999_999, categoria=lazer)   # depois do dia 13: fora
    c = resumo_do_mes(db, "2026-10", HOJE).comparacao
    assert (c.atual, c.anterior, c.pct, c.ate_dia) == (300_000, 100_000, 200, 13)
    inteiro = resumo_do_mes(db, "2026-10", date(2026, 11, 5)).comparacao   # mês passado: meses inteiros
    assert (inteiro.anterior, inteiro.ate_dia) == (1_099_999, None)


def test_comparacao_some_sem_despesa_no_mes_anterior_e_no_futuro(db):
    _base(db)
    assert resumo_do_mes(db, "2026-10", HOJE).comparacao is None
    assert resumo_do_mes(db, "2026-12", HOJE).comparacao is None


def test_compra_no_cartao_conta_no_mes_da_fatura(db):
    conta, lazer = _base(db)
    cartao = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, cartao, date(2026, 9, 20), -50_000, categoria=lazer, fatura_id=fatura.id)
    definir_limite(db, "2026-10", None, 400_000)
    assert resumo_do_mes(db, "2026-10", HOJE).pra_gastar.restante == 400_000 - 300_000 - 50_000


def test_texto_whatsapp(db):
    conta, lazer = _base(db)
    definir_limite(db, "2026-10", None, 400_000)
    novo_lancamento(db, conta, date(2026, 10, 20), -12_000, categoria=lazer, status="pendente", descricao="Internet")
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=100_000, valor_manual=60_000))
    db.flush()
    texto = texto_whatsapp(resumo_do_mes(db, "2026-10", HOJE))
    linhas = texto.splitlines()
    assert linhas[0] == "🏠 *Casa · Outubro 2026*"
    assert "💰 Gastos: *R$ 3.120,00* de R$ 10.000,00 de receita (31%)" in linhas
    assert "✅ Sobram R$ 6.880,00" in linhas
    assert "📏 Limite de gastos do mês: R$ 4.000,00 (gastamos 78%)" in linhas
    assert "🧾 Só de parcelas: R$ 0,00" in linhas
    assert "✅ Ainda dá para gastar R$ 880,00 (R$ 46,31 por dia)" in linhas   # 400.000 − 312.000
    assert not any("🐷" in l for l in linhas)                               # o "guardado" saiu do boletim
    assert not any("Falta pagar" in l or "Internet" in l for l in linhas)
    assert any(l.startswith("🛟 Reserva: R$ 600,00 de R$ 1.000,00 (60%)") for l in linhas)
    assert not any(l.startswith("💳") for l in linhas)                     # sem cartão: some
    assert not any(l.startswith("📊") for l in linhas)                     # sem mês anterior: some


def test_texto_whatsapp_falta_com_reserva_e_cartoes(db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    novo_lancamento(db, conta, date(2026, 10, 1), 100_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 5), -160_000, descricao="Obra")
    _reserva(db, 600_000)
    itau = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    f = obter_ou_criar_fatura(db, itau, "2026-10")
    novo_lancamento(db, conta, date(2026, 10, 2), -30_000, fatura_id=f.id)
    linhas = texto_whatsapp(resumo_do_mes(db, "2026-10", HOJE)).splitlines()
    assert "⚠️ Faltam R$ 900,00 → sai da reserva (15% da reserva de R$ 6.000,00)" in linhas
    assert "💳 Cartões no mês: R$ 300,00 · hoje use o Itaú (vence 10/11)" in linhas
    assert not any(l.startswith("   •") or l.startswith("👉") for l in linhas)


def test_texto_whatsapp_mes_passado_acima_do_teto(db):
    _base(db)
    definir_limite(db, "2026-09", None, 100_000)
    novo_lancamento(db, db.scalars(select(Conta)).first(), date(2026, 9, 5), -150_000, descricao="Obra")
    linhas = texto_whatsapp(resumo_do_mes(db, "2026-09", HOJE)).splitlines()
    assert "📏 Limite de gastos do mês: R$ 1.000,00 (gastamos 150%)" in linhas
    assert "🚨 Fechamos o mês R$ 500,00 acima do limite" in linhas


def test_texto_whatsapp_tudo_em_dia_so_sem_alerta_e_sem_teto_estourado(db):
    _base(db)
    definir_limite(db, "2026-10", None, 100_000)
    r = replace(resumo_do_mes(db, "2026-10", HOJE), alertas=[])
    assert r.pra_gastar.restante < 0
    assert "✅ Tudo em dia" not in texto_whatsapp(r)
    r_ok = replace(r, pra_gastar=None)
    assert "✅ Tudo em dia" in texto_whatsapp(r_ok)


def test_texto_whatsapp_sem_tudo_em_dia_quando_receita_menos_gasto_negativo_sem_teto(db):
    _estourado(db, 160_000, atual=None)
    set_metas_503020(db, [0, 0, 100])  # a terceira meta leva tudo: o teto automático some
    r = replace(resumo_do_mes(db, "2026-10", HOJE), alertas=[])
    assert r.pra_gastar is None and r.gasto_receita.saldo < 0
    assert "Tudo em dia" not in texto_whatsapp(r)


def test_pra_gastar_automatico_e_rotulo_no_whatsapp(db):
    _base(db)
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert (r.pra_gastar.teto, r.pra_gastar.automatico) == (800_000, True)
    assert "Limite de gastos do mês: R$ 8.000,00" in texto_whatsapp(r)
    definir_limite(db, "2026-10", None, 400_000)
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert r.pra_gastar.automatico is False and "📏 Limite de gastos do mês: R$ 4.000,00" in texto_whatsapp(r)


def test_resumo_traz_o_gasto_com_parcelas_e_o_que_falta_para_o_limite(db):
    conta, _ = _base(db)
    definir_limite(db, "2026-10", None, 400_000)
    cartao = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    f = obter_ou_criar_fatura(db, cartao, "2026-10")  # vence 10/10: conta em outubro
    parcela = novo_lancamento(db, cartao, date(2026, 10, 10), -50_000, status="pendente", descricao="Tv", fatura_id=f.id)
    parcela.grupo_parcela_id, parcela.parcela_num, parcela.parcela_total = "g", 2, 6
    db.flush()
    r = resumo_do_mes(db, "2026-10", HOJE)
    assert r.pra_gastar.parcelas == 50_000
    linhas = texto_whatsapp(r).splitlines()
    assert "🧾 Só de parcelas: R$ 500,00" in linhas
    assert any(l.startswith("✅ Ainda dá para gastar ") for l in linhas)


def test_texto_whatsapp_passou_do_limite(db):
    conta, _ = _base(db)
    definir_limite(db, "2026-10", None, 100_000)
    novo_lancamento(db, conta, date(2026, 10, 6), -150_000, descricao="Obra")
    linhas = texto_whatsapp(resumo_do_mes(db, "2026-10", HOJE)).splitlines()
    assert any(l.startswith("🚨 Passamos do limite em R$ ") for l in linhas)
