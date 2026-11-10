"""Vouchers de sala VIP: renovação anual, usar e desfazer. Dados fictícios."""
from datetime import date

import pytest

from app.models import Conta
from app.services.sala_vip import desfazer_voucher, situacao_vip, usar_voucher


def _cartao(total=4, usados=0, renova=date(2027, 3, 15)):
    return Conta(nome="C", tipo="cartao", vip_vouchers=total, vip_usados=usados, vip_renova_em=renova)


def test_sem_beneficio():
    assert situacao_vip(_cartao(total=None), date(2026, 10, 30)) is None


def test_antes_da_renovacao():
    v = situacao_vip(_cartao(usados=1), date(2026, 10, 30))
    assert (v.total, v.usados, v.restantes, v.renova_em) == (4, 1, 3, date(2027, 3, 15))


def test_no_dia_da_renovacao_zera_e_avanca():
    v = situacao_vip(_cartao(usados=4), date(2027, 3, 15))
    assert (v.usados, v.restantes, v.renova_em) == (0, 4, date(2028, 3, 15))


def test_renovacao_esquecida_ha_anos():
    assert situacao_vip(_cartao(usados=2, renova=date(2024, 5, 1)), date(2026, 10, 30)).renova_em == date(2027, 5, 1)


def test_29_de_fevereiro():
    assert situacao_vip(_cartao(renova=date(2028, 2, 29)), date(2028, 3, 1)).renova_em == date(2029, 2, 28)


def test_sem_data_nao_renova():
    v = situacao_vip(_cartao(usados=4, renova=None), date(2030, 1, 1))
    assert (v.usados, v.renova_em) == (4, None)


def test_usar_grava_os_efetivos_e_conta():
    c = _cartao(usados=4, renova=date(2026, 10, 1))
    usar_voucher(c, date(2026, 10, 30))
    assert (c.vip_usados, c.vip_renova_em) == (1, date(2027, 10, 1))


def test_usar_sem_restantes_e_recusado():
    with pytest.raises(ValueError, match="Sem vouchers"):
        usar_voucher(_cartao(total=2, usados=2), date(2026, 10, 30))


def test_desfazer_nao_fica_negativo():
    c = _cartao(usados=0)
    desfazer_voucher(c, date(2026, 10, 30))
    assert c.vip_usados == 0
    c.vip_usados = 2
    desfazer_voucher(c, date(2026, 10, 30))
    assert c.vip_usados == 1


def _cartao_vip(db, total=4, usados=1):
    from tests.factories import nova_conta, novo_cartao
    c = novo_cartao(db, "Cartão Nubank", pagadora=nova_conta(db, "Nubank"))
    c.vip_vouchers, c.vip_usados, c.vip_renova_em = total, usados, date(2027, 3, 15)
    db.commit()
    return c


def test_tela_de_cartoes_mostra_vouchers(client, db):
    _cartao_vip(db)
    texto = client.get("/cartoes").text
    assert "Sala VIP" in texto and "3 de 4" in texto and "15/03/2027" in texto


def test_sem_beneficio_nao_mostra_sala_vip(client, db):
    from tests.factories import nova_conta, novo_cartao
    novo_cartao(db, pagadora=nova_conta(db))
    db.commit()
    assert "Sala VIP" not in client.get("/cartoes").text


def test_usar_e_desfazer_pela_tela(client, db):
    c = _cartao_vip(db, total=2, usados=1)
    client.post(f"/cartoes/{c.id}/vip/usar")
    db.refresh(c)
    assert c.vip_usados == 2
    r = client.post(f"/cartoes/{c.id}/vip/usar")
    assert r.status_code == 400 and "Sem vouchers" in r.text
    client.post(f"/cartoes/{c.id}/vip/desfazer")
    db.refresh(c)
    assert c.vip_usados == 1


def test_editar_cartao_grava_e_tira_o_beneficio(client, db):
    c = _cartao_vip(db)
    assert 'name="vip_vouchers"' in client.get(f"/cartoes/{c.id}").text
    dados = {"nome": c.nome, "cor": "#6366f1", "limite": "10.000,00", "fechamento": "3", "vencimento": "10",
             "pagadora_id": str(c.conta_pagamento_id)}
    client.post(f"/cartoes/{c.id}/editar", data={**dados, "vip_vouchers": "2", "vip_renova_em": "2027-06-01"})
    db.refresh(c)
    assert (c.vip_vouchers, c.vip_usados, c.vip_renova_em) == (2, 1, date(2027, 6, 1))
    client.post(f"/cartoes/{c.id}/editar", data={**dados, "vip_vouchers": "", "vip_renova_em": ""})
    db.refresh(c)
    assert (c.vip_vouchers, c.vip_renova_em) == (None, None)
