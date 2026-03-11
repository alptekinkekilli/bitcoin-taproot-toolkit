"""
musig2.py — BIP-327 MuSig2 Referans Implementasyonu
=====================================================
Standart : BIP-327  (https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki)
Bağımlılık: Yalnızca Python standart kütüphanesi (hashlib, secrets, struct)
Amaç     : Eğitim ve prototip; üretim ortamı için güvenlik denetimi zorunludur.

Protokol Özeti
--------------
MuSig2, n-of-n eşik şemasını Schnorr imzası görünümüne indirgeyen
iki turlu bir multisig protokolüdür. Zincir üzerinde tek imza ve tek
açık anahtar göründüğünden hem gizlilik hem de verimlilik sağlanır.

İki Tur Akışı
-------------
  Tur 1 (Nonce Paylaşımı):
    Her katılımcı bağımsız olarak iki nonce üretir (k1, k2),
    karşılık gelen R1 = k1·G, R2 = k2·G noktalarını paylaşır.

  Tur 2 (Kısmi İmza):
    Tüm R'ler bir araya getirildikten sonra her katılımcı
    s_i = k1 + b·k2 + e·a_i·d_i  hesaplar ve paylaşır.

  Birleştirme:
    s = Σ s_i  →  64-bayt Schnorr imzası = (R.x ‖ s)

Wagner Saldırısına Karşı İki Nonce
-----------------------------------
Tek nonce ile koordinatör Wagner'in genelleştirilmiş doğum günü
saldırısıyla özel anahtarları elde edebilir. İki nonce ve
b = H(R1‖R2‖Q‖msg) bağlama faktörü bu saldırıyı engeller.

Güvenlik Uyarıları
------------------
  - Aynı oturum için nonce_gen() yalnızca BİR KEZ çağrılmalıdır.
  - Gizli nonce (k1, k2) kısmımza üretildikten hemen sonra bellekten silinmelidir.
  - Bu implementasyon sabit-zamanlı değildir; yan kanal saldırılarına açıktır.

Bağlantılar
-----------
  BIP-327 : https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki
  BIP-340 : https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
  README  : ./README.md §4
"""

import hashlib
import secrets
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── Secp256k1 Eğri Sabitleri ──────────────────────────────────────────────────

P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

@dataclass
class Point:
    x: Optional[int]
    y: Optional[int]

    @property
    def is_infinity(self) -> bool:
        return self.x is None

INFINITY = Point(None, None)
G = Point(GX, GY)


# ── Secp256k1 Aritmetiği ──────────────────────────────────────────────────────

def point_add(P1: Point, P2: Point) -> Point:
    """
    Secp256k1 nokta toplama (affine koordinatlar).

    Özel durumlar:
      - P1 = ∞  →  P2 döner (birim eleman)
      - P2 = ∞  →  P1 döner
      - P1 = P2  →  teğet formülü (doubling)
      - P1.x = P2.x, P1.y ≠ P2.y  →  ∞ (ters noktalar)

    Formül (genel):
      λ = (y2 - y1) · (x2 - x1)^{-1}  (mod P)
      x3 = λ² - x1 - x2               (mod P)
      y3 = λ·(x1 - x3) - y1           (mod P)
    """
    if P1.is_infinity:
        return P2
    if P2.is_infinity:
        return P1
    if P1.x == P2.x:
        if P1.y != P2.y:
            return INFINITY
        # Nokta iki katına alma
        lam = (3 * P1.x * P1.x * pow(2 * P1.y, P - 2, P)) % P
    else:
        lam = ((P2.y - P1.y) * pow(P2.x - P1.x, P - 2, P)) % P
    x3 = (lam * lam - P1.x - P2.x) % P
    y3 = (lam * (P1.x - x3) - P1.y) % P
    return Point(x3, y3)

def point_mul(k: int, pt: Point) -> Point:
    """
    Skaler çarpma: k·pt  (double-and-add algoritması).

    k'nın her biti soldan sağa işlenir:
      - Bit 1 ise sonucu mevcut addend ile topla
      - Her adımda addend iki katına alınır

    Zaman karmaşıklığı: O(log k) nokta işlemi ≈ 256 iterasyon.
    NOT: Bu implementasyon sabit-zamanlı değildir.
    """
    result = INFINITY
    addend = pt
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result

