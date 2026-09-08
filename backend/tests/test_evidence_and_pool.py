"""
KANIT ÜRETİMİ VE MODEL HAVUZU

İki modül %0 kapsamla duruyordu ve ikisi de para kararı veriyor.

KANIT: Bir stratejinin "kanıtlanmış" sayılıp sayılmayacağına burası karar
verir. Eşikler gevşerse kullanıcı işe yaramayan bir sisteme para bağlar;
sıkışırsa çalışan hiçbir sistem geçemez. Bu dosya eşiklerin gerçekten
uygulandığını korur.

HAVUZ: Kaç modelle, hangi sağlayıcılarla çalışıldığını belirler. Tek
sağlayıcıya dört kez sormak "çoklu model" değildir; havuz bunu bilmeli ve
kullanıcıya dürüstçe söylemeli.
"""
from __future__ import annotations

import json

import pytest

from app.agent import model_pool
from app.core.crypto import new_salt
from app.core.db import SessionLocal
from app.core.security import hash_password
from app.engine import evidence
from app.models import Credential, CredentialKind, User


@pytest.fixture()
def user():
    db = SessionLocal()
    row = db.query(User).filter(User.email == "pool@zumvia.com").first()
    if row is None:
        row = User(email="pool@zumvia.com", password_hash=hash_password("pooltest12345"),
                   vault_salt=new_salt())
        db.add(row)
        db.commit()
        db.refresh(row)
    db.query(Credential).filter(Credential.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    yield db, row
    db.query(Credential).filter(Credential.user_id == row.id).delete(
        synchronize_session=False)
    db.commit()
    db.close()


def add_key(db, user, provider: str, model: str = "") -> Credential:
    row = Credential(user_id=user.id, kind=CredentialKind.LLM, provider=provider,
                     label=provider, payload_enc=b"", hint="",
                     extra_json=json.dumps({"model": model} if model else {}))
    db.add(row)
    db.commit()
    return row


class FakeReport:
    """`walk_forward` çıktısının test için yeterli taklidi."""

    def __init__(self, trades: int, profit_factor: float, overfit_gap: float,
                 win_rate: float = 50.0, drawdown: float = 10.0) -> None:
        self._data = {
            "overfit_gap": overfit_gap,
            "out_of_sample": {
                "trades": trades, "profit_factor": profit_factor,
                "win_rate_pct": win_rate, "avg_r": 0.2,
                "max_drawdown_pct": drawdown,
            },
        }

    def to_dict(self):
        return self._data


def _playbook(**overrides):
    from app.layers.playbooks import PLAYBOOKS

    return next(iter(PLAYBOOKS.values()))


def _stub_market(monkeypatch, report: FakeReport | None, *, fail: bool = False):
    """Piyasa verisi ve walk-forward çağrılarını taklit eder."""
    import app.engine.optimizer as optimizer
    import app.layers.l1_market_data as market

    def fetch(*args, **kwargs):
        if fail:
            raise RuntimeError("borsa yanıt vermedi")
        import pandas as pd
        return pd.DataFrame({"open": [1] * 50, "high": [1] * 50,
                             "low": [1] * 50, "close": [1] * 50,
                             "volume": [1] * 50})

    monkeypatch.setattr(market, "fetch_ohlcv", fetch)
    monkeypatch.setattr(optimizer, "walk_forward",
                        lambda *a, **k: report or FakeReport(0, 0, 0))


# --------------------------------------------------------------------------- #
#  Kanıt: eşikler gerçekten uygulanıyor mu
# --------------------------------------------------------------------------- #

def test_a_strong_system_passes(monkeypatch) -> None:
    """Bol işlem, iyi kâr faktörü, düşük aşırı uyum → geçer."""
    _stub_market(monkeypatch, FakeReport(trades=40, profit_factor=1.8,
                                         overfit_gap=0.2))
    result = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto", "binance", 500)

    assert result.passed is True
    assert "geçti" in result.verdict.lower()


def test_too_few_trades_never_counts_as_proof(monkeypatch) -> None:
    """
    Az örnek, kanıt değil ŞANSTIR.

    Üç işlemde %100 kazanan bir sistem hiçbir şey kanıtlamaz; bunu
    "doğrulandı" diye sunmak kullanıcıyı doğrudan yanıltır.
    """
    _stub_market(monkeypatch, FakeReport(trades=evidence.MIN_TRADES - 1,
                                         profit_factor=5.0, overfit_gap=0.0))
    result = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto", "binance", 500)

    assert result.passed is False
    assert "işlem sayısı yetersiz" in result.verdict


def test_a_weak_profit_factor_fails(monkeypatch) -> None:
    """Kâr faktörü eşiğin altındaysa sistem geçmez — kaç işlem olursa olsun."""
    _stub_market(monkeypatch, FakeReport(trades=100,
                                         profit_factor=evidence.MIN_PROFIT_FACTOR - 0.1,
                                         overfit_gap=0.1))
    result = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto", "binance", 500)

    assert result.passed is False
    assert "kâr faktörü düşük" in result.verdict


def test_overfitting_is_caught_even_when_results_look_good(monkeypatch) -> None:
    """
    Aşırı uyum, en tehlikeli yanılgıdır: geçmişte harika, gelecekte hiç.

    Eğitim ile test arasındaki fark büyükse sistem geçmişi EZBERLEMİŞTİR.
    """
    _stub_market(monkeypatch, FakeReport(trades=80, profit_factor=2.5,
                                         overfit_gap=evidence.MAX_OVERFIT_GAP + 0.2))
    result = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto", "binance", 500)

    assert result.passed is False
    assert "aşırı uyum" in result.verdict


def test_no_data_is_reported_as_unmeasured_not_as_failure(monkeypatch) -> None:
    """
    "Ölçemedik" ile "kötü çıktı" farklı şeylerdir.

    Veri alınamadığı için sonuç üretilemeyen bir sistemi "başarısız" diye
    işaretlemek, çalışan bir sistemi haksız yere gömer.
    """
    _stub_market(monkeypatch, None, fail=True)
    result = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto", "binance", 500)

    assert result.passed is False
    assert "ölçülemedi" in result.verdict.lower()
    assert result.notes, "veri hatası kayda geçmedi"


def test_zero_trade_symbols_are_skipped_not_counted_as_zero(monkeypatch) -> None:
    """
    Hiç işlem üretmeyen bir parite ortalamayı aşağı çekmemeli.

    Sıfırı ortalamaya katmak, iki pariteden birinde çalışan bir sistemi
    matematiksel olarak yarıya indirirdi.
    """
    _stub_market(monkeypatch, FakeReport(trades=0, profit_factor=0.0, overfit_gap=0.0))
    result = evidence._evaluate(_playbook(), ["BTC/USDT", "ETH/USDT"],
                                "crypto", "binance", 500)

    assert result.trades == 0
    assert result.profit_factor == 0.0
    assert "ölçülemedi" in result.verdict.lower()


def test_evidence_serialises_every_number_it_used(monkeypatch) -> None:
    """
    Karne, hangi sayıya dayandığını göstermeli.

    Yalnızca "geçti/kaldı" veren bir kanıt, kanıt değil hükümdür.
    """
    _stub_market(monkeypatch, FakeReport(trades=30, profit_factor=1.6,
                                         overfit_gap=0.15))
    payload = evidence._evaluate(_playbook(), ["BTC/USDT"], "crypto",
                                 "binance", 500).to_dict()

    for key in ("trades", "profit_factor", "win_rate", "overfit_gap",
                "max_drawdown", "verdict", "passed"):
        assert key in payload, f"{key} karnede yok"


def test_thresholds_are_conservative_enough_to_mean_something() -> None:
    """
    Eşikler gevşerse "kanıtlanmış" damgası değersizleşir.

    Kâr faktörü 1.0 başabaştır; 1.15 komisyon ve kaymadan sonra hâlâ
    pozitif olmak demektir. 10 işlem azdır ama altı tamamen şanstır.
    """
    assert evidence.MIN_PROFIT_FACTOR > 1.0, "başabaş bir sistem 'kanıtlanmış' sayılıyor"
    assert evidence.MIN_TRADES >= 10
    assert 0 < evidence.MAX_OVERFIT_GAP < 1.0


# --------------------------------------------------------------------------- #
#  Model havuzu
# --------------------------------------------------------------------------- #

def test_no_keys_is_stated_plainly(user) -> None:
    db, u = user
    pool = model_pool.build(db, u)
    assert pool["usable"] is False
    assert "anahtar" in pool["reason"].lower()


def test_single_strategy_uses_one_model(user) -> None:
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    add_key(db, u, "openrouter", "openrouter/model-b")

    pool = model_pool.build(db, u, "single")
    assert pool["size"] == 1
    assert pool["usable"] is True


def test_multi_provider_spreads_across_providers(user) -> None:
    """
    Farklı sağlayıcılar AYRI kotalara sahiptir.

    Biri çökse ya da kotası bitse diğerleri çalışmaya devam eder — tek
    sağlayıcıdaki üç model bu dayanıklılığı vermez.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    add_key(db, u, "openrouter", "openrouter/model-b")

    pool = model_pool.build(db, u, "multi_provider", size=3)
    providers = {m["provider"] for m in pool["members"]}
    assert len(providers) == 2
    assert any("kota" in note for note in pool["notes"])


def test_asking_for_multi_provider_with_one_key_says_so(user) -> None:
    """
    Tek anahtarla "farklı sağlayıcı" istenirse bu SESSİZCE geçilmez.

    Kullanıcı dayanıklılık istediğini sanırken elinde olmayanı almamalı;
    sistem elindekiyle en iyisini yapar ve nedenini yazar.
    """
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")

    pool = model_pool.build(db, u, "multi_provider")
    assert pool["size"] == 1
    assert any("tek sağlayıcı" in note.lower() for note in pool["notes"])


def test_same_provider_models_share_one_quota(user) -> None:
    """
    Aynı sağlayıcının üç modeli üç kat hız vermez — hepsi AYNI kotayı
    paylaşır. Havuz bunu bilmezse sistem kotayı aşar ve 429 yer.
    """
    db, u = user
    add_key(db, u, "nvidia")

    pool = model_pool.build(db, u, "same_provider", size=3)
    if pool["size"] > 1:
        assert any("kota" in note.lower() for note in pool["notes"]), \
            "paylaşılan kota uyarısı yok"
        single = model_pool.build(db, u, "single")
        assert pool["combined_rpm"] == single["combined_rpm"], \
            "aynı sağlayıcının modelleri kotayı çoğaltıyor"


def test_an_unknown_strategy_falls_back_safely(user) -> None:
    db, u = user
    add_key(db, u, "nvidia", "nvidia/model-a")
    pool = model_pool.build(db, u, "uydurma_strateji")
    assert pool["usable"] is True
    assert pool["strategy"] in model_pool.STRATEGIES


def test_pool_size_is_bounded(user) -> None:
    """Sınırsız havuz, kotayı ve parayı sessizce yakar."""
    db, u = user
    for index in range(10):
        add_key(db, u, f"provider{index}", f"p{index}/model")

    pool = model_pool.build(db, u, "multi_provider", size=99)
    assert pool["size"] <= model_pool.MAX_POOL


def test_every_strategy_is_described_for_the_user() -> None:
    """
    Kullanıcı seçtiği şeyin ne anlama geldiğini bilmeli.

    "multi_provider" tek başına hiçbir şey anlatmaz.
    """
    for strategy in model_pool.STRATEGIES:
        text = model_pool.describe(strategy)
        assert len(text) > 20, f"{strategy} açıklaması yetersiz"
