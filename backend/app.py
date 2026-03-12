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

import sys, os, uuid, json, secrets as sec_mod, hashlib, struct
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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
    taproot_address, schnorr_sign,
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

app = FastAPI(title="Taproot Wallet API", version="1.0.0")

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
        print(f"[Core] Bitcoin Core v26+ bağlandı: chain={info['chain']} "
              f"blocks={info['blocks']} progress={info['verificationprogress']:.4f}")
    except RPCConnectionError as e:
        print(f"[Core] UYARI: Bağlantı kurulamadı, Esplora'ya geçildi.\n  {e}")
        _core_rpc = None
    except RPCError as e:
        print(f"[Core] UYARI: RPC hatası, Esplora'ya geçildi.\n  {e}")
        _core_rpc = None


def get_utxo_manager(network: str = "testnet") -> UTXOManager:
    """
    Ağ ve Core durumuna göre uygun UTXOManager döner.

    Core aktifse: listunspent → scantxoutset → Esplora fallback
    Core pasifse: yalnızca Esplora
    """
    return UTXOManager(network=network, rpc=_core_rpc)


# ── In-Memory State ────────────────────────────────────────────────────────────

wallets: List[Dict] = []          # {id, label, sk_hex, address, network}
musig2_sessions: Dict[str, Dict] = {}  # session_id → session state


# ── Pydantic Models ───────────────────────────────────────────────────────────

class WalletCreate(BaseModel):
    label: str
    network: str = "testnet"  # testnet | mainnet

class TxRequest(BaseModel):
    from_address: str
    to_address: str
    amount_sat: int
    fee_sat: int = 500

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def find_wallet(address: str) -> Optional[Dict]:
    return next((w for w in wallets if w["address"] == address), None)

def esplora_base(network: str) -> str:
    return "https://mempool.space/testnet/api" if network == "testnet" else "https://mempool.space/api"

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
    sk = sec_mod.token_bytes(32)
    testnet = req.network in ("testnet", "testnet4")
    xonly_pk, address = taproot_address(sk, testnet=testnet)

    wallet = {
        "id": str(uuid.uuid4())[:8],
        "label": req.label,
        "sk_hex": sk.hex(),
        "xonly_pk": xonly_pk.hex(),
        "address": address,
        "network": req.network,
    }
    wallets.append(wallet)

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


@app.delete("/api/wallet/{address}")
def delete_wallet(address: str):
    global wallets
    before = len(wallets)
    wallets = [w for w in wallets if w["address"] != address]
    if len(wallets) == before:
        raise HTTPException(404, "Cüzdan bulunamadı")
    return {"ok": True}


@app.get("/api/wallet/{address}/balance")
def get_balance(address: str):
    w = find_wallet(address)
    if not w:
        raise HTTPException(404, "Cüzdan bulunamadı")

    # Core aktifse UTXOManager, değilse Esplora
    mgr = get_utxo_manager(w["network"])
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
    w = find_wallet(address)
    network = w["network"] if w else "testnet"
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
    w = find_wallet(req.from_address)
    if not w:
        raise HTTPException(404, "Kaynak cüzdan bulunamadı")

    sk = bytes.fromhex(w["sk_hex"])
    xonly_pk = bytes.fromhex(w["xonly_pk"])
    my_spk = build_p2tr_scriptpubkey(w["xonly_pk"])  # 0x51 0x20 + xonly

    # ── UTXO Toplama (Core RPC veya Esplora) ─────────────────────────────────
    mgr = get_utxo_manager(w["network"])
    try:
        all_utxos = mgr.fetch_utxos(req.from_address)
    except Exception as e:
        raise HTTPException(502, f"UTXO sorgusu başarısız: {e}")

    confirmed_utxos = [u for u in all_utxos if u.confirmations >= 1]
    if not confirmed_utxos:
        raise HTTPException(400, "Onaylanmış P2TR UTXO bulunamadı")

    # ── scriptPubKey Doğrulama ────────────────────────────────────────────────
    # Core UTXO'ları için scriptpubkey dolu, Esplora için inşa et
    for cu in confirmed_utxos:
        if not cu.scriptpubkey or len(cu.scriptpubkey) != 34:
            cu.scriptpubkey = my_spk
            cu.is_p2tr = True

    # ── Alıcı scriptPubKey ────────────────────────────────────────────────────
    if not req.to_address.startswith(("tb1p", "bc1p", "bcrt1p")):
        raise HTTPException(400, "Geçersiz P2TR adresi (tb1p... / bc1p... olmalı)")

    import urllib.request as ur
    base = esplora_base(w["network"])
    try:
        with ur.urlopen(f"{base}/address/{req.to_address}", timeout=8) as r:
            addr_info = json.loads(r.read())
        recipient_spk = bytes.fromhex(addr_info["scriptpubkey"])
    except Exception:
        raise HTTPException(400, "Alıcı adres scriptPubKey'i alınamadı")

    # P2TR çıktı kontrolü
    is_p2tr, _ = parse_p2tr_scriptpubkey(recipient_spk.hex())
    if not is_p2tr:
        raise HTTPException(400, "Alıcı adresi P2TR (Taproot) değil")

    # ── Coin Selection (largest-first) ───────────────────────────────────────
    try:
        selected, change_sat = CoinSelector.largest_first(
            confirmed_utxos, req.amount_sat, req.fee_sat
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # ── TX Çıktıları ──────────────────────────────────────────────────────────
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > CoinSelector.DUST_LIMIT_SAT:
        outputs.append(TxOutput(change_sat, my_spk))
    else:
        change_sat = 0  # dust: ücrette erit

    # ── İmzalama (SIGHASH_DEFAULT = 0x00) ────────────────────────────────────
    signer = TaprootSigner(sighash_type=SighashType.DEFAULT)
    try:
        raw, witnesses = signer.sign_transaction(sk, selected, outputs)
    except ValueError as e:
        raise HTTPException(400, f"İmzalama hatası: {e}")

    tx_hex = raw.hex()
    summary = TaprootSigner.decode_tx_summary(raw)

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
    }