def has_even_y(pt: Point) -> bool:
    return pt.y % 2 == 0

def point_from_bytes(b: bytes) -> Point:
    """33 bayt sıkıştırılmış nokta → Point"""
    if len(b) != 33:
        raise ValueError("33 bayt gerekli")
    prefix = b[0]
    x = int.from_bytes(b[1:], "big")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        raise ValueError("Geçersiz nokta")
    if (y % 2) != (prefix - 2):
        y = P - y
    return Point(x, y)

def point_to_bytes(pt: Point) -> bytes:
    """Point → 33 bayt sıkıştırılmış"""
    prefix = b"\x02" if has_even_y(pt) else b"\x03"
    return prefix + pt.x.to_bytes(32, "big")

def xonly_bytes(pt: Point) -> bytes:
    """Point → 32 bayt x-only (BIP340)"""
    return pt.x.to_bytes(32, "big")


# ── BIP340 Tagged Hash ────────────────────────────────────────────────────────

def tagged_hash(tag: str, data: bytes) -> bytes:
    """
    BIP-340 Tagged Hash.

    Farklı protokol bağlamlarında hash çakışmasını önler:
      H = SHA256( SHA256(tag) ‖ SHA256(tag) ‖ data )

    tag_hash'i iki kez dahil etmek, farklı etiketlerin
    aynı hash'e yol açmasını imkânsız kılar.
    """
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


# ── BIP-327 MuSig2 ────────────────────────────────────────────────────────────

def key_agg_hash_list(pk_list: List[bytes]) -> bytes:
    """
    BIP-327 §Key Aggregation — L değerini hesapla.

    L = TaggedHash("KeyAgg list", sort(pk1) ‖ sort(pk2) ‖ ...)

    Sıralama (lexicografik) zorunludur; aksi takdirde her
    katılımcı farklı L → farklı Q hesaplar ve protokol çöker.
    pk_list'in çağıran tarafından sıralanmış olması beklenir.
    """
    return tagged_hash("KeyAgg list", b"".join(sorted(pk_list)))

def key_agg_coeff(pk_list: List[bytes], pk_i: bytes) -> int:
    """
    BIP-327 §Key Aggregation — katılımcı katsayısı (a_i).

    a_i = TaggedHash("KeyAgg coefficient", L ‖ pk_i)  mod N

    Katsayılar iki amaca hizmet eder:
      1. Rogue-key saldırısını önler (rastgele lineer kombinasyon)
      2. Her katılımcının katkısını birbirinden bağımsız kılar

    Argümanlar:
        pk_list : Sıralanmış tam katılımcı listesi
        pk_i    : Katsayısı hesaplanacak katılımcının açık anahtarı
    """
    L = key_agg_hash_list(pk_list)
    h = tagged_hash("KeyAgg coefficient", L + pk_i)
    return int.from_bytes(h, "big") % N

def key_aggregation(pk_list: List[bytes]) -> Tuple[Point, List[int]]:
    """
    Açık anahtarları agrege et → (Q, [a_i])
    Q = Σ a_i * P_i
    """
    coeffs = [key_agg_coeff(pk_list, pk) for pk in pk_list]
    Q = INFINITY
    for coeff, pk_bytes in zip(coeffs, pk_list):
        P_i = point_from_bytes(pk_bytes)
        Q = point_add(Q, point_mul(coeff, P_i))
    return Q, coeffs

