"""
taproot_signer.py — BIP-341 Taproot İmza Köprüsü (Core v26+ Uyumlu)
=====================================================================

SIGHASH_DEFAULT (0x00) vs SIGHASH_ALL (0x01) — Teknik Analiz
──────────────────────────────────────────────────────────────

Bitcoin Core v26+ ve BIP-341 §Common signature message:

  SIGHASH_DEFAULT (0x00):
    ┌──────────────────────────────────────────────────────────────────┐
    │ Witness'a EKLENMEZ: sighash_type byte                            │
    │ İmza boyutu: 64 byte (Schnorr R‖s)                              │
    │ Hash mesajında: hash_type = 0x00 (dahil edilir)                  │
    │                                                                   │
    │  witness = [<64-byte sig>]                                        │
    └──────────────────────────────────────────────────────────────────┘

  SIGHASH_ALL (0x01):
    ┌──────────────────────────────────────────────────────────────────┐
    │ Witness'a EKLENİR: 0x01 byte (sonuna)                           │
    │ İmza boyutu: 65 byte (R‖s ‖ 0x01)                              │
    │                                                                   │
    │  witness = [<64-byte sig><0x01>]                                  │
    └──────────────────────────────────────────────────────────────────┘

  Matematiksel Fark:
    Sighash hesaplama fomülü her ikisi için özdeştir:
        H_tag("TapSighash", epoch ‖ hash_type ‖ version ‖ locktime ‖ ...)
    Yalnızca hash_type byte'ının commitment (taahhüt) içeriği değişir.

  Neden 0x00 Tercih Edilir?
    1. 1 byte tasarruf → vBytes azalır → ücret düşer
    2. Standard form: tüm Taproot implementasyonları 0x00 bekler
    3. Wallet policy: bazı donanım cüzdanlar 0x01 reddeder (non-standard)
    4. Script template'leri: CHECKSIGADD opcode SIGHASH_DEFAULT'u varsayar

  Commitment Yapısı (BIP-341 §Taproot signature hash):
    Legacy ECDSA:   HASH(hashtype ‖ tx_fields)
    Taproot:        HASH_tag("TapSighash", epoch(0x00) ‖ hash_type ‖ tx_fields)

    "Epoch" (0x00): Gelecek BIP'lerin farklı sighash versiyonları için
    ayrılmış bir alan. Şu an 0x00 dışında geçerli değer yok.

  BIP-341 Güvenlik Avantajı:
    sha_amounts       = SHA256(Σ tüm girdi tutarları)
    sha_scriptpubkeys = SHA256(Σ tüm girdi scriptleri)

    Legacy BIP-143'te yalnızca harcanan çıktının tutarı imzalanırdı.
    Bu "fee attack"a izin veriyordu: donanım cüzdan yanlış tutar
    gösterilirken kullanıcı daha yüksek ücreti imzalıyordu.

    Taproot: İmzacı tüm tutarları ve script'leri taahhüt eder.
    Yanlış tutar → farklı sighash → imza geçersiz.

Bu Modülün Rolü:
  raw_tx.py'deki düşük seviye fonksiyonları Bitcoin Core entegrasyonu ile
  birleştirir. CoreUTXO → UTXO dönüşümü, scriptPubKey doğrulama ve
  imza sonrası TX doğrulama burada gerçekleşir.
"""

import sys
import os
from typing import List, Optional, Tuple, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'btc_examples'))
from raw_tx import (
    UTXO, TxOutput,
    taproot_sighash, schnorr_sign, build_tx,
    _point_mul, _xonly, G, N,
)
from .utxo_manager import CoreUTXO, CoinSelector, build_p2tr_scriptpubkey


# ── Sighash Tipi Sabitleri ────────────────────────────────────────────────────