@app.post("/api/tx/broadcast")
def broadcast(req: BroadcastRequest):
    result = broadcast_tx(req.tx_hex)
    if result:
        return {"txid": result}
    raise HTTPException(400, "Yayınlama başarısız")


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
    testnet = req.network == "testnet"
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

    # Aggregate nonces
    pub_nonces = [(bytes.fromhex(p["pub_nonce"][0]), bytes.fromhex(p["pub_nonce"][1]))
                  for p in s["participants"]]
    agg_R1, agg_R2 = nonce_agg(pub_nonces)
    s["agg_nonce"] = [_xonly(agg_R1).hex(), _xonly(agg_R2).hex()]

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

    confirmed.sort(key=lambda u: u["value"], reverse=True)
    u = confirmed[0]
    inp = UTXO(txid=u["txid"], vout=u["vout"], value_sat=u["value"], scriptpubkey=agg_spk)

    import urllib.request, json
    base = esplora_base(s["network"])
    try:
        with urllib.request.urlopen(f"{base}/address/{req.to_address}", timeout=8) as r:
            addr_info = json.loads(r.read())
        recipient_spk = bytes.fromhex(addr_info["scriptpubkey"])
    except Exception:
        raise HTTPException(400, "Alıcı adresi doğrulanamadı")

    change_sat = u["value"] - req.amount_sat - req.fee_sat
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > 546:
        outputs.append(TxOutput(change_sat, agg_spk))

    sighash = taproot_sighash([inp], outputs, 0)

    # Compute partial sigs for all participants
    pk_list_bytes = sorted([bytes.fromhex(pk) for pk in s["pk_list"]])
    Q, _ = key_aggregation(pk_list_bytes)
    pub_nonces = [(bytes.fromhex(p["pub_nonce"][0]), bytes.fromhex(p["pub_nonce"][1]))
                  for p in s["participants"]]
    agg_R1, agg_R2 = nonce_agg(pub_nonces)
    agg_nonce_pt = (agg_R1, agg_R2)
    R, b = session_ctx(agg_nonce_pt, Q, sighash)

    partial_sigs = []
    for p in s["participants"]:
        sk = bytes.fromhex(p["sk_hex"])
        pk = bytes.fromhex(p["pk_hex"])
        coeff = key_agg_coeff(pk_list_bytes, pk)
        k_pair = (p["nonce_secret"][0], p["nonce_secret"][1])
        si = partial_sign(k_pair, sk, coeff, Q, agg_nonce_pt, sighash)
        p["partial_sig"] = si
        partial_sigs.append(si)

    final_sig = partial_sig_agg(partial_sigs, R)
    valid = schnorr_verify(sighash, _xonly(Q), final_sig)

    raw = build_tx([inp], outputs, [final_sig])
    s["tx_hex"] = raw.hex()
    s["final_sig"] = final_sig.hex()
    s["state"] = "SIGNED"
    s["_outputs"] = outputs
    s["_inp"] = u

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
    result = broadcast_tx(s["tx_hex"])
    if result:
        s["state"] = "BROADCAST"
        return {"txid": result}
    raise HTTPException(400, "Yayınlama başarısız")


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


# ── Static Files ──────────────────────────────────────────────────────────────

frontend_dir = os.path.join(os.path.dirname(__file__), '..', 'frontend')
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))
