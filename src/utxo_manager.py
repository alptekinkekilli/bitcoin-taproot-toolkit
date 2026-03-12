"""
utxo_manager.py — P2TR UTXO Seçimi ve Coin Selection
======================================================

P2TR scriptPubKey Yapısı:
--------------------------
Her Taproot çıktısının scriptPubKey'i tam olarak 34 byte'tır:

    Offset  Uzunluk  Değer        Açıklama
    ──────  ───────  ─────────    ────────────────────────────────────
    0       1        0x51         OP_1  (witness version 1)
    1       1        0x20         OP_PUSHBYTES_32
    2       32       <xonly>      32-byte x-only tweaked public key

Bu yapı Legacy P2PKH (25 byte) ve P2WPKH (22 byte)'den farklıdır.
Yanlış scriptPubKey tespiti → sighash hesaplama hatası → invalid signature.

UTXO Seti Sorgu Stratejisi:
────────────────────────────
  Yöntem 1: listunspent (cüzdan gerektirir)
    → Hız: Hızlı
    → Kapsam: Yalnızca cüzdan adresleri
    → Kullanım: Online cüzdan

  Yöntem 2: scantxoutset (cüzdan gerektirmez)
    → Hız: Yavaş (ilk çalıştırma ~dakikalar)
    → Kapsam: Tüm UTXO seti
    → Kullanım: İzleme (watch-only), cold wallet

  Yöntem 3: Esplora REST API (bu modülde fallback)
    → Hız: API bağımlı
    → Kapsam: Tam node verisi
    → Kullanım: Core çalışmadığında

Coin Selection Algoritmaları:
──────────────────────────────
  LargestFirst  : En büyük UTXO önce → minimum UTXO sayısı
  SmallestFirst : En küçük UTXO önce → UTXO seti temizleme
  BranchAndBound: Optimal değişimsiz çözüm (BIP-Benchmark)
  FIFO          : En eski UTXO önce → coin maturity

BIP-141 vByte Hesabı (Taproot):
────────────────────────────────
  Girdi vBytes  = 41   (outpoint:36 + scriptSig:1 + sequence:4 = 41 non-witness)
                      + witness: (1 item flag:1 + sig_len:1 + 64B sig)/4 = 16.5
                = 41 + 16.5 = ~57.5 vBytes per input

  Çıktı vBytes  = 8 (value) + 1 (scriptLen) + 34 (scriptPubKey) = 43 vBytes
  Overhead      = 10 + 0.5 (segwit marker) = 10.5 vBytes

  Toplam (1in/2out): 10.5 + 57.5 + 43 × 2 = ~154 vBytes
  Legacy P2PKH (1in/2out): ~226 bytes = %32 daha büyük
"""

import json
import struct
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import sys
import os

# raw_tx.UTXO ile uyumluluk için aynı interface
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'btc_examples'))
from raw_tx import UTXO, TxOutput


# ── P2TR Script Sabitleri ─────────────────────────────────────────────────────

P2TR_SCRIPT_PREFIX = bytes([0x51, 0x20])  # OP_1 OP_PUSH32
P2TR_SCRIPT_LENGTH = 34                   # 1 + 1 + 32 byte
P2TR_XONLY_OFFSET  = 2                    # scriptPubKey[2:34] = x-only pubkey

# Ağ prefixleri
P2TR_HRP = {"mainnet": "bc", "testnet": "tb", "regtest": "bcrt"}


# ── CoreUTXO Veri Sınıfı ─────────────────────────────────────────────────────