class SighashType:
    """
    BIP-341 §Common signature message sighash tipleri.

    Witness Serileştirme Kuralı (BIP-341 §Spending):
        SIGHASH_DEFAULT (0x00): witness = [sig_64B]
        Diğer tipler:           witness = [sig_64B ‖ sighash_type_byte]
    """
    DEFAULT      = 0x00  # Tüm girdi+çıktı, witness'ta byte yok (64B sig)
    ALL          = 0x01  # DEFAULT ile özdeş ama witness'ta 0x01 eklenir (65B)
    NONE         = 0x02  # Çıktı imzalanmaz (RBF için tehlikeli)
    SINGLE       = 0x03  # Aynı indeksteki çıktı imzalanır
    ANYONECANPAY = 0x80  # Modifier: yalnızca bu girdi imzalanır

    # Geçerli kombinasyonlar
    ANYONECANPAY_ALL    = 0x81
    ANYONECANPAY_NONE   = 0x82
    ANYONECANPAY_SINGLE = 0x83

    @classmethod
    def witness_sig_size(cls, sighash_type: int) -> int:
        """
        Witness alanında imzanın kaç byte yer kapladığını döner.

        SIGHASH_DEFAULT: 64 byte (sighash type byte witness'ta yok)
        Diğer:           65 byte (64 byte sig + 1 byte sighash type)
        """
        return 64 if sighash_type == cls.DEFAULT else 65

    @classmethod
    def serialize_witness_sig(cls, schnorr_sig: bytes, sighash_type: int) -> bytes:
        """
        İmzayı witness formatına hazırla.

        BIP-341 §Spending:
            sighash_type == 0x00: witness_sig = sig (64B)
            sighash_type != 0x00: witness_sig = sig ‖ sighash_type (65B)
        """
        if len(schnorr_sig) != 64:
            raise ValueError(f"Schnorr imzası 64 byte olmalı, alınan: {len(schnorr_sig)}")
        if sighash_type == cls.DEFAULT:
            return schnorr_sig
        return schnorr_sig + bytes([sighash_type])


# ── TaprootSigner ──────────────────────────────────────────────────────────────

