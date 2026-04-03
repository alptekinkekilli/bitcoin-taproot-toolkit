#!/usr/bin/env python3
"""
recover_session.py — MuSig2 Session Fon Kurtarma Aracı
=======================================================
Tamamlanmış veya kilitlenmiş bir distributed MuSig2 session'ındaki
fonları kurtarmak için kullanılır. Her iki katılımcının özel anahtarı
gereklidir.

Desteklenen senaryolar:
  1. Eski session (pre-BIP-327): Katsayılar H(L||pk_i) ile hesaplanmıştı.
     Yeni kod farklı bir aggregate key ürettiğinden normal UI çalışmaz.
  2. Yeni session (post-BIP-327): MuSig2* optimizasyonu var.
  3. Bilinen aggregate key: Session JSON olmadan doğrudan adres verilir.

Güvenlik uyarıları:
  - Özel anahtarlar komut satırından girilir; shell geçmişine kaydolabilir.
    Hassas ortamlarda `--sk1 -` ve `--sk2 -` kullanarak stdin'den okuyun.
  - Testnet anahtarları mainnet'te KULLANILMAMALIDIR.
  - Bu araç yalnızca testnet4 destekler (--network mainnet henüz aktif değil).

Kullanım:
  python recover_session.py \\
    --session 2bc52991 \\
    --sk1 <katilimci1_private_key_hex> \\
    --sk2 <katilimci2_private_key_hex> \\
    --recipient tb1p... \\
    [--fee 500] [--dry-run]

  # Session JSON olmadan (aggregate key biliniyor):
  python recover_session.py \\
    --agg-xonly bc50838d... \\
    --sk1 ... --sk2 ... \\
    --pk-list 023b5b...,03366f... \\
    --recipient tb1p...
"""

import sys
import json
import argparse
import hashlib
import struct
import secrets
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── Proje modüllerini import et ───────────────────────────────────────────────
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT / "btc_examples"))

from raw_tx import (
    _tagged_hash, _bech32m_encode,
    schnorr_sign, schnorr_verify,
    taproot_sighash, build_tx, broadcast_tx,
    UTXO, TxOutput,
    G, N, P as FIELD_P,
    _point_add, _point_mul, _xonly, _lift_x,
    varint,
)

# ── Sabitler ──────────────────────────────────────────────────────────────────
SESSIONS_JSON = _ROOT / "backend" / "data" / "dmusig2_sessions.json"
ESPLORA = {
    "testnet4": "https://mempool.space/testnet4/api",
    "mainnet":  "https://mempool.space/api",
}

# ── Secp256k1 yardımcıları ────────────────────────────────────────────────────

