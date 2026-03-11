# Bitcoin Taproot Developer Toolkit

Sıfır dış bağımlılıkla, **BIP-340 / BIP-341 / BIP-327** standartlarını
uçtan uca implement eden eğitim ve prototip amaçlı Python kütüphanesi.

```
btc_examples/
├── musig2.py     # MuSig2 (BIP-327) — n-of-n Schnorr multisig
└── raw_tx.py     # Taproot ham transaction üretimi ve testnet yayını
```

---

## İçindekiler

1. [Arka Plan — Neden Taproot?](#1-arka-plan--neden-taproot)
2. [BIP Referansları](#2-bip-referansları)
3. [Kurulum](#3-kurulum)
4. [Modül 1 — musig2.py](#4-modül-1--musig2py)
   - [Mimari](#41-mimari)
   - [Matematiksel Temel](#42-matematiksel-temel)
   - [API Referansı](#43-api-referansı)
   - [Kullanım Örnekleri](#44-kullanım-örnekleri)
5. [Modül 2 — raw_tx.py](#5-modül-2--raw_txpy)
   - [Transaction Yapısı](#51-transaction-yapısı)
   - [BIP-341 Sighash Algoritması](#52-bip-341-sighash-algoritması)
   - [API Referansı](#53-api-referansı)
   - [Testnet Kullanım Kılavuzu](#54-testnet-kullanım-kılavuzu)
6. [Güvenlik Uyarıları](#6-güvenlik-uyarıları)
7. [Mimari Kararlar ve Sınırlılıklar](#7-mimari-kararlar-ve-sınırlılıklar)
8. [Hata Ayıklama](#8-hata-ayıklama)

---

## 1. Arka Plan — Neden Taproot?

**Taproot** (Kasım 2021, blok 709 632), Bitcoin tarihinin en kapsamlı
konsensüs güncellemesidir. Üç BIP'ı bir arada sunar:

| BIP | Konu | Getirisi |
|-----|------|----------|
| BIP-340 | Schnorr İmzalar | 64-bayt imza; doğrusal agregasyon; batch doğrulama |
| BIP-341 | Taproot | Pay-to-Taproot (P2TR) çıktı tipi; key-path / script-path harcama |
| BIP-342 | Tapscript | Script sisteminin modernize edilmesi |

### Geleneksel Multisig'e Karşı MuSig2

```
Eski yöntem (P2SH / P2WSH multisig):
  Zincirde: OP_2 <pk1> <pk2> <pk3> OP_3 OP_CHECKMULTISIG
  Witness : <sig1> <sig2> + redeem script
  → N imza, N açık anahtar zincirde görünür (gizlilik yok)
  → vbyte maliyeti O(N)

MuSig2 + Taproot (key-path):
  Zincirde: OP_1 <32-byte-aggregate-key>
  Witness : <64-byte-schnorr-sig>
  → Tüm katılımcılar tek imzaya indirgenir (gizlilik tam)
  → vbyte maliyeti O(1) — kaç imzacı olursa olsun
```

---

## 2. BIP Referansları

| Standart | Başlık | Kritik Bölümler |
|----------|--------|-----------------|
| [BIP-340](https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki) | Schnorr Signatures | §Specification, §Signing, §Verification |
| [BIP-341](https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki) | Taproot | §Script validation rules, §Signature validation |
| [BIP-327](https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki) | MuSig2 | §Key Aggregation, §Nonce Generation, §Signing |
| [BIP-350](https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki) | Bech32m | §Encoding |

---

## 3. Kurulum

### Gereksinimler

- Python 3.8+
- Standart kütüphane dışında **sıfır zorunlu bağımlılık**
  (`hashlib`, `secrets`, `struct`, `json`, `urllib` yeterli)

### Sanal Ortam Oluşturma

```bash
# Ortamı oluştur
python3 -m venv ~/taproot

# Aktive et (Linux / macOS)
source ~/taproot/bin/activate

# Aktive et (Windows PowerShell)
~/taproot/Scripts/Activate.ps1

# İsteğe bağlı: yüksek seviyeli wrapper kütüphaneler
pip install bitcoin-utils bip_utils coincurve
```

### Dosyaları Çalıştır

```bash
cd ~/taproot/btc_examples

# MuSig2 demo
python musig2.py

# Taproot transaction demo (testnet — internet gerektirir)
python raw_tx.py
```

---

## 4. Modül 1 — musig2.py

### 4.1 Mimari

```
┌─────────────────────────────────────────────────────────────┐
│                     KATILIMCI (Alice)                       │
│  sk_alice ──► P_alice = sk·G                                │
└──────────────────────┬──────────────────────────────────────┘
                       │ pk_alice (paylaş)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  ANAHTAR AGREGASYONU (KeyAgg)               │
│                                                             │
│  L  = H("KeyAgg list", sort([pk1, pk2, ...]))               │
│  a_i = H("KeyAgg coefficient", L ‖ pk_i) mod N             │
│  Q  = Σ  a_i · P_i          (agrege açık anahtar)          │
└──────────────────────┬──────────────────────────────────────┘
                       │ Q (herkese duyur)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    NONCE TURU (Round 1)                     │
│                                                             │
│  Her katılımcı bağımsız olarak:                             │
│    rand  = random_bytes(32)                                 │
│    k1    = H("MuSig/nonce", rand ‖ sk ‖ pk ‖ msg ‖ 0x00)  │
│    k2    = H("MuSig/nonce", rand ‖ sk ‖ pk ‖ msg ‖ 0x01)  │
│    R1,R2 = k1·G, k2·G     (paylaş, gizli k'ları sakla)     │
└──────────────────────┬──────────────────────────────────────┘
                       │ (R1_i, R2_i) paylaş
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   NONCE AGREGASYONU                         │
│                                                             │
│  R1 = Σ R1_i ,  R2 = Σ R2_i                                │
│  b  = H("MuSig/noncecoef", R1 ‖ R2 ‖ Q ‖ msg)             │
│  R  = R1 + b·R2                (nihai nonce noktası)        │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   İMZA TURU (Round 2)                       │
│                                                             │
│  e   = H("BIP0340/challenge", R.x ‖ Q.x ‖ msg)            │
│  s_i = k1 + b·k2 + e·a_i·d_i  (mod N)                     │
│        (d_i: işaret düzeltilmiş özel anahtar)              │
└──────────────────────┬──────────────────────────────────────┘
                       │ s_i paylaş
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   İMZA BİRLEŞTİRME                         │
│                                                             │
│  s   = Σ s_i  (mod N)                                      │
│  sig = R.x ‖ s          (64 bayt BIP-340 Schnorr imzası)   │
│                                                             │
│  Doğrulama: s·G == R + e·Q    ← herhangi bir düğüm yapabilir│
└─────────────────────────────────────────────────────────────┘
```

### 4.2 Matematiksel Temel

#### Neden İki Nonce? (Wagner Saldırısına Karşı)

Tek nonce kullanıldığında, kötü niyetli bir koordinatör
**Wagner'in genelleştirilmiş doğum günü saldırısı** ile özel
anahtarları elde edebilir. İki nonce ve `b` katsayısı bu saldırıyı
kırıktır çünkü `b` tüm nonce taahhütlerini bağlar.

```
Tek nonce (ZAYIF):  R = Σ R_i
İki nonce  (GÜÇLÜ): R = R1 + b·R2  ,  b = H(R1‖R2‖Q‖msg)
```

#### İşaret Normalleştirme (BIP-340)

Secp256k1 üzerinde her `x` koordinatı için iki `y` değeri mevcuttur.
BIP-340, her zaman **çift y koordinatını** seçer (implicit y).
Bu yüzden hem Q hem R normalize edilir; işaret uyumsuzluğu
özel anahtar / nonce'un negatifini alarak çözülür.

```python
if not has_even_y(Q):
    d = N - d        # özel anahtarı negate et
if not has_even_y(R):
    k1 = N - k1      # nonce'ları negate et
    k2 = N - k2
```

### 4.3 API Referansı

#### `key_aggregation(pk_list: List[bytes]) -> (Point, List[int])`

```
Giriş  : Katılımcıların 33-bayt sıkıştırılmış açık anahtarları
Çıkış  : (Q, coefficients)
           Q    — agrege Taproot açık anahtarı (Point)
           coeffs — her pk için a_i katsayısı

NOT: pk_list mutlaka sıralı olmalıdır (BIP-327 §Key Aggregation).
     Sıralama deterministik olmazeğer farklı sıra kullanılırsa
     farklı Q üretilir → funds erişilemez hale gelir.
```

```python
pk_list = sorted([pk_alice, pk_bob])   # MUTLAKA sorted()
Q, coeffs = key_aggregation(pk_list)
```

---

#### `nonce_gen(sk, pk, msg) -> ((k1,k2), (R1,R2))`

```
Giriş  : sk  — 32-bayt özel anahtar
          pk  — 33-bayt açık anahtar
          msg — 32-bayt mesaj (genellikle tx sighash)
Çıkış  : (secret_nonce, public_nonce)
           secret_nonce = (k1, k2) — ASLA paylaşılmaz, tek kullanım
           public_nonce = (R1, R2) — diğer katılımcılara gönderilir

KRITIK: Aynı (sk, msg) çifti için nonce_gen iki kez çağrılmamalıdır.
        secrets.token_bytes(32) ile rastgelelik sağlanır.
```

---

#### `partial_sign(secret_nonce, sk, coeff, Q, agg_nonce, msg) -> int`

```
Giriş  : secret_nonce — nonce_gen'den dönen (k1, k2)
          sk           — 32-bayt özel anahtar
          coeff        — key_agg_coeff() ile hesaplanan a_i
          Q            — agrege açık anahtar
          agg_nonce    — nonce_agg() sonucu (R1, R2)
          msg          — 32-bayt sighash
Çıkış  : s_i (int) — kısmi imza skaler değeri

s_i = k1 + b·k2 + H(R‖Q‖msg) · a_i · d_i  (mod N)
```

---

#### `partial_sig_agg(partial_sigs, R) -> bytes`

```
Giriş  : partial_sigs — [s1, s2, ...] kısmi imzalar
          R            — session_ctx'ten gelen nihai nonce noktası
Çıkış  : 64-bayt Schnorr imzası (R.x ‖ s)
          s = Σ s_i mod N
```

---

#### `schnorr_verify(msg, pubkey_xonly, sig) -> bool`

```
Giriş  : msg          — 32-bayt orijinal mesaj
          pubkey_xonly — 32-bayt x-only açık anahtar
          sig          — 64-bayt Schnorr imzası
Çıkış  : True / False

Doğrulama denklemi: s·G == R + e·Q
Bu fonksiyon standart BIP-340 doğrulaması yapar;
Bitcoin düğümleri de aynı algoritmayı kullanır.
```

### 4.4 Kullanım Örnekleri

#### 2-of-2 MuSig2 (Temel Akış)

```python
import secrets
from musig2 import (
    point_mul, point_to_bytes, xonly_bytes,
    key_aggregation, key_agg_coeff,
    nonce_gen, nonce_agg, session_ctx,
    partial_sign, partial_sig_agg, schnorr_verify,
    tagged_hash, G
)

# ── 1. Anahtar Çiftleri ───────────────────────────────────────
sk_alice = secrets.token_bytes(32)
sk_bob   = secrets.token_bytes(32)

P_alice = point_mul(int.from_bytes(sk_alice, "big"), G)
P_bob   = point_mul(int.from_bytes(sk_bob,   "big"), G)

pk_alice = point_to_bytes(P_alice)
pk_bob   = point_to_bytes(P_bob)

# ── 2. Anahtar Agregasyonu ────────────────────────────────────
pk_list = sorted([pk_alice, pk_bob])   # sıralama zorunlu
Q, _    = key_aggregation(pk_list)

a_alice = key_agg_coeff(pk_list, pk_alice)
a_bob   = key_agg_coeff(pk_list, pk_bob)

print("Taproot x-only pubkey:", xonly_bytes(Q).hex())

# ── 3. İmzalanacak Mesaj (normalde BIP-341 sighash) ──────────
msg = tagged_hash("TapSighash", b"transaction_data_here")

# ── 4. Nonce Üretimi (Round 1) — eş zamanlı değil, sıralı ────
(k_alice, pub_nonce_alice) = nonce_gen(sk_alice, pk_alice, msg)
(k_bob,   pub_nonce_bob)   = nonce_gen(sk_bob,   pk_bob,   msg)

# pub_nonce'lar karşı tarafa iletilir

# ── 5. Nonce Agregasyonu ──────────────────────────────────────
agg_R1, agg_R2 = nonce_agg([pub_nonce_alice, pub_nonce_bob])
agg_nonce = (agg_R1, agg_R2)

# ── 6. Kısmi İmzalar (Round 2) ────────────────────────────────
s_alice = partial_sign(k_alice, sk_alice, a_alice, Q, agg_nonce, msg)
s_bob   = partial_sign(k_bob,   sk_bob,   a_bob,   Q, agg_nonce, msg)

# ── 7. Birleştirme ────────────────────────────────────────────
R, _ = session_ctx(agg_nonce, Q, msg)
sig  = partial_sig_agg([s_alice, s_bob], R)

# ── 8. Doğrulama ─────────────────────────────────────────────
assert schnorr_verify(msg, xonly_bytes(Q), sig), "İmza geçersiz!"
print("İmza doğrulandı:", sig.hex())
```

#### 3-of-3 MuSig2 (N katılımcıya genişletme)

```python
# N katılımcı için tek fark: pk_list uzunluğu
participants = [(secrets.token_bytes(32)) for _ in range(3)]
pub_keys = sorted([
    point_to_bytes(point_mul(int.from_bytes(sk, "big"), G))
    for sk in participants
])

Q, _ = key_aggregation(pub_keys)
# Geri kalan akış aynı — partial_sign N kez çağrılır
```

---

## 5. Modül 2 — raw_tx.py

### 5.1 Transaction Yapısı

Segwit v1 (Taproot) serileştirme formatı (BIP-141):

```
┌──────────────────────────────────────────────────────────────┐
│ nVersion   (4 bayt, little-endian)                          │
│ marker     (1 bayt: 0x00)   ← segwit işareti                │
│ flag       (1 bayt: 0x01)   ← segwit işareti                │
├──────────────────────────────────────────────────────────────┤
│ INPUT COUNT  (varint)                                        │
│ ┌────────────────────────────────────────────────────────┐  │
│ │ txid        (32 bayt, little-endian)                   │  │
│ │ vout        (4 bayt, little-endian)                    │  │
│ │ scriptSig   (varint + data) ← Taproot'ta BOŞTUR (0x00) │  │
│ │ nSequence   (4 bayt) ← 0xFFFFFFFD (RBF aktif)          │  │
│ └────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│ OUTPUT COUNT (varint)                                        │
│ ┌────────────────────────────────────────────────────────┐  │
│ │ value       (8 bayt, little-endian, satoshi)           │  │
│ │ scriptPubKey (varint + data)                           │  │
│ │              OP_1(0x51) OP_PUSH32(0x20) <xonly-key>   │  │
│ └────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│ WITNESS (her girdi için)                                     │
│ ┌────────────────────────────────────────────────────────┐  │
│ │ item count  (varint: 1)                                │  │
│ │ item[0] len (varint: 64)                               │  │
│ │ item[0] data (64 bayt Schnorr imzası)                  │  │
│ └────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│ nLockTime   (4 bayt, little-endian)                         │
└──────────────────────────────────────────────────────────────┘

TxID  = SHA256(SHA256(legacy_serialization))  ← witness hariç
WTxID = SHA256(SHA256(full_serialization))    ← witness dahil
```

### 5.2 BIP-341 Sighash Algoritması

Taproot key-path harcama için sighash (BIP-341 §Common signature message):

```
SigHash = TaggedHash("TapSighash",
    0x00                 ← epoch
  ‖ hash_type            ← 0x00 = SIGHASH_DEFAULT (tümü)
  ‖ nVersion             (4 bayt LE)
  ‖ nLockTime            (4 bayt LE)
  ‖ sha_prevouts         = SHA256(Σ outpoints)
  ‖ sha_amounts          = SHA256(Σ input_values)
  ‖ sha_scriptpubkeys    = SHA256(Σ input_scriptpubkeys)
  ‖ sha_sequences        = SHA256(Σ nSequences)
  ‖ sha_outputs          = SHA256(Σ outputs)
  ‖ spend_type           = 0x00 (key-path, annex yok)
  ‖ outpoint (bu girdi)  (txid + vout)
  ‖ amount   (bu girdi)  (8 bayt LE)
  ‖ scriptpubkey (bu girdi)
  ‖ nSequence (bu girdi) (4 bayt LE)
  ‖ input_index          (4 bayt LE)
)
```

**Neden yeni sighash?** BIP-143 (SegWit v0) belirli saldırılara (özellikle
quadratic hashing) açıktı. BIP-341 tüm girdi tutarlarını ve scriptpubkey'leri
sighash'e dahil eder; imzacı hangi UTXO'yu harcadığından kesinlikle emin olur.

### 5.3 API Referansı

#### `taproot_address(sk, testnet) -> (xonly_pubkey, address)`

```
Giriş  : sk      — 32-bayt özel anahtar (bytes)
          testnet — True: tb1p... adresi, False: bc1p... adresi
Çıkış  : (xonly_pk, bech32m_address)
           xonly_pk — 32-bayt x-only açık anahtar
           adres    — bech32m kodlanmış P2TR adresi

İşlem:
  d    = int(sk)
  P    = d·G
  eğer P.y tek ise → d = N - d  (BIP-340 normalleştirme)
  xonly = P.x (32 bayt)
  adres = bech32m_encode(hrp, xonly)
```

---

#### `taproot_sighash(inputs, outputs, input_index, ...) -> bytes`

```
Giriş  : inputs       — List[UTXO]
          outputs      — List[TxOutput]
          input_index  — hangi girdi imzalanıyor
          sighash_type — 0: SIGHASH_DEFAULT, 1: ALL, 2: NONE, 3: SINGLE
          version      — tx versiyonu (varsayılan: 2)
          locktime     — nLockTime (varsayılan: 0)
Çıkış  : 32-bayt sighash (imzalanacak mesaj)
```

---

#### `build_tx(inputs, outputs, witnesses, ...) -> bytes`

```
Giriş  : inputs    — List[UTXO]
          outputs   — List[TxOutput]
          witnesses — List[bytes]  (her girdi için 64-bayt Schnorr imzası)
          version   — (varsayılan: 2)
          locktime  — (varsayılan: 0)
Çıkış  : ham transaction baytları (broadcast'a hazır)
```

---

#### `broadcast_tx(tx_hex) -> Optional[str]`

```
Giriş  : tx_hex — hex string olarak ham transaction
Çıkış  : txid string (başarı) | None (hata)
Endpoint: https://mempool.space/testnet/api/tx (HTTP POST)
```

### 5.4 Testnet Kullanım Kılavuzu

#### Adım 1 — Yeni Anahtar Oluştur

```python
# raw_tx.py içinde sk satırını değiştir:
import secrets
sk = secrets.token_bytes(32)
print("SK (yedekle!):", sk.hex())
```

```bash
python raw_tx.py
# Çıktıdan adresi kopyala: tb1p...
```

#### Adım 2 — Testnet Bakiyesi Al

Aşağıdaki faucet sitelerinden birini kullan:

| Faucet | URL |
|--------|-----|
| mempool.space | https://mempool.space/testnet/faucet |
| coinfaucet.eu | https://coinfaucet.eu/en/btc-testnet |
| bitcoinfaucet.uo1 | https://bitcoinfaucet.uo1.net |

```bash
# UTXO onayını bekle (genellikle 1–2 blok ~ 10–20 dakika)
# Durumu kontrol et:
curl https://mempool.space/testnet/api/address/tb1p.../utxo
```

#### Adım 3 — Transaction Oluştur ve Yayınla

```python
# raw_tx.py — gerçek kullanım için düzenle:

# Alıcı adresi belirle (başka bir testnet adresi)
recipient_xonly = bytes.fromhex("aabbcc...")   # 32 bayt
recipient_spk   = bytes([0x51, 0x20]) + recipient_xonly

# Ücret hesaplama (yaklaşık):
# Taproot key-path tx boyutu ≈ 110-150 vbyte
# Testnet ücreti genellikle 1-5 sat/vbyte yeterli
fee_sat = 200  # 200 sat sabit ücret (testnet)
```

```bash
python raw_tx.py
# Başarılı ise txid ekrana yazdırılır:
# Explorer: https://mempool.space/testnet/tx/<txid>
```

#### Adım 4 — MuSig2 ile Taproot Harcama

```python
# musig2.py'den Q elde et
from musig2 import key_aggregation, xonly_bytes, ...
Q, _ = key_aggregation(sorted([pk_alice, pk_bob]))

# raw_tx.py'de taproot adresini Q'dan üret
q_xonly = xonly_bytes(Q)
spk = bytes([0x51, 0x20]) + q_xonly
addr = _bech32m_encode("tb", q_xonly)

# Sighash hesapla → musig2.py ile imzala → yayınla
sighash = taproot_sighash(inputs, outputs, 0)
sig = partial_sig_agg([
    partial_sign(k_alice, sk_alice, a_alice, Q, agg_nonce, sighash),
    partial_sign(k_bob,   sk_bob,   a_bob,   Q, agg_nonce, sighash),
], R)
```

---

## 6. Güvenlik Uyarıları

### Kritik — Üretimde Kullanmadan Önce

> **Bu kod eğitim amaçlıdır. Gerçek fonları yönetmek için
> production-grade kütüphaneler kullanın.**

| Risk | Açıklama | Önlem |
|------|----------|-------|
| **Nonce yeniden kullanımı** | Aynı `(sk, msg)` ile iki kez imza üretmek özel anahtarı sızdırır | Her imza oturumu için `nonce_gen()` yalnızca bir kez çağrılmalı |
| **Yan kanal saldırıları** | Python'un büyük tam sayı aritmetiği sabit zamanlı değildir | Üretimde `libsecp256k1` (C kütüphanesi) kullanılmalı |
| **Özel anahtar yönetimi** | `sk` değişkeni bellekte düz metin olarak durur | HSM / güvenli depolama + `secrets.token_bytes()` ile üretim |
| **Sıralama determinizmi** | `sorted(pk_list)` her iki tarafta **aynı** sonucu vermeli | Katılımcılar pk_list'i protokol dışı kanal üzerinden doğrulamalı |
| **Dust limiti** | 546 sat altı çıktılar düğümlerce reddedilir | `change_sat > 546` kontrolü yapılmalı |
| **RBF** | `nSequence=0xFFFFFFFD` ile işlem değiştirilebilir | Kesinlik gerekiyorsa `0xFFFFFFFF` kullan |

### Güvenli Özel Anahtar Üretimi

```python
import secrets

# DOGRU — kriptografik güçlü rastgelelik
sk = secrets.token_bytes(32)

# YANLIS — tahmin edilebilir
sk = bytes(32)                   # sıfır anahtar
sk = b"my_password"[:32]         # düşük entropi
```

---

## 7. Mimari Kararlar ve Sınırlılıklar

### Tasarım Tercihleri

| Karar | Gerekçe |
|-------|---------|
| Sıfır dış bağımlılık | Tedarik zinciri riski yok; BIP davranışı şeffaf |
| Pure Python aritmetik | Okunabilirlik öncelikli; hız ikincil |
| Bech32m sıfırdan implement | BIP-350'yi satır satır anlamak için |
| `secrets` modülü | `random` modülü kriptografi için yetersiz |

### Bilinen Sınırlılıklar

- **Script-path spend yok**: Yalnızca key-path harcama desteklenir;
  `MAST` / `Tapscript` implementasyonu dışarıda bırakıldı.
- **Tek girdi / sınırlı çıktı**: Çoklu UTXO harcama için
  `taproot_sighash`'e döngü eklenmesi gerekir.
- **PSBT desteği yok**: BIP-174 (Partially Signed Bitcoin Transaction)
  entegrasyonu eklenmedi.
- **Hız**: Büyük N için `point_mul` yavaş; `coincurve` ile değiştirilebilir.

### Üretim İçin Önerilen Kütüphaneler

```python
# Python — yüksek seviye
from bitcoinutils.setup import setup
from bitcoinutils.transactions import Transaction

# Python — Schnorr / secp256k1
import coincurve   # libsecp256k1 binding

# Python — tam BIP-327 MuSig2
# (Kasım 2023 itibarıyla resmi Python impl. yok;
#  reference impl.: https://github.com/bitcoin/bips/blob/master/bip-0327)
```

---

## 8. Hata Ayıklama

### Yaygın Hatalar

#### `ValueError: Geçersiz nokta`
```
Sebep  : Bozuk veya sıkıştırılmamış public key verisi
Çözüm  : 33-bayt olduğunu, prefix'in 0x02 veya 0x03 olduğunu doğrula
```

#### Sighash Uyuşmazlığı
```
Sebep  : UTXO value veya scriptpubkey yanlış girilmiş
Çözüm  : Esplora API'dan tx hex'i alıp UTXO değerlerini doğrula
         curl https://mempool.space/testnet/api/tx/<txid>
```

#### `[HATA] Yayınlama başarısız: non-mandatory-script-verify-flag`
```
Sebep  : İmza geçersiz veya sighash yanlış hesaplanmış
Çözüm  : schnorr_verify() ile imzayı lokal doğrula
          tx'i https://btc-script-debugger.visvirial.com ile incele
```

#### `[HATA] Yayınlama başarısız: dust`
```
Sebep  : Çıktı değeri 546 sat'ın altında
Çözüm  : change_sat ve send_sat değerlerini artır
```

### Test Vektörleri

BIP-340 imza doğrulaması için resmi test vektörleri:

```python
# https://github.com/bitcoin/bips/blob/master/bip-0340/test-vectors.csv
msg    = bytes.fromhex("243F6A8885A308D313198A2E03707344A4093822299F31D008")
pubkey = bytes.fromhex("F9308A019258C31049344F85F89D5229B531C845836F99B08")
sig    = bytes.fromhex("E907831F80848D1069A5371B402410364BDF1C5F8307B0084")

assert schnorr_verify(msg[:32], pubkey[:32], sig[:64])
```

---

## Lisans

MIT License — eğitim ve araştırma amaçlı serbestçe kullanılabilir.
Üretim ortamında kullanımdan önce bağımsız güvenlik denetimi zorunludur.

---

*Bitcoin BIP'leri sürekli gelişmektedir. En güncel standartlar için
[bitcoin/bips](https://github.com/bitcoin/bips) reposunu takip edin.*
