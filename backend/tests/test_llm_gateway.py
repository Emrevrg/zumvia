"""Katman 3 — Şema zorlaması ve fail-safe testleri."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.layers.l3_llm_gateway import (
    PROVIDERS,
    TradeDecision,
    build_system_prompt,
    extract_json,
    provider_catalog,
)

# --------------------------------------------------------------------------- #
#  JSON ayıklama — reasoning modelleri her türlü gürültüyü döndürebilir
# --------------------------------------------------------------------------- #


def test_extracts_plain_json() -> None:
    assert extract_json('{"action": "BUY"}') == {"action": "BUY"}


def test_extracts_json_from_markdown_fence() -> None:
    text = 'İşte kararım:\n```json\n{"action": "SELL", "confidence": 0.8}\n```\nUmarım yardımcı olur.'
    assert extract_json(text)["action"] == "SELL"


def test_strips_reasoning_think_block() -> None:
    text = '<think>Uzun uzun düşünüyorum...</think>{"action": "WAIT"}'
    assert extract_json(text) == {"action": "WAIT"}


def test_finds_json_inside_surrounding_prose() -> None:
    text = 'Karar: {"action": "BUY", "reasoning": "test {içeride} süslü"} — bitti.'
    assert extract_json(text)["action"] == "BUY"


def test_returns_none_for_garbage() -> None:
    assert extract_json("model bugün konuşmak istemiyor") is None
    assert extract_json("") is None


# --------------------------------------------------------------------------- #
#  Şema zorlaması
# --------------------------------------------------------------------------- #


def test_valid_decision_parses() -> None:
    d = TradeDecision.model_validate({
        "action": "BUY", "confidence": 0.82, "stop_loss": 98.0,
        "take_profit": 110.0, "reasoning": "test",
    })
    assert d.action == "BUY" and d.confidence == pytest.approx(0.82)


def test_turkish_and_alias_actions_normalized() -> None:
    for raw, expected in (("al", "BUY"), ("SAT", "SELL"), ("bekle", "WAIT"),
                          ("long", "BUY"), ("hold", "WAIT"), ("kapat", "CLOSE")):
        d = TradeDecision.model_validate(
            {"action": raw, "confidence": 0.9, "stop_loss": 1, "take_profit": 5})
        assert d.action == expected


def test_percentage_confidence_normalized() -> None:
    d = TradeDecision.model_validate(
        {"action": "WAIT", "confidence": "85%"})
    assert d.confidence == pytest.approx(0.85)


def test_price_with_currency_symbol_is_cleaned() -> None:
    d = TradeDecision.model_validate(
        {"action": "BUY", "confidence": 0.9, "stop_loss": "$98.50",
         "take_profit": "110,00"})
    assert d.stop_loss == pytest.approx(98.50)


def test_entry_without_levels_is_rejected() -> None:
    """FAIL-SAFE: SL/TP olmayan giriş kararı şemadan geçemez."""
    with pytest.raises(ValidationError):
        TradeDecision.model_validate({"action": "BUY", "confidence": 0.95})


def test_wait_without_levels_is_allowed() -> None:
    d = TradeDecision.model_validate({"action": "WAIT", "confidence": 0.2})
    assert d.action == "WAIT"


def test_confidence_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeDecision.model_validate(
            {"action": "WAIT", "confidence": -0.5})


def test_unknown_action_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeDecision.model_validate(
            {"action": "YOLO", "confidence": 0.9, "stop_loss": 1, "take_profit": 2})


# --------------------------------------------------------------------------- #
#  Persona ve sağlayıcı kataloğu
# --------------------------------------------------------------------------- #


def test_persona_declares_capital_protection() -> None:
    prompt = build_system_prompt()
    assert "sermayeyi" in prompt.lower()
    assert "JSON" in prompt


def test_recovery_addendum_forbids_risk_increase() -> None:
    prompt = build_system_prompt(recovery_mode=True)
    assert "TOPARLANMA MODU AKTİF" in prompt
    assert "risk artırmak" in prompt.lower()


def test_user_notes_are_appended() -> None:
    prompt = build_system_prompt(extra_rules="Sadece BTC işle.")
    assert "Sadece BTC işle." in prompt


def test_every_provider_has_base_url_or_is_custom() -> None:
    for pid, spec in PROVIDERS.items():
        assert spec.base_url or pid == "custom"


def test_catalog_is_serializable() -> None:
    catalog = provider_catalog()
    assert any(p["id"] == "gemini" for p in catalog)
    assert any(p["id"] == "ollama" and not p["needs_key"] for p in catalog)