def decompress_pk(pk_bytes: bytes):
    """33-byte compressed pubkey → (x, y) tam nokta."""
    x = int.from_bytes(pk_bytes[1:], "big")
    y_sq = (pow(x, 3, FIELD_P) + 7) % FIELD_P
    y = pow(y_sq, (FIELD_P + 1) // 4, FIELD_P)
    if pow(y, 2, FIELD_P) != y_sq:
        raise ValueError(f"Geçersiz pubkey: {pk_bytes.hex()}")
    parity = pk_bytes[0]
    if parity == 0x02:
        y = y if y % 2 == 0 else FIELD_P - y
    else:
        y = y if y % 2 != 0 else FIELD_P - y
    from raw_tx import Point
    return Point(x, y)

def pk_from_sk(sk_int: int):
    """SK integer → compressed 33-byte pubkey."""
    P = _point_mul(sk_int, G)
    prefix = b"\x02" if P.y % 2 == 0 else b"\x03"
    return prefix + P.x.to_bytes(32, "big")


# ── Key Aggregation ───────────────────────────────────────────────────────────

def _key_agg_coeffs_old(pk_list_sorted: list[bytes]) -> list[int]:
    """
    Eski (pre-BIP-327) katsayılar: tüm anahtarlar için H(L||pk_i).
    MuSig2* optimizasyonu YOK — ikinci anahtar için a_i=1 uygulanmaz.
    """
    L = _tagged_hash("KeyAgg list", b"".join(pk_list_sorted))
    return [
        int.from_bytes(_tagged_hash("KeyAgg coefficient", L + pk), "big") % N
        for pk in pk_list_sorted
    ]

def _key_agg_coeffs_new(pk_list_sorted: list[bytes]) -> list[int]:
    """
    Yeni (post-BIP-327) katsayılar: ikinci farklı anahtar için a_i=1 (MuSig2*).
    """
    L = _tagged_hash("KeyAgg list", b"".join(pk_list_sorted))
    first = pk_list_sorted[0]
    second = None
    for pk in pk_list_sorted[1:]:
        if pk != first:
            second = pk
            break

    coeffs = []
    for pk in pk_list_sorted:
        if second is not None and pk == second:
            coeffs.append(1)
        else:
            h = _tagged_hash("KeyAgg coefficient", L + pk)
            coeffs.append(int.from_bytes(h, "big") % N)
    return coeffs

def _aggregate_point(pk_list_sorted: list[bytes], coeffs: list[int]):
    """Katsayılar ile aggregate public key noktasını hesapla: Q = Σ a_i·P_i"""
    Q = None
    for pk_bytes, a in zip(pk_list_sorted, coeffs):
        P = decompress_pk(pk_bytes)
        aP = _point_mul(a, P)
        Q = _point_add(Q if Q is not None else aP.__class__(None, None), aP) if Q else aP
    return Q

def _aggregate_point_safe(pk_list_sorted: list[bytes], coeffs: list[int]):
    """Güvenli aggregate: INFINITY desteği ile."""
    from raw_tx import Point, INFINITY
    Q = INFINITY
    for pk_bytes, a in zip(pk_list_sorted, coeffs):
        P = decompress_pk(pk_bytes)
        aP = _point_mul(a, P)
        Q = _point_add(Q, aP)
    return Q

def detect_coeff_mode(pk_list_sorted: list[bytes], expected_xonly: bytes) -> str:
    """
    Verilen pk_list ve beklenen aggregate x-only'den hangi katsayı modunun
    kullanıldığını otomatik algıla.

    Döner: "old" | "new" | "unknown"
    """
    for mode, fn in [("new", _key_agg_coeffs_new), ("old", _key_agg_coeffs_old)]:
        coeffs = fn(pk_list_sorted)
        Q = _aggregate_point_safe(pk_list_sorted, coeffs)
        if not Q.is_infinity and Q.x.to_bytes(32, "big") == expected_xonly:
            return mode
    return "unknown"

def compute_aggregate_sk(
    sk_map: dict[bytes, int],
    pk_list_sorted: list[bytes],
    coeffs: list[int],
) -> int:
    """
    d_agg = Σ (a_i · d_i) mod N

    sk_map: {compressed_pk_bytes: sk_int}
    """
    d_agg = 0
    for pk_bytes, a in zip(pk_list_sorted, coeffs):
        d_i = sk_map.get(pk_bytes)
        if d_i is None:
            raise ValueError(
                f"Pubkey için SK bulunamadı: {pk_bytes.hex()[:16]}...\n"
                f"Sağlanan pubkey'ler: {[k.hex()[:16] for k in sk_map]}"
            )
        d_agg = (d_agg + a * d_i) % N
    return d_agg


# ── Bech32m decode (P2TR scriptpubkey türetmek için) ─────────────────────────

def _bech32m_decode_p2tr(addr: str) -> bytes:
    """
    tb1p.../bc1p... adresinden 32-byte x-only pubkey çıkar.
    Döner: 32-byte x-only (P2TR witness program)
    """
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    addr_lower = addr.lower()
    sep = addr_lower.rfind("1")
    if sep < 1:
        raise ValueError(f"Geçersiz bech32m adresi: {addr}")
    data5 = [CHARSET.index(c) for c in addr_lower[sep + 1:]]
    # İlk değer witness version, sonrası 5-bit gruplar
    version = data5[0]
    if version != 1:
        raise ValueError(f"Taproot değil (witness version {version})")
    # 5→8 bit dönüşümü (checksum son 6 karakter hariç)
    payload5 = data5[1:-6]
    acc, bits, result = 0, 0, []
    for val in payload5:
        acc = (acc << 5) | val
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((acc >> bits) & 0xFF)
    if len(result) != 32:
        raise ValueError(f"P2TR bekleniyor (32 byte), {len(result)} byte geldi")
    return bytes(result)

def addr_to_scriptpubkey(addr: str) -> bytes:
    """P2TR adresi → OP_1 OP_PUSH32 <xonly> scriptpubkey."""
    xonly = _bech32m_decode_p2tr(addr)
    return bytes([0x51, 0x20]) + xonly


# ── API yardımcıları ──────────────────────────────────────────────────────────

def api_get_utxos(address: str, network: str) -> list:
    url = f"{ESPLORA[network]}/address/{address}/utxo"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())

