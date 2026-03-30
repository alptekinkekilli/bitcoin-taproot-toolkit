"""
raw_tx.py — BIP-341 Taproot Ham Transaction Üretimi ve Yayınlama
================================================================
Standartlar : BIP-340 (Schnorr), BIP-341 (Taproot), BIP-350 (Bech32m)
Ağ         : Bitcoin Testnet (mempool.space Esplora API)
Bağımlılık : Yalnızca Python standart kütüphanesi
Amaç       : Eğitim ve prototip; üretim için güvenlik denetimi zorunludur.

Modül Kapsamı
-------------
Bu modül Taproot'un "key-path spend" (anahtar-yolu harcama) senaryosunu
eksiksiz implement eder:

  Özel Anahtar
      │
      ▼
  x-only Açık Anahtar  ──►  P2TR Adres (bech32m: tb1p... / bc1p...)
      │
      ▼  UTXO alındıktan sonra
  BIP-341 Sighash  ──►  Schnorr İmzası  ──►  Segwit v1 TX  ──►  Broadcast

Segwit v1 Transaction Formatı (BIP-141)
----------------------------------------
  nVersion   (4B LE)
  0x00 0x01                 ← segwit marker + flag
  [INPUT COUNT] [INPUTS]
    ├─ outpoint (txid 32B LE + vout 4B LE)
    ├─ scriptSig = 0x00     ← native segwit, boş
    └─ nSequence 4B LE
  [OUTPUT COUNT] [OUTPUTS]
    ├─ value 8B LE
    └─ scriptPubKey: 0x51 0x20 <32-byte-xonly>  ← OP_1 OP_PUSH32
  [WITNESS per input]
    └─ 1 item: 64-bayt Schnorr imzası
  nLockTime  (4B LE)

BIP-341 Sighash Farkı (Legacy vs Taproot)
------------------------------------------
  Legacy (BIP-143): Yalnızca harcanan çıktının tutarı imzalanır.
                    Donanım cüzdan aldatma saldırısına açık.
  Taproot (BIP-341): TÜM girdi tutarları ve scriptpubkey'ler imzalanır.
                     İmzacı ne harcadığını kesin olarak bilir.

Güvenlik Uyarıları
------------------
  - sk (özel anahtar) hiçbir zaman paylaşılmamalı veya loglara yazılmamalı.
  - Testnet anahtarları mainnet'te KULLANILMAMALIDIR.
  - Dust limiti: çıktı değeri > 546 sat olmalıdır.

Bağlantılar
-----------
  BIP-341 : https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki
  BIP-350 : https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki
  Esplora : https://github.com/Blockstream/esplora/blob/master/API.md
  README  : ./README.md §5
"""

import hashlib
import secrets
import struct
import json
import urllib.request
import urllib.error
from typing import List, Optional, Tuple


# ── Secp256k1 ─────────────────────────────────────────────────────────────────

P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    @property
    def is_infinity(self):
        return self.x is None

INFINITY = Point(None, None)
G = Point(GX, GY)

def _point_add(P1, P2):
    if P1.is_infinity: return P2
    if P2.is_infinity: return P1
    if P1.x == P2.x:
        if P1.y != P2.y: return INFINITY
        lam = (3 * P1.x**2 * pow(2 * P1.y, P - 2, P)) % P
    else:
        lam = ((P2.y - P1.y) * pow(P2.x - P1.x, P - 2, P)) % P
    x3 = (lam**2 - P1.x - P2.x) % P
    return Point(x3, (lam * (P1.x - x3) - P1.y) % P)

def _point_mul(k, pt):
    r, a = INFINITY, pt
    while k:
        if k & 1: r = _point_add(r, a)
        a = _point_add(a, a)
        k >>= 1
    return r

def _xonly(pt) -> bytes:
    return pt.x.to_bytes(32, "big")

def _tagged_hash(tag: str, data: bytes) -> bytes:
    h = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(h + h + data).digest()

def _lift_x(xb: bytes) -> Point:
    x = int.from_bytes(xb, "big")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        raise ValueError("Geçersiz x koordinatı")
    return Point(x, y if y % 2 == 0 else P - y)