def nonce_gen(sk: bytes, pk: bytes, msg: bytes) -> Tuple[Tuple[int, int], Tuple[bytes, bytes]]:
    """
    BIP-327 §Nonce Generation — çift nonce üretimi.

    Algoritma:
        rand  = CSPRNG(32 bayt)       ← her çağrıda farklı
        k_j   = H("MuSig/nonce", rand ‖ sk ‖ pk ‖ msg ‖ j)  mod N
        R_j   = k_j · G              (j ∈ {0, 1})

    Neden iki nonce?
        Wagner'in generalize doğum günü saldırısına karşı.
        b = H(R1‖R2‖Q‖msg) bağlama faktörü sayesinde koordinatör
        R1, R2 kombinasyonunu manipüle edemez.

    KRITIK GÜVENLİK KURALI:
        Bu fonksiyon aynı (sk, msg) çifti için birden fazla kez
        çağrılmamalıdır. Nonce yeniden kullanımı özel anahtarı
        açığa çıkarır (bkz. ECDSA Sony PS3 açığı).

    Döner:
        secret_nonce = (k1, k2)   — YALNIZCA yerel saklanır
        public_nonce = (R1, R2)   — diğer katılımcılara iletilir
    """
    rand = secrets.token_bytes(32)
    k1 = int.from_bytes(tagged_hash("MuSig/nonce", rand + sk + pk + msg + b"\x00"), "big") % N
    k2 = int.from_bytes(tagged_hash("MuSig/nonce", rand + sk + pk + msg + b"\x01"), "big") % N
    R1 = point_to_bytes(point_mul(k1, G))
    R2 = point_to_bytes(point_mul(k2, G))
    return (k1, k2), (R1, R2)

def nonce_agg(pub_nonces: List[Tuple[bytes, bytes]]) -> Tuple[Point, Point]:
    """
    Tüm açık nonce'ları agrege et
    R1 = Σ R1_i , R2 = Σ R2_i
    """
    R1 = INFINITY
    R2 = INFINITY
    for (r1_bytes, r2_bytes) in pub_nonces:
        R1 = point_add(R1, point_from_bytes(r1_bytes))
        R2 = point_add(R2, point_from_bytes(r2_bytes))
    return R1, R2

def session_ctx(agg_nonce: Tuple[Point, Point], Q: Point, msg: bytes) -> Tuple[Point, int]:
    """
    BIP-327 §Signing — oturum bağlamı hesapla.

    Adımlar:
        b = TaggedHash("MuSig/noncecoef", R1.x ‖ R2.x ‖ Q.x ‖ msg)  mod N
        R = R1 + b·R2

    b katsayısı tüm nonce taahhütlerini ve mesajı bağlar.
    Koordinatör b'yi manipüle edemez çünkü R1, R2, Q ve msg
    taahhüt aşamasında kilitlenmiştir.

    R.y normalleştirmesi:
        BIP-340 her zaman çift y'yi seçer. R.y tek ise
        katılımcılar partial_sign içinde k1, k2'yi negate eder.
        Bu fonksiyon ham R'yi döndürür; işaret düzeltmesi
        partial_sign'ın sorumluluğundadır.

    Döner:
        (R, b) — R: nihai nonce noktası, b: bağlama katsayısı
    """
    R1, R2 = agg_nonce
    b_input = (xonly_bytes(R1) + xonly_bytes(R2) +
               xonly_bytes(Q) + msg)
    b = int.from_bytes(tagged_hash("MuSig/noncecoef", b_input), "big") % N
    R = point_add(R1, point_mul(b, R2))
    if not has_even_y(R):
        # R'yi normalize et: k → N - k
        return R, b
    return R, b

def partial_sign(
    secret_nonce: Tuple[int, int],
    sk: bytes,
    coeff: int,
    Q: Point,
    agg_nonce: Tuple[Point, Point],
    msg: bytes,
) -> int:
    """
    BIP-327 §Signing — kısmi imza üretimi.

    Formül:
        e   = TaggedHash("BIP0340/challenge", R.x ‖ Q.x ‖ msg)  mod N
        s_i = k1 + b·k2 + e · a_i · d_i                        (mod N)

    İşaret Normalleştirme (BIP-340 uyumluluğu):
        Q.y tek ise → d_i = N - d_i      (özel anahtar negate)
        R.y tek ise → k1  = N - k1       (nonce'lar negate)
                      k2  = N - k2

    Bu normalleştirmeler olmadan doğrulama denklemi
        s·G = R + e·Q
    tutmaz çünkü Bitcoin düğümleri Q ve R'nin çift y'ye sahip
    olduğunu varsayar.

    Argümanlar:
        secret_nonce : nonce_gen()'den dönen (k1, k2) — tek kullanımlık
        sk           : 32-bayt özel anahtar
        coeff        : key_agg_coeff() ile hesaplanan a_i değeri
        Q            : agrege açık anahtar (key_aggregation sonucu)
        agg_nonce    : nonce_agg() sonucu (R1, R2)
        msg          : 32-bayt sighash

    Döner:
        s_i (int) — kısmi imza skaleri; diğer katılımcılara iletilir
    """
    k1, k2 = secret_nonce
    R, b = session_ctx(agg_nonce, Q, msg)

    # İmza hash'i (BIP340)
    e = int.from_bytes(
        tagged_hash("BIP0340/challenge",
                    xonly_bytes(R) + xonly_bytes(Q) + msg),
        "big"
    ) % N

    d = int.from_bytes(sk, "big")

    # Eğer Q.y tek ise özel anahtar işaretini düzelt
    if not has_even_y(Q):
        d = N - d

    # Eğer R.y tek ise nonce işaretlerini düzelt
    if not has_even_y(R):
        k1 = N - k1
        k2 = N - k2

    s_i = (k1 + b * k2 + e * coeff * d) % N
    return s_i