def api_broadcast(tx_hex: str, network: str) -> str:
    url = f"{ESPLORA[network]}/tx"
    req = urllib.request.Request(
        url, data=tx_hex.encode(), headers={"Content-Type": "text/plain"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"Broadcast hatası: {err}")


# ── Ana kurtarma mantığı ──────────────────────────────────────────────────────

def recover(args):
    network = args.network

    # ── 1. Session veya doğrudan parametrelerden pk_list + agg_xonly al ──────
    if args.session:
        print(f"[1/6] Session yükleniyor: {args.session}")
        if not SESSIONS_JSON.exists():
            raise FileNotFoundError(f"Sessions dosyası bulunamadı: {SESSIONS_JSON}")
        sessions = json.loads(SESSIONS_JSON.read_text())
        sid = args.session
        s = sessions.get(sid)
        if s is None:
            raise ValueError(f"Session bulunamadı: {sid}")

        agg_xonly_hex = s.get("agg_xonly")
        if not agg_xonly_hex:
            raise ValueError("Session'da agg_xonly yok — henüz kayıt tamamlanmamış?")

        pk_list_sorted = [bytes.fromhex(pk) for pk in s["pk_list_sorted"]]
        state = s.get("state", "?")
        print(f"       State     : {state}")
        print(f"       agg_xonly : {agg_xonly_hex}")
        print(f"       Katılımcı : {len(pk_list_sorted)}")

    elif args.agg_xonly and args.pk_list:
        print("[1/6] Doğrudan parametreler kullanılıyor")
        agg_xonly_hex = args.agg_xonly
        pk_list_sorted = sorted(
            [bytes.fromhex(pk.strip()) for pk in args.pk_list.split(",")]
        )
    else:
        raise ValueError("--session VEYA (--agg-xonly + --pk-list) gerekli")

    agg_xonly = bytes.fromhex(agg_xonly_hex)

    # ── 2. Katsayı modunu algıla ─────────────────────────────────────────────
    print("[2/6] Katsayı modu algılanıyor...")
    mode = detect_coeff_mode(pk_list_sorted, agg_xonly)
    if mode == "unknown":
        raise ValueError(
            "Hiçbir katsayı modu (eski/yeni) beklenen aggregate key'i üretemedi.\n"
            "agg_xonly veya pk_list yanlış olabilir."
        )

    if mode == "old":
        coeffs = _key_agg_coeffs_old(pk_list_sorted)
        print(f"       Mod: ESKİ (pre-BIP-327, MuSig2* optimizasyonu YOK)")
    else:
        coeffs = _key_agg_coeffs_new(pk_list_sorted)
        print(f"       Mod: YENİ (post-BIP-327, MuSig2* optimizasyonu var)")

    # ── 3. Özel anahtarları yükle ve aggregate SK hesapla ───────────────────
    print("[3/6] Aggregate özel anahtar hesaplanıyor...")

    def read_sk(val: str, label: str) -> int:
        if val == "-":
            raw = input(f"  {label} (hex, gizli): ").strip()
        else:
            raw = val.strip()
        if len(raw) != 64:
            raise ValueError(f"{label}: 64-hex-karakter bekleniyor, {len(raw)} geldi")
        return int(raw, 16)

    sk1_int = read_sk(args.sk1, "SK-1")
    sk2_int = read_sk(args.sk2, "SK-2")

    # Her iki SK'nın pubkey'ini türet ve pk_list ile eşleştir
    pk1 = pk_from_sk(sk1_int)
    pk2 = pk_from_sk(sk2_int)

    sk_map: dict[bytes, int] = {}
    for sk_int, pk_bytes in [(sk1_int, pk1), (sk2_int, pk2)]:
        if pk_bytes in pk_list_sorted:
            sk_map[pk_bytes] = sk_int
        else:
            # Negated sk (BIP-340 normalleştirme denemesi)
            neg_sk_int = N - sk_int
            neg_pk = pk_from_sk(neg_sk_int)
            if neg_pk in pk_list_sorted:
                sk_map[neg_pk] = neg_sk_int
            else:
                raise ValueError(
                    f"SK pubkey'i pk_list'te bulunamadı: {pk_bytes.hex()[:16]}...\n"
                    f"pk_list: {[p.hex()[:16] for p in pk_list_sorted]}"
                )

    print(f"       SK-1 → {pk1.hex()[:16]}... ✓")
    print(f"       SK-2 → {pk2.hex()[:16]}... ✓")

    d_agg = compute_aggregate_sk(sk_map, pk_list_sorted, coeffs)

    # Doğrulama: d_agg * G == Q (agg_xonly)
    Q_check = _point_mul(d_agg, G)
    if Q_check.x.to_bytes(32, "big") != agg_xonly:
        # Q.y tek olabilir — negasyon gerekir (BIP-340)
        d_agg = N - d_agg
        Q_check = _point_mul(d_agg, G)
        if Q_check.x.to_bytes(32, "big") != agg_xonly:
            raise RuntimeError(
                "d_agg·G ≠ Q — SK'lar yanlış olabilir ya da pk_list uyumsuz"
            )
    print(f"       d_agg·G = Q ✓ (x={agg_xonly_hex[:16]}...)")

    # ── 4. Taproot adresi ve UTXO'ları bul ───────────────────────────────────
    address = _bech32m_encode("tb" if network == "testnet4" else "bc", agg_xonly)
    spk = bytes([0x51, 0x20]) + agg_xonly

    print(f"[4/6] UTXO sorgusu: {address}")
    raw_utxos = api_get_utxos(address, network)

    if not raw_utxos:
        print("       ⚠ Bu adreste UTXO bulunamadı. Fonlar zaten harcanmış olabilir.")
        return

    confirmed = [u for u in raw_utxos if u.get("status", {}).get("confirmed")]
    all_utxos = confirmed if confirmed else raw_utxos

    total_in = sum(u["value"] for u in all_utxos)
    print(f"       {len(all_utxos)} UTXO, toplam {total_in} sat")
    for u in all_utxos:
        conf = "✓" if u.get("status", {}).get("confirmed") else "⏳"
        print(f"       {conf} {u['txid'][:16]}...:{u['vout']}  {u['value']} sat")

    # ── 5. TX oluştur ─────────────────────────────────────────────────────────
    fee = args.fee
    recipient_spk = addr_to_scriptpubkey(args.recipient)
    out_value = total_in - fee

    if out_value < 546:
        raise ValueError(
            f"Çıktı değeri {out_value} sat — dust limitinin ({546} sat) altında. "
            f"Fee'yi azalt (--fee {fee}) veya daha fazla UTXO bekle."
        )

    print(f"[5/6] TX oluşturuluyor...")
    print(f"       Gönderilecek : {out_value} sat")
    print(f"       Fee          : {fee} sat")
    print(f"       Alıcı        : {args.recipient}")

    inputs = [
        UTXO(u["txid"], u["vout"], u["value"], spk)
        for u in all_utxos
    ]
    outputs = [TxOutput(out_value, recipient_spk)]
    d_agg_bytes = d_agg.to_bytes(32, "big")

    witnesses = []
    for idx in range(len(inputs)):
        sighash = taproot_sighash(inputs, outputs, idx)
        sig = schnorr_sign(sighash, d_agg_bytes)
        # Doğrulama
        if not schnorr_verify(sighash, agg_xonly, sig):
            raise RuntimeError(f"İmza doğrulaması başarısız (input {idx})")
        witnesses.append(sig)
        print(f"       Input {idx}: imzalandı ✓")

    raw_tx = build_tx(inputs, outputs, witnesses)
    tx_hex = raw_tx.hex()

    # TxID hesapla (witness hariç double-SHA256)
    legacy = struct.pack("<I", 2)  # nVersion
    legacy += varint(len(inputs))
    for inp in inputs:
        legacy += bytes.fromhex(inp.txid)[::-1]
        legacy += struct.pack("<I", inp.vout)
        legacy += b"\x00"
        legacy += struct.pack("<I", 0xFFFFFFFD)
    legacy += varint(len(outputs))
    for out in outputs:
        legacy += struct.pack("<q", out.value_sat)
        legacy += varint(len(out.scriptpubkey)) + out.scriptpubkey
    legacy += struct.pack("<I", 0)
    txid = hashlib.sha256(hashlib.sha256(legacy).digest()).digest()[::-1].hex()

    print(f"       TX hex uzunluğu: {len(tx_hex) // 2} byte")
    print(f"       TxID (tahmini) : {txid}")

    if args.dry_run:
        print()
        print("=" * 60)
        print("  DRY-RUN: Broadcast atlandı.")
        print(f"  TX hex:\n  {tx_hex[:80]}...")
        print("=" * 60)
        return

    # ── 6. Broadcast ─────────────────────────────────────────────────────────
    print("[6/6] Broadcast ediliyor...")
    txid_confirmed = api_broadcast(tx_hex, network)
    print()
    print("=" * 60)
    print("  ✓ Başarıyla yayınlandı!")
    print(f"  TxID: {txid_confirmed}")
    print(f"  Explorer: https://mempool.space/testnet4/tx/{txid_confirmed}")
    print("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MuSig2 session fonlarını kurtarır (her iki SK gerekli)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    src = parser.add_argument_group("Kaynak (birini seç)")
    src.add_argument(
        "--session", "-s", metavar="SESSION_ID",
        help="dmusig2_sessions.json'dan session ID (ör: 2bc52991)",
    )
    src.add_argument(
        "--agg-xonly", metavar="HEX",
        help="Aggregate public key x-only (64-char hex)",
    )
    src.add_argument(
        "--pk-list", metavar="PK1,PK2",
        help="Virgülle ayrılmış sıralı compressed pubkey'ler (33-byte hex)",
    )

    keys = parser.add_argument_group("Özel anahtarlar")
    keys.add_argument(
        "--sk1", required=True, metavar="HEX|-",
        help="Katılımcı 1 özel anahtarı (64-char hex veya '-' stdin için)",
    )
    keys.add_argument(
        "--sk2", required=True, metavar="HEX|-",
        help="Katılımcı 2 özel anahtarı (64-char hex veya '-' stdin için)",
    )

    tx = parser.add_argument_group("Transaction")
    tx.add_argument(
        "--recipient", "-r", required=True, metavar="ADDRESS",
        help="Fonların gönderileceği testnet4 adresi (tb1p...)",
    )
    tx.add_argument(
        "--fee", type=int, default=500, metavar="SATS",
        help="Miner ücreti satoshi cinsinden (varsayılan: 500)",
    )
    tx.add_argument(
        "--network", choices=["testnet4"], default="testnet4",
        help="Ağ (şu an yalnızca testnet4)",
    )
    tx.add_argument(
        "--dry-run", action="store_true",
        help="TX oluştur ama broadcast etme",
    )

    args = parser.parse_args()

    if not args.session and not (args.agg_xonly and args.pk_list):
        parser.error("--session VEYA (--agg-xonly + --pk-list) gerekli")

    try:
        recover(args)
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print(f"\n[HATA] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nİptal edildi.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
