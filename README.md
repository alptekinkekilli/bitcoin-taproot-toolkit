# Taproot Wallet

Bitcoin **BIP-340 / BIP-341 / BIP-327** standartlarını sıfır dış bağımlılıkla
implement eden, Sparrow Wallet uyumlu eğitim/prototip amaçlı cüzdan uygulaması.

---

## Mimari

```
taproot/
├── backend/
│   └── app.py          # FastAPI REST API (port 8000)
├── frontend/
│   ├── index.html      # Tek sayfa uygulama
│   ├── app.js          # Wallet UI mantığı
│   └── style.css
├── btc_examples/
│   ├── raw_tx.py       # BIP-341 transaction imzalama (sıfır bağımlılık)
│   └── musig2.py       # MuSig2 / BIP-327 Schnorr multisig
└── src/
    └── taproot_signer.py
```

---

## Özellikler

| Özellik | Açıklama |
|---------|----------|
| P2TR adresi | BIP-341 key-path tweak uygulanmış |
| HD cüzdan | BIP-32 + BIP-86 (`m/86'/coin_type'/0'`) |
| Sparrow uyumu | `tpub` descriptor + `tprv` imzalama anahtarı |
| MuSig2 | n-of-n Schnorr multisig oturumu |
| Testnet4 | Mempool.space Esplora API |
| Bitcoin Core | `importdescriptors` ile izleme |

---

## Sparrow Wallet Entegrasyonu

### Neden HD Yapı?

Sparrow, tek ham private key (WIF) yerine BIP-32 HD türetme yollarıyla çalışır.
Eski mimari (raw sk → doğrudan adres) ile Sparrow'un descriptor'dan türettiği
adresler farklıydı ve transaction'lar görünmüyordu.

### Çözüm: BIP-86 HD Adres Hizalaması

Yeni BIP-341 (`hd: true`) cüzdanlarda:

```
seed_sk  (32 byte rastgele)
    │
    ▼  HMAC-SHA512("Bitcoin seed", seed_sk)
master  (m)
    │
    ├─ m/86'  (purpose, hardened)
    ├─ m/86'/1'  (coin_type=1 testnet, hardened)
    ├─ m/86'/1'/0'  (account, hardened)
    ├─ m/86'/1'/0'/0  (external chain, non-hardened)
    └─ m/86'/1'/0'/0/0  (first receive address, non-hardened)
                │
                ▼  BIP-341 key-path tweak
            P2TR adres  ←── frontend'de görünen = Sparrow Receive adresi
```

Descriptor şablonu (Sparrow testnet4):
```
tr([fingerprint/86h/1h/0h]tpub.../<0;1>/*)#checksum
```

### Cüzdan Türleri

| Flag | Tür | Adres Türetme | İmzalama |
|------|-----|--------------|---------|
| (yok) | Legacy | `sk → P` (tweaksız) | raw sk |
| `bip341: true` | Tek-key BIP-341 | `sk → tweak → Q` | tweaked sk |
| `bip341: true, hd: true` | **HD BIP-341** (yeni) | `seed → m/86'/1'/0'/0/0 → tweak → Q` | HD child → tweaked sk |

### Sparrow'a Import Adımları

1. Frontend → cüzdan satırı → **⬇ Sparrow** → `.descriptor` dosyasını indir
2. Dosyayı aç; `MASTER_TPRV:` satırını kopyala (`tprv8ZgxMBic...`)
3. Sparrow (testnet4 modu) → `File → New Wallet` → cüzdana isim ver
4. Script Type: **Taproot (Single Sig)**
5. Keystore 1 → **Master Private Key (BIP32)** → **Enter Private Key**
6. `MASTER_TPRV` değerini yapıştır
7. Derivation: `m/86'/1'/0'` — otomatik gelir, değiştirme
8. **Import Keystore** → **Apply**

> Sparrow **Receive** sekmesindeki adres = frontend'deki wallet adresiyle aynı.
> Transaction'lar Bitcoin Core testnet4 üzerinden otomatik görünür.

### Yeni Cüzdan Oluşturma

1. Frontend → **Cüzdanlar** sekmesi → **+ Yeni Cüzdan**
2. Etiket gir, ağ: **Testnet4**
3. **Oluştur** — HD seed üretilir, BIP-86 `m/86'/1'/0'/0/0` adresi gösterilir
4. Bu adresi doğrudan kullan veya **⬇ Sparrow** ile Sparrow'a aktar

---

## BIP-341 Key-Path Tweak

```python
# internal key P
d = int.from_bytes(sk, "big")
P = d * G
if P.y % 2 != 0:          # normalise to even y
    d = N - d; P = d * G

# tweak
t = int.from_bytes(tagged_hash("TapTweak", P.x), "big") % N
d_tweaked = (d + t) % N    # signing key
Q = d_tweaked * G           # output key
address = bech32m(Q.x)      # P2TR adres
```

---

## Teknik Notlar

### OpenSSL 3.0 RIPEMD-160 Sorunu

`hashlib.new('ripemd160')` OpenSSL 3.0'da devre dışı bırakıldı.
BIP-32 fingerprint hesabı için sıfırdan Python RIPEMD-160 implementasyonu yazıldı
(`backend/app.py` → `_ripemd160`). Test vektörü: `RIPEMD160(b'') = 9c1185a5...`

### tpub / tprv Version Bytes

| Format | Version Bytes | Kullanım |
|--------|--------------|---------|
| `xpub` | `0x0488B21E` | mainnet extended public key |
| `tpub` | `0x043587CF` | testnet extended public key |
| `xprv` | `0x0488ADE4` | mainnet extended private key |
| `tprv` | `0x04358394` | testnet extended private key |

Sparrow testnet4 modu `xpub`/`xprv` reddetmekte, `tpub`/`tprv` gerektirmektedir.
Bitcoin Core testnet4 da `tr()` descriptor içinde `xpub` yerine `tpub` ister.

### Bitcoin Core Watch-Only

Mevcut adresler için `importdescriptors` ile tek adres izleme:
```bash
curl --user user:pass --data-binary \
  '{"method":"importdescriptors","params":[[{"desc":"addr(tb1p...)#checksum","timestamp":106398}]]}' \
  http://127.0.0.1:18332/
```
> Pruned node'larda `timestamp` olarak `pruneheight` kullanılmalı (genesis'ten tarama mümkün değil).

---

## Güvenlik Uyarıları

- Private key'ler `backend/data/wallets.json` içinde **şifresiz** saklanır
- `backend/data/` `.gitignore`'a eklenmiştir — commit edilmemeli
- Yalnızca **testnet** kullanımı içindir; mainnet'te bağımsız güvenlik denetimi şart
- `bip341-test` gibi eski (tweaksız-olmayan) wallet'lar Sparrow ile uyumlu değildir;
  `hd: true` flag'li yeni wallet'lara geçiş önerilir