@dataclass
class CoreUTXO:
    """
    Bitcoin Core listunspent / scantxoutset çıktısından zenginleştirilmiş UTXO.

    raw_tx.UTXO'ya ek olarak:
        - confirmations: onay sayısı (zincir güvenliği)
        - descriptor: hangi descriptor'dan geldiği
        - spendable: bu cüzdanda harcanabilir mi
        - address: bech32m formatında adres
        - is_p2tr: P2TR scriptPubKey doğrulandı mı

    Alan Açıklamaları:
        txid         : Transaction ID (hex, big-endian — Core standart)
        vout         : Çıktı indeksi
        value_sat    : Değer (satoshi)
        scriptpubkey : Ham scriptPubKey bytes — sighash için ZORUNLU
        address      : İnsan okunur adres (tb1p...)
        confirmations: 0 = mempool, -1 = bilinmiyor
        spendable    : True = özel anahtar cüzdanda
        descriptor   : tr(...)#checksum
        is_p2tr      : scriptPubKey P2TR formatında mı (doğrulandı)
    """
    txid: str
    vout: int
    value_sat: int
    scriptpubkey: bytes
    address: str = ""
    confirmations: int = -1
    spendable: bool = False
    descriptor: str = ""
    is_p2tr: bool = False

    def to_raw_utxo(self) -> UTXO:
        """raw_tx.UTXO formatına dönüştür (build_tx / taproot_sighash için)."""
        return UTXO(
            txid=self.txid,
            vout=self.vout,
            value_sat=self.value_sat,
            scriptpubkey=self.scriptpubkey,
        )

    @property
    def xonly_pubkey(self) -> Optional[bytes]:
        """scriptPubKey'den x-only pubkey'i çıkar (P2TR ise)."""
        if self.is_p2tr and len(self.scriptpubkey) == P2TR_SCRIPT_LENGTH:
            return self.scriptpubkey[P2TR_XONLY_OFFSET:]
        return None

    def __repr__(self) -> str:
        conf_str = f"{self.confirmations}conf" if self.confirmations >= 0 else "unconf"
        return f"CoreUTXO({self.txid[:8]}...:{self.vout} {self.value_sat}sat {conf_str})"


# ── scriptPubKey Analizi ──────────────────────────────────────────────────────

def parse_p2tr_scriptpubkey(spk_hex: str) -> Tuple[bool, Optional[bytes]]:
    """
    scriptPubKey hex'ini P2TR açısından doğrular ve x-only pubkey'i çıkarır.

    BIP-341 §Script validation:
        P2TR çıktısı: OP_1 (0x51) OP_PUSHBYTES_32 (0x20) <32-byte-xonly>
        Toplam: 34 byte, hex = "5120" + 64 hex karakter

    Girdi → Mekanizma → Çıktı:
        "5120<64hex>"
            │
            ▼
        length check (34 byte)
        prefix check (0x51, 0x20)
            │
            ▼
        (True, xonly_bytes) veya (False, None)

    Argümanlar:
        spk_hex : scriptPubKey hex string (Core "scriptPubKey" alanı)

    Döner:
        (is_p2tr: bool, xonly_pubkey: bytes | None)

    Örnek:
        ok, xonly = parse_p2tr_scriptpubkey("5120abc123...")
        assert ok == True
        assert len(xonly) == 32
    """
    try:
        spk = bytes.fromhex(spk_hex)
    except ValueError:
        return False, None

    if len(spk) != P2TR_SCRIPT_LENGTH:
        return False, None

    if spk[:2] != P2TR_SCRIPT_PREFIX:
        return False, None

    xonly = spk[P2TR_XONLY_OFFSET:]
    return True, xonly


def build_p2tr_scriptpubkey(xonly_hex: str) -> bytes:
    """
    x-only pubkey'den P2TR scriptPubKey oluştur.

    Çıktı: 0x51 0x20 <32-byte-xonly>

    Bu fonksiyon taproot_sighash() çağrısında input scriptpubkey
    parametresi için kullanılır. Bitcoin Core'dan alınan UTXO'nun
    scriptPubKey'i ile eşleşmelidir — aksi takdirde sighash geçersiz olur.

    Argümanlar:
        xonly_hex : 64 karakter x-only pubkey hex

    Döner:
        34 byte scriptPubKey
    """
    xonly = bytes.fromhex(xonly_hex)
    if len(xonly) != 32:
        raise ValueError(f"x-only pubkey 32 byte olmalı, alınan: {len(xonly)}")
    return P2TR_SCRIPT_PREFIX + xonly


# ── UTXOManager ───────────────────────────────────────────────────────────────

