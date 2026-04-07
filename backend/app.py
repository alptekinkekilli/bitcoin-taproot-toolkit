"""
app.py — Taproot Wallet Backend (FastAPI)
=========================================
Bitcoin Taproot toolkit için REST API + statik dosya sunucusu.
Testnet üzerinde çalışır. Özel anahtarlar yalnızca bellekte tutulur.

Bitcoin Core v26+ Entegrasyonu:
    USE_CORE_RPC=true ortam değişkeni ile Bitcoin Core RPC aktifleştirilir.
    Aksi hâlde Esplora API (mempool.space) kullanılır.

    Gerekli ortam değişkenleri (Core mod):
        USE_CORE_RPC=true
        BITCOIN_RPCUSER=<kullanıcı>
        BITCOIN_RPCPASSWORD=<şifre>
        BITCOIN_NETWORK=testnet   # testnet | testnet4 | mainnet | regtest
        BITCOIN_WALLET=<cüzdan>   # isteğe bağlı

    Descriptor Wallet Notu:
        Bitcoin Core v26+ ortamında cüzdanlar descriptor tabanlı olmalıdır.
        importdescriptors API'si tr(xonly) formatını kullanır.
        importprivkey / importpubkey artık desteklenmez.
"""

import sys, os, uuid, json, secrets as sec_mod, hashlib, struct, hmac as _hmac_mod, time as _time
import logging
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
import asyncio
from pydantic import BaseModel

# btc_examples modülleri
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'btc_examples'))
from musig2 import (
    point_mul, point_to_bytes, xonly_bytes, G, N,
    key_aggregation, key_agg_coeff,
    nonce_gen, nonce_agg, session_ctx,
    partial_sign, partial_sig_agg, schnorr_verify,
    tagged_hash, point_from_bytes
)
from raw_tx import (
    taproot_address, taproot_tweak_key, schnorr_sign,
    taproot_sighash, build_tx,
    get_utxos, get_tx_hex, broadcast_tx,
    UTXO, TxOutput, _bech32m_encode, _xonly, _point_mul
)

# src/ entegrasyon katmanı
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.core_connector import CoreConnector, RPCConnectionError, RPCError, LegacyMethodError
from src.descriptor_wallet import DescriptorWallet, DescriptorChecksum
from src.utxo_manager import UTXOManager, CoreUTXO, CoinSelector, build_p2tr_scriptpubkey, parse_p2tr_scriptpubkey
from src.taproot_signer import TaprootSigner, SighashType

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Taproot Wallet API", version="1.0.0")

# Phase 5: Oturum TTL — 48 saat
SESSION_TTL_SECONDS = 48 * 3600

# ── Bitcoin Core RPC Bağlantısı (opsiyonel) ───────────────────────────────────
#
# USE_CORE_RPC=true ile Bitcoin Core v26+ tam düğüm kullanılır.
# False ise tüm UTXO/TX işlemleri Esplora API üzerinden gider.
#
# Descriptor Wallet Zorunluluğu (v26+):
#   importprivkey / importpubkey → LegacyMethodError → importdescriptors'a yönlendir
#   Yeni cüzdan: createwallet(..., descriptors=True)
#   Anahtar ithalatı: importdescriptors([{"desc": "tr(xonly)#checksum", ...}])

_USE_CORE = os.environ.get("USE_CORE_RPC", "false").lower() == "true"
_core_rpc: Optional[CoreConnector] = None

if _USE_CORE:
    try:
        _core_rpc = CoreConnector(
            network=os.environ.get("BITCOIN_NETWORK", "testnet"),
            rpcuser=os.environ.get("BITCOIN_RPCUSER"),
            rpcpassword=os.environ.get("BITCOIN_RPCPASSWORD"),
            wallet_name=os.environ.get("BITCOIN_WALLET"),
        )
        info = _core_rpc.health_check()
        logger.info("[Core] Bitcoin Core v26+ bağlandı: chain=%s blocks=%s progress=%.4f",
                    info['chain'], info['blocks'], info['verificationprogress'])
    except RPCConnectionError as e:
        logger.warning("[Core] Bağlantı kurulamadı, Esplora'ya geçildi: %s", e)
        _core_rpc = None
    except RPCError as e:
        logger.warning("[Core] RPC hatası, Esplora'ya geçildi: %s", e)
        _core_rpc = None


def get_utxo_manager(network: str = "testnet") -> UTXOManager:
    """
    Ağ ve Core durumuna göre uygun UTXOManager döner.

    Core aktifse: listunspent → scantxoutset → Esplora fallback
    Core pasifse: yalnızca Esplora
    """
    return UTXOManager(network=network, rpc=_core_rpc)


# ── Kalıcı State (JSON dosyası) ───────────────────────────────────────────────
#
# Cüzdan özel anahtarları yalnızca bellekte değil, disk'e de kaydedilir.
# Sunucu restart'ta cüzdanlar kaybolmaz.
#
# Güvenlik: Bu dosya ASLA git'e commit edilmemeli (.gitignore'a eklendi).
# Üretim için HSM veya şifreli vault kullanın.

_DATA_DIR  = os.path.join(os.path.dirname(__file__), "data")
_WALLETS_F = os.path.join(_DATA_DIR, "wallets.json")

os.makedirs(_DATA_DIR, exist_ok=True)

