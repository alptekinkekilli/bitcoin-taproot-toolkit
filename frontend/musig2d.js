/**
 * musig2d.js — Tarayıcı-Taraflı BIP-327 MuSig2 Dağıtık Kripto
 * =============================================================
 * Özel anahtarlar asla sunucuya gönderilmez.
 * Tüm imzalama işlemleri bu modül ile tarayıcıda gerçekleşir.
 *
 * Kullanılan API'ler:
 *   - native BigInt  : secp256k1 alan aritmetiği
 *   - crypto.subtle  : SHA-256 (WebCrypto)
 *   - crypto.getRandomValues : CSPRNG (nonce üretimi)
 *
 * Tüm async fonksiyonlar Promise döner.
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
      if (P1.y !== P2.y) return null;   // ters noktalar → sonsuz
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
    if (hex.length % 2 !== 0) throw new Error('Tek sayıda hex karakter');
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
    const total = arrays.reduce((s, a) => s + a.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const a of arrays) { out.set(a, offset); offset += a.length; }
    return out;
  }

  function pointToBytes(pt) {
    const buf = new Uint8Array(33);
    buf[0] = hasEvenY(pt) ? 0x02 : 0x03;
    buf.set(bigintToBytes(pt.x, 32), 1);
    return buf;
  }

  function xonlyBytes(pt) {
    return bigintToBytes(pt.x, 32);
  }

  function pointFromBytes(bytes) {
    if (bytes.length !== 33) throw new Error('33 byte gerekli');
    const prefix = bytes[0];
    const x = bytesToBigint(bytes.slice(1));
    const y_sq = modp(modPow(x, 3n, P) + 7n);
    let y = modPow(y_sq, (P + 1n) / 4n, P);
    if (modPow(y, 2n, P) !== y_sq) throw new Error('Nokta eğri üzerinde değil');
    if (y % 2n !== BigInt(prefix - 2)) y = P - y;
    return { x, y };
  }

  // ── WebCrypto SHA-256 ──────────────────────────────────────────────────────

  async function sha256(data) {
    const buf = await crypto.subtle.digest('SHA-256', data);
    return new Uint8Array(buf);
  }

  async function taggedHash(tag, data) {
    const tagBytes = new TextEncoder().encode(tag);
    const tagHash  = await sha256(tagBytes);
    return sha256(concatBytes(tagHash, tagHash, data));
  }

  // ── BIP-327 Anahtar Agregasyon Katsayısı ──────────────────────────────────

  async function keyAggCoeff(pkListSortedHex, pkHex) {
    // pkListSortedHex: backend'den gelen sıralı hex listesi
    const sorted = pkListSortedHex.map(hexToBytes).sort((a, b) => {
      for (let i = 0; i < 33; i++) {
        if (a[i] < b[i]) return -1;
        if (a[i] > b[i]) return 1;
      }
      return 0;
    });
    const L   = await taggedHash('KeyAgg list', concatBytes(...sorted));
    const pkI = hexToBytes(pkHex);
    const h   = await taggedHash('KeyAgg coefficient', concatBytes(L, pkI));
    return bytesToBigint(h) % N;
  }

  // ── BIP-327 Nonce Üretimi ──────────────────────────────────────────────────

  async function nonceGen(skHex, pkHex, msgHex) {
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
      secretNonce: { k1, k2 },          // YALNIZCA tarayıcıda tut!
      pubNonce: {
        r1: bytesToHex(pointToBytes(R1)),
        r2: bytesToHex(pointToBytes(R2)),
      },
    };
  }

  // ── BIP-327 Kısmi İmzalama ────────────────────────────────────────────────

  async function partialSign(secretNonce, skHex, coeffBigint, aggXonlyHex, aggNonce, msgHex) {
    // secretNonce : {k1, k2} — BigInt çifti, nonceGen'den dönen değer
    // skHex       : 32-byte özel anahtar hex
    // coeffBigint : keyAggCoeff'ten dönen BigInt (a_i)
    // aggXonlyHex : 32-byte x-only agrege pubkey hex (backend'den)
    // aggNonce    : {r1: hex33, r2: hex33} (backend'den)
    // msgHex      : 32-byte sighash hex (backend'den)

    const { k1, k2 } = secretNonce;
    const skBytes     = hexToBytes(skHex);
    const msgBytes    = hexToBytes(msgHex);

    // Q'yu x-only'den lift_x ile yeniden oluştur (çift y seç)
    const Qx   = bytesToBigint(hexToBytes(aggXonlyHex));
    const Qy_sq = modp(modPow(Qx, 3n, P) + 7n);
    let   Qy   = modPow(Qy_sq, (P + 1n) / 4n, P);
    if (Qy % 2n !== 0n) Qy = P - Qy;
    const Q = { x: Qx, y: Qy };

    const R1 = pointFromBytes(hexToBytes(aggNonce.r1));
    const R2 = pointFromBytes(hexToBytes(aggNonce.r2));

    // b = H_noncecoef(R1.x ‖ R2.x ‖ Q.x ‖ msg)
    const bInput = concatBytes(xonlyBytes(R1), xonlyBytes(R2), xonlyBytes(Q), msgBytes);
    const bHash  = await taggedHash('MuSig/noncecoef', bInput);
    const b      = bytesToBigint(bHash) % N;

    const R = pointAdd(R1, pointMul(b, R2));

    // e = H_challenge(R.x ‖ Q.x ‖ msg)
    const eHash = await taggedHash('BIP0340/challenge',
      concatBytes(xonlyBytes(R), xonlyBytes(Q), msgBytes));
    const e = bytesToBigint(eHash) % N;

    let d     = bytesToBigint(skBytes);
    let k1eff = k1;
    let k2eff = k2;

    // BIP-340 normalize: Q.y tek ise d negate
    if (!hasEvenY(Q)) d = modn(N - d);
    // R.y tek ise nonce'ları negate
    if (R !== null && !hasEvenY(R)) {
      k1eff = modn(N - k1);
      k2eff = modn(N - k2);
    }

    const si = modn(k1eff + b * k2eff + e * coeffBigint * d);
    return bytesToHex(bigintToBytes(si, 32));
  }

  // ── Yardımcılar ───────────────────────────────────────────────────────────

  function derivePublicKey(skHex) {
    const sk = bytesToBigint(hexToBytes(skHex));
    if (sk === 0n || sk >= N) throw new Error('Geçersiz özel anahtar');
    const pt = pointMul(sk, G);
    return bytesToHex(pointToBytes(pt));
  }

  function generatePrivateKey() {
    // Güvenli rastgele 32-byte özel anahtar üret
    let sk;
    do {
      sk = crypto.getRandomValues(new Uint8Array(32));
    } while (bytesToBigint(sk) === 0n || bytesToBigint(sk) >= N);
    return bytesToHex(sk);
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  return {
    generatePrivateKey,
    derivePublicKey,
    nonceGen,
    keyAggCoeff,
    partialSign,
    // iç yardımcılar (test amaçlı)
    hexToBytes,
    bytesToHex,
    bytesToBigint,
  };

})();