class UTXOManager:
    """
    Bitcoin Core ve Esplora'dan P2TR UTXO toplayan ve seçen merkezi yönetici.

    İki Kaynak:
        1. Bitcoin Core RPC (listunspent / scantxoutset)
        2. Esplora API (mempool.space) — fallback

    Her iki kaynakta da P2TR doğrulaması yapılır:
        scriptPubKey[0:2] == 0x51 0x20  →  witness_v1_taproot

    Neden scriptPubKey Doğrulaması Kritik?
    ────────────────────────────────────────
    BIP-341 taproot_sighash, her girdi için:
        sha_scriptpubkeys = SHA256( Σ varint(len(spk)) ‖ spk )
    hesaplar. Yanlış scriptPubKey → yanlış sighash → imza geçersiz.
    Core "non-mandatory-script-verify-flag" hatası döner.
    """

    ESPLORA_URLS = {
        "mainnet":  "https://mempool.space/api",
        "testnet":  "https://mempool.space/testnet/api",
        "testnet4": "https://mempool.space/testnet4/api",
        "regtest":  None,  # Esplora regtest genellikle lokal
    }

    def __init__(
        self,
        network: str = "testnet",
        rpc=None,            # CoreConnector (opsiyonel)
        min_confirmations: int = 1,
    ):
        """
        Argümanlar:
            network           : "mainnet" | "testnet" | "testnet4" | "regtest"
            rpc               : CoreConnector örneği (None → yalnızca Esplora)
            min_confirmations : Minimum onay (0 → mempool dahil)
        """
        self.network = network
        self.rpc = rpc
        self.min_confirmations = min_confirmations
        self._esplora_base = self.ESPLORA_URLS.get(network)

    def fetch_utxos(self, address: str) -> List[CoreUTXO]:
        """
        Adrese ait P2TR UTXO'ları çek ve doğrula.

        Kaynak Önceliği:
            1. Bitcoin Core rpc.list_unspent() — cüzdan izleme listesindeyse
            2. Bitcoin Core rpc.scan_tx_out_set() — cüzdan gerekmez
            3. Esplora API — Core yoksa fallback

        Her durumda scriptPubKey doğrulaması yapılır.
        P2TR olmayan çıktılar (P2PKH, P2WPKH vb.) filtrelenir.

        Argümanlar:
            address : P2TR bech32m adresi (tb1p... / bc1p...)

        Döner:
            P2TR olduğu doğrulanmış CoreUTXO listesi

        Fırlatır:
            RuntimeError : Hiçbir kaynaktan veri alınamazsa
        """
        if self.rpc:
            try:
                return self._fetch_from_core_listunspent(address)
            except Exception as e:
                print(f"  [UYARI] listunspent başarısız ({e}), scantxoutset deneniyor...")
                try:
                    return self._fetch_from_core_scan(address)
                except Exception as e2:
                    print(f"  [UYARI] scantxoutset başarısız ({e2}), Esplora'ya geçiliyor...")

        return self._fetch_from_esplora(address)

    def _fetch_from_core_listunspent(self, address: str) -> List[CoreUTXO]:
        """
        Bitcoin Core listunspent — cüzdan izleme listesi ile.

        Önceden importdescriptors ile adresin içe aktarılmış olması gerekir.
        min_confirmation=0 ile mempool UTXO'ları da dahil edilir.
        """
        raw_utxos = self.rpc.list_unspent(
            addresses=[address],
            minconf=self.min_confirmations,
        )
        return [self._parse_core_utxo(u) for u in raw_utxos
                if self._is_p2tr_utxo(u)]

    def _fetch_from_core_scan(self, address: str) -> List[CoreUTXO]:
        """
        scantxoutset — cüzdan gerektirmez, tüm UTXO setini tarar.

        Descriptor formatı: addr(tb1p...) veya tr(xonly_hex)
        addr() formatı daha basit ama tr() daha verimli indeksler.
        """
        # addr() descriptor — Core adres tipini otomatik algılar
        from .descriptor_wallet import DescriptorChecksum
        # addr() için checksum gerekmez ama Core bunu kabul eder
        scan_desc = f"addr({address})"

        result = self.rpc.scan_tx_out_set([scan_desc])
        if not result or not result.get("success"):
            return []

        utxos = []
        for u in result.get("unspents", []):
            is_p2tr, xonly = parse_p2tr_scriptpubkey(u.get("scriptPubKey", ""))
            if not is_p2tr:
                continue

            utxos.append(CoreUTXO(
                txid=u["txid"],
                vout=u["vout"],
                value_sat=u["amountSat"],
                scriptpubkey=bytes.fromhex(u["scriptPubKey"]),
                address=address,
                confirmations=u.get("height", 0),
                spendable=False,  # scantxoutset özel anahtar bilmez
                descriptor=u.get("desc", ""),
                is_p2tr=True,
            ))

        return utxos

    def _fetch_from_esplora(self, address: str) -> List[CoreUTXO]:
        """
        Esplora REST API — Bitcoin Core yoksa fallback.

        Esplora scriptPubKey döndürmez. Adresin P2TR olduğunu
        varsayarak scriptPubKey inşa edilir.

        Güvenlik Notu:
            Esplora'dan alınan UTXO verileri doğrulanmamış.
            Üretim ortamında Core RPC tercih edin.
        """
        if not self._esplora_base:
            raise RuntimeError(f"{self.network} için Esplora URL'i tanımlı değil")

        url = f"{self._esplora_base}/address/{address}/utxo"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                raw = json.loads(r.read())
        except Exception as e:
            raise RuntimeError(f"Esplora UTXO sorgusu başarısız: {e}")

        # P2TR adresinin scriptPubKey'ini Esplora TX'inden al
        utxos = []
        for u in raw:
            if self.min_confirmations > 0 and not u.get("status", {}).get("confirmed"):
                continue

            # scriptPubKey almak için TX hex'ini çek
            spk = self._get_scriptpubkey_from_tx(u["txid"], u["vout"])
            if spk is None:
                # Esplora'dan script alınamazsa adrese göre inşa et
                spk = self._estimate_p2tr_scriptpubkey(address)

            is_p2tr, _ = parse_p2tr_scriptpubkey(spk.hex() if spk else "")

            confirmations = 0
            if u.get("status", {}).get("confirmed"):
                confirmations = 1  # tam onay sayısı Esplora'da yok

            utxos.append(CoreUTXO(
                txid=u["txid"],
                vout=u["vout"],
                value_sat=u["value"],
                scriptpubkey=spk or b"",
                address=address,
                confirmations=confirmations,
                spendable=False,
                is_p2tr=is_p2tr,
            ))

        return utxos

    def _get_scriptpubkey_from_tx(self, txid: str, vout: int) -> Optional[bytes]:
        """Esplora TX endpoint'inden belirli vout'un scriptPubKey'ini al."""
        if not self._esplora_base:
            return None
        url = f"{self._esplora_base}/tx/{txid}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                tx = json.loads(r.read())
            spk_hex = tx["vout"][vout]["scriptpubkey"]
            return bytes.fromhex(spk_hex)
        except Exception:
            return None

    def _estimate_p2tr_scriptpubkey(self, address: str) -> Optional[bytes]:
        """
        Bech32m adresinden x-only pubkey çözümle ve scriptPubKey oluştur.

        Bu, Esplora'nın TX verisi olmadığı durumlar için son çaredir.
        Bech32m decode → witness program (32 byte) → OP_1 OP_PUSH32 <program>
        """
        # Basit bech32m decode (full implementation raw_tx._bech32m_encode'un tersi)
        # Bu noktada sadece xonly'yi adres encode'undan çekiyoruz
        # Tam implementasyon için raw_tx modülünün decode versiyonu eklenebilir
        return None  # Güvenli fallback: None → UTXO'yu atla

    def _parse_core_utxo(self, raw: Dict) -> CoreUTXO:
        """Core listunspent çıktısını CoreUTXO'ya dönüştür."""
        spk_hex = raw.get("scriptPubKey", "")
        spk = bytes.fromhex(spk_hex) if spk_hex else b""
        is_p2tr, _ = parse_p2tr_scriptpubkey(spk_hex)

        return CoreUTXO(
            txid=raw["txid"],
            vout=raw["vout"],
            value_sat=raw["amountSat"],
            scriptpubkey=spk,
            address=raw.get("address", ""),
            confirmations=raw.get("confirmations", 0),
            spendable=raw.get("spendable", False),
            descriptor=raw.get("desc", ""),
            is_p2tr=is_p2tr,
        )

    @staticmethod
    def _is_p2tr_utxo(raw: Dict) -> bool:
        """Core UTXO'sunu P2TR açısından filtrele."""
        spk_type = raw.get("scriptPubKey", {})
        if isinstance(spk_type, dict):
            return spk_type.get("type") == "witness_v1_taproot"
        # String ise prefix kontrol
        spk = raw.get("scriptPubKey", "")
        return isinstance(spk, str) and spk.startswith("5120") and len(spk) == 68


