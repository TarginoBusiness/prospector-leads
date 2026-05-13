"""Testes basicos da engine de scoring (sem banco)."""
from scoring import engine


def test_lead_super_quente_sao_luis():
    """Cenario do exemplo: clinica odonto em Sao Luis pedindo bot + app."""
    texto = (
        "Estou precisando de um bot pra WhatsApp pra minha clinica odontologica "
        "aqui em Sao Luis. Tambem queria um app pros pacientes."
    )
    r = engine.calcular(source="workana", texto_para_analisar=texto)
    assert r.score == 100, f"esperado 100, veio {r.score} com breakdown={r.breakdown}"
    assert r.cidade_tag == "sao-luis"
    assert r.nicho == "clinica_odonto"
    cats = {s.categoria for s in r.intent_signals}
    assert "C_whatsapp" in cats
    assert "B_app" in cats


def test_lead_frio_gmaps():
    """Lead generico vindo de Google Maps, sem cidade-alvo, sem intent."""
    r = engine.calcular(source="gmaps", texto_para_analisar="Padaria do Joao em Belo Horizonte")
    assert r.score < 30


def test_lead_morno_sao_paulo_imobiliaria():
    texto = "Imobiliaria precisando de site institucional em Sao Paulo"
    r = engine.calcular(source="workana", texto_para_analisar=texto)
    # base 40 (workana) + cidade 10 (SP) + nicho 15 (imob) + intent 30 (site) = 95
    assert 80 <= r.score <= 100
    assert r.cidade_tag == "sao-paulo"
    assert r.nicho == "imobiliaria"