def partial_sig_agg(partial_sigs: List[int], R: Point) -> bytes:
    """
    BIP-327 §Partial Signature Aggregation — final imza üretimi.

    Toplama:
        s   = Σ s_i  mod N
        sig = R.x (32 bayt) ‖ s (32 bayt)   → 64 bayt BIP-340 imzası

    Bu işlem herhangi bir tarafça (katılımcı olmayan koordinatör dahil)
    yapılabilir; s_i değerleri gizlilik içermez.

    Doğrulama denklemi (Bitcoin düğümlerinin kullandığı):
        s·G == R + e·Q
        Σ s_i · G == R + e · Σ a_i·P_i
        ✓ (dağılım özelliği ile her s_i terimi ayrı ayrı tutarlar)

    Argümanlar:
        partial_sigs : Her katılımcıdan toplanan [s_1, s_2, ...] listesi
        R            : session_ctx()'ten dönen nihai nonce noktası

    Döner:
        64-bayt Schnorr imzası — herhangi bir BIP-340 doğrulayıcısıyla
        standart tek-imzacı Schnorr gibi doğrulanabilir
    """
    s = sum(partial_sigs) % N
    sig = xonly_bytes(R) + s.to_bytes(32, "big")
    return sig

def schnorr_verify(msg: bytes, pubkey_xonly: bytes, sig: bytes) -> bool:
    """
    BIP-340 §Verification — Schnorr imza doğrulama.

    Algoritma:
        1. sig[0:32] → r  (nonce x-koordinatı)
        2. sig[32:64] → s (imza skaleri)
        3. pubkey_xonly → Q (lift_x: çift y seç)
        4. e = TaggedHash("BIP0340/challenge", r ‖ Q.x ‖ msg)
        5. R = s·G - e·Q
        6. Geçerlilik: R ≠ ∞, R.y çift, R.x == r

    Bu fonksiyon hem tekli Schnorr hem de MuSig2 agrege
    imzalarını doğrular (zincir üzerinde fark yoktur).

    Argümanlar:
        msg          : 32-bayt mesaj (sighash veya test verisi)
        pubkey_xonly : 32-bayt x-only açık anahtar (BIP-340)
        sig          : 64-bayt Schnorr imzası

    Döner:
        True  — imza matematiksel olarak geçerli
        False — geçersiz (hatalı imza, hatalı anahtar veya hatalı mesaj)
    """
    if len(sig) != 64:
        return False
    r_int = int.from_bytes(sig[:32], "big")
    s_int = int.from_bytes(sig[32:], "big")
    if r_int >= P or s_int >= N:
        return False
    P_x = int.from_bytes(pubkey_xonly, "big")
    P_y_sq = (pow(P_x, 3, P) + 7) % P
    P_y = pow(P_y_sq, (P + 1) // 4, P)
    if pow(P_y, 2, P) != P_y_sq:
        return False
    if P_y % 2 != 0:
        P_y = P - P_y
    pub_pt = Point(P_x, P_y)
    e = int.from_bytes(
        tagged_hash("BIP0340/challenge",
                    sig[:32] + pubkey_xonly + msg),
        "big"
    ) % N
    R = point_add(point_mul(s_int, G), point_mul(N - e, pub_pt))
    if R.is_infinity or not has_even_y(R) or R.x != r_int:
        return False
    return True


# ── Demo ──────────────────────────────────────────────────────────────────────

def demo():
    print("=" * 60)
    print("   MuSig2 (BIP-327) - 2-of-2 Taproot Multisig Demo")
    print("=" * 60)

    # 1. Anahtar çiftleri oluştur
    sk1 = secrets.token_bytes(32)
    sk2 = secrets.token_bytes(32)

    P1 = point_mul(int.from_bytes(sk1, "big"), G)
    P2 = point_mul(int.from_bytes(sk2, "big"), G)

    pk1 = point_to_bytes(P1)
    pk2 = point_to_bytes(P2)

    print(f"\n[Katılımcı 1]")
    print(f"  Özel anahtar : {sk1.hex()}")
    print(f"  Açık anahtar : {pk1.hex()}")

    print(f"\n[Katılımcı 2]")
    print(f"  Özel anahtar : {sk2.hex()}")
    print(f"  Açık anahtar : {pk2.hex()}")

    # 2. Anahtar agregasyonu
    pk_list = sorted([pk1, pk2])       # BIP-327: lexicografik sıralama
    Q, coeffs = key_aggregation(pk_list)

    # Her katılımcının katsayısını bul
    coeff1 = key_agg_coeff(pk_list, pk1)
    coeff2 = key_agg_coeff(pk_list, pk2)

    print(f"\n[Agrege Açık Anahtar (Q)]")
    print(f"  x-only : {xonly_bytes(Q).hex()}")
    print(f"  Koeff1 : {coeff1}")
    print(f"  Koeff2 : {coeff2}")

    # 3. İmzalanacak mesaj (normalde tx hash'i)
    msg = tagged_hash("example/msg", b"taproot musig2 demo transaction")
    print(f"\n[Mesaj Hash] {msg.hex()}")

    # 4. Nonce üretimi (her katılımcı bağımsız üretir)
    (k1_pair, pub_nonce1) = nonce_gen(sk1, pk1, msg)
    (k2_pair, pub_nonce2) = nonce_gen(sk2, pk2, msg)

    print(f"\n[Nonce'lar]")
    print(f"  Katılımcı 1 R1: {pub_nonce1[0].hex()}")
    print(f"  Katılımcı 1 R2: {pub_nonce1[1].hex()}")
    print(f"  Katılımcı 2 R1: {pub_nonce2[0].hex()}")
    print(f"  Katılımcı 2 R2: {pub_nonce2[1].hex()}")

    # 5. Nonce agregasyonu
    agg_R1, agg_R2 = nonce_agg([pub_nonce1, pub_nonce2])
    agg_nonce = (agg_R1, agg_R2)

    # 6. Kısmi imzalar
    R, b = session_ctx(agg_nonce, Q, msg)

    s1 = partial_sign(k1_pair, sk1, coeff1, Q, agg_nonce, msg)
    s2 = partial_sign(k2_pair, sk2, coeff2, Q, agg_nonce, msg)

    print(f"\n[Kısmi İmzalar]")
    print(f"  s1 : {s1.to_bytes(32,'big').hex()}")
    print(f"  s2 : {s2.to_bytes(32,'big').hex()}")

    # 7. İmza birleştirme
    final_sig = partial_sig_agg([s1, s2], R)
    print(f"\n[Final Schnorr İmzası (64 bayt)]")
    print(f"  {final_sig.hex()}")

    # 8. Doğrulama
    valid = schnorr_verify(msg, xonly_bytes(Q), final_sig)
    print(f"\n[Doğrulama] {'✓ GEÇERLİ' if valid else '✗ GEÇERSİZ'}")

    # Taproot output script
    q_xonly = xonly_bytes(Q)
    taproot_script = bytes([0x51, 0x20]) + q_xonly  # OP_1 <32-byte-key>
    print(f"\n[Taproot Output Script]")
    print(f"  {taproot_script.hex()}")
    print(f"  (OP_1 OP_PUSHBYTES_32 <agrege-pubkey>)")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    demo()