def schnorr_verify(msg: bytes, xonly_pk: bytes, sig: bytes) -> bool:
    """
    BIP-340 §Verification — Schnorr imza doğrulama.

    Algoritma:
        P  = lift_x(xonly_pk)                    secp256k1 noktasına dönüştür
        r  = int(sig[:32])                        R.x bileşeni
        s  = int(sig[32:])                        s skaleri
        e  = H("BIP0340/challenge", r ‖ P.x ‖ msg) mod N
        R  = s·G − e·P
        Geçerli: R is not infinity, R.y çift, R.x == r

    Neden 'lift_x' (x-only)?
        BIP-340 Taproot'ta açık anahtarlar yalnızca x koordinatını saklar.
        Çift y varsayılır. lift_x, x'ten çift y'li noktayı üretir.

    Argümanlar:
        msg      : 32-byte mesaj (TapSighash veya başka)
        xonly_pk : 32-byte x-only public key
        sig      : 64-byte Schnorr imzası (R.x ‖ s)

    Döner:
        True = geçerli, False = geçersiz
    """
    if len(sig) != 64 or len(xonly_pk) != 32 or len(msg) != 32:
        return False
    try:
        Q = _lift_x(xonly_pk)          # Public key noktası (P ile karışmasın)
        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        if r >= P or s >= N:           # P burada secp256k1 field prime (modül seviyesi)
            return False
        e = int.from_bytes(
            _tagged_hash("BIP0340/challenge",
                         sig[:32] + xonly_pk + msg), "big") % N
        # R = s·G - e·Q  (Q.y negasyonu: secp256k1 field prime P ile)
        neg_Q = Point(Q.x, (-Q.y) % P)
        R = _point_add(_point_mul(s, G), _point_mul(e, neg_Q))
        if R.is_infinity or R.y % 2 != 0 or R.x != r:
            return False
        return True
    except Exception:
        return False


def schnorr_sign(msg: bytes, sk: bytes) -> bytes:
    """
    BIP-340 §Signing — deterministik Schnorr imzası.

    Algoritma:
        d0   = int(sk)
        P    = d0·G
        d    = d0  eğer P.y çift, aksi hâlde N - d0   (normalleştirme)
        t    = d XOR H("BIP0340/aux", rand)            (rastgele maskeleme)
        k0   = H("BIP0340/nonce", t ‖ P.x ‖ msg)  mod N
        R    = k0·G
        k    = k0 eğer R.y çift, aksi hâlde N - k0
        e    = H("BIP0340/challenge", R.x ‖ P.x ‖ msg)  mod N
        sig  = R.x (32B) ‖ (k + e·d) mod N (32B)

    Deterministik ama gizli rastgelelik (BIP-340 §Default Signing):
        Saf deterministik imza (RFC 6979 gibi) implementation
        hatasına karşı savunmasızdır. BIP-340, ek rastgelelik
        (aux_rand) karıştırarak bu riski azaltır.

    Argümanlar:
        msg : 32-bayt mesaj (BIP-341 sighash)
        sk  : 32-bayt özel anahtar

    Döner:
        64-bayt Schnorr imzası (R.x ‖ s)
    """
    d0 = int.from_bytes(sk, "big")
    P_pt = _point_mul(d0, G)
    d = d0 if P_pt.y % 2 == 0 else N - d0
    rand = _tagged_hash("BIP0340/aux", secrets.token_bytes(32))
    t = d ^ int.from_bytes(rand, "big")
    k0 = int.from_bytes(
        _tagged_hash("BIP0340/nonce",
                     t.to_bytes(32,"big") + _xonly(P_pt) + msg), "big") % N
    R = _point_mul(k0, G)
    k = k0 if R.y % 2 == 0 else N - k0
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge",
                     _xonly(R) + _xonly(P_pt) + msg), "big") % N
    sig = _xonly(R) + ((k + e * d) % N).to_bytes(32, "big")
    return sig


# ── Taproot Adresi ────────────────────────────────────────────────────────────

def _bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk

def _bech32m_encode(hrp: str, data: bytes) -> str:
    """Bech32m (BIP350) encode - Taproot adresleri için"""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    conv = []
    acc, bits = 0, 0
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            conv.append((acc >> bits) & 31)
    if bits:
        conv.append((acc << (5 - bits)) & 31)

    values = [1] + conv  # witness version 1
    hrp_exp = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    polymod = _bech32_polymod(hrp_exp + values + [0, 0, 0, 0, 0, 0]) ^ 0x2bc830a3
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in values + checksum)

def taproot_tweak_key(sk: bytes) -> Tuple[bytes, bytes]:
    """
    BIP-341 key-path tweak uygular (script-path yok, boş merkle root).

    Adımlar:
        d  = int(sk)
        P  = d·G  ;  eğer P.y tek → d = N - d  (BIP-340 normalleştirme)
        t  = H_TapTweak(P.x)   # tagged hash, 32 byte
        Q  = P + t·G           # output key
        d' = (d + t) mod N     # tweaked signing key

    Argümanlar:
        sk : 32-bayt özel anahtar (raw scalar)

    Döner:
        (internal_xonly: bytes,   # P.x — BIP-341 internal key
         tweaked_sk:     bytes)   # d' bytes — imzalama için kullanılır
    """
    d = int.from_bytes(sk, "big")
    P_pt = _point_mul(d, G)
    if P_pt.y % 2 != 0:       # BIP-340: even-y normalisation
        d = N - d
        P_pt = _point_mul(d, G)
    internal_xonly = _xonly(P_pt)
    t = int.from_bytes(_tagged_hash("TapTweak", internal_xonly), "big") % N
    tweaked_d = (d + t) % N
    return internal_xonly, tweaked_d.to_bytes(32, "big")


def taproot_address(sk: bytes, testnet: bool = True, bip341: bool = True) -> Tuple[bytes, str]:
    """
    BIP-341 P2TR adresi üretimi (key-path only).

    bip341=True  (varsayılan): standart BIP-341 tweak uygulanır.
        Q = P + H_TapTweak(P.x)·G  →  adres = bech32m(Q.x)
        Bitcoin Core tr() descriptor ve Sparrow ile uyumlu.

    bip341=False (legacy): tweak uygulanmaz, P.x doğrudan adres olur.
        Geriye dönük uyumluluk — mevcut cüzdanlar için.

    Argümanlar:
        sk      : 32-bayt özel anahtar
        testnet : True → tb1p... (testnet), False → bc1p... (mainnet)
        bip341  : True → BIP-341 tweak (önerilen), False → tweaksız (legacy)

    Döner:
        (internal_xonly: bytes, address: str)
        bip341=True → internal_xonly = P.x (internal key), adres Q.x'den türetilir
        bip341=False → internal_xonly = P.x = output key (tweak yok)
    """
    hrp = "tb" if testnet else "bc"
    if bip341:
        internal_xonly, tweaked_sk = taproot_tweak_key(sk)
        tweaked_d = int.from_bytes(tweaked_sk, "big")
        Q_pt = _point_mul(tweaked_d, G)
        output_xonly = _xonly(Q_pt)
        return internal_xonly, _bech32m_encode(hrp, output_xonly)
    else:
        # Legacy: tweaksız (mevcut cüzdanlar için geriye dönük uyumluluk)
        d = int.from_bytes(sk, "big")
        P_pt = _point_mul(d, G)
        if P_pt.y % 2 != 0:
            d = N - d
            P_pt = _point_mul(d, G)
        xonly = _xonly(P_pt)
        return xonly, _bech32m_encode(hrp, xonly)


# ── Yardımcı Serileştirme ─────────────────────────────────────────────────────

def varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    elif n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    else:
        return b"\xff" + struct.pack("<Q", n)

def le32(n: int) -> bytes: return struct.pack("<I", n)
def le64(n: int) -> bytes: return struct.pack("<q", n)


# ── BIP-341 Sighash ───────────────────────────────────────────────────────────