def _load_wallets() -> List[Dict]:
    if os.path.exists(_WALLETS_F):
        try:
            with open(_WALLETS_F, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def _save_wallets():
    with open(_WALLETS_F, "w") as f:
        json.dump(wallets, f, indent=2)

_MUSIG2_F = os.path.join(_DATA_DIR, "musig2_sessions.json")

def _load_musig2() -> Dict[str, Dict]:
    if os.path.exists(_MUSIG2_F):
        try:
            with open(_MUSIG2_F, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_musig2():
    """MuSig2 oturumlarını diske kaydet. JSON-serializable olmayan TxOutput/_inp hariç tutulur."""
    serializable = {}
    for sid, s in musig2_sessions.items():
        serializable[sid] = {k: v for k, v in s.items() if k not in ("_outputs", "_inp")}
    with open(_MUSIG2_F, "w") as f:
        json.dump(serializable, f, indent=2)

wallets: List[Dict] = _load_wallets()
musig2_sessions: Dict[str, Dict] = _load_musig2()

_DMUSIG2_F = os.path.join(_DATA_DIR, "dmusig2_sessions.json")

def _migrate_dmusig2_session(s: dict) -> bool:
    """Backfill agg_q_even_y for sessions created before this field was added.
    Returns True if the session was modified."""
    import sys
    if s.get("agg_q_even_y") is None and s.get("pk_list_sorted"):
        try:
            pk_list_bytes = [bytes.fromhex(pk) for pk in s["pk_list_sorted"]]
            Q_m, _ = key_aggregation(pk_list_bytes)
            s["agg_q_even_y"] = (Q_m.y % 2 == 0)
            logger.info("[MIGRATE] Session %s backfilled agg_q_even_y=%s", s.get('id'), s['agg_q_even_y'])
            return True
        except Exception as exc:
            logger.warning("[MIGRATE] Session %s migration failed: %s", s.get('id'), exc)
    return False

def _load_dmusig2() -> Dict[str, Dict]:
    if os.path.exists(_DMUSIG2_F):
        try:
            with open(_DMUSIG2_F, "r") as f:
                data = json.load(f)
            # Migrate old sessions that lack agg_q_even_y
            migrated = sum(1 for s in data.values() if _migrate_dmusig2_session(s))
            if migrated:
                with open(_DMUSIG2_F, "w") as fw:
                    json.dump(data, fw, indent=2)
            return data
        except Exception:
            return {}
    return {}

def _save_dmusig2():
    with open(_DMUSIG2_F, "w") as f:
        json.dump(dmusig2_sessions, f, indent=2)

dmusig2_sessions: Dict[str, Dict] = _load_dmusig2()


# ── Pydantic Models ───────────────────────────────────────────────────────────

class WalletCreate(BaseModel):
    label: str
    network: str = "testnet"  # testnet | mainnet

class WalletImport(BaseModel):
    label: str
    network: str = "testnet4"   # testnet4 | mainnet
    master_xprv: str            # MASTER_TPRV veya MASTER_XPRV

class TxRequest(BaseModel):
    from_address: str
    to_address: str
    amount_sat: int
    fee_sat: int = 500
    utxo_ids: list[str] | None = None  # ["txid:vout", …] — coin control

class BroadcastRequest(BaseModel):
    tx_hex: str

class MusigCreate(BaseModel):
    label: str
    n_participants: int
    network: str = "testnet"

class MusigNonce(BaseModel):
    participant_index: int

class MusigPartialSign(BaseModel):
    participant_index: int
    from_address: str  # MuSig2 aggregate address
    to_address: str
    amount_sat: int
    fee_sat: int = 500

# ── Dağıtık MuSig2 Models ─────────────────────────────────────────────────────

class DMusig2Create(BaseModel):
    label: str
    n_participants: int
    network: str = "testnet4"
    source_session_id: Optional[str] = None

class DMusig2Register(BaseModel):
    participant_index: int
    pubkey_hex: str  # 33 bytes compressed hex

class DMusig2BuildTx(BaseModel):
    to_address: str
    amount_sat: int
    fee_sat: int = 500
    description: str = ""

class DMusig2SubmitNonce(BaseModel):
    participant_index: int
    pubnonces: List[Dict[str, str]]  # [{r1: hex33, r2: hex33}, ...] one per input

class DMusig2SubmitSig(BaseModel):
    participant_index: int
    partial_sigs: List[str]  # [hex32_scalar, ...] one per input


# ── Helpers ───────────────────────────────────────────────────────────────────

# Bech32 karakter seti
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _bech32_decode_words(hrp: str, addr: str):
    """Bech32/Bech32m adres → (witness_version, witness_program_bytes)."""
    addr_low = addr.lower()
    if addr_low[:len(hrp)+1] != hrp + "1":
        return None, None
    data_chars = addr_low[len(hrp)+1:]
    data = []
    for c in data_chars:
        d = _BECH32_CHARSET.find(c)
        if d < 0:
            return None, None
        data.append(d)
    # Son 6 karakter checksum — yoksay
    if len(data) < 7:
        return None, None
    decoded = data[:-6]
    # 5-bit grubunu 8-bit bayta çevir
    ver = decoded[0]
    bits5 = decoded[1:]
    acc, bits, result = 0, 0, []
    for val in bits5:
        acc = ((acc << 5) | val) & 0xFFFF_FFFF
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((acc >> bits) & 0xFF)
    return ver, bytes(result)

def address_to_scriptpubkey(addr: str) -> bytes:
    """
    Bech32/Bech32m adresi → scriptPubKey baytları.

    Desteklenen tipler:
      P2WPKH  : tb1q / bc1q → OP_0 OP_PUSH20 <20B>  (22 bayt)
      P2TR    : tb1p / bc1p → OP_1 OP_PUSH32 <32B>  (34 bayt)
      P2WSH   : tb1q / bc1q → OP_0 OP_PUSH32 <32B>  (34 bayt)

    BIP-141: scriptPubKey = OP_n <witness_program>
      witness version 0 → OP_0 = 0x00
      witness version 1 → OP_1 = 0x51 (Taproot)
    """
    addr_low = addr.lower()
    for hrp in ("tb", "bc", "bcrt"):
        if addr_low.startswith(hrp + "1"):
            ver, prog = _bech32_decode_words(hrp, addr_low)
            if prog is None:
                raise ValueError(f"Bech32 decode hatası: {addr}")
            if ver == 0:
                op_ver = 0x00                    # OP_0
            else:
                op_ver = 0x50 + ver              # OP_1..OP_16
            return bytes([op_ver, len(prog)]) + prog
    raise ValueError(f"Desteklenmeyen adres formatı: {addr}")

# ── WIF & Descriptor Yardımcıları ────────────────────────────────────────────

_B58_CHARS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    result = ""
    while n > 0:
        n, r = divmod(n, 58)
        result = _B58_CHARS[r] + result
    for byte in data:
        if byte == 0:
            result = "1" + result
        else:
            break
    return result

def _b58decode(s: str) -> bytes:
    """Base58 string → bytes (checksum doğrulamasız)."""
    n = 0
    for ch in s:
        n = n * 58 + _B58_CHARS.index(ch)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result

def decode_master_xprv(xprv_str: str) -> tuple:
    """
    MASTER_TPRV / MASTER_XPRV string → (master_sk: bytes, master_chain: bytes).
    BIP-32 extended private key yapısı:
      [4B version][1B depth][4B parent_fp][4B child_num][32B chain][1B 0x00][32B sk]
    checksum (4B) son dört byte — toplam 82B payload.
    """
    raw = _b58decode(xprv_str)
    if len(raw) < 78:
        raise ValueError(f"Geçersiz xprv uzunluğu: {len(raw)}")
    # Checksum doğrula
    payload, chk = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if chk != expected:
        raise ValueError("xprv checksum hatası — geçersiz anahtar")
    depth     = payload[4]
    chain     = payload[13:45]   # bytes 13-44
    sk        = payload[46:78]   # bytes 46-77 (byte 45 = 0x00 padding)
    if depth != 0:
        raise ValueError(f"Yalnızca master key (depth=0) destekleniyor, bu key depth={depth}")
    return sk, chain

def sk_to_wif(sk_bytes: bytes, testnet: bool = False) -> str:
    """32-bayt özel anahtar → WIF (sıkıştırılmış)."""
    prefix = b"\xef" if testnet else b"\x80"
    payload = prefix + sk_bytes + b"\x01"
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _b58encode(payload + checksum)

# ── BIP-32 HD Key Derivation (Sparrow Descriptor için) ───────────────────────

# Pure-Python RIPEMD-160 (OpenSSL 3.0+ legacy algoritmaları devre dışı bıraktı)
def _ripemd160(msg: bytes) -> bytes:
    """BIP-32 fingerprint için minimal saf-Python RIPEMD-160."""
    # Sabitler
    KL = [0x00000000,0x5A827999,0x6ED9EBA1,0x8F1BBCDC,0xA953FD4E]
    KR = [0x50A28BE6,0x5C4DD124,0x6D703EF3,0x7A6D76E9,0x00000000]
    RL = [
        0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,
        7,4,13,1,10,6,15,3,12,0,9,5,2,14,11,8,
        3,10,14,4,9,15,8,1,2,7,0,6,13,11,5,12,
        1,9,11,10,0,8,12,4,13,3,7,15,14,5,6,2,
        4,0,5,9,7,12,2,10,14,1,3,8,11,6,15,13,
    ]
    RR = [
        5,14,7,0,9,2,11,4,13,6,15,8,1,10,3,12,
        6,11,3,7,0,13,5,10,14,15,8,12,4,9,1,2,
        15,5,1,3,7,14,6,9,11,8,12,2,10,0,4,13,
        8,6,4,1,3,11,15,0,5,12,2,13,9,7,10,14,
        12,15,10,4,1,5,8,7,6,2,13,14,0,3,9,11,
    ]
    SL = [
        11,14,15,12,5,8,7,9,11,13,14,15,6,7,9,8,
        7,6,8,13,11,9,7,15,7,12,15,9,11,7,13,12,
        11,13,6,7,14,9,13,15,14,8,13,6,5,12,7,5,
        11,12,14,15,14,15,9,8,9,14,5,6,8,6,5,12,
        9,15,5,11,6,8,13,12,5,12,13,14,11,8,5,6,
    ]
    SR = [
        8,9,9,11,13,15,15,5,7,7,8,11,14,14,12,6,
        9,13,15,7,12,8,9,11,7,7,12,7,6,15,13,11,
        9,7,15,11,8,6,6,14,12,13,5,14,13,13,7,5,
        15,5,8,11,14,14,6,14,6,9,12,9,12,5,15,8,
        8,5,12,9,12,5,14,6,8,13,6,5,15,13,11,11,
    ]
    def F(j, x, y, z):
        if   j < 16: return x ^ y ^ z
        elif j < 32: return (x & y) | (~x & z)
        elif j < 48: return (x | ~y) ^ z
        elif j < 64: return (x & z) | (y & ~z)
        else:        return x ^ (y | ~z)
    def rol(x, n): return ((x << n) | (x >> (32-n))) & 0xFFFFFFFF
    # Padding
    ml = len(msg) * 8
    msg += b'\x80'
    msg += b'\x00' * (-(len(msg) + 8) % 64)
    msg += ml.to_bytes(8, 'little')
    h = [0x67452301,0xEFCDAB89,0x98BADCFE,0x10325476,0xC3D2E1F0]
    for i in range(len(msg) // 64):
        X = [int.from_bytes(msg[i*64+j*4:i*64+j*4+4],'little') for j in range(16)]
        al,bl,cl,dl,el = h
        ar,br,cr,dr,er = h
        for j in range(80):
            T = rol((al + F(j,bl,cl,dl) + X[RL[j]] + KL[j//16]) & 0xFFFFFFFF, SL[j])
            T = (T + el) & 0xFFFFFFFF
            al,bl,cl,dl,el = el,T,bl,rol(cl,10),dl
            T = rol((ar + F(79-j,br,cr,dr) + X[RR[j]] + KR[j//16]) & 0xFFFFFFFF, SR[j])
            T = (T + er) & 0xFFFFFFFF
            ar,br,cr,dr,er = er,T,br,rol(cr,10),dr
        T = (h[1] + cl + dr) & 0xFFFFFFFF
        h[1] = (h[2] + dl + er) & 0xFFFFFFFF
        h[2] = (h[3] + el + ar) & 0xFFFFFFFF
        h[3] = (h[4] + al + br) & 0xFFFFFFFF
        h[4] = (h[0] + bl + cr) & 0xFFFFFFFF
        h[0] = T
    return b''.join(x.to_bytes(4,'little') for x in h)

def _bip32_hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)) — BIP-32 fingerprint için."""
    return _ripemd160(hashlib.sha256(data).digest())

def _bip32_pub_compressed(sk: bytes) -> bytes:
    """32-byte private key → 33-byte compressed public key."""
    P = point_mul(int.from_bytes(sk, 'big'), G)   # musig2: point_mul(k, pt)
    prefix = b'\x02' if P.y % 2 == 0 else b'\x03'
    return prefix + P.x.to_bytes(32, 'big')

def _bip32_child(parent_sk: bytes, parent_chain: bytes, index: int):
    """
    BIP-32 child private key derivation.
    index >= 0x80000000 → hardened.
    Returns (child_sk, child_chain, parent_fingerprint_4bytes).
    """
    parent_pub = _bip32_pub_compressed(parent_sk)
    parent_fp = _bip32_hash160(parent_pub)[:4]
    if index >= 0x80000000:
        data = b'\x00' + parent_sk + index.to_bytes(4, 'big')
    else:
        data = parent_pub + index.to_bytes(4, 'big')
    I = _hmac_mod.new(parent_chain, data, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    child_int = (int.from_bytes(IL, 'big') + int.from_bytes(parent_sk, 'big')) % N
    if child_int == 0 or int.from_bytes(IL, 'big') >= N:
        raise ValueError("Geçersiz child key (olasılık çok düşük)")
    return child_int.to_bytes(32, 'big'), IR, parent_fp

def _bip32_xpub(pub: bytes, chain: bytes, depth: int,
                parent_fp: bytes, child_num: int, testnet: bool) -> str:
    """BIP-32 genişletilmiş public key → Base58Check string (tpub / xpub)."""
    version = b'\x04\x35\x87\xcf' if testnet else b'\x04\x88\xb2\x1e'
    payload = (
        version +
        bytes([depth]) +
        parent_fp +
        child_num.to_bytes(4, 'big') +
        chain +
        pub
    )
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _b58encode(payload + chk)

def _bip32_xprv(sk: bytes, chain: bytes, depth: int,
                parent_fp: bytes, child_num: int, testnet: bool = False) -> str:
    """BIP-32 genişletilmiş private key → Base58Check string (xprv/tprv)."""
    version = b'\x04\x35\x83\x94' if testnet else b'\x04\x88\xad\xe4'  # tprv / xprv
    payload = (
        version +
        bytes([depth]) +
        parent_fp +
        child_num.to_bytes(4, 'big') +
        chain +
        b'\x00' + sk
    )
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return _b58encode(payload + chk)

def _hd_master_keys(seed_sk: Optional[bytes], master_kv: Optional[tuple]) -> tuple:
    """Seed veya master_kv'den (master_sk, master_chain) döner."""
    if master_kv:
        return master_kv
    master_I = _hmac_mod.new(b'Bitcoin seed', seed_sk, hashlib.sha512).digest()
    return master_I[:32], master_I[32:]


def _hd_child_at_index(seed_sk: Optional[bytes], testnet: bool,
                        change: int = 0, index: int = 0,
                        master_kv: Optional[tuple] = None):
    """
    BIP-86 HD derivation → m/86'/coin_type'/0'/change/index → BIP-341 P2TR.

    change=0: receive  change=1: internal (change output)
    master_kv=(master_sk, master_chain) verilirse HMAC adımı atlanır (import edilmiş cüzdanlar).

    Returns: (child_sk_bytes, internal_xonly_bytes, p2tr_address_str)
    """
    coin_type = 1 if testnet else 0
    master_sk, master_chain = _hd_master_keys(seed_sk, master_kv)
    k1, c1, _ = _bip32_child(master_sk, master_chain, 0x80000000 + 86)
    k2, c2, _ = _bip32_child(k1, c1, 0x80000000 + coin_type)
    k3, c3, _ = _bip32_child(k2, c2, 0x80000000)
    k4, c4, _ = _bip32_child(k3, c3, change)
    k5, _c5, _ = _bip32_child(k4, c4, index)
    internal_xonly, address = taproot_address(k5, testnet=testnet, bip341=True)
    return k5, internal_xonly, address


def _hd_child_for_address(seed_sk: Optional[bytes], testnet: bool,
                           master_kv: Optional[tuple] = None):
    """index=0 receive adresi türetir. Geriye dönük uyumluluk için korundu."""
    return _hd_child_at_index(seed_sk, testnet, change=0, index=0, master_kv=master_kv)


def _wallet_master_kv(w: Dict) -> Optional[tuple]:
    """hd_imported cüzdan için (master_sk, master_chain) döner, yoksa None."""
    if w.get("hd_imported"):
        return (bytes.fromhex(w["sk_hex"]), bytes.fromhex(w["master_chain_hex"]))
    return None


def make_sparrow_descriptor(sk_hex: Optional[str], testnet: bool = True,
                             master_kv: Optional[tuple] = None):
    """
    Raw private key (veya master_kv) → Sparrow-uyumlu BIP-32 HD Taproot descriptor + master xprv.

    master_kv=(master_sk, master_chain) verilirse sk_hex'ten master türetme atlanır.
    Bu, import edilen cüzdanlarda kullanılır (sk_hex = master_sk, master_chain ayrı saklanır).

    Sparrow testnet4 modunda: tpub + 86h/1h/0h  (BIP-86 testnet, coin_type=1)
    Sparrow mainnet modunda : xpub + 86h/0h/0h  (BIP-86 mainnet, coin_type=0)

    Returns: (descriptor_str, master_xprv_str)
    """
    coin_type = 1 if testnet else 0

    if master_kv:
        master_sk, master_chain = master_kv
    else:
        sk = bytes.fromhex(sk_hex)
        master_I = _hmac_mod.new(b'Bitcoin seed', sk, hashlib.sha512).digest()
        master_sk, master_chain = master_I[:32], master_I[32:]

    master_pub = _bip32_pub_compressed(master_sk)
    master_fp = _bip32_hash160(master_pub)[:4]

    # Master xprv/tprv (depth=0, Sparrow bunu türetme için kullanır)
    master_xprv = _bip32_xprv(
        master_sk, master_chain,
        depth=0,
        parent_fp=b'\x00\x00\x00\x00',
        child_num=0,
        testnet=testnet,
    )

    # m/86' → m/86'/coin_type' → m/86'/coin_type'/0'  (BIP-86 account)
    k1, c1, _ = _bip32_child(master_sk, master_chain, 0x80000000 + 86)
    k2, c2, _ = _bip32_child(k1, c1, 0x80000000 + coin_type)
    k3, c3, acct_parent_fp = _bip32_child(k2, c2, 0x80000000)

    account_pub = _bip32_pub_compressed(k3)
    xpub = _bip32_xpub(
        account_pub, c3,
        depth=3,
        parent_fp=acct_parent_fp,
        child_num=0x80000000,
        testnet=testnet,
    )

    fp_hex = master_fp.hex()
    path = f"86h/{coin_type}h/0h"
    inner = f"tr([{fp_hex}/{path}]{xpub}/<0;1>/*)"
    return descsum_create(inner), master_xprv

# BIP-380 Descriptor Checksum
_DESC_INPUT = "0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~ijklmnopqrstuvwxyzABCDEFGH`#\"\\ "
_DESC_CHECKSUM = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _descsum_polymod(symbols):
    GEN = [0xf5dee51989, 0xa9fdca3312, 0x1bab10e32d, 0x3706b1677a, 0x644d626ffd]
    chk = 1
    for v in symbols:
        top = chk >> 35
        chk = (chk & 0x7ffffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((top >> i) & 1) else 0
    return chk ^ 1

def _descsum_expand(s: str):
    groups, symbols = [], []
    for c in s:
        if c not in _DESC_INPUT:
            return None
        v = _DESC_INPUT.find(c)
        symbols.append(v & 31)
        groups.append(v >> 5)
        if len(groups) == 3:
            symbols.append(groups[0] * 9 + groups[1] * 3 + groups[2])
            groups = []
    if groups:
        rem = len(groups)
        symbols.append(
            (rem == 3) * groups[0] * 9 +
            (rem >= 2) * groups[rem - 2] * 3 +
            groups[-1]
        )
    return symbols

def descsum_create(s: str) -> str:
    """Descriptor string'e BIP-380 checksum ekler."""
    exp = _descsum_expand(s)
    if exp is None:
        return s
    checksum = _descsum_polymod(exp + [0] * 8)
    return s + "#" + "".join(_DESC_CHECKSUM[(checksum >> (5 * (7 - i))) & 31] for i in range(8))

def find_wallet(address: str) -> Optional[Dict]:
    return next((w for w in wallets if w["address"] == address), None)

def find_wallet_for_address(address: str) -> tuple:
    """
    Verilen adresi sahip cüzdanı ve HD indisini döner.

    Önce birincil adreste arar, sonra hd_addresses içinde.
    Returns: (wallet_dict, hd_index_or_None)
      hd_index: int  → HD türetme indisi (0, 1, 2, …)
      hd_index: None → birincil adres veya HD olmayan cüzdan
    """
    for w in wallets:
        if w["address"] == address:
            return w, None
        hd_addrs = w.get("hd_addresses", {})
        for idx_str, info in hd_addrs.items():
            if info.get("address") == address:
                return w, int(idx_str)
    return None, None

def esplora_base(network: str) -> str:
    return {
        "testnet":  "https://mempool.space/testnet/api",
        "testnet4": "https://mempool.space/testnet4/api",
        "mainnet":  "https://mempool.space/api",
    }.get(network, "https://mempool.space/testnet4/api")

def wallet_public(w: Dict) -> Dict:
    """Özel anahtar olmadan cüzdan verisi döner."""
    return {k: v for k, v in w.items() if k != "sk_hex"}


# ── Wallet Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/wallet/new")
def create_wallet(req: WalletCreate):
    """
    Yeni Taproot cüzdanı oluştur.

    Bitcoin Core v26+ aktifse:
        1. tr(xonly_hex)#checksum descriptor oluştur
        2. importdescriptors ile Core cüzdanına izleme adresi olarak ekle
        3. Sonraki listunspent çağrısında adres tanınır

    importprivkey kullanılmaz — descriptor wallet zorunlu (v26+ kısıtı).
    """
    seed_sk = sec_mod.token_bytes(32)
    testnet = req.network in ("testnet", "testnet4")
    # BIP-86 HD: m/86'/coin_type'/0'/0/0 → BIP-341 tweak → adres
    # Sparrow descriptor'ının ilk receive adresiyle eşleşir.
    _child_sk, xonly_pk, address = _hd_child_for_address(seed_sk, testnet)

    wallet = {
        "id": str(uuid.uuid4())[:8],
        "label": req.label,
        "sk_hex": seed_sk.hex(),      # root seed (HD türetme için)
        "xonly_pk": xonly_pk.hex(),   # m/86'/…/0'/0/0 internal key
        "address": address,            # HD child BIP-341 adresi
        "network": req.network,
        "bip341": True,
        "hd": True,                   # HD-derived: signing'de child türet
    }
    wallets.append(wallet)
    _save_wallets()

    # Bitcoin Core v26+ entegrasyonu: descriptor olarak kaydet
    core_import_result = None
    if _core_rpc:
        import time as _time
        try:
            core_import_result = DescriptorWallet.import_taproot_key(
                rpc=_core_rpc,
                xonly_hex=xonly_pk.hex(),
                label=req.label,
                # Yeni üretilen anahtar → şu anki zaman.
                # timestamp=0 genesis'ten tarar, mainnet'te saatler sürer.
                # Geçmişte alınmış işlemleri görmek için eski tarih ver.
                timestamp=int(_time.time()),
            )
        except LegacyMethodError as e:
            # Bu branch teorik — import_taproot_key zaten importdescriptors kullanır
            core_import_result = {"error": str(e)}
        except Exception as e:
            core_import_result = {"warning": f"Core import başarısız: {e}"}

    result = wallet_public(wallet)
    if core_import_result:
        result["core_import"] = core_import_result
    return result


@app.get("/api/wallet/list")
def list_wallets():
    return [wallet_public(w) for w in wallets]


@app.post("/api/wallet/import")
def import_wallet(req: WalletImport):
    """
    MASTER_TPRV / MASTER_XPRV'den Taproot cüzdanı import eder.

    1. MASTER_TPRV decode → (master_sk, master_chain)
    2. BIP-86 HD derivation: m/86'/coin_type'/0'/0/0 → adres türet
    3. Cüzdanı kaydet (hd_imported=True, master_chain_hex ayrı saklanır)
    """
    try:
        master_sk, master_chain = decode_master_xprv(req.master_xprv.strip())
    except ValueError as e:
        raise HTTPException(400, f"Anahtar çözümleme hatası: {e}")

    # Aynı MASTER_TPRV ile ikinci kez import önlemi
    master_sk_hex = master_sk.hex()
    if any(w.get("sk_hex") == master_sk_hex and w.get("hd_imported") for w in wallets):
        raise HTTPException(409, "Bu anahtar zaten import edilmiş")

    testnet = req.network != "mainnet"
    _, xonly_pk, address = _hd_child_for_address(
        None, testnet, master_kv=(master_sk, master_chain)
    )

    wallet = {
        "id":               str(uuid.uuid4())[:8],
        "label":            req.label,
        "sk_hex":           master_sk_hex,          # master private key
        "master_chain_hex": master_chain.hex(),     # chain code (türetme için)
        "xonly_pk":         xonly_pk.hex(),
        "address":          address,
        "network":          req.network,
        "bip341":           True,
        "hd":               True,
        "hd_imported":      True,
    }
    wallets.append(wallet)
    _save_wallets()
    return wallet_public(wallet)


@app.post("/api/wallet/{wallet_id}/hd-scan")
def hd_scan(wallet_id: str, gap_limit: int = 20):
    """
    HD cüzdanın tüm receive adreslerini Esplora üzerinden tarar.

    m/86'/coin_type'/0'/0/0 … /0/N adreslerini türetir.
    Her adres için Esplora'dan UTXO sayısı ve bakiye çeker.
    Sonuçları wallet.hd_addresses olarak kaydeder.

    gap_limit: art arda bu kadar boş adres görülürse tarama durur (BIP-44 default=20).
    Zorunlu HD cüzdan: hd=True olmayan cüzdanlarda 400 döner.
    """
    import urllib.request, json as _json

    w = next((x for x in wallets if x["id"] == wallet_id), None)
    if not w:
        raise HTTPException(404, "Cüzdan bulunamadı")
    if not w.get("hd"):
        raise HTTPException(400, "Yalnızca HD cüzdanlar taranabilir")

    testnet    = w["network"] != "mainnet"
    seed_sk    = bytes.fromhex(w["sk_hex"]) if not w.get("hd_imported") else None
    master_kv  = _wallet_master_kv(w)
    base       = esplora_base(w["network"])

    found      = []
    gap        = 0
    index      = 0

    while gap < gap_limit and index < 200:   # max 200 adres güvenlik sınırı
        _, xonly, address = _hd_child_at_index(
            seed_sk, testnet, change=0, index=index, master_kv=master_kv
        )
        # Esplora adres bilgisi
        utxo_count = 0
        balance_sat = 0
        try:
            url = f"{base}/address/{address}"
            with urllib.request.urlopen(url, timeout=10) as r:
                info = _json.loads(r.read())
            funded   = info.get("chain_stats", {}).get("funded_txo_sum", 0)
            spent    = info.get("chain_stats", {}).get("spent_txo_sum", 0)
            balance_sat = funded - spent
            utxo_count  = info.get("chain_stats", {}).get("funded_txo_count", 0) \
                        - info.get("chain_stats", {}).get("spent_txo_count", 0)
        except Exception:
            pass   # ağ hatası → boş kabul et

        entry = {
            "index":       index,
            "address":     address,
            "xonly_pk":    xonly.hex(),
            "balance_sat": balance_sat,
            "utxo_count":  utxo_count,
        }
        found.append(entry)

        if balance_sat == 0 and utxo_count == 0:
            gap += 1
        else:
            gap = 0

        index += 1

    # Tüm adresleri wallet'a kaydet (boş olanlar dahil — gap analizi için)
    hd_addresses = {str(e["index"]): {
        "address":     e["address"],
        "xonly_pk":    e["xonly_pk"],
        "balance_sat": e["balance_sat"],
        "utxo_count":  e["utxo_count"],
    } for e in found}
    w["hd_addresses"] = hd_addresses
    _save_wallets()

    return {
        "scanned": len(found),
        "gap_limit": gap_limit,
        "addresses": found,
        "total_balance_sat": sum(e["balance_sat"] for e in found),
        "active_count": sum(1 for e in found if e["balance_sat"] > 0),
    }


@app.get("/api/wallet/export")
def export_wallets():
    """
    Tüm cüzdanları WIF özel anahtarı ve Bitcoin Core descriptor'ı ile dışa aktarır.
    Dönen descriptor (tr(WIF)#checksum) Bitcoin Core'a importdescriptors ile yüklenebilir.
    """
    result = []
    for w in wallets:
        sk = bytes.fromhex(w["sk_hex"])
        testnet = w["network"] != "mainnet"
        wif = sk_to_wif(sk, testnet=testnet)
        desc = descsum_create(f"tr({wif})")
        result.append({
            "label":      w["label"],
            "network":    w["network"],
            "address":    w["address"],
            "xonly_pk":   w["xonly_pk"],
            "wif":        wif,
            "descriptor": desc,
            "core_import_cmd": (
                f"bitcoin-cli importdescriptors "
                f"'[{{\"desc\":\"{desc}\",\"timestamp\":\"now\",\"label\":\"{w['label']}\"}}]'"
            ),
        })
    return result


@app.get("/api/wallet/export-bsms/{label}")
def export_wallet_bsms(label: str):
    """
    Sparrow Wallet için BIP-32 HD Taproot descriptor üretir.

    Format: tr([fingerprint/86h/coin_typeh/0h]xpub/<0;1>/*)#checksum
    Ham private key, BIP-32 seed olarak kullanılarak master key türetilir.

    Sparrow import:
        File → New Wallet → Script Type: Taproot (Single Sig)
        Keystore 1 → xPub / Watch Only → descriptor'ı yapıştır.
    """
    w = next((x for x in wallets if x["label"] == label), None)
    if not w:
        raise HTTPException(404, f"Cüzdan bulunamadı: {label}")

    testnet = w["network"] != "mainnet"
    sk_hex  = w["sk_hex"]
    address = w["address"]
    xonly   = w["xonly_pk"]

    if w.get("hd_imported"):
        master_chain = bytes.fromhex(w["master_chain_hex"])
        master_kv = (bytes.fromhex(sk_hex), master_chain)
        desc, master_xprv = make_sparrow_descriptor(None, testnet=testnet, master_kv=master_kv)
    else:
        desc, master_xprv = make_sparrow_descriptor(sk_hex, testnet=testnet)
    xprv_label = "MASTER_TPRV" if testnet else "MASTER_XPRV"

    content = (
        f"DESCRIPTOR:\n"
        f"{desc}\n"
        f"\n"
        f"{xprv_label} (BIP32 imzalama anahtarı):\n"
        f"{master_xprv}\n"
        f"\n"
        f"# ── Sparrow Wallet Import Rehberi (testnet4) ────────────────────\n"
        f"# 1. Sparrow → File → New Wallet → isim gir\n"
        f"# 2. Script Type: Taproot (Single Sig)\n"
        f"# 3. Keystore 1 → Master Private Key (BIP32) → Enter Private Key\n"
        f"# 4. {xprv_label} satırını yapıştır\n"
        f"# 5. Derivation: m/86'/1'/0' — otomatik gelir, değiştirme\n"
        f"# 6. Import Keystore → Apply\n"
        f"#\n"
        f"# Sparrow Receive adresi = aşağıdaki P2TR adresiyle aynı olmalı.\n"
        f"#\n"
        f"# ── Cüzdan Bilgileri ─────────────────────────────────────────────\n"
        f"# Label    : {label}\n"
        f"# Network  : {w['network']}\n"
        f"# P2TR Addr: {address}\n"
        f"# xonly_pk : {xonly}\n"
    )

    filename = label.replace(" ", "_") + ".descriptor"
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ScanSinceRequest(BaseModel):
    since: int  # Unix timestamp

@app.post("/api/wallet/{wallet_id}/scan-since")
def set_scan_since(wallet_id: str, req: ScanSinceRequest):
    """Import edilmiş cüzdan için tarama başlangıç tarihini kaydet."""
    w = next((x for x in wallets if x["id"] == wallet_id), None)
    if not w:
        raise HTTPException(404, "Cüzdan bulunamadı")
    w["scan_since"] = req.since
    _save_wallets()
    return {"ok": True, "scan_since": req.since}


@app.delete("/api/wallet/{address}")
def delete_wallet(address: str):
    global wallets
    before = len(wallets)
    wallets = [w for w in wallets if w["address"] != address]
    if len(wallets) == before:
        raise HTTPException(404, "Cüzdan bulunamadı")
    _save_wallets()
    return {"ok": True}


@app.get("/api/wallet/{address}/balance")
def get_balance(address: str):
    w = find_wallet(address)
    # Wallet listesinde yoksa ağı adresten tahmin et (MuSig2 agg adresleri için)
    if w:
        network = w["network"]
    elif address.startswith("tb1") or address.startswith("m") or address.startswith("n"):
        network = "testnet4"
    else:
        network = "mainnet"

    mgr = get_utxo_manager(network)
    try:
        core_utxos = mgr.fetch_utxos(address)
        confirmed   = sum(u.value_sat for u in core_utxos if u.confirmations >= 1)
        unconfirmed = sum(u.value_sat for u in core_utxos if u.confirmations == 0)
        return {
            "confirmed_sat": confirmed,
            "unconfirmed_sat": unconfirmed,
            "total_sat": confirmed + unconfirmed,
            "utxo_count": len(core_utxos),
            "source": "core_rpc" if _core_rpc else "esplora",
        }
    except Exception as e:
        raise HTTPException(502, f"Bakiye sorgusu başarısız: {e}")


@app.get("/api/wallet/{address}/utxos")
def get_wallet_utxos(address: str):
    w, _ = find_wallet_for_address(address)
    network = w["network"] if w else "testnet4"
    mgr = get_utxo_manager(network)
    try:
        core_utxos = mgr.fetch_utxos(address)
        return [
            {
                "txid": u.txid,
                "vout": u.vout,
                "value": u.value_sat,
                "confirmations": u.confirmations,
                "is_p2tr": u.is_p2tr,
                "spendable": u.spendable,
                "scriptpubkey": u.scriptpubkey.hex(),
            }
            for u in core_utxos
        ]
    except Exception:
        # Esplora fallback (ham format)
        return get_utxos(address)


@app.get("/api/wallet/{address}/txs")
def get_wallet_txs(address: str):
    import urllib.request, json
    w = find_wallet(address)
    if not w:
        raise HTTPException(404, "Cüzdan bulunamadı")
    base = esplora_base(w["network"])
    url = f"{base}/address/{address}/txs"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"API hatası: {e}")


# ── Transaction Endpoints ─────────────────────────────────────────────────────

@app.post("/api/tx/build")
def build_transaction(req: TxRequest):
    """
    Taproot transaction oluştur ve imzala.

    SIGHASH_DEFAULT (0x00) Kullanım Gerekçesi:
        BIP-341'de SIGHASH_DEFAULT, witness'a ek byte eklemez (64B imza).
        SIGHASH_ALL (0x01) ise 65B imza üretir — 1 vByte fazla.
        Tüm girdi+çıktıları taahhüt eder; SIGHASH_ALL ile matematiksel özdeş.
        Standard form: Core ve donanım cüzdanlar 0x00 bekler.

    scriptPubKey Zorunluluğu:
        BIP-341 sighash, sha_scriptpubkeys = SHA256(Σ input scriptpubkeys) hesaplar.
        Yanlış scriptPubKey → geçersiz sighash → "non-mandatory-script-verify-flag".
        P2TR için: 0x51 0x20 <32-byte-xonly> (34 byte, her zaman).
    """
    w, hd_index = find_wallet_for_address(req.from_address)
    if not w:
        raise HTTPException(404, "Kaynak cüzdan bulunamadı")

    sk = bytes.fromhex(w["sk_hex"])

    # HD alt adres için doğru xonly_pk ve spk türet
    if hd_index is not None:
        hd_info = w.get("hd_addresses", {}).get(str(hd_index), {})
        xonly_pk = bytes.fromhex(hd_info["xonly_pk"])
    else:
        xonly_pk = bytes.fromhex(w["xonly_pk"])
    my_spk = b'\x51\x20' + xonly_pk

    # ── UTXO Toplama ─────────────────────────────────────────────────────────
    # HD cüzdanlar için tüm alt-adres UTXO'larını da topla (coin control veya
    # otomatik seçimde yeterli bakiye olabilmesi için).
    mgr = get_utxo_manager(w["network"])

    # adres → (spk, tweaked_signing_sk) haritası — imzalama için lazım
    addr_info: dict[str, tuple[bytes, bytes]] = {}

    def _collect_utxos_for_address(addr: str, spk: bytes, tweaked_sk: bytes) -> list:
        try:
            utxos = mgr.fetch_utxos(addr)
        except Exception:
            return []
        for u in utxos:
            if not u.scriptpubkey or len(u.scriptpubkey) != 34:
                u.scriptpubkey = spk
                u.is_p2tr = True
        addr_info[addr] = (spk, tweaked_sk)
        return [u for u in utxos if u.confirmations >= 1]

    confirmed_utxos: list = []

    if w.get("hd"):
        testnet_w = w["network"] != "mainnet"
        master_kv = _wallet_master_kv(w)
        seed_sk   = None if master_kv else sk

        # Tüm bilinen HD alt-adreslerini tara
        for idx_str, info in w.get("hd_addresses", {}).items():
            sub_idx = int(idx_str)
            sub_addr = info.get("address", "")
            if not sub_addr:
                continue
            child_sk, _, _ = _hd_child_at_index(seed_sk, testnet_w, 0, sub_idx, master_kv)
            _, tweaked = taproot_tweak_key(child_sk)
            sub_spk = b'\x51\x20' + bytes.fromhex(info["xonly_pk"])
            confirmed_utxos.extend(_collect_utxos_for_address(sub_addr, sub_spk, tweaked))

        # Birincil adres de taranacaksa (index 0 hd_addresses'de yoksa)
        if req.from_address not in addr_info:
            child_sk0, _, _ = _hd_child_at_index(seed_sk, testnet_w, 0, 0, master_kv)
            _, tweaked0 = taproot_tweak_key(child_sk0)
            confirmed_utxos.extend(_collect_utxos_for_address(req.from_address, my_spk, tweaked0))
    else:
        # Tek-anahtar cüzdan
        if w.get("bip341"):
            _, tweaked_sk = taproot_tweak_key(sk)
        else:
            tweaked_sk = sk
        try:
            utxos = mgr.fetch_utxos(req.from_address)
        except Exception as e:
            raise HTTPException(502, f"UTXO sorgusu başarısız: {e}")
        for u in utxos:
            if not u.scriptpubkey or len(u.scriptpubkey) != 34:
                u.scriptpubkey = my_spk
                u.is_p2tr = True
        addr_info[req.from_address] = (my_spk, tweaked_sk)
        confirmed_utxos = [u for u in utxos if u.confirmations >= 1]

    if not confirmed_utxos:
        raise HTTPException(400, "Onaylanmış P2TR UTXO bulunamadı")

    # ── Alıcı scriptPubKey ────────────────────────────────────────────────────
    try:
        recipient_spk = address_to_scriptpubkey(req.to_address)
    except ValueError as exc:
        raise HTTPException(400, f"Alıcı adres hatalı: {exc}")

    # ── Coin Selection ────────────────────────────────────────────────────────
    # Kullanıcı UTXO seçmişse önce onlar, yetmezse smallest-first ile tamamla.
    try:
        if req.utxo_ids:
            pinned_set = set(req.utxo_ids)
            pinned  = [u for u in confirmed_utxos if f"{u.txid}:{u.vout}" in pinned_set]
            rest    = sorted(
                [u for u in confirmed_utxos if f"{u.txid}:{u.vout}" not in pinned_set],
                key=lambda u: u.value_sat
            )
            pinned_total = sum(u.value_sat for u in pinned)
            need = req.amount_sat + req.fee_sat
            if pinned_total >= need:
                selected   = pinned
                change_sat = pinned_total - need
            else:
                extra, extra_change = CoinSelector.smallest_first(
                    rest, need - pinned_total, 0
                )
                selected   = pinned + extra
                change_sat = extra_change
        else:
            selected, change_sat = CoinSelector.smallest_first(
                confirmed_utxos, req.amount_sat, req.fee_sat
            )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # ── TX Çıktıları ──────────────────────────────────────────────────────────
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > CoinSelector.DUST_LIMIT_SAT:
        outputs.append(TxOutput(change_sat, my_spk))
    else:
        change_sat = 0

    # ── Per-input signing key listesi ─────────────────────────────────────────
    # Her UTXO'nun scriptpubkey'i hangi adrese aitse o adresin sk'sini kullan.
    # addr_info: { address → (spk, tweaked_sk) }
    spk_to_tweaked: dict[bytes, bytes] = {spk: tsk for spk, tsk in addr_info.values()}

    sk_per_input: list[bytes] = []
    for u in selected:
        tweaked = spk_to_tweaked.get(u.scriptpubkey)
        if tweaked is None:
            raise HTTPException(500, f"İmza anahtarı bulunamadı: spk={u.scriptpubkey.hex()}")
        sk_per_input.append(tweaked)

    # Tek-anahtar durumda sk_per_input yerine sadece sk kullan (backward-compat)
    single_sk = sk_per_input[0] if len(set(sk_per_input)) == 1 else None

    signer = TaprootSigner(sighash_type=SighashType.DEFAULT)
    try:
        if single_sk:
            raw, witnesses = signer.sign_transaction(single_sk, selected, outputs)
        else:
            raw, witnesses = signer.sign_transaction(
                sk_per_input[0], selected, outputs, sk_per_input=sk_per_input
            )
    except ValueError as e:
        raise HTTPException(400, f"İmzalama hatası: {e}")

    tx_hex = raw.hex()
    summary = TaprootSigner.decode_tx_summary(raw)

    # Hangi UTXO'ların kullanıldığını frontend'e bildir
    # (adres→label için frontend kendi eşleştirmesini yapar)
    used_utxos = [
        {"txid": u.txid, "vout": u.vout, "value": u.value_sat,
         "id": f"{u.txid}:{u.vout}"}
        for u in selected
    ]

    return {
        "tx_hex": tx_hex,
        "tx_size": len(raw),
        "tx_vbytes": summary,
        "fee_sat": req.fee_sat,
        "change_sat": change_sat,
        "input_count": len(selected),
        "output_count": len(outputs),
        "sighash_type": "SIGHASH_DEFAULT (0x00)",
        "signature": witnesses[0].hex() if witnesses else "",
        "used_utxos": used_utxos,
    }


@app.post("/api/tx/broadcast")
def broadcast(req: BroadcastRequest):
    """
    TX'i ağa yayınla. raw_tx.broadcast_tx yerine doğrudan Esplora kullanılır:
    raw_tx modülü uvicorn tarafından import anında cache'lenir ve ESPLORA_TESTNET
    sabitini o anki değeriyle kilitler. Ağ değişikliği sonrası reload tutarsızlık
    yaratır. Burada esplora_base() ile her zaman doğru URL seçilir.
    """
    import urllib.request as ur, urllib.error

    # Ağı cüzdanlardan tespit et (yoksa testnet4)
    networks = list({w["network"] for w in wallets}) if wallets else []
    network  = networks[0] if len(networks) == 1 else os.environ.get("BITCOIN_NETWORK", "testnet4")
    url      = esplora_base(network) + "/tx"

    try:
        req2 = ur.Request(url, data=req.tx_hex.encode(),
                          headers={"Content-Type": "text/plain"})
        with ur.urlopen(req2, timeout=15) as r:
            txid = r.read().decode()
        return {"txid": txid}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise HTTPException(400, f"Yayınlama başarısız: {err}")
    except Exception as e:
        raise HTTPException(500, f"Bağlantı hatası: {e}")


@app.get("/api/tx/{txid}")
def get_transaction(txid: str, network: str = "testnet"):
    import urllib.request, json
    base = esplora_base(network)
    try:
        with urllib.request.urlopen(f"{base}/tx/{txid}", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"API hatası: {e}")


# ── Bitcoin Core Entegrasyon Endpoints ───────────────────────────────────────

@app.get("/api/core/status")
def core_status():
    """
    Bitcoin Core v26+ bağlantı durumu ve node bilgisi.

    Descriptor Wallet Uyum Kontrolü:
        chain    : test / main / regtest / testnet4
        blocks   : Senkronize blok yüksekliği
        progress : 1.0 = tam senkronize

    Legacy Metodlar (v26+ devre dışı):
        importprivkey, importpubkey, addmultisigaddress,
        createmultisig, importwallet, dumpwallet, dumpprivkey

    Modern Karşılıklar:
        importdescriptors([{"desc": "tr(xonly)#checksum", "timestamp": 0}])
        createwallet(name, descriptors=True)
    """
    if not _core_rpc:
        return {
            "connected": False,
            "mode": "esplora",
            "message": "Bitcoin Core RPC devre dışı. USE_CORE_RPC=true ile aktifleştirin.",
            "legacy_methods_disabled": True,
            "descriptor_wallet_required": True,
        }

    try:
        info   = _core_rpc.health_check()          # getblockchaininfo
        net    = _core_rpc.get_network_info()
        mem    = _core_rpc.get_mempool_info()
        fee    = _core_rpc.estimate_smart_fee(conf_target=6)
        return {
            "connected": True,
            "mode": "core_rpc",
            "chain": info["chain"],
            "blocks": info["blocks"],
            "headers": info["headers"],
            "sync_progress": round(info["verificationprogress"], 6),
            "pruned": info.get("pruned", False),
            "peer_count": net.get("connections", 0),
            "mempool_tx_count": mem.get("size", 0),
            "mempool_size_mb": round(mem.get("bytes", 0) / 1e6, 2),
            "fee_sat_per_vbyte": fee.get("sat_per_vbyte"),
            "network": _core_rpc.network,
            "rpc_port": _core_rpc.rpcport,
            # Descriptor wallet zorunluluğu bilgisi
            "descriptor_wallet": {
                "required": True,
                "note": "v26+ tüm cüzdanlar descriptor tabanlıdır",
                "disabled_legacy_methods": list(LegacyMethodError.LEGACY_METHODS),
                "import_method": "importdescriptors",
                "taproot_format": "tr(xonly_hex)#checksum",
            }
        }
    except RPCConnectionError as e:
        return {"connected": False, "error": str(e)}
    except RPCError as e:
        return {"connected": False, "error": f"RPC {e.code}: {e.message}"}


@app.post("/api/core/import-wallet")
def core_import_wallet(data: dict):
    """
    Mevcut cüzdanı Bitcoin Core'a descriptor olarak aktar.

    importprivkey yerine importdescriptors (v26+ zorunlu).

    Body: {"xonly_hex": "64-char-hex", "label": "...", "timestamp": 0}
    """
    if not _core_rpc:
        raise HTTPException(503, "Bitcoin Core RPC bağlı değil")

    xonly_hex = data.get("xonly_hex", "")
    label     = data.get("label", "")
    timestamp = data.get("timestamp", 0)

    try:
        results = DescriptorWallet.import_taproot_key(
            rpc=_core_rpc,
            xonly_hex=xonly_hex,
            label=label,
            timestamp=timestamp,
        )
        descriptor = DescriptorWallet.taproot_key_path(xonly_hex)
        return {
            "success": True,
            "descriptor": descriptor,
            "results": results,
        }
    except LegacyMethodError as e:
        raise HTTPException(400, f"Legacy metod hatası: {e.message}")
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/core/fee-estimate")
def core_fee_estimate(conf_target: int = 6):
    """estimatesmartfee — hedef blok için sat/vByte tahmini."""
    if not _core_rpc:
        raise HTTPException(503, "Bitcoin Core RPC bağlı değil")
    try:
        return _core_rpc.estimate_smart_fee(conf_target=conf_target)
    except RPCError as e:
        raise HTTPException(400, str(e))


# ── MuSig2 Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/musig2/new")
def create_musig2_session(req: MusigCreate):
    sid = str(uuid.uuid4())[:8]
    participants = []
    for i in range(req.n_participants):
        sk = sec_mod.token_bytes(32)
        P = _point_mul(int.from_bytes(sk, "big"), G)
        pk = point_to_bytes(P)
        participants.append({
            "index": i,
            "label": f"Katılımcı {i+1}",
            "sk_hex": sk.hex(),
            "pk_hex": pk.hex(),
            "nonce_secret": None,
            "pub_nonce": None,
            "partial_sig": None,
        })

    pk_list = sorted([bytes.fromhex(p["pk_hex"]) for p in participants])
    Q, _ = key_aggregation(pk_list)
    testnet = req.network in ("testnet", "testnet4")
    agg_address = _bech32m_encode("tb" if testnet else "bc", _xonly(Q))

    musig2_sessions[sid] = {
        "id": sid,
        "label": req.label,
        "n": req.n_participants,
        "network": req.network,
        "state": "KEYS_READY",
        "participants": participants,
        "pk_list": [p["pk_hex"] for p in participants],
        "agg_xonly": _xonly(Q).hex(),
        "agg_address": agg_address,
        "agg_nonce": None,
        "final_sig": None,
        "tx_hex": None,
        "utxos": [],
    }
    _save_musig2()
    return _session_public(musig2_sessions[sid])


@app.get("/api/musig2/list")
def list_musig2():
    return [_session_public(s) for s in musig2_sessions.values()]


@app.get("/api/musig2/{sid}")
def get_session(sid: str):
    s = musig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    return _session_public(s)


@app.post("/api/musig2/{sid}/nonces")
def generate_nonces(sid: str):
    s = musig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")

    pk_list_bytes = sorted([bytes.fromhex(pk) for pk in s["pk_list"]])
    msg = tagged_hash("MuSig/session", bytes.fromhex(s["agg_xonly"]))

    for p in s["participants"]:
        sk = bytes.fromhex(p["sk_hex"])
        pk = bytes.fromhex(p["pk_hex"])
        secret, pub = nonce_gen(sk, pk, msg)
        p["nonce_secret"] = [secret[0], secret[1]]
        p["pub_nonce"] = [pub[0].hex(), pub[1].hex()]

    s["state"] = "NONCES_READY"
    s["tx_hex"] = None
    s["final_sig"] = None

    # Aggregate nonces
    pub_nonces = [(bytes.fromhex(p["pub_nonce"][0]), bytes.fromhex(p["pub_nonce"][1]))
                  for p in s["participants"]]
    agg_R1, agg_R2 = nonce_agg(pub_nonces)
    s["agg_nonce"] = [_xonly(agg_R1).hex(), _xonly(agg_R2).hex()]
    _save_musig2()

    return _session_public(s)


@app.post("/api/musig2/{sid}/sign")
def musig2_sign(sid: str, req: MusigPartialSign):
    s = musig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] not in ("NONCES_READY", "SIGNING"):
        raise HTTPException(400, f"Geçersiz durum: {s['state']}")

    # Build transaction for signing
    agg_spk = bytes([0x51, 0x20]) + bytes.fromhex(s["agg_xonly"])
    utxos_raw = get_utxos(s["agg_address"])
    confirmed = [u for u in utxos_raw if u.get("status", {}).get("confirmed")]
    if not confirmed:
        raise HTTPException(400, "MuSig2 adresinde UTXO yok")

    # Smallest-first UTXO seçimi — gerektiği kadar UTXO ekle
    confirmed.sort(key=lambda u: u["value"])
    selected, total_in = [], 0
    for u in confirmed:
        selected.append(u)
        total_in += u["value"]
        if total_in >= req.amount_sat + req.fee_sat:
            break

    if total_in < req.amount_sat + req.fee_sat:
        raise HTTPException(
            400,
            f"Yetersiz bakiye: toplam {sum(u['value'] for u in confirmed)} sat, "
            f"gerekli {req.amount_sat + req.fee_sat} sat "
            f"(miktar {req.amount_sat} + ücret {req.fee_sat})"
        )

    inputs = [UTXO(txid=u["txid"], vout=u["vout"], value_sat=u["value"], scriptpubkey=agg_spk)
              for u in selected]

    try:
        recipient_spk = address_to_scriptpubkey(req.to_address)
    except Exception:
        raise HTTPException(400, "Alıcı adresi doğrulanamadı")

    change_sat = total_in - req.amount_sat - req.fee_sat
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > 546:
        outputs.append(TxOutput(change_sat, agg_spk))

    # Her girdi için ayrı nonce üret ve imzala (nonce yeniden kullanımı yok)
    pk_list_bytes = sorted([bytes.fromhex(pk) for pk in s["pk_list"]])
    Q, _ = key_aggregation(pk_list_bytes)

    all_sigs = []
    for idx in range(len(inputs)):
        sighash = taproot_sighash(inputs, outputs, idx)

        # Sighash'i nonce tohumu olarak kullan — her girdi için benzersiz
        per_pub_nonces, per_secrets = [], []
        for p in s["participants"]:
            sk_b = bytes.fromhex(p["sk_hex"])
            pk_b = bytes.fromhex(p["pk_hex"])
            secret, pub = nonce_gen(sk_b, pk_b, sighash)
            per_pub_nonces.append((pub[0], pub[1]))
            per_secrets.append((secret[0], secret[1]))

        agg_R1, agg_R2 = nonce_agg(per_pub_nonces)
        agg_nonce_pt = (agg_R1, agg_R2)
        R, _ = session_ctx(agg_nonce_pt, Q, sighash)

        partial_sigs = []
        for j, p in enumerate(s["participants"]):
            sk_b = bytes.fromhex(p["sk_hex"])
            pk_b = bytes.fromhex(p["pk_hex"])
            coeff = key_agg_coeff(pk_list_bytes, pk_b)
            si = partial_sign(per_secrets[j], sk_b, coeff, Q, agg_nonce_pt, sighash)
            partial_sigs.append(si)

        final_sig = partial_sig_agg(partial_sigs, R)
        if not schnorr_verify(sighash, _xonly(Q), final_sig):
            raise HTTPException(500, f"Girdi {idx} Schnorr doğrulaması başarısız")
        all_sigs.append(final_sig)

    valid = True
    final_sig = all_sigs[0]  # raporlama için
    raw = build_tx(inputs, outputs, all_sigs)
    s["tx_hex"] = raw.hex()
    s["final_sig"] = final_sig.hex()
    s["state"] = "SIGNED"
    s["_outputs"] = outputs
    s["_inp"] = u
    _save_musig2()

    return {
        **_session_public(s),
        "tx_hex": raw.hex(),
        "sighash": sighash.hex(),
        "final_sig": final_sig.hex(),
        "valid": valid,
    }


@app.post("/api/musig2/{sid}/broadcast")
def musig2_broadcast(sid: str):
    s = musig2_sessions.get(sid)
    if not s or not s.get("tx_hex"):
        raise HTTPException(400, "İmzalanmış transaction yok")
    import urllib.request as ur, urllib.error
    url = esplora_base(s.get("network", "testnet4")) + "/tx"
    try:
        req2 = ur.Request(url, data=s["tx_hex"].encode(),
                          headers={"Content-Type": "text/plain"})
        with ur.urlopen(req2, timeout=15) as r:
            txid = r.read().decode()
        s["state"] = "BROADCAST"
        _save_musig2()
        return {"txid": txid}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise HTTPException(400, f"Yayınlama başarısız: {err}")
    except Exception as e:
        raise HTTPException(500, f"Bağlantı hatası: {e}")


@app.get("/api/musig2/{sid}/utxos")
def musig2_utxos(sid: str):
    s = musig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    return get_utxos(s["agg_address"])


def _session_public(s: Dict) -> Dict:
    """Oturum verisini döner (participant sk_hex gizlenir)."""
    result = {k: v for k, v in s.items() if k not in ("_outputs", "_inp")}
    result["participants"] = [{
        k2: v2 for k2, v2 in p.items()
        if k2 not in ("sk_hex", "nonce_secret", "partial_sig")
    } for p in s["participants"]]
    return result


# ── Dağıtık MuSig2 Endpoints ──────────────────────────────────────────────────
#
# Backend YALNIZCA koordinatördür: pubkey, pubnonce, partial_sig toplar.
# Özel anahtarlar asla sunucuya gönderilmez — tüm imzalama tarayıcıda yapılır.
#
# Durum makinesi:
#   COLLECTING_PUBKEYS  → tüm pubkey'ler alındığında → READY_FOR_TX
#   READY_FOR_TX        → tx parametreleri girildiğinde (sighash hesabı) → COLLECTING_NONCES
#   COLLECTING_NONCES   → tüm nonce'lar alındığında → COLLECTING_SIGS
#   COLLECTING_SIGS     → tüm partial_sig'ler alındığında → SIGNED
#   SIGNED              → broadcast edildiğinde → BROADCAST

@app.post("/api/musig2d/new")
def create_dmusig2_session(req: DMusig2Create):
    if req.n_participants < 2 or req.n_participants > 10:
        raise HTTPException(400, "Katılımcı sayısı 2-10 arasında olmalı")
    sid = str(uuid.uuid4())[:8]
    participants = [
        {"index": i, "label": f"Katılımcı {i+1}",
         "pubkey": None, "pubnonces": [], "partial_sigs": []}
        for i in range(req.n_participants)
    ]
    dmusig2_sessions[sid] = {
        "id": sid,
        "label": req.label,
        "n": req.n_participants,
        "network": req.network,
        "state": "COLLECTING_PUBKEYS",
        "participants": participants,
        "pk_list_sorted": [],
        "agg_xonly": None,
        "agg_q_even_y": None,   # True if aggregate Q has even Y — frontend needs this for d-negation
        "agg_address": None,
        "agg_nonces": [],     # [{r1: hex, r2: hex}, ...] per input
        "sighashes": [],      # [hex, ...] per input
        "inputs": [],
        "to_address": None,
        "amount_sat": None,
        "fee_sat": None,
        "change_sat": 0,
        "description": "",
        "tx_hex": None,
        "final_sig": None,
        "created_at": int(_time.time()),   # Phase 5: TTL hesabı için
        "txid": None,
        "source_session_id": req.source_session_id,
    }
    _save_dmusig2()
    return dmusig2_sessions[sid]


@app.get("/api/musig2d/list")
def list_dmusig2():
    def _norm(s):
        return {**s, "txid": s.get("txid"), "created_at": s.get("created_at"), "source_session_id": s.get("source_session_id")}
    return [_norm(s) for s in dmusig2_sessions.values()]


@app.get("/api/musig2d/actions")
def dmusig2_actions(pubkey: str):
    """
    Verilen pubkey'in (33-byte compressed hex) tüm oturumlardaki
    bekleyen aksiyonlarını döner.

    Her eleman:
      { session_id, label, network, state, participant_index, action }

    action değerleri (aktif):
      "build_tx"           — koordinatör, TX henüz oluşturulmamış
      "submit_nonce"       — nonce henüz gönderilmemiş
      "submit_partial_sig" — kısmi imza henüz gönderilmemiş
      "broadcast"          — TX imzalandı, yayınlanabilir (tüm katılımcılar)

    action değerleri (bekleme):
      "wait_pubkeys"       — diğer katılımcıların pubkey kaydını bekliyor
      "wait_coordinator"   — koordinatörün TX oluşturmasını bekliyor
      "wait_nonce"         — diğerlerinin nonce göndermesini bekliyor
      "wait_sig"           — diğerlerinin imzasını bekliyor
      "done"               — oturum tamamlandı
    """
    pubkey = pubkey.strip().lower()
    if len(pubkey) != 66:
        raise HTTPException(400, "pubkey 33-byte compressed hex (66 karakter) olmalı")

    def _nonces_missing(p: dict) -> bool:
        """Katılımcının nonce'u eksik mi? Multi-input ve None-filled listeleri de yakalar."""
        nonces = p.get("pubnonces") or []
        return not nonces or any(n is None for n in nonces)

    def _sigs_missing(p: dict) -> bool:
        """Katılımcının kısmi imzası eksik mi? Multi-input ve None-filled listeleri de yakalar."""
        sigs = p.get("partial_sigs") or []
        return not sigs or any(sig is None for sig in sigs)

    now = _time.time()
    results = []
    for s in dmusig2_sessions.values():
        # Phase 5: Süresi dolmuş oturumları /actions'dan gizle
        created_at = s.get("created_at", now)  # eski oturumlar için şimdiki zaman → hemen expire etme
        expires_at = created_at + SESSION_TTL_SECONDS
        if now > expires_at:
            continue

        for p in s["participants"]:
            if p.get("pubkey") != pubkey:
                continue

            idx   = p["index"]
            state = s["state"]

            if state == "COLLECTING_PUBKEYS":
                # Kendi pubkey'i zaten kayıtlı (bu satıra geldik), diğerlerini bekliyor
                action = "wait_pubkeys"

            elif state == "READY_FOR_TX":
                # Koordinatör (idx=0) TX oluşturmalı; diğerleri bekliyor
                action = "build_tx" if idx == 0 else "wait_coordinator"

            elif state == "COLLECTING_NONCES":
                action = "submit_nonce" if _nonces_missing(p) else "wait_nonce"

            elif state == "COLLECTING_SIGS":
                action = "submit_partial_sig" if _sigs_missing(p) else "wait_sig"

            elif state == "SIGNED":
                # Herhangi bir katılımcı broadcast yapabilir (koordinatör offline olabilir)
                action = "broadcast"

            else:  # BROADCAST veya bilinmeyen
                action = "done"

            # Participant-level nonce/sig özeti — frontend diff için
            participants_summary = [
                {
                    "index": pp["index"],
                    "label": pp["label"],
                    "has_nonce": bool(pp.get("pubnonces") and
                                     any(n is not None for n in pp["pubnonces"])),
                    "has_sig":   bool(pp.get("partial_sigs") and
                                     any(sg is not None for sg in pp["partial_sigs"])),
                }
                for pp in s["participants"]
            ]

            results.append({
                "session_id":        s["id"],
                "label":             s["label"],
                "network":           s["network"],
                "state":             state,
                "participant_index": idx,
                "action":            action,
                "participants":      participants_summary,
                "expires_at":        int(expires_at),   # Unix timestamp
            })
            break  # bir oturumda en fazla bir eşleşme

    return results


@app.get("/api/musig2d/{sid}")
def get_dmusig2_session(sid: str):
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    # Backfill agg_q_even_y for sessions loaded from disk before this field existed
    if _migrate_dmusig2_session(s):
        _save_dmusig2()
    return {**s, "txid": s.get("txid"), "created_at": s.get("created_at"), "source_session_id": s.get("source_session_id")}


@app.delete("/api/musig2d/{sid}")
def delete_dmusig2_session(sid: str):
    if sid not in dmusig2_sessions:
        raise HTTPException(404, "Oturum bulunamadı")
    del dmusig2_sessions[sid]
    _save_dmusig2()
    return {"ok": True}


@app.post("/api/musig2d/{sid}/register")
def dmusig2_register(sid: str, req: DMusig2Register):
    """Katılımcı pubkey'ini kaydeder. Tüm pubkey'ler gelince agg_address hesaplanır."""
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] != "COLLECTING_PUBKEYS":
        raise HTTPException(400, f"Pubkey kaydı için geçersiz durum: {s['state']}")

    idx = req.participant_index
    if idx < 0 or idx >= s["n"]:
        raise HTTPException(400, f"Geçersiz katılımcı indeksi: {idx}")

    try:
        pk_bytes = bytes.fromhex(req.pubkey_hex)
        if len(pk_bytes) != 33:
            raise ValueError("33 byte compressed pubkey gerekli")
        point_from_bytes(pk_bytes)
    except Exception as e:
        raise HTTPException(400, f"Geçersiz pubkey: {e}")

    s["participants"][idx]["pubkey"] = req.pubkey_hex

    if all(p["pubkey"] is not None for p in s["participants"]):
        pk_list_bytes = sorted([bytes.fromhex(p["pubkey"]) for p in s["participants"]])
        Q, _ = key_aggregation(pk_list_bytes)
        testnet = s["network"] in ("testnet", "testnet4")
        agg_address = _bech32m_encode("tb" if testnet else "bc", _xonly(Q))
        s["pk_list_sorted"] = [b.hex() for b in pk_list_bytes]
        s["agg_xonly"] = _xonly(Q).hex()
        s["agg_q_even_y"] = (Q.y % 2 == 0)
        s["agg_address"] = agg_address
        s["state"] = "READY_FOR_TX"
        logger.debug("[register] Session %s: agg_q_even_y=%s, Q.y=%s", sid, s['agg_q_even_y'], hex(Q.y))

    _save_dmusig2()
    return s


@app.post("/api/musig2d/{sid}/build-tx")
def dmusig2_build_tx(sid: str, req: DMusig2BuildTx):
    """
    TX parametrelerini alır, UTXO seçer, sighash(ler) hesaplar.
    Katılımcılar bu sighash üzerinde nonce üretip imzalayacak.
    """
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] not in ("READY_FOR_TX", "COLLECTING_NONCES"):
        raise HTTPException(400, f"TX oluşturma için geçersiz durum: {s['state']}")

    agg_spk = bytes([0x51, 0x20]) + bytes.fromhex(s["agg_xonly"])

    try:
        utxos_raw = get_utxos(s["agg_address"])
    except Exception as e:
        raise HTTPException(502, f"UTXO sorgusu başarısız: {e}")

    confirmed = [u for u in utxos_raw if u.get("status", {}).get("confirmed")]
    if not confirmed:
        raise HTTPException(400, "MuSig2 adresinde onaylanmış UTXO yok")

    total_needed = req.amount_sat + req.fee_sat
    confirmed.sort(key=lambda u: u["value"])

    selected_utxos, total_in = [], 0
    for u in confirmed:
        selected_utxos.append(u)
        total_in += u["value"]
        if total_in >= total_needed:
            break

    if total_in < total_needed:
        avail = sum(u["value"] for u in confirmed)
        raise HTTPException(400, f"Yetersiz bakiye: {avail} sat, gerekli: {total_needed} sat")

    try:
        recipient_spk = address_to_scriptpubkey(req.to_address)
    except Exception as e:
        raise HTTPException(400, f"Alıcı adres hatası: {e}")

    change_sat = total_in - req.amount_sat - req.fee_sat
    utxo_objs = [UTXO(txid=u["txid"], vout=u["vout"], value_sat=u["value"], scriptpubkey=agg_spk)
                 for u in selected_utxos]
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > 546:
        outputs.append(TxOutput(change_sat, agg_spk))

    sighashes = [taproot_sighash(utxo_objs, outputs, i).hex()
                 for i in range(len(utxo_objs))]

    s["inputs"] = [{"txid": u["txid"], "vout": u["vout"], "value": u["value"]}
                   for u in selected_utxos]
    s["sighashes"] = sighashes
    s["to_address"] = req.to_address
    s["amount_sat"] = req.amount_sat
    s["fee_sat"] = req.fee_sat
    s["change_sat"] = change_sat if change_sat > 546 else 0
    s["description"] = req.description

    # Nonce/sig listelerini sıfırla (birden fazla build-tx çağrısına karşı)
    n_inputs = len(sighashes)
    for p in s["participants"]:
        p["pubnonces"] = [None] * n_inputs
        p["partial_sigs"] = [None] * n_inputs
    s["agg_nonces"] = [None] * n_inputs
    s["state"] = "COLLECTING_NONCES"

    _save_dmusig2()
    return s


@app.post("/api/musig2d/{sid}/submit-nonce")
def dmusig2_submit_nonce(sid: str, req: DMusig2SubmitNonce):
    """
    Katılımcı, her input için oluşturduğu pubnonce çiftini gönderir.
    Tüm katılımcılar gönderince agg_nonce hesaplanır → COLLECTING_SIGS durumuna geçilir.
    """
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] != "COLLECTING_NONCES":
        raise HTTPException(400, f"Nonce gönderimi için geçersiz durum: {s['state']}")

    idx = req.participant_index
    if idx < 0 or idx >= s["n"]:
        raise HTTPException(400, f"Geçersiz katılımcı indeksi: {idx}")

    n_inputs = len(s["sighashes"])
    if len(req.pubnonces) != n_inputs:
        raise HTTPException(400, f"{n_inputs} input için {n_inputs} nonce gerekli")

    for i, pn in enumerate(req.pubnonces):
        try:
            r1 = bytes.fromhex(pn["r1"])
            r2 = bytes.fromhex(pn["r2"])
            if len(r1) != 33 or len(r2) != 33:
                raise ValueError("33-byte compressed point gerekli")
            point_from_bytes(r1)
            point_from_bytes(r2)
        except Exception as e:
            raise HTTPException(400, f"Input {i} geçersiz nonce: {e}")

    s["participants"][idx]["pubnonces"] = [
        {"r1": pn["r1"], "r2": pn["r2"]} for pn in req.pubnonces
    ]

    if all(p["pubnonces"] and all(n is not None for n in p["pubnonces"])
           for p in s["participants"]):
        agg_nonces = []
        for i in range(n_inputs):
            pub_nonces_i = [
                (bytes.fromhex(p["pubnonces"][i]["r1"]),
                 bytes.fromhex(p["pubnonces"][i]["r2"]))
                for p in s["participants"]
            ]
            agg_R1, agg_R2 = nonce_agg(pub_nonces_i)
            agg_nonces.append({"r1": point_to_bytes(agg_R1).hex(),
                                "r2": point_to_bytes(agg_R2).hex()})
        s["agg_nonces"] = agg_nonces
        s["state"] = "COLLECTING_SIGS"

    _save_dmusig2()
    return s


@app.post("/api/musig2d/{sid}/submit-partial-sig")
def dmusig2_submit_sig(sid: str, req: DMusig2SubmitSig):
    """
    Katılımcı, her input için hesapladığı kısmi imzayı gönderir.
    Tüm katılımcılar gönderince imzalar birleştirilir ve TX oluşturulur.
    """
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] != "COLLECTING_SIGS":
        raise HTTPException(400, f"İmza gönderimi için geçersiz durum: {s['state']}")

    idx = req.participant_index
    if idx < 0 or idx >= s["n"]:
        raise HTTPException(400, f"Geçersiz katılımcı indeksi: {idx}")

    n_inputs = len(s["sighashes"])
    if len(req.partial_sigs) != n_inputs:
        raise HTTPException(400, f"{n_inputs} input için {n_inputs} kısmi imza gerekli")

    for i, sig_hex in enumerate(req.partial_sigs):
        try:
            sig_bytes = bytes.fromhex(sig_hex)
            if len(sig_bytes) != 32:
                raise ValueError("32-byte skaler gerekli")
        except Exception as e:
            raise HTTPException(400, f"Input {i} geçersiz kısmi imza: {e}")

    s["participants"][idx]["partial_sigs"] = list(req.partial_sigs)

    if all(p["partial_sigs"] and all(sig is not None for sig in p["partial_sigs"])
           for p in s["participants"]):
        # Tüm kısmi imzalar toplandı — aggregate et
        pk_list_bytes = [bytes.fromhex(pk) for pk in s["pk_list_sorted"]]
        Q, _ = key_aggregation(pk_list_bytes)
        agg_spk = bytes([0x51, 0x20]) + bytes.fromhex(s["agg_xonly"])

        utxo_objs = [
            UTXO(txid=inp["txid"], vout=inp["vout"],
                 value_sat=inp["value"], scriptpubkey=agg_spk)
            for inp in s["inputs"]
        ]
        try:
            recipient_spk = address_to_scriptpubkey(s["to_address"])
        except Exception as e:
            raise HTTPException(400, f"Alıcı adres hatası: {e}")

        outputs = [TxOutput(s["amount_sat"], recipient_spk)]
        if s["change_sat"] > 546:
            outputs.append(TxOutput(s["change_sat"], agg_spk))

        all_final_sigs = []
        for i in range(n_inputs):
            sighash = bytes.fromhex(s["sighashes"][i])
            agg_nonce_i = s["agg_nonces"][i]
            agg_R1 = point_from_bytes(bytes.fromhex(agg_nonce_i["r1"]))
            agg_R2 = point_from_bytes(bytes.fromhex(agg_nonce_i["r2"]))
            agg_nonce_pt = (agg_R1, agg_R2)
            R, b = session_ctx(agg_nonce_pt, Q, sighash)

            partial_sigs_i = [
                int.from_bytes(bytes.fromhex(p["partial_sigs"][i]), "big")
                for p in s["participants"]
            ]

            q_even_actual = (Q.y % 2 == 0)
            # DEBUG INFO — daima log (geçici, karşılaştırma için)
            logger.info("[SIGN-DEBUG] Input %d | Q_even_y=%s(stored=%s) | R.x=%s | R_even=%s | b=%s",
                        i, q_even_actual, s.get('agg_q_even_y'),
                        R.x.to_bytes(32,'big').hex(), R.y % 2 == 0, hex(b)[2:])
            for pi, p in enumerate(s["participants"]):
                logger.info("  [SIGN-DEBUG] partial_sig[%d]=%s | pubkey=%s",
                            pi, p['partial_sigs'][i], p.get('pubkey','?')[:16])

            final_sig = partial_sig_agg(partial_sigs_i, R)
            logger.info("[SIGN-DEBUG] final_sig=%s | verify=%s",
                        final_sig.hex(), schnorr_verify(sighash, _xonly(Q), final_sig))

            if not schnorr_verify(sighash, _xonly(Q), final_sig):
                detail = (
                    f"Input {i} Schnorr doğrulaması başarısız. "
                    f"Q_even_y(actual)={q_even_actual}, "
                    f"Q_even_y(stored)={s.get('agg_q_even_y')}, "
                    f"R_even_y={R.y % 2 == 0}. "
                    f"Eski oturum ise yeni oturum açın."
                )
                raise HTTPException(400, detail)

            all_final_sigs.append(final_sig)

        raw = build_tx(utxo_objs, outputs, all_final_sigs)
        s["tx_hex"] = raw.hex()
        s["final_sig"] = all_final_sigs[0].hex()
        s["state"] = "SIGNED"

    _save_dmusig2()
    return s


@app.post("/api/musig2d/{sid}/broadcast")
def dmusig2_broadcast(sid: str):
    s = dmusig2_sessions.get(sid)
    if not s or not s.get("tx_hex"):
        raise HTTPException(400, "İmzalanmış transaction yok")
    import urllib.request as ur, urllib.error
    url = esplora_base(s.get("network", "testnet4")) + "/tx"
    try:
        req2 = ur.Request(url, data=s["tx_hex"].encode(),
                          headers={"Content-Type": "text/plain"})
        with ur.urlopen(req2, timeout=15) as r:
            txid = r.read().decode()
        s["state"] = "BROADCAST"
        s["txid"] = txid
        _save_dmusig2()
        return {"txid": txid}
    except ur.error.HTTPError as e:
        err = e.read().decode()
        raise HTTPException(400, f"Yayınlama başarısız: {err}")
    except Exception as e:
        raise HTTPException(500, f"Bağlantı hatası: {e}")


@app.post("/api/musig2d/{sid}/reset-nonces")
def dmusig2_reset_nonces(sid: str):
    """
    Nonce ve partial sig verilerini sıfırlar, state'i COLLECTING_NONCES'a döndürür.
    Aggregate Schnorr doğrulaması başarısız olduğunda yeniden deneme için kullanılır.
    Sighash'ler ve TX parametreleri korunur — build-tx yeniden çalıştırılmaz.
    """
    s = dmusig2_sessions.get(sid)
    if not s:
        raise HTTPException(404, "Oturum bulunamadı")
    if s["state"] not in ("COLLECTING_SIGS", "COLLECTING_NONCES"):
        raise HTTPException(400, f"Sıfırlama için geçersiz durum: {s['state']}")

    n_inputs = len(s["sighashes"])
    for p in s["participants"]:
        p["pubnonces"] = [None] * n_inputs
        p["partial_sigs"] = [None] * n_inputs

    s["agg_nonces"] = [None] * n_inputs
    s["state"] = "COLLECTING_NONCES"

    _save_dmusig2()
    return s


# ── Phase 6: SSE — Session Olay Akışı (Placeholder) ──────────────────────────

def _sse_participants_snapshot(s: dict) -> str:
    """Katılımcı nonce/sig durumunun karşılaştırma string'i — değişim tespiti için."""
    parts = [
        f"{p['index']}:{bool(p.get('pubnonces'))}:{bool(p.get('partial_sigs'))}"
        for p in s.get("participants", [])
    ]
    return "|".join(parts)


async def _sse_generator(sid: str):
    """
    SSE async generator — Phase 6 placeholder.

    Protokol:
      event: connected  — bağlantı kuruldu, ilk durum gönderildi
      event: update     — session state veya katılımcı verisi değişti
      : heartbeat       — 15s'de bir keepalive (yorum satırı, event değil)
      event: deleted    — oturum silindi veya bulunamadı

    Disconnect: client bağlantıyı koparınca Starlette async generator'ı
    kapatır → GeneratorExit fırlatılır → finally bloğu garantili çalışır.
    """
    HEARTBEAT_INTERVAL = 15   # saniye
    POLL_INTERVAL      = 2    # saniye — in-memory store polling

    s = dmusig2_sessions.get(sid)
    if not s:
        yield f"event: deleted\ndata: {json.dumps({'detail': 'Oturum bulunamadı'})}\n\n"
        return

    last_state        = s["state"]
    last_participants = _sse_participants_snapshot(s)
    ticks_since_hb    = 0   # heartbeat sayacı

    # İlk mesaj — client bağlandı, mevcut durum
    yield f"event: connected\ndata: {json.dumps({'session_id': sid, 'state': last_state})}\n\n"

    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)   # blocking değil — await
            ticks_since_hb += POLL_INTERVAL

            s = dmusig2_sessions.get(sid)
            if not s:
                yield f"event: deleted\ndata: {json.dumps({'session_id': sid})}\n\n"
                return

            current_state        = s["state"]
            current_participants = _sse_participants_snapshot(s)

            if current_state != last_state or current_participants != last_participants:
                last_state        = current_state
                last_participants = current_participants
                ticks_since_hb    = 0   # güncelleme heartbeat yerine geçer
                payload = json.dumps({"session_id": sid, "state": current_state})
                yield f"event: update\ndata: {payload}\n\n"

            elif ticks_since_hb >= HEARTBEAT_INTERVAL:
                # Değişim yoksa keepalive — SSE yorum satırı olarak gönderilir,
                # tarayıcı bunu event olarak işlemez, yalnızca bağlantıyı açık tutar
                yield ": heartbeat\n\n"
                ticks_since_hb = 0

    except GeneratorExit:
        # Client bağlantıyı kapattı — temiz çıkış, kaynak serbest bırakılır
        pass
    finally:
        # Gelecekte abonelik kaydı / cleanup buraya gelir
        pass


@app.get("/api/musig2d/{sid}/events")
async def dmusig2_events(sid: str):
    """
    SSE endpoint — session güncellemelerini gerçek zamanlı push eder.

    Phase 6 placeholder: polling tabanlı in-memory store izleme.
    Gelecekte: asyncio.Queue veya pub/sub ile gerçek push mimarisine geçilecek.

    Headers:
      Cache-Control: no-cache       — proxy cache'ini engelle
      X-Accel-Buffering: no         — nginx buffer'ını devre dışı bırak
      Connection: keep-alive        — bağlantının açık kalmasını zorla
    """
    return StreamingResponse(
        _sse_generator(sid),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


# ── Static Files ──────────────────────────────────────────────────────────────

frontend_dir = os.path.join(os.path.dirname(__file__), '..', 'frontend')
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))
