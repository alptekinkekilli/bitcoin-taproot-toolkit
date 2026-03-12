"""
app.py — Taproot Wallet Backend (FastAPI)
=========================================
Bitcoin Taproot toolkit için REST API + statik dosya sunucusu.
Testnet üzerinde çalışır. Özel anahtarlar yalnızca bellekte tutulur.
"""

import sys, os, uuid, json, secrets as sec_mod, hashlib, struct
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

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

app = FastAPI(title="Taproot Wallet API", version="1.0.0")

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
    sk = sec_mod.token_bytes(32)
    testnet = req.network == "testnet"
    xonly_pk, address = taproot_address(sk, testnet=testnet)
    d = int.from_bytes(sk, "big")
    P = _point_mul(d, G)
    if P.y % 2 != 0:
        d = N - d
        P = _point_mul(d, G)
    wallet = {
        "id": str(uuid.uuid4())[:8],
        "label": req.label,
        "sk_hex": sk.hex(),
        "xonly_pk": xonly_pk.hex(),
        "address": address,
        "network": req.network,
    }
    wallets.append(wallet)
    return wallet_public(wallet)


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
    utxos = get_utxos(address)
    confirmed = sum(u["value"] for u in utxos if u.get("status", {}).get("confirmed"))
    unconfirmed = sum(u["value"] for u in utxos if not u.get("status", {}).get("confirmed"))
    return {
        "confirmed_sat": confirmed,
        "unconfirmed_sat": unconfirmed,
        "total_sat": confirmed + unconfirmed,
        "utxo_count": len(utxos),
    }


@app.get("/api/wallet/{address}/utxos")
def get_wallet_utxos(address: str):
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
    w = find_wallet(req.from_address)
    if not w:
        raise HTTPException(404, "Kaynak cüzdan bulunamadı")

    sk = bytes.fromhex(w["sk_hex"])
    xonly_pk = bytes.fromhex(w["xonly_pk"])
    my_spk = bytes([0x51, 0x20]) + xonly_pk

    utxos_raw = get_utxos(req.from_address)
    confirmed = [u for u in utxos_raw if u.get("status", {}).get("confirmed")]
    if not confirmed:
        raise HTTPException(400, "Onaylanmış UTXO bulunamadı")

    # En büyük UTXO'yu seç
    confirmed.sort(key=lambda u: u["value"], reverse=True)
    u = confirmed[0]

    if u["value"] < req.amount_sat + req.fee_sat:
        raise HTTPException(400, f"Yetersiz bakiye: {u['value']} sat")

    inp = UTXO(txid=u["txid"], vout=u["vout"], value_sat=u["value"], scriptpubkey=my_spk)

    # Alıcı scriptpubkey (basit P2TR varsayımı — tb1p veya bc1p adres)
    if len(req.to_address) == 62 and req.to_address.startswith(("tb1p", "bc1p")):
        # bech32m decode edip xonly al (basit: son 32 byte)
        # Tam decode yerine: Esplora'dan script çek
        import urllib.request as ur
        base = esplora_base(w["network"])
        try:
            with ur.urlopen(f"{base}/address/{req.to_address}", timeout=8) as r:
                addr_info = json.loads(r.read())
            recipient_spk = bytes.fromhex(addr_info["scriptpubkey"])
        except Exception:
            raise HTTPException(400, "Alıcı adresi doğrulanamadı")
    else:
        raise HTTPException(400, "Geçersiz Taproot adresi (tb1p... veya bc1p... olmalı)")

    change_sat = u["value"] - req.amount_sat - req.fee_sat
    outputs = [TxOutput(req.amount_sat, recipient_spk)]
    if change_sat > 546:
        outputs.append(TxOutput(change_sat, my_spk))

    sighash = taproot_sighash([inp], outputs, 0)
    sig = schnorr_sign(sighash, sk)

    raw = build_tx([inp], outputs, [sig])
    tx_hex = raw.hex()

    txid_data = hashlib.sha256(hashlib.sha256(
        # legacy part için sadece non-witness serialize
        struct.pack("<I", 2) +
        bytes([1]) + bytes.fromhex(u["txid"])[::-1] + struct.pack("<I", u["vout"]) +
        b"\x00" + struct.pack("<I", 0xFFFFFFFD) +
        bytes([len(outputs)]) +
        b"".join(struct.pack("<q", o.value_sat) + bytes([len(o.scriptpubkey)]) + o.scriptpubkey for o in outputs) +
        struct.pack("<I", 0)
    ).digest()).hexdigest()

    return {
        "tx_hex": tx_hex,
        "tx_size": len(raw),
        "fee_sat": req.fee_sat,
        "change_sat": change_sat if change_sat > 546 else 0,
        "sighash": sighash.hex(),
        "signature": sig.hex(),
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