class UTXO:
    def __init__(self, txid: str, vout: int, value_sat: int, scriptpubkey: bytes):
        self.txid = txid           # hex string (little-endian olarak saklanır)
        self.vout = vout
        self.value_sat = value_sat
        self.scriptpubkey = scriptpubkey  # ör: OP_1 <32-byte>

class TxOutput:
    def __init__(self, value_sat: int, scriptpubkey: bytes):
        self.value_sat = value_sat
        self.scriptpubkey = scriptpubkey

def taproot_sighash(
    inputs: List[UTXO],
    outputs: List[TxOutput],
    input_index: int,
    sighash_type: int = 0,
    version: int = 2,
    locktime: int = 0,
) -> bytes:
    """
    BIP-341 §Common signature message — Taproot sighash hesaplama.

    sighash_type değerleri:
        0x00 = SIGHASH_DEFAULT  → tüm girdi ve çıktılar (önerilen)
        0x01 = SIGHASH_ALL      → SIGHASH_DEFAULT ile özdeş
        0x02 = SIGHASH_NONE     → çıktıları imzalamaz
        0x03 = SIGHASH_SINGLE   → yalnızca aynı indeksteki çıktıyı imzalar
        + 0x80 = SIGHASH_ANYONECANPAY  → yalnızca bu girdiyi imzalar

    Bu implementasyon yalnızca 0x00 (SIGHASH_DEFAULT) destekler.

    Sighash Bileşenleri (BIP-341 §Taproot sighash):
        sha_prevouts      = SHA256(Σ outpoints)           ← harcanan UTXO'lar
        sha_amounts       = SHA256(Σ input_values)        ← tüm tutarlar
        sha_scriptpubkeys = SHA256(Σ input_scriptpubkeys) ← tüm scriptler
        sha_sequences     = SHA256(Σ nSequences)
        sha_outputs       = SHA256(Σ outputs)
        spend_type        = 0x00 (key-path, annex yok)
        + bu girdi için: outpoint, amount, scriptpubkey, nSequence, index

    Legacy'den farkı:
        BIP-143'te yalnızca harcanan çıktının tutarı imzalanırdı.
        Donanım cüzdan saldırılarında kullanıcı sahte tutar imzalatılabilirdi.
        BIP-341, sha_amounts ile TÜM tutarları imzaya dahil eder.

    Argümanlar:
        inputs       : Tüm transaction girdileri (List[UTXO])
        outputs      : Tüm transaction çıktıları (List[TxOutput])
        input_index  : İmzalanan girdi indeksi (0'dan başlar)
        sighash_type : İmza kapsamı (varsayılan: 0 = DEFAULT)
        version      : nVersion (varsayılan: 2)
        locktime     : nLockTime (varsayılan: 0)

    Döner:
        32-bayt sighash — schnorr_sign() fonksiyonuna doğrudan verilir
    """
    # sha_prevouts
    sha_prevouts = hashlib.sha256(b"".join(
        bytes.fromhex(inp.txid)[::-1] + struct.pack("<I", inp.vout)
        for inp in inputs
    )).digest()

    # sha_amounts
    sha_amounts = hashlib.sha256(b"".join(
        le64(inp.value_sat) for inp in inputs
    )).digest()

    # sha_scriptpubkeys
    sha_scriptpubkeys = hashlib.sha256(b"".join(
        varint(len(inp.scriptpubkey)) + inp.scriptpubkey
        for inp in inputs
    )).digest()

    # sha_sequences (nSequence = 0xFFFFFFFD varsayılan RBF)
    sha_sequences = hashlib.sha256(b"".join(
        struct.pack("<I", 0xFFFFFFFD) for _ in inputs
    )).digest()

    # sha_outputs
    sha_outputs = hashlib.sha256(b"".join(
        le64(o.value_sat) + varint(len(o.scriptpubkey)) + o.scriptpubkey
        for o in outputs
    )).digest()

    # ── BIP-341 §Common signature message ────────────────────────────────────
    #
    # ANYONECANPAY (0x80) olmayan durumda (SIGHASH_DEFAULT = 0x00):
    #   Per-input alanlar (outpoint, amount, scriptPubKey, nSequence) EKLENMEZ.
    #   Bu veriler sha_prevouts / sha_amounts / sha_scriptpubkeys / sha_sequences
    #   toplu hash'lerinde zaten taahhüt edilmiştir.
    #   "Data about this input" bloğu yalnızca: spend_type + input_index (4B LE)
    #
    # ANYONECANPAY durumunda (hash_type & 0x80):
    #   sha_* alanları yoktur; bunun yerine bu girdinin outpoint/amount/spk/seq
    #   doğrudan mesaja eklenir.
    #
    # Kaynak: BIP-341 §Common signature message
    #   https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki

    is_anyonecanpay = bool(sighash_type & 0x80)
    base_type = sighash_type & 0x03  # ALL=0x01/DEFAULT=0x00, NONE=0x02, SINGLE=0x03

    msg = bytes([0x00])              # epoch
    msg += bytes([sighash_type])     # hash_type
    msg += le32(version)
    msg += le32(locktime)

    if not is_anyonecanpay:
        # Tüm girdi outpoints, tutarlar, scriptler, sequence'lar toplu hash'te
        msg += sha_prevouts
        msg += sha_amounts
        msg += sha_scriptpubkeys
        msg += sha_sequences

    if base_type not in (0x02,):     # SIGHASH_NONE değilse çıktılar eklenir
        msg += sha_outputs

    msg += bytes([0x00])             # spend_type: key-path spend, annex yok

    if is_anyonecanpay:
        # ANYONECANPAY: bu girdinin alanları doğrudan mesaja girer
        inp = inputs[input_index]
        msg += bytes.fromhex(inp.txid)[::-1]           # outpoint txid (32B LE)
        msg += struct.pack("<I", inp.vout)              # outpoint vout (4B LE)
        msg += le64(inp.value_sat)                      # amount (8B LE)
        msg += varint(len(inp.scriptpubkey)) + inp.scriptpubkey  # scriptPubKey
        msg += struct.pack("<I", 0xFFFFFFFD)            # nSequence (RBF)
    else:
        # Normal (non-ANYONECANPAY): yalnızca girdi indeksi
        msg += struct.pack("<I", input_index)           # input_index (4B LE)

    return _tagged_hash("TapSighash", msg)


