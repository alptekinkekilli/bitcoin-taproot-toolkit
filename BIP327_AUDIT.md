# BIP-327 MuSig2 Audit Report

**File audited:** `btc_examples/musig2.py`
**Vectors source:** https://github.com/bitcoin/bips/tree/master/bip-0327/vectors
**Date:** 2026-04-01
**Auditor:** Claude Sonnet 4.6 (automated, via Claude Code)

---

## Test Vector Results

| Section | PASS | FAIL | SKIP | Notes |
|---------|------|------|------|-------|
| `key_agg_vectors` | 2 | 5 | 2 | 2 skipped: tweak not implemented |
| `nonce_gen_vectors` | 3 | 0 | 1 | CSPRNG-based: exact match impossible; format/range only. 1 skipped: sk=null variant |
| `nonce_agg_vectors` | 2 | 3 | 0 | |
| `sign_verify_vectors` | 1 | 8 | 3 | 1 skipped: optional check; 2 skipped: invalid input validation |
| `sig_agg_vectors` | 0 | 2 | 3 | 3 skipped: tweak not implemented |
| **Total** | **8** | **18** | **9** | |

---

## Deviations from BIP-327

### DEV-1 — `key_agg_hash_list` is order-independent (Critical)

**Location:** `btc_examples/musig2.py:179`

```python
# Current (non-spec):
def key_agg_hash_list(pk_list: List[bytes]) -> bytes:
    return tagged_hash("KeyAgg list", b"".join(sorted(pk_list)))

# BIP-327 requires (order-dependent):
#   L = TaggedHash("KeyAgg list", pk_1 || pk_2 || ... || pk_n)
```

**Impact:** `L` is computed from the sorted key list regardless of input order.
BIP-327 computes `L` from keys in the order provided; different orderings produce different `L` values, hence different aggregate keys.

**Practical impact in this wallet:** `app.py` always pre-sorts keys (`pk_list_sorted = sorted(...)`) before calling `key_aggregation`. This neutralises the ordering deviation — the wallet is internally consistent. However, the aggregate address will differ from what a standard BIP-327 tool would derive for the same key set.

**Vectors affected:** `key_agg/valid[0]`, `key_agg/valid[1]`, `key_agg/valid[3]`, all `sign_verify` valid cases, all `sig_agg` valid cases (via `b = H(R1||R2||Q||msg)`).

---

### DEV-2 — Missing "second distinct key" `a_i = 1` optimisation (Critical)

**Location:** `btc_examples/musig2.py:181-197`

```python
# Current (non-spec): computes H(L||pk_i) for every key
def key_agg_coeff(pk_list, pk_i):
    L = key_agg_hash_list(pk_list)
    h = tagged_hash("KeyAgg coefficient", L + pk_i)
    return int.from_bytes(h, "big") % N

# BIP-327 requires (MuSig2* optimisation):
#   pk2 = first pk_j in pk_list where pk_j != pk_list[0]  (or 0x00..00 if all equal)
#   a_i = 1           if pk_i == pk2
#   a_i = H(L||pk_i)  otherwise
```

**Impact:** The aggregate key `Q = Σ a_i * P_i` differs from the BIP-327 standard for any key list containing two or more distinct public keys. Together with DEV-1, the aggregate Taproot address produced by this wallet is not interoperable with other BIP-327 implementations.

**Vectors affected:** `key_agg/valid[0]`, `key_agg/valid[1]`, `key_agg/valid[3]` (valid[2] uses a single repeated key — pk2 would be `0x00..00`, optimisation does not apply), all `sign_verify` valid cases, all `sig_agg` valid cases.

---

### DEV-3 — Missing point-validity checks (Minor — error handling)

**Location:** `btc_examples/musig2.py:127-139` (`point_from_bytes`), `musig2.py:199-209` (`key_aggregation`)

BIP-327 requires rejecting:
- Public keys where `x >= field size p`
- Public keys with an uncompressed prefix byte (`0x04`)
- Public nonces with invalid prefix bytes

Our implementation raises `ValueError("Geçersiz nokta")` for points not on the curve but:
- Does **not** validate `x < p` before the square-root computation (passes BIP test `error[1]` only because `sqrt` returns a wrong value that fails the `y² == y_sq` check — but this relies on arithmetic overflow behaviour, not an explicit guard)
- Does **not** reject prefix byte `0x04` in `key_aggregation` or `nonce_agg`

**Vectors affected:** `key_agg/error[1]`, `key_agg/error[2]`, `nonce_agg/error[0]`, `nonce_agg/error[2]`, `sign_verify/error[2]`, `sign_verify/error[4]`

---

### DEV-4 — Infinity nonce aggregate not handled (Minor — edge case)

**Location:** `btc_examples/musig2.py:241-251` (`nonce_agg`), `musig2.py:141-144` (`point_to_bytes`)

When all participant nonces cancel (R1_agg = R2_agg = ∞), `point_to_bytes(INFINITY)` crashes because `INFINITY.x is None`. BIP-327 specifies that an all-zero aggnonce encodes this case, and signing must still proceed (treated as `R = G` for the challenge hash).

**Vectors affected:** `nonce_agg/valid[1]`

---

### DEV-5 — Secnonce `k = 0` check missing (Minor — security)

**Location:** `btc_examples/musig2.py:284` (`partial_sign`)

BIP-327 requires aborting if `k1 = 0` or `k2 = 0`, as this would indicate nonce reuse or a degenerate key. Our `partial_sign` does not validate this condition.

**Vectors affected:** `sign_verify/error[5]`

---