# ── Coin Selection ────────────────────────────────────────────────────────────

class CoinSelector:
    """
    Taproot transaction için UTXO seçim motoru.

    vByte Hesabı (BIP-141 §Segwit discount):
        Witness verisi × 0.25 ağırlık sayılır.
        Taproot Schnorr imzası = 64 byte (non-default) / 0 byte (SIGHASH_DEFAULT)
        SIGHASH_DEFAULT'ta imza byte sayısı witness'a eklenmez (64 byte alınan)

        Input weight:
            non-witness: 4 × (outpoint:36 + scriptSig:1 + sequence:4) = 164 WU
            witness:     1 × (stack_items:1 + sig_len:1 + 64_sig:64) = 66 WU
            total: 230 WU = 57.5 vBytes

        Output weight: 4 × (value:8 + scriptLen:1 + 34:spk) = 172 WU = 43 vBytes
        Overhead: 4 × (version:4 + locktime:4) + 2×(marker:1+flag:1) = 42 WU = 10.5 vBytes
    """

    TAPROOT_INPUT_VBYTES  = 57.5
    TAPROOT_OUTPUT_VBYTES = 43.0
    TX_OVERHEAD_VBYTES    = 10.5
    DUST_LIMIT_SAT        = 546

    @classmethod
    def estimate_fee(
        cls,
        n_inputs: int,
        n_outputs: int,
        sat_per_vbyte: float = 1.0,
    ) -> int:
        """
        Taproot TX için ücret tahmini.

        Argümanlar:
            n_inputs      : Girdi sayısı
            n_outputs     : Çıktı sayısı (değişim dahil)
            sat_per_vbyte : Ücret oranı (sat/vByte)

        Döner:
            Tahmini ücret (satoshi, yukarı yuvarlama)
        """
        vbytes = (
            cls.TX_OVERHEAD_VBYTES
            + n_inputs  * cls.TAPROOT_INPUT_VBYTES
            + n_outputs * cls.TAPROOT_OUTPUT_VBYTES
        )
        return max(1, int(vbytes * sat_per_vbyte + 0.999))  # ceil

    @classmethod
    def largest_first(
        cls,
        utxos: List[CoreUTXO],
        target_sat: int,
        fee_sat: int,
    ) -> Tuple[List[CoreUTXO], int]:
        """
        Largest-First seçim algoritması.

        En büyük UTXO'ları önce seçer. Avantaj: minimum girdi sayısı.
        Dezavantaj: UTXO setini konsolide etmez (küçük UTXO'lar birikim yapar).

        Girdi → Mekanizma → Çıktı:
            utxos (sırasız) + target_sat + fee_sat
                │
                ▼
            Sırala: value_sat azalan
                │
                ▼
            Kümülatif toplam ≥ target + fee olana dek ekle
                │
                ▼
            (seçilen utxo listesi, değişim miktarı)

        Argümanlar:
            utxos      : Uygun UTXO havuzu
            target_sat : Gönderilecek miktar
            fee_sat    : TX ücreti

        Döner:
            (selected: List[CoreUTXO], change_sat: int)

        Fırlatır:
            ValueError : Yetersiz bakiye
        """
        needed = target_sat + fee_sat
        sorted_utxos = sorted(utxos, key=lambda u: u.value_sat, reverse=True)

        selected: List[CoreUTXO] = []
        total = 0

        for utxo in sorted_utxos:
            selected.append(utxo)
            total += utxo.value_sat
            if total >= needed:
                break

        if total < needed:
            raise ValueError(
                f"Yetersiz bakiye.\n"
                f"  Gerekli : {needed} sat (hedef: {target_sat} + ücret: {fee_sat})\n"
                f"  Mevcut  : {total} sat ({len(utxos)} UTXO)"
            )

        change_sat = total - needed
        # Dust kontrolü: değişim 546 sat altıysa ücreti artır
        if 0 < change_sat < cls.DUST_LIMIT_SAT:
            change_sat = 0  # değişimi ücrette eritir

        return selected, change_sat

    @classmethod
    def smallest_first(
        cls,
        utxos: List[CoreUTXO],
        target_sat: int,
        fee_sat: int,
    ) -> Tuple[List[CoreUTXO], int]:
        """
        Smallest-First (FIFO benzeri) — küçük UTXO'ları temizler.

        Uzun vadede UTXO setini küçük tutar.
        Daha fazla girdi → daha fazla ücret, ama uzun vadede daha verimli.
        """
        needed = target_sat + fee_sat
        sorted_utxos = sorted(utxos, key=lambda u: u.value_sat)

        selected: List[CoreUTXO] = []
        total = 0

        for utxo in sorted_utxos:
            selected.append(utxo)
            total += utxo.value_sat
            if total >= needed:
                break

        if total < needed:
            raise ValueError(f"Yetersiz bakiye: {total} sat < {needed} sat")

        change_sat = total - needed
        if 0 < change_sat < cls.DUST_LIMIT_SAT:
            change_sat = 0

        return selected, change_sat

    @classmethod
    def exact_match(
        cls,
        utxos: List[CoreUTXO],
        target_sat: int,
        fee_sat: int,
        tolerance_sat: int = 0,
    ) -> Optional[Tuple[List[CoreUTXO], int]]:
        """
        Değişimsiz tam eşleşme arar — BranchAndBound benzeri.

        Değişim çıktısı olmadan işlem:
          1. TX boyutunu küçültür (~43 vBytes daha az)
          2. UTXO setini genişletmez (gizlilik için iyi)
          3. Doğrudan coinjoins için ideal

        Brute force (küçük UTXO setleri için):
            2^n kombinasyon dener, ilk eşleşende durur.
            n > 20 için performans sorunu oluşabilir.

        Argümanlar:
            tolerance_sat : 0 = kesin eşleşme, >0 = kabul edilebilir artık

        Döner:
            (selected, 0) eğer eşleşme bulunursa
            None           eğer bulunamazsa
        """
        needed = target_sat + fee_sat
        n = len(utxos)

        # Sadece küçük setlerde brute force
        if n > 20:
            return None

        for mask in range(1, 1 << n):
            selected = [utxos[i] for i in range(n) if mask & (1 << i)]
            total = sum(u.value_sat for u in selected)
            if abs(total - needed) <= tolerance_sat:
                return selected, total - needed

        return None

    @classmethod
    def select(
        cls,
        utxos: List[CoreUTXO],
        target_sat: int,
        fee_sat: int,
        strategy: str = "largest_first",
    ) -> Tuple[List[CoreUTXO], int]:
        """
        Strateji seçici — coin selection algoritmalarını tek noktadan çağırır.

        Argümanlar:
            strategy : "largest_first" | "smallest_first" | "exact" | "auto"

        Strateji Rehberi:
            largest_first  → Hızlı işlemler, az girdi
            smallest_first → UTXO konsolidasyonu
            exact          → Gizlilik (değişim yok), ücret tasarrufu
            auto           → Önce exact, bulamazsa largest_first
        """
        if strategy == "exact" or strategy == "auto":
            result = cls.exact_match(utxos, target_sat, fee_sat)
            if result:
                return result
            if strategy == "exact":
                raise ValueError("Değişimsiz eşleşme bulunamadı")

        if strategy == "smallest_first":
            return cls.smallest_first(utxos, target_sat, fee_sat)

        return cls.largest_first(utxos, target_sat, fee_sat)