# ── Transaction Serileştirme ──────────────────────────────────────────────────

def build_tx(
    inputs: List[UTXO],
    outputs: List[TxOutput],
    witnesses: List[bytes],
    version: int = 2,
    locktime: int = 0,
) -> bytes:
    """
    BIP-141 §Transaction ID — Segwit v1 (Taproot) serileştirme.

    Çıktı formatı (tam belge: BIP-141 §New serialization):
        nVersion    (4B, LE)
        0x00 0x01   segwit marker ve flag
        varint      girdi sayısı
        [girdi] x N:
            txid        (32B, LE — little-endian txid)
            vout        (4B, LE)
            scriptSig   0x00  ← native segwit: boş
            nSequence   (4B, LE)  0xFFFFFFFD = RBF aktif
        varint      çıktı sayısı
        [çıktı] x M:
            value       (8B, LE, satoshi cinsinden)
            scriptPubKey (varint uzunluk + veri)
        [witness] x N:
            varint  item sayısı (key-path için: 1)
            varint  item uzunluğu (64)
            data    64-bayt Schnorr imzası
        nLockTime   (4B, LE)

    TxID ve WTxID:
        TxID  = SHA256d(legacy_format)   witness alanı dahil değil
        WTxID = SHA256d(full_format)     witness dahil
        Bitcoin düğümleri işlem kimliği olarak TxID kullanır.

    RBF (Replace-By-Fee):
        nSequence = 0xFFFFFFFD → işlem mempool'da ücret artırılabilir.
        Kesinlik gerekiyorsa 0xFFFFFFFF kullanın.

    Argümanlar:
        inputs    : Harcanan UTXO listesi
        outputs   : Oluşturulan çıktı listesi
        witnesses : Her girdi için imza (Taproot key-path: 64B Schnorr)
        version   : nVersion (varsayılan: 2)
        locktime  : nLockTime (varsayılan: 0)

    Döner:
        Ham transaction baytları — hex'e çevrilerek yayınlanabilir
    """
    raw = b""
    raw += le32(version)
    raw += b"\x00\x01"             # segwit marker + flag

    # Girdiler
    raw += varint(len(inputs))
    for inp in inputs:
        raw += bytes.fromhex(inp.txid)[::-1]
        raw += struct.pack("<I", inp.vout)
        raw += b"\x00"             # scriptSig boş (native segwit)
        raw += struct.pack("<I", 0xFFFFFFFD)  # nSequence (RBF)

    # Çıktılar
    raw += varint(len(outputs))
    for out in outputs:
        raw += le64(out.value_sat)
        raw += varint(len(out.scriptpubkey))
        raw += out.scriptpubkey

    # Witness
    for sig in witnesses:
        raw += varint(1)           # 1 witness item
        raw += varint(len(sig))
        raw += sig

    raw += le32(locktime)
    return raw


