# Taproot Wallet

BIP-327 MuSig2 Taproot 2-of-2 multisig wallet with distributed signing support.

## Architecture

```
taproot/
├── backend/
│   ├── app.py            # FastAPI REST API
│   ├── .env.testnet      # Testnet config (Esplora API)
│   └── .env.mainnet      # Mainnet config (Bitcoin Core RPC)
├── frontend/
│   ├── index.html        # Single-page UI
│   └── app.js            # Vanilla JS frontend
├── src/
│   ├── core_connector.py # Bitcoin Core RPC client
│   ├── descriptor_wallet.py
│   ├── utxo_manager.py
│   └── taproot_signer.py
├── btc_examples/
│   ├── musig2.py         # BIP-327 MuSig2 primitives
│   └── raw_tx.py         # Taproot tx construction
└── start.sh              # Launcher
```

## Requirements

- Python 3.10+
- fastapi 0.135.1
- uvicorn 0.41.0
- pydantic 2.12.5

### Testnet (default — no Bitcoin Core required)

Uses mempool.space Esplora API for testnet4.

### Mainnet

- Bitcoin Core v26+ running on `localhost:8332`
- `bitcoin.conf`: `rpcuser`, `rpcpassword` set
- Descriptor wallet named `taproot-wallet` created:
  ```
  bitcoin-cli createwallet "taproot-wallet" false true "" false true
  ```

## Installation

```bash
python3 -m venv ~/taproot
source ~/taproot/bin/activate
pip install fastapi==0.135.1 uvicorn==0.41.0 pydantic==2.12.5
```

## Usage

```bash
./start.sh                  # testnet4 on port 8000
./start.sh testnet4         # same
./start.sh mainnet          # mainnet (Bitcoin Core required)
./start.sh mainnet 9000     # mainnet, custom port
```

Open `http://localhost:8000` in your browser.
Swagger docs: `http://localhost:8000/docs`

## API Endpoints

### Wallet

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/wallet/new` | Create new Taproot address |
| `GET` | `/api/wallet/list` | List all wallets |
| `GET` | `/api/wallet/export` | Export all public keys |
| `GET` | `/api/wallet/export-bsms/{label}` | BSMS export |
| `DELETE` | `/api/wallet/{address}` | Delete wallet |
| `GET` | `/api/wallet/{address}/balance` | Address balance |
| `GET` | `/api/wallet/{address}/utxos` | UTXOs |
| `GET` | `/api/wallet/{address}/txs` | Transaction history |

### Transactions

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/tx/build` | Build raw transaction |
| `POST` | `/api/tx/broadcast` | Broadcast signed transaction |
| `GET` | `/api/tx/{txid}` | Transaction details |

### Bitcoin Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/core/status` | Core RPC connection status |
| `POST` | `/api/core/import-wallet` | Import descriptor wallet |
| `GET` | `/api/core/fee-estimate` | Fee estimate (sat/vB) |

### MuSig2 (Single-device)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/musig2/new` | Create 2-of-2 session |
| `GET` | `/api/musig2/list` | List sessions |
| `GET` | `/api/musig2/{sid}` | Session details |
| `POST` | `/api/musig2/{sid}/nonces` | Submit nonces |
| `POST` | `/api/musig2/{sid}/sign` | Submit partial signature |
| `POST` | `/api/musig2/{sid}/broadcast` | Broadcast |
| `GET` | `/api/musig2/{sid}/utxos` | Session UTXOs |

### MuSig2 Distributed (Multi-device)

Participants sign from separate browsers. PIN-protected SK encryption via AES-GCM (WebCrypto).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/musig2d/new` | Create distributed session |
| `GET` | `/api/musig2d/list` | List sessions |
| `GET` | `/api/musig2d/actions?pubkey={hex}` | Pending actions for a pubkey |
| `GET` | `/api/musig2d/{sid}` | Session details |
| `DELETE` | `/api/musig2d/{sid}` | Delete session |
| `POST` | `/api/musig2d/{sid}/register` | Register pubkey |
| `POST` | `/api/musig2d/{sid}/build-tx` | Build TX (coordinator only) |
| `POST` | `/api/musig2d/{sid}/submit-nonce` | Submit nonce |
| `POST` | `/api/musig2d/{sid}/submit-partial-sig` | Submit partial signature |
| `POST` | `/api/musig2d/{sid}/broadcast` | Broadcast final TX |
| `GET` | `/api/musig2d/{sid}/events` | SSE event stream |

Sessions expire after **48 hours**.

## Distributed Signing Flow

```
Coordinator                          Participant
-----------                          -----------
POST /musig2d/new
  → share session ID out-of-band →
POST /musig2d/{sid}/register (idx=0)   POST /musig2d/{sid}/register (idx=1)
POST /musig2d/{sid}/build-tx
POST /musig2d/{sid}/submit-nonce       POST /musig2d/{sid}/submit-nonce
POST /musig2d/{sid}/submit-partial-sig POST /musig2d/{sid}/submit-partial-sig
POST /musig2d/{sid}/broadcast
```

The `/api/musig2d/actions` endpoint drives the dashboard UI: it returns a `state` label and per-participant `has_nonce`/`has_sig` flags, enabling diff-aware 3-second polling.

## Status & Roadmap

**Current version:** `v0.1.0` — BIP-327 compliant, testnet4 only.

Two items must be completed before mainnet deployment:

| # | Item | Risk if skipped |
|---|------|----------------|
| **BLOCKER-1** | Nonce storage: `localStorage` → in-memory (`Map`) | Nonce reuse on page reload → private key leak |
| **BLOCKER-2** | Constant-time ECC: Python integers → `libsecp256k1` binding | Timing side-channel → key recovery |

Full details: [ROADMAP.md](ROADMAP.md)

## License

MIT — see [LICENSE](LICENSE)
