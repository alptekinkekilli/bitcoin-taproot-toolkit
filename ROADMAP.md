# Roadmap

## Current Release — v0.1.0

> BIP-327 MuSig2 compliant 2-of-2 Taproot wallet, testnet4 only.

**What's included:**

- Full BIP-327 MuSig2 implementation (`btc_examples/musig2.py`)
  - Order-dependent `L` computation (DEV-1 fixed)
  - MuSig2\* second-key optimisation `a_i = 1` (DEV-2 fixed)
  - `point_from_bytes` input validation: invalid prefix, `x ≥ p` (DEV-3 fixed)
  - Infinity nonce aggregate handling: `cbytes_ext` in b-input, `R → G` (DEV-4 fixed)
  - `k = 0` secnonce guard in `partial_sign` and `nonce_gen` (DEV-5 fixed)
  - 33-byte compressed encoding for R1, R2 in `b` factor (DEV-6 fixed)
  - **28 / 28 BIP-327 test vectors passing** (7 skipped: tweaks, optional checks)
- Distributed signing over HTTP — coordinator + participant flow
- PIN-protected private key encryption (AES-GCM, WebCrypto)
- Action queue dashboard with 3-second polling
- Testnet4 via mempool.space Esplora API
- Mainnet path via Bitcoin Core RPC (infrastructure ready, gated)

**Audit:** [`BIP327_AUDIT.md`](BIP327_AUDIT.md)

---

## v0.2.0 — Mainnet Prerequisites

> These two items are **blocking**. Mainnet deployment must not proceed without both.

### BLOCKER-1 — Nonce Storage: `localStorage` → In-Memory

**Current behaviour:**
Secnonces (k1, k2) are persisted in `localStorage`. A browser crash or tab
restore can silently reload a used nonce, leading to nonce reuse — which leaks
the private key.

**Required change:**

```
localStorage.setItem("nonce_…", …)   →   nonceMap.set(key, value)   // Map in RAM
```

- On page reload: warn the user that the nonce is gone and a new signing
  session must be started.
- Secnonce must never survive a page reload.
- Affected file: `frontend/app.js` (all `localStorage` nonce read/write paths)

**Risk if skipped:** Nonce reuse under any page-reload scenario → full private
key exposure.

---

### BLOCKER-2 — Constant-Time Scalar Arithmetic: Python → `libsecp256k1`

**Current behaviour:**
`btc_examples/musig2.py` uses Python's arbitrary-precision integers for all
elliptic curve operations. Python integer arithmetic is **not constant-time**:
execution time varies with operand values, creating a timing side-channel that
can leak secret scalars (private key, nonce).

**Required change:**

Replace the Python ECC core with a binding that wraps `libsecp256k1`, which
provides hardware-level constant-time guarantees:

| Option | Notes |
|--------|-------|
| `python-secp256k1` (cffi) | Drop-in, pure Python API, links system libsecp256k1 |
| Rust wrapper via `PyO3` | Higher performance, stronger isolation |

Minimum scope:
- `point_mul` → `secp256k1_ecmult`
- `partial_sign` scalar path → constant-time mod-N arithmetic
- Key generation / `nonce_gen` → `secp256k1_nonce_function_bip340`

**Risk if skipped:** Timing oracle on signing operations → private key or nonce
recovery via statistical analysis (e.g., Minerva-class attacks).

---

## Backlog (Not Blocking)

These items improve spec coverage and interoperability but are **not required**
for a safe mainnet deployment within this wallet's own key set.

| Item | BIP-327 Section | Notes |
|------|----------------|-------|
| Key tweaking (`KeyAggContext` with tweaks) | §Key Aggregation with Tweaks | Required for Taproot script-path spend |
| `PARTIAL_SIG_VERIFY` | §Partial Signature Verification | Blame attribution for faulty participants |
| `sk = None` nonce generation | §Nonce Generation | Allows hardware-wallet integration |
| SSE real-time push | — | Replace 3-second polling with server-sent events |

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v0.1.0 | 2026-04-01 | BIP-327 full compliance (28/28 vectors), distributed signing, audit report |