# ── Testnet API ───────────────────────────────────────────────────────────────

ESPLORA_TESTNET = "https://mempool.space/testnet4/api"

def get_utxos(address: str) -> list:
    """
    Esplora REST API — adrese ait UTXO listesini çek.

    Endpoint: GET /address/{address}/utxo
    Yanıt   : [{"txid": str, "vout": int, "value": int,
                 "status": {"confirmed": bool, "block_height": int}}, ...]

    Yalnızca onaylanmış UTXO'ları işlemek için:
        utxos = [u for u in get_utxos(addr) if u["status"]["confirmed"]]

    Argümanlar:
        address : Bech32m formatında P2TR adresi (tb1p... veya bc1p...)

    Döner:
        UTXO sözlük listesi (boş liste = bakiye yok veya API hatası)
    """
    url = f"{ESPLORA_TESTNET}/address/{address}/utxo"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [HATA] UTXO sorgusu başarısız: {e}")
        return []

def get_tx_hex(txid: str) -> Optional[str]:
    """Tx ham hex'ini çek (scriptpubkey için)"""
    url = f"{ESPLORA_TESTNET}/tx/{txid}/hex"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode()
    except Exception as e:
        print(f"  [HATA] Tx sorgusu başarısız: {e}")
        return None

def broadcast_tx(tx_hex: str) -> Optional[str]:
    """
    Esplora REST API — ham transaction'ı testnet'e yayınla.

    Endpoint : POST /tx
    İstek    : Content-Type: text/plain, body = tx_hex
    Başarı   : HTTP 200, body = txid (64-char hex)
    Hata     : HTTP 400, body = hata mesajı

    Yaygın Hata Mesajları:
        "non-mandatory-script-verify-flag" → imza veya sighash hatalı
        "dust"                             → çıktı 546 sat'ın altında
        "bad-txns-inputs-missingorspent"   → UTXO zaten harcanmış
        "min relay fee not met"            → ücret çok düşük

    Argümanlar:
        tx_hex : build_tx().hex() çıktısı

    Döner:
        txid string (başarı) | None (hata)
    """
    url = f"{ESPLORA_TESTNET}/tx"
    data = tx_hex.encode()
    req = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()   # txid döner
    except urllib.error.HTTPError as e:
        print(f"  [HATA] Yayınlama başarısız: {e.read().decode()}")
        return None


# ── Demo ──────────────────────────────────────────────────────────────────────