class TaprootSigner:
    """
    Taproot key-path imzalama motoru — Bitcoin Core v26+ uyumlu.

    İş Akışı (Girdi → Mekanizma → Çıktı):
    ─────────────────────────────────────
    CoreUTXO listesi + özel anahtar + hedef
        │
        ▼
    [1] scriptPubKey doğrula (0x51 0x20 <32B>)
        │
        ▼
    [2] TxOutput listesi oluştur (hedef + değişim)
        │
        ▼
    [3] taproot_sighash() — BIP-341 TapSighash
        SIGHASH_DEFAULT (0x00): epoch(0) ‖ 0x00 ‖ version ‖ locktime ‖ ...
        │
        ▼
    [4] schnorr_sign(sighash, sk) — BIP-340 imzası
        64-byte: R.x (32B) ‖ s (32B)
        │
        ▼
    [5] SighashType.serialize_witness_sig()
        SIGHASH_DEFAULT → 64B  (witness'ta type byte yok)
        SIGHASH_ALL     → 65B  (witness'ta 0x01 eklenir)
        │
        ▼
    [6] build_tx() — Segwit v1 serileştirme
        nVersion ‖ 0x00 0x01 ‖ inputs ‖ outputs ‖ witnesses ‖ nLockTime
        │
        ▼
    Ham TX hex — Core sendrawtransaction / Esplora broadcast

    Neden Bu Sınıf?
        raw_tx.py tek girdi / tek çıktı senaryosu için yazıldı.
        TaprootSigner çok-girdi, coin selection ve Core entegrasyonunu
        tek arayüzde birleştirir.
    """

    def __init__(self, sighash_type: int = SighashType.DEFAULT):
        """
        Argümanlar:
            sighash_type : Varsayılan: SIGHASH_DEFAULT (0x00)
                           Diğer değerler üretim ortamında dikkatli kullanılmalı.
        """
        self.sighash_type = sighash_type

    def sign_transaction(
        self,
        sk: bytes,
        inputs: List[CoreUTXO],
        outputs: List[TxOutput],
        version: int = 2,
        locktime: int = 0,
    ) -> Tuple[bytes, List[bytes]]:
        """
        Taproot transaction'ını imzalar.

        Her girdi için ayrı sighash ve imza hesaplanır.
        Aynı anahtar birden fazla girdiyi imzalayabilir (key-path spend).

        Argümanlar:
            sk       : 32-byte özel anahtar (raw scalar)
            inputs   : Harcanan CoreUTXO listesi (scriptPubKey zorunlu)
            outputs  : TxOutput listesi (hedef + değişim)
            version  : nVersion (2 önerilir)
            locktime : nLockTime (0 = anlık, >0 = timelock)

        Döner:
            (raw_tx_bytes, [witness_sig_i, ...])

        Fırlatır:
            ValueError : scriptPubKey eksik veya geçersiz P2TR
        """
        # scriptPubKey doğrulama
        raw_inputs = []
        for i, cu in enumerate(inputs):
            if not cu.scriptpubkey or len(cu.scriptpubkey) != 34:
                raise ValueError(
                    f"Girdi {i}: scriptPubKey eksik veya hatalı uzunluk.\n"
                    f"  P2TR için tam olarak 34 byte gerekli.\n"
                    f"  Mevcut: {len(cu.scriptpubkey)} byte: {cu.scriptpubkey.hex()!r}"
                )
            if cu.scriptpubkey[:2] != bytes([0x51, 0x20]):
                raise ValueError(
                    f"Girdi {i}: P2TR scriptPubKey değil.\n"
                    f"  Beklenen prefix: 5120\n"
                    f"  Alınan:          {cu.scriptpubkey[:2].hex()}\n"
                    f"  Bu, legacy veya P2WPKH UTXO olabilir."
                )
            raw_inputs.append(cu.to_raw_utxo())

        # Her girdi için sighash + imza
        witnesses: List[bytes] = []
        for idx in range(len(raw_inputs)):
            sighash = taproot_sighash(
                inputs=raw_inputs,
                outputs=outputs,
                input_index=idx,
                sighash_type=self.sighash_type,
                version=version,
                locktime=locktime,
            )
            sig = schnorr_sign(sighash, sk)
            witness_sig = SighashType.serialize_witness_sig(sig, self.sighash_type)
            witnesses.append(witness_sig)

        raw_tx = build_tx(
            inputs=raw_inputs,
            outputs=outputs,
            witnesses=witnesses,
            version=version,
            locktime=locktime,
        )

        return raw_tx, witnesses

    def build_and_sign(
        self,
        sk: bytes,
        utxos: List[CoreUTXO],
        to_address_scriptpubkey: bytes,
        amount_sat: int,
        fee_sat: int,
        change_scriptpubkey: Optional[bytes] = None,
        coin_strategy: str = "largest_first",
    ) -> Tuple[bytes, int, int]:
        """
        Tam TX inşa ve imzalama — UTXO seçimi dahil.

        Girdi → Mekanizma → Çıktı:
            sk + utxos + to_spk + amount_sat + fee_sat
                │
                ▼
            CoinSelector.select(strategy) → (selected_utxos, change_sat)
                │
                ▼
            TxOutput listesi oluştur
                change > 546 sat → change çıktısı ekle
                change < 546 sat → ücrette erit
                │
                ▼
            sign_transaction() → (raw_tx, witnesses)
                │
                ▼
            (raw_tx_bytes, actual_fee_sat, change_sat)

        Argümanlar:
            sk                      : 32-byte özel anahtar
            utxos                   : Uygun UTXO havuzu
            to_address_scriptpubkey : Alıcı scriptPubKey (34B P2TR)
            amount_sat              : Gönderilecek miktar (sat)
            fee_sat                 : TX ücreti (sat)
            change_scriptpubkey     : Değişim adresi scriptPubKey
                                      (None → değişimsiz)
            coin_strategy           : "largest_first" | "smallest_first" | "auto"

        Döner:
            (raw_tx_bytes, actual_fee_sat, change_sat)

        Fırlatır:
            ValueError : Yetersiz bakiye veya geçersiz parametreler
        """
        if amount_sat < CoinSelector.DUST_LIMIT_SAT:
            raise ValueError(
                f"Gönderim miktarı dust limitinin altında: {amount_sat} < 546 sat"
            )

        # UTXO seçimi
        selected, change_sat = CoinSelector.select(
            utxos=utxos,
            target_sat=amount_sat,
            fee_sat=fee_sat,
            strategy=coin_strategy,
        )

        # Çıktı listesi
        outputs: List[TxOutput] = [
            TxOutput(value_sat=amount_sat, scriptpubkey=to_address_scriptpubkey)
        ]

        actual_fee = fee_sat
        if change_sat > CoinSelector.DUST_LIMIT_SAT and change_scriptpubkey:
            outputs.append(TxOutput(
                value_sat=change_sat,
                scriptpubkey=change_scriptpubkey,
            ))
        elif change_sat > 0:
            # Dust altı değişim → ücrete ekle
            actual_fee += change_sat
            change_sat = 0

        raw_tx, _ = self.sign_transaction(
            sk=sk,
            inputs=selected,
            outputs=outputs,
        )

        return raw_tx, actual_fee, change_sat

    @staticmethod
    def verify_signature(
        schnorr_sig: bytes,
        message: bytes,
        xonly_pubkey: bytes,
    ) -> bool:
        """
        BIP-340 Schnorr imza doğrulama.

        Algoritma:
            P = lift_x(xonly_pubkey)   → x-koordinatından nokta
            R = lift_x(sig[:32])       → imza'nın R noktası
            e = H_tag("BIP0340/challenge", R.x ‖ P.x ‖ msg) mod N
            s·G == R + e·P             → geçerli imza koşulu

        Argümanlar:
            schnorr_sig   : 64-byte imza (R.x ‖ s) — sighash_type byte olmadan
            message       : 32-byte TapSighash (veya herhangi bir mesaj)
            xonly_pubkey  : 32-byte x-only public key

        Döner:
            True = imza geçerli, False = geçersiz
        """
        if len(schnorr_sig) != 64:
            return False
        if len(message) != 32:
            return False
        if len(xonly_pubkey) != 32:
            return False

        try:
            from raw_tx import schnorr_verify
            return schnorr_verify(message, xonly_pubkey, schnorr_sig)
        except Exception:
            return False

    @staticmethod
    def decode_tx_summary(raw_tx: bytes) -> Dict:
        """
        Ham TX bytes'ından özet bilgi çıkar (debug için).

        Döner:
            {
                "version": 2,
                "input_count": 1,
                "output_count": 2,
                "size_bytes": 154,
                "witness_present": True,
            }
        """
        if len(raw_tx) < 10:
            return {"error": "TX çok kısa"}

        offset = 0
        version = int.from_bytes(raw_tx[offset:offset+4], "little")
        offset += 4

        has_witness = (raw_tx[offset] == 0x00 and raw_tx[offset+1] == 0x01)
        if has_witness:
            offset += 2

        # Varint okuma (basit, <0xfd için)
        def read_varint(data, pos):
            v = data[pos]
            if v < 0xfd:
                return v, pos + 1
            elif v == 0xfd:
                return int.from_bytes(data[pos+1:pos+3], "little"), pos + 3
            elif v == 0xfe:
                return int.from_bytes(data[pos+1:pos+5], "little"), pos + 5
            else:
                return int.from_bytes(data[pos+1:pos+9], "little"), pos + 9

        n_inputs, offset = read_varint(raw_tx, offset)
        # Girdileri atla: txid(32) + vout(4) + scriptSig(varint+data) + seq(4)
        for _ in range(n_inputs):
            offset += 32 + 4  # txid + vout
            script_len, offset = read_varint(raw_tx, offset)
            offset += script_len + 4  # script + sequence

        n_outputs, offset = read_varint(raw_tx, offset)

        return {
            "version": version,
            "input_count": n_inputs,
            "output_count": n_outputs,
            "size_bytes": len(raw_tx),
            "witness_present": has_witness,
        }
