/**
 * musig2d.js — Tarayıcı-Taraflı BIP-327 MuSig2 Dağıtık Kripto
 * =============================================================
 * Özel anahtarlar asla sunucuya gönderilmez.
 *
 * SHA-256: Önce WebCrypto (HTTPS/localhost) dener; mevcut değilse
 * (HTTP üzerinden IP erişimi gibi güvensiz bağlamlar) saf-JS fallback kullanır.
 * Bu sayede Katılımcı 2'nin farklı makineden HTTP erişiminde de çalışır.
 */

'use strict';

const MuSig2D = (() => {

  // ── secp256k1 Eğri Sabitleri ───────────────────────────────────────────────

  const P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2Fn;
  const N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141n;
  const GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798n;
  const GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8n;
  const G  = { x: GX, y: GY };

  // ── Modüler Aritmetik ──────────────────────────────────────────────────────

  function modp(x) { return ((x % P) + P) % P; }
  function modn(x) { return ((x % N) + N) % N; }

  function modPow(base, exp, mod) {
    base = ((base % mod) + mod) % mod;
    let result = 1n;
    while (exp > 0n) {
      if (exp & 1n) result = result * base % mod;
      base = base * base % mod;
      exp >>= 1n;
    }
    return result;
  }

  function modInvP(x) { return modPow(x, P - 2n, P); }

  // ── secp256k1 Nokta İşlemleri ──────────────────────────────────────────────

  function pointAdd(P1, P2) {
    if (P1 === null) return P2;
    if (P2 === null) return P1;
    if (P1.x === P2.x) {
      if (P1.y !== P2.y) return null;
      const lam = modp(3n * P1.x * P1.x * modInvP(2n * P1.y));
      const x3  = modp(lam * lam - 2n * P1.x);
      return { x: x3, y: modp(lam * (P1.x - x3) - P1.y) };
    }
    const lam = modp((P2.y - P1.y) * modInvP(P2.x - P1.x));
    const x3  = modp(lam * lam - P1.x - P2.x);
    return { x: x3, y: modp(lam * (P1.x - x3) - P1.y) };
  }

  function pointMul(k, pt) {
    k = modn(k);
    let result = null;
    let addend = pt;
    while (k > 0n) {
      if (k & 1n) result = pointAdd(result, addend);
      addend = pointAdd(addend, addend);
      k >>= 1n;
    }
    return result;
  }

  function hasEvenY(pt) { return pt !== null && pt.y % 2n === 0n; }

  // ── Kodlama / Çözme ───────────────────────────────────────────────────────

  function hexToBytes(hex) {
    if (typeof hex !== 'string') throw new Error(`hexToBytes: string beklendi, ${typeof hex} geldi`);
    if (hex.length % 2 !== 0) throw new Error(`hexToBytes: tek sayıda hex karakter (${hex.length})`);
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++)
      out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    return out;
  }

  function bytesToHex(bytes) {
    return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function bytesToBigint(bytes) {
    let n = 0n;
    for (const b of bytes) n = (n << 8n) | BigInt(b);
    return n;
  }

  function bigintToBytes(n, length = 32) {
    const hex = n.toString(16).padStart(length * 2, '0');
    return hexToBytes(hex);
  }

  function concatBytes(...arrays) {
    for (let i = 0; i < arrays.length; i++) {
      if (!(arrays[i] instanceof Uint8Array))
        throw new Error(`concatBytes: argüman ${i} Uint8Array değil (${typeof arrays[i]})`);
    }
    const total = arrays.reduce((s, a) => s + a.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const a of arrays) { out.set(a, offset); offset += a.length; }
    return out;
  }

  function pointToBytes(pt) {
    if (pt === null) throw new Error('pointToBytes: infinity noktası');
    const buf = new Uint8Array(33);
    buf[0] = hasEvenY(pt) ? 0x02 : 0x03;
    buf.set(bigintToBytes(pt.x, 32), 1);
    return buf;
  }

  function xonlyBytes(pt) {
    if (pt === null) throw new Error('xonlyBytes: infinity noktası');
    return bigintToBytes(pt.x, 32);
  }

  function pointFromBytes(bytes) {
    if (!(bytes instanceof Uint8Array) || bytes.length !== 33)
      throw new Error('pointFromBytes: 33 byte Uint8Array gerekli');
    const prefix = bytes[0];
    if (prefix !== 0x02 && prefix !== 0x03) throw new Error('pointFromBytes: geçersiz prefix');
    const x = bytesToBigint(bytes.slice(1));
    const y_sq = modp(modPow(x, 3n, P) + 7n);
    let y = modPow(y_sq, (P + 1n) / 4n, P);
    if (modPow(y, 2n, P) !== y_sq) throw new Error('Nokta eğri üzerinde değil');
    if (y % 2n !== BigInt(prefix - 2)) y = P - y;
    return { x, y };
  }

  // ── Saf-JS SHA-256 (HTTP/güvensiz bağlam için fallback) ───────────────────
  //
  // WebCrypto (crypto.subtle) yalnızca HTTPS veya localhost'ta çalışır.
  // Katılımcı 2 ağ IP'si üzerinden HTTP ile bağlandığında crypto.subtle=undefined
  // olur ve "Cannot read properties of undefined (reading 'digest')" hatası alınır.
  // Bu implementasyon her ortamda çalışır.

  const _SHA256_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  function _sha256Pure(data) {
    const r32 = (x, n) => (x >>> n) | (x << (32 - n));
    const H = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);

    const len = data.length;
    const paddedLen = Math.ceil((len + 9) / 64) * 64;
    const padded = new Uint8Array(paddedLen);
    padded.set(data);
    padded[len] = 0x80;
    const dv = new DataView(padded.buffer);
    // 64-bit big-endian uzunluk (bit cinsinden); pratik girdi <2^29 bit
    dv.setUint32(paddedLen - 4, (len * 8) >>> 0, false);
    dv.setUint32(paddedLen - 8, Math.floor(len / 0x20000000) >>> 0, false);

    const W = new Uint32Array(64);
    for (let blk = 0; blk < paddedLen; blk += 64) {
      const bv = new DataView(padded.buffer, blk, 64);
      for (let j = 0; j < 16; j++) W[j] = bv.getUint32(j * 4, false);
      for (let j = 16; j < 64; j++) {
        const s0 = r32(W[j-15], 7) ^ r32(W[j-15], 18) ^ (W[j-15] >>> 3);
        const s1 = r32(W[j-2], 17) ^ r32(W[j-2], 19) ^ (W[j-2] >>> 10);
        W[j] = (W[j-16] + s0 + W[j-7] + s1) | 0;
      }
      let [a, b, c, d, e, f, g, h] = H;
      for (let j = 0; j < 64; j++) {
        const S1  = r32(e, 6) ^ r32(e, 11) ^ r32(e, 25);
        const ch  = (e & f) ^ (~e & g);
        const t1  = (h + S1 + ch + _SHA256_K[j] + W[j]) | 0;
        const S0  = r32(a, 2) ^ r32(a, 13) ^ r32(a, 22);
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2  = (S0 + maj) | 0;
        h=g; g=f; f=e; e=(d+t1)|0; d=c; c=b; b=a; a=(t1+t2)|0;
      }
      H[0]=(H[0]+a)|0; H[1]=(H[1]+b)|0; H[2]=(H[2]+c)|0; H[3]=(H[3]+d)|0;
      H[4]=(H[4]+e)|0; H[5]=(H[5]+f)|0; H[6]=(H[6]+g)|0; H[7]=(H[7]+h)|0;
    }
    const result = new Uint8Array(32);
    const rv = new DataView(result.buffer);
    for (let i = 0; i < 8; i++) rv.setUint32(i * 4, H[i], false);
    return result;
  }

  // WebCrypto mevcut mu? (HTTPS veya localhost)
  const _useWebCrypto = typeof crypto !== 'undefined' &&
                        crypto.subtle != null &&
                        typeof crypto.subtle.digest === 'function';

  async function sha256(data) {
    if (!(data instanceof Uint8Array))
      throw new Error(`sha256: Uint8Array beklendi, ${typeof data} geldi`);
    if (_useWebCrypto) {
      const buf = await crypto.subtle.digest('SHA-256', data);
      return new Uint8Array(buf);
    }
    return _sha256Pure(data);   // HTTP/IP bağlamı için fallback
  }

  async function taggedHash(tag, data) {
    const tagBytes = new TextEncoder().encode(tag);
    const tagHash  = await sha256(tagBytes);
    return sha256(concatBytes(tagHash, tagHash, data));
  }

  // ── BIP-327 Anahtar Agregasyon Katsayısı ──────────────────────────────────

  async function keyAggCoeff(pkListSortedHex, pkHex) {
    if (!Array.isArray(pkListSortedHex) || pkListSortedHex.length === 0)
      throw new Error('keyAggCoeff: pkListSortedHex boş veya dizi değil');
    if (typeof pkHex !== 'string' || pkHex.length !== 66)
      throw new Error('keyAggCoeff: pkHex 33-byte compressed hex (66 karakter) gerekli');

    // BIP-327: L = H_KeyAgg_list(pk_list) — caller-provided order, no re-sort
    const pkListBytes = pkListSortedHex.map(hexToBytes);
    const pkIBytes    = hexToBytes(pkHex);

    // MuSig2* optimisation (BIP-327 §Key Aggregation):
    // The second distinct key in the list gets a_i = 1.
    // Same as Python backend get_second_key() + key_agg_coeff() — DEV-2 fix.
    const firstKey = pkListBytes[0];
    let secondKey  = null;
    for (let i = 1; i < pkListBytes.length; i++) {
      if (!pkListBytes[i].every((b, j) => b === firstKey[j])) {
        secondKey = pkListBytes[i];
        break;
      }
    }
    if (secondKey !== null && pkIBytes.every((b, j) => b === secondKey[j])) {
      return 1n;   // MuSig2* optimisation: a_i = 1 for second distinct key
    }

    const L = await taggedHash('KeyAgg list', concatBytes(...pkListBytes));
    const h = await taggedHash('KeyAgg coefficient', concatBytes(L, pkIBytes));
    return bytesToBigint(h) % N;
  }

  // ── BIP-327 Nonce Üretimi ──────────────────────────────────────────────────

  async function nonceGen(skHex, pkHex, msgHex) {
    if (typeof skHex !== 'string' || skHex.length !== 64)
      throw new Error(`nonceGen: skHex 32-byte hex (64 karakter) gerekli, ${skHex?.length} karakter geldi`);
    if (typeof pkHex !== 'string' || pkHex.length !== 66)
      throw new Error(`nonceGen: pkHex 33-byte hex (66 karakter) gerekli, ${pkHex?.length} karakter geldi`);
    if (typeof msgHex !== 'string' || msgHex.length !== 64)
      throw new Error(`nonceGen: msgHex 32-byte hex (64 karakter) gerekli, ${msgHex?.length} karakter geldi`);

    const skBytes  = hexToBytes(skHex);
    const pkBytes  = hexToBytes(pkHex);
    const msgBytes = hexToBytes(msgHex);
    const rand     = crypto.getRandomValues(new Uint8Array(32));

    const k1h = await taggedHash('MuSig/nonce',
      concatBytes(rand, skBytes, pkBytes, msgBytes, new Uint8Array([0])));
    const k2h = await taggedHash('MuSig/nonce',
      concatBytes(rand, skBytes, pkBytes, msgBytes, new Uint8Array([1])));

    const k1 = bytesToBigint(k1h) % N;
    const k2 = bytesToBigint(k2h) % N;
    const R1 = pointMul(k1, G);
    const R2 = pointMul(k2, G);

    return {
      secretNonce: { k1, k2 },     // YALNIZCA tarayıcıda tut!
      pubNonce: {
        r1: bytesToHex(pointToBytes(R1)),
        r2: bytesToHex(pointToBytes(R2)),
      },
    };
  }

  // ── BIP-327 Kısmi İmzalama ────────────────────────────────────────────────

  /**
   * qEvenY: whether the aggregate public key Q (from key_aggregation) has even Y.
   * Backend stores this as session.agg_q_even_y.
   * When Q has odd Y, each participant's private key must be negated (d → N - d)
   * so that the signing corresponds to lift_x(Q.x) used by Schnorr verification.
   * BUG FIX: previously Q was always reconstructed with even-Y here, so this
   * negation never triggered — causing ~50% signature failures.
   */
  async function partialSign(secretNonce, skHex, coeffBigint, aggXonlyHex, aggNonce, msgHex, qEvenY = true) {
    if (!secretNonce || typeof secretNonce.k1 !== 'bigint' || typeof secretNonce.k2 !== 'bigint')
      throw new Error('partialSign: secretNonce {k1, k2} BigInt çifti gerekli');
    if (typeof skHex !== 'string' || skHex.length !== 64)
      throw new Error('partialSign: skHex 64 karakter hex gerekli');
    if (typeof aggXonlyHex !== 'string' || aggXonlyHex.length !== 64)
      throw new Error('partialSign: aggXonlyHex 32-byte hex gerekli');
    if (!aggNonce || typeof aggNonce.r1 !== 'string' || typeof aggNonce.r2 !== 'string')
      throw new Error('partialSign: aggNonce {r1, r2} hex string gerekli');
    if (typeof msgHex !== 'string' || msgHex.length !== 64)
      throw new Error('partialSign: msgHex 32-byte hex gerekli');

    const { k1, k2 } = secretNonce;
    const skBytes     = hexToBytes(skHex);
    const msgBytes    = hexToBytes(msgHex);

    // Q'yu x-only'den lift_x ile oluştur (çift y seç — BIP-340)
    const Qx    = bytesToBigint(hexToBytes(aggXonlyHex));
    const Qy_sq = modp(modPow(Qx, 3n, P) + 7n);
    let   Qy    = modPow(Qy_sq, (P + 1n) / 4n, P);
    if (Qy % 2n !== 0n) Qy = P - Qy;
    const Q = { x: Qx, y: Qy };

    const R1 = pointFromBytes(hexToBytes(aggNonce.r1));
    const R2 = pointFromBytes(hexToBytes(aggNonce.r2));

    // b = H_noncecoef(cbytes(R1) ‖ cbytes(R2) ‖ xbytes(Q) ‖ msg)  — BIP-327 §4.2
    // R1, R2: 33-byte compressed (same as Python backend FIX-6 / DEV-6)
    // Q: 32-byte x-only
    const bHash = await taggedHash('MuSig/noncecoef',
      concatBytes(pointToBytes(R1), pointToBytes(R2), xonlyBytes(Q), msgBytes));
    const b = bytesToBigint(bHash) % N;

    const R = pointAdd(R1, pointMul(b, R2));
    if (R === null) throw new Error('partialSign: R = infinity — geçersiz nonce kombinasyonu');

    // e = H_challenge(R.x ‖ Q.x ‖ msg)
    const eHash = await taggedHash('BIP0340/challenge',
      concatBytes(xonlyBytes(R), xonlyBytes(Q), msgBytes));
    const e = bytesToBigint(eHash) % N;

    let d     = bytesToBigint(skBytes);
    let k1eff = k1;
    let k2eff = k2;

    if (!qEvenY)      d     = modn(N - d);
    if (!hasEvenY(R)) { k1eff = modn(N - k1); k2eff = modn(N - k2); }

    const si = modn(k1eff + b * k2eff + e * coeffBigint * d);

    // DEBUG — karşılaştırma için backend değerleriyle eşleştir
    console.group('[partialSign DEBUG]');
    console.log('qEvenY          :', qEvenY);
    console.log('R.x (hex)       :', R.x.toString(16).padStart(64,'0'));
    console.log('R_even_y        :', hasEvenY(R));
    console.log('b (hex)         :', b.toString(16));
    console.log('e (hex)         :', e.toString(16));
    console.log('coeff (hex)     :', coeffBigint === 1n ? '1 (MuSig2* opt)' : coeffBigint.toString(16));
    console.log('d_negated       :', !qEvenY);
    console.log('si (hex)        :', si.toString(16).padStart(64,'0'));
    console.groupEnd();

    return bytesToHex(bigintToBytes(si, 32));
  }

  // ── Yardımcılar ───────────────────────────────────────────────────────────

  function derivePublicKey(skHex) {
    if (typeof skHex !== 'string' || skHex.length !== 64)
      throw new Error('derivePublicKey: 32-byte hex (64 karakter) gerekli');
    const sk = bytesToBigint(hexToBytes(skHex));
    if (sk === 0n || sk >= N) throw new Error('Geçersiz özel anahtar');
    return bytesToHex(pointToBytes(pointMul(sk, G)));
  }

  function generatePrivateKey() {
    let sk;
    do {
      sk = crypto.getRandomValues(new Uint8Array(32));
    } while (bytesToBigint(sk) === 0n || bytesToBigint(sk) >= N);
    return bytesToHex(sk);
  }

  // Güvenli bağlam durumunu frontend'e raporla
  function secureContextStatus() {
    return {
      isSecureContext: typeof window !== 'undefined' && window.isSecureContext,
      hasWebCrypto: _useWebCrypto,
      sha256Backend: _useWebCrypto ? 'WebCrypto' : 'Pure-JS (fallback)',
    };
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  return {
    generatePrivateKey,
    derivePublicKey,
    nonceGen,
    keyAggCoeff,
    partialSign,
    secureContextStatus,
    hexToBytes,
    bytesToHex,
    bytesToBigint,
  };

})();