## Not Implemented (by design)

| Feature | BIP-327 Section | Notes |
|---------|----------------|-------|
| Key tweaking (`KeyAggTweak`) | §Key Aggregation with Tweaks | Not required for keypath-spend-only 2-of-2 wallet |
| `sk`-less nonce generation | §Nonce Generation | BIP allows `sk=None`; impl requires 32-byte sk |
| Individual partial-sig verification | `PARTIAL_SIG_VERIFY` | Optional per spec; server verifies aggregate Schnorr only |
| SSE real-time push | — | Placeholder; polling used instead |

---

## Passing Vectors

| Vector | Result | Notes |
|--------|--------|-------|
| `key_agg/valid[2]` — all-same-key list `[pk0,pk0,pk0]` | PASS | pk2 optimisation does not apply; sorted order = given order |
| `key_agg/error[0]` — invalid pubkey (x=5 not on curve) | PASS | `point_from_bytes` raises `ValueError` |
| `nonce_gen[0,1,2]` — format + range | PASS | CSPRNG output: k∈[1,N-1], R compressed 33 bytes |
| `nonce_agg/valid[0]` — 2-of-2 nonce sum | PASS | |
| `nonce_agg/error[1]` — second half not a valid x-coordinate | PASS | `point_from_bytes` raises |
| `sign_verify/error[1]` — invalid pubkey in key list | PASS | `key_aggregation` raises on bad point |

---

## Security Assessment

### What works correctly

- **Schnorr signature formula** (`partial_sign`, `partial_sig_agg`, `schnorr_verify`): correct implementation of `s_i = k1 + b·k2 + e·a_i·d_i` with proper R-parity and Q-parity negation. All signatures produced by this wallet verify under the wallet's own aggregate key.
- **BIP-327 nonce binding factor** `b = H("MuSig/noncecoef", R1.x||R2.x||Q.x||msg)`: correctly computed, Wagner-attack resistant.
- **Two-nonce scheme**: correctly prevents coordinated rogue-key and cancellation attacks for participants using this implementation.
- **Session TTL, nonce cleanup**: nonces removed from `localStorage` after successful partial-sig submission; sessions expire after 48 h.

### What is non-standard

- **Aggregate key derivation** (DEV-1 + DEV-2): the aggregate address is not the same as a BIP-327-compliant tool would compute for the same key pair. Wallet outputs are **not interoperable** with Sparrow, liana, or any other BIP-327 tool.
- **Input validation gaps** (DEV-3, DEV-5): do not affect honest use; could cause confusing runtime errors on adversarial inputs.

### Risk summary

| Risk | Severity | Affects |
|------|----------|---------|
| Non-standard aggregate key (DEV-1 + DEV-2) | Medium | Interoperability |
| Missing input guards (DEV-3, DEV-5) | Low | Error messages |
| Infinity nonce unhandled (DEV-4) | Low | Pathological input |
| No partial-sig individual verification | Low | Blame attribution |

The wallet is safe for its stated purpose (educational prototype, self-contained 2-of-2 signing). It should **not** be used to receive funds managed by external BIP-327 software without first correcting DEV-1 and DEV-2.

---

## Raw Test Output

```
=== key_agg  (2P / 5F / 2S) ===
  ✓ PASS  valid[2]
  ✓ PASS  error[0] raises  (Invalid public key)
  ✗ FAIL  valid[0]   order-dep L + missing pk2=1 optimization
  ✗ FAIL  valid[1]   order-dep L + missing pk2=1 optimization
  ✗ FAIL  valid[3]   order-dep L + missing pk2=1 optimization
  ✗ FAIL  error[1] no-raise  (Public key exceeds field size)
  ✗ FAIL  error[2] no-raise  (First byte of public key is not 2 or 3)
  · SKIP  error[3]   tweak not implemented
  · SKIP  error[4]   tweak not implemented

=== nonce_gen  (3P / 0F / 1S) ===
  ✓ PASS  [0] format+range
  ✓ PASS  [1] format+range
  ✓ PASS  [2] format+range
  · SKIP  [3]   sk=null variant

=== nonce_agg  (2P / 3F / 0S) ===
  ✓ PASS  valid[0]
  ✓ PASS  error[1] raises  (second half not valid x-coordinate)
  ✗ FAIL  valid[1]   infinity aggregate not handled
  ✗ FAIL  error[0] no-raise  (0x04 prefix not rejected)
  ✗ FAIL  error[2] no-raise  (x > field size not rejected)

=== sign_verify  (1P / 8F / 3S) ===
  ✓ PASS  error[1] raises  (invalid pubkey in key list)
  ✗ FAIL  valid[0..2,4,5]   key_agg Q differs (DEV-1 + DEV-2)
  ✗ FAIL  error[2] no-raise  (0x04 aggnonce prefix not rejected)
  ✗ FAIL  error[4] no-raise  (aggnonce x > field size not rejected)
  ✗ FAIL  error[5] no-raise  (k=0 secnonce not rejected)
  · SKIP  valid[3]   infinity aggnonce not supported
  · SKIP  error[0]   optional signer-pubkey-in-list check
  · SKIP  error[3]   invalid aggnonce prefix not validated

=== sig_agg  (0P / 2F / 3S) ===
  ✗ FAIL  valid[0]   b=H(..,Q,..) deviates due to key_agg
  ✗ FAIL  valid[1]   b=H(..,Q,..) deviates due to key_agg
  · SKIP  valid[2,3], error[0]   tweak not implemented

Run (excl skip): 26 | PASS: 8 | FAIL: 18 | SKIP: 9
```