def demo():
    print("=" * 60)
    print("   Taproot Ham Transaction Demo (Testnet)")
    print("=" * 60)

    # 1. Anahtar çifti
    sk = bytes.fromhex(
        # Gerçek kullanımda: secrets.token_bytes(32).hex()
        "b94f5374fce5edbc8e2a8697c15331677e6ebf0b000000000000000000000001"
    )
    # sk = secrets.token_bytes(32)   # Yeni anahtar üretmek için bu satırı aç
    import secrets
    sk = secrets.token_bytes(32)
    print("SK (yedekle!):", sk.hex())

    xonly_pk, address = taproot_address(sk, testnet=True)

    print(f"\n[Cüzdan]")
    print(f"  Adres (testnet) : {address}")
    print(f"  x-only pubkey   : {xonly_pk.hex()}")

    # 2. Çıktı scriptleri
    #    Taproot: OP_1 (0x51) + OP_PUSHBYTES_32 (0x20) + <32-byte-xonly>
    my_spk = bytes([0x51, 0x20]) + xonly_pk

    # 3. Gerçek UTXO kontrolü
    print(f"\n[UTXO Sorgusu] {address}")
    utxos = get_utxos(address)

    if not utxos:
        print("  Bu adrese ait UTXO bulunamadı.")
        print("  Testnet faucet'ten bakiye al ve tekrar dene.")
        print(f"  Faucet: https://testnet-faucet.mempool.co")
        print()
        # Simülasyon ile devam et
        print("  [SİMÜLASYON] Örnek bir UTXO ile devam ediliyor...")
        fake_txid = "a" * 64
        utxos = [{"txid": fake_txid, "vout": 0, "value": 10000, "status": {"confirmed": True}}]
        simulation = True
    else:
        simulation = False
        for u in utxos:
            print(f"  txid: {u['txid']}  vout: {u['vout']}  değer: {u['value']} sat")

    # 4. İlk UTXO'yu harca
    u = utxos[0]
    inp_utxo = UTXO(
        txid=u["txid"],
        vout=u["vout"],
        value_sat=u["value"],
        scriptpubkey=my_spk,
    )

    # 5. Çıktılar: 8000 sat gönder, 1000 sat ücret, kalan para üstü
    send_sat   = 8000
    fee_sat    = 1000
    change_sat = inp_utxo.value_sat - send_sat - fee_sat

    # Alıcı: kendi adresimize gönder (demo)
    recipient_spk = my_spk
    change_spk    = my_spk

    outputs = []
    if send_sat > 0:
        outputs.append(TxOutput(send_sat, recipient_spk))
    if change_sat > 546:             # dust limiti
        outputs.append(TxOutput(change_sat, change_spk))

    print(f"\n[Transaction]")
    print(f"  Girdi       : {inp_utxo.value_sat} sat")
    print(f"  Gönderim    : {send_sat} sat")
    print(f"  Para üstü   : {change_sat} sat")
    print(f"  Ücret       : {fee_sat} sat")

    # 6. Sighash hesapla
    sighash = taproot_sighash(
        inputs=[inp_utxo],
        outputs=outputs,
        input_index=0,
    )
    print(f"\n[Sighash] {sighash.hex()}")

    # 7. Schnorr ile imzala
    sig = schnorr_sign(sighash, sk)
    print(f"[İmza]    {sig.hex()}")

    # 8. Ham transaction oluştur
    raw_tx = build_tx(
        inputs=[inp_utxo],
        outputs=outputs,
        witnesses=[sig],
    )
    tx_hex = raw_tx.hex()
    print(f"\n[Ham Transaction ({len(raw_tx)} bayt)]")
    print(f"  {tx_hex}")

    # Txid = double-SHA256(raw_tx) ancak legacy kısım (segwit hariç)
    # Basit gösterim için hash hesapla
    txid = hashlib.sha256(hashlib.sha256(raw_tx).digest()).digest()[::-1].hex()
    print(f"\n[Tx Hash (tahmini)] {txid}")

    # 9. Yayınla
    if not simulation:
        print(f"\n[Yayınlama] Testnet'e gönderiliyor...")
        result = broadcast_tx(tx_hex)
        if result:
            print(f"  Başarılı! Txid: {result}")
            print(f"  Explorer: https://mempool.space/testnet4/tx/{result}")
        else:
            print("  Yayınlama başarısız.")
    else:
        print("\n[Yayınlama] Simülasyon modunda - gerçek UTXO olmadığı için atlandı.")
        print("  Gerçek test için:")
        print("  1. Yukarıdaki adresi bir testnet faucet'e gir")
        print("  2. sk satırını secrets.token_bytes(32) ile değiştir")
        print("  3. Scripti tekrar çalıştır")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    demo()
