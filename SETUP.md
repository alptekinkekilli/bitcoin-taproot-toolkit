# Taproot Wallet — Kurulum & Test Kılavuzu

## Hızlı Kurulum (tek komut)

```bash
git clone https://github.com/alptekinkekilli/bitcoin-taproot-toolkit.git taproot
cd taproot
python3 -m venv .
./start.sh
```

Tarayıcı: **http://localhost:8000**

---

## Gereksinimler

| Araç | Versiyon | Not |
|------|----------|-----|
| Python | 3.9+ | `python3 --version` |
| pip | herhangi | venv ile birlikte gelir |
| Git | herhangi | klonlama için |
| İnternet | — | Testnet4 için mempool.space API |

Bitcoin Core **gerekmez** (testnet4 modunda Esplora API kullanılır).

---

## Adım Adım Kurulum

### 1. Repoyu İndir

```bash
git clone https://github.com/alptekinkekilli/bitcoin-taproot-toolkit.git taproot
cd taproot
```

### 2. Python Sanal Ortamı Oluştur

```bash
python3 -m venv .
```

> `bin/` ve `lib/` klasörleri oluşur. Bunlar `.gitignore`'dadır, paylaşılmaz.

### 3. Ortam Dosyasını Kontrol Et

`backend/.env.testnet` dosyası repoda hazır gelir:
```
USE_CORE_RPC=false
BITCOIN_NETWORK=testnet4
```

Değiştirmen gerekmez. Mainnet için `backend/.env.mainnet` oluştur (bkz. `backend/.env.example`).

### 4. Sunucuyu Başlat

```bash
./start.sh              # testnet4 (varsayılan)
./start.sh testnet4     # testnet4 (açık)
./start.sh testnet4 9000  # farklı port
```

İlk çalıştırmada `fastapi` ve `uvicorn` otomatik yüklenir (~5 sn).

### 5. Tarayıcıyı Aç

```
http://localhost:8000
```

---

## Dağıtık MuSig2 — Farklı Bilgisayarlarda Test

### Senaryo

```
Makine A (Koordinatör + Katılımcı 1)   Makine B (Katılımcı 2)
─────────────────────────────────────   ──────────────────────
./start.sh → port 8000                  ./start.sh → port 8000
Tarayıcı: localhost:8000                Tarayıcı: [Makine A IP]:8000
```

Makine B, Makine A'daki **aynı backend'e** bağlanır.
Her iki tarayıcı da farklı katılımcı rolünü oynar; özel anahtarlar kendi tarayıcısında kalır.

---

### Adım Adım Dağıtık MuSig2 Testi

#### Ön Hazırlık

**Her iki makinede de** aynı kurulum:
```bash
git clone https://github.com/alptekinkekilli/bitcoin-taproot-toolkit.git taproot
cd taproot && python3 -m venv . && ./start.sh
```

Makine A'yı koordinatör olarak seçiyoruz.
**Makine A'nın IP adresini öğren:**
```bash
# Linux/macOS:
ip addr show | grep "inet " | grep -v 127.0.0.1
# veya:
hostname -I
```

Makine B, Makine A'nın portuna erişebilmelidir.
Güvenlik duvarı için:
```bash
# Ubuntu/Debian:
sudo ufw allow 8000/tcp
```

---

#### Oturum Akışı (Adım Adım)

##### ADIM 1 — Koordinatör (Makine A): Oturum Oluştur

1. Makine A tarayıcısı → **Dağıtık MuSig2** sekmesi
2. **+ Yeni Oturum** → Etiket: `test-2of2`, N: `2`, Ağ: `Testnet4` → **Oluştur**
3. **Oturum ID**'yi kopyala (örn. `a1b2c3d4`)
4. Bu ID'yi Makine B'ye ilet (mesaj, not vb.)

---

##### ADIM 2 — Makine B: Oturuma Katıl

1. Makine B tarayıcısı → `http://[Makine A IP]:8000`
2. **Dağıtık MuSig2** sekmesi → **↗ Oturuma Katıl**
3. Oturum ID'yi gir → **Katıl**

---

##### ADIM 3 — Her İki Taraf: Özel Anahtar Gir ve Pubkey Kaydet

**Makine A (Katılımcı 1):**
1. Oturum ekranı → Katılımcı Aksiyonu
2. **⚙ Üret** ile yeni özel anahtar üret (veya kendin gir)
3. Katılımcı: **Katılımcı 1** seçili olmalı
4. **Pubkey Kaydet** butonuna tıkla

**Makine B (Katılımcı 2):**
1. **⚙ Üret** ile farklı bir özel anahtar üret
2. Katılımcı: **Katılımcı 2** seç
3. **Pubkey Kaydet**

Her iki pubkey kaydedilince durum `READY_FOR_TX` olur ve MuSig2 adresi görünür.

---

##### ADIM 4 — Koordinatör: MuSig2 Adresine Para Gönder

1. Oturum ekranındaki **agrege adresi** kopyala (örn. `tb1p...`)
2. Bu adrese testnet4 coin gönder:
   - [mempool.space/testnet4/faucet](https://mempool.space/testnet4/) veya
   - Başka bir testnet4 cüzdanından gönder
3. Onay beklenmesi gerekebilir (1 blok, ~10 dk)

---

##### ADIM 5 — Koordinatör: TX Oluştur (Sighash Hesapla)

1. Makine A → Oturum ekranı → **Transaction Oluştur** kartı
2. Alıcı adres, miktar ve ücret gir
3. **Sighash Hesapla** → Durum `COLLECTING_NONCES` olur

---

##### ADIM 6 — Her İki Taraf: Nonce Üret & Gönder

**Makine A** ve **Makine B** sırasıyla (veya eş zamanlı):
1. **↺ Güncelle** ile son durumu çek
2. Özel anahtarın hâlâ girili olduğunu doğrula
3. **Nonce Üret & Gönder** butonuna tıkla

Her iki nonce gelince durum `COLLECTING_SIGS` olur.

---

##### ADIM 7 — Her İki Taraf: Kısmi İmza Hesapla & Gönder

**Makine A** ve **Makine B**:
1. **↺ Güncelle**
2. **Kısmi İmza Üret & Gönder** butonuna tıkla
   (Tarayıcı: secp256k1 + BIP-327 kısmi imza hesabı yapar — ~1 sn)

Her iki imza gelince backend Schnorr doğrulaması yapar ve TX hazırlanır. Durum `SIGNED` olur.

---

##### ADIM 8 — Yayınla

1. Makine A veya B → **⚡ Yayınla**
2. TXID görünür
3. [mempool.space/testnet4](https://mempool.space/testnet4/) üzerinde doğrula

---

### Tek Bilgisayarda Test (2 Sekme)

Gerçek ağ olmadan da test edilebilir:

1. **Sekme 1** → Koordinatör olarak hareket et (Katılımcı 1)
2. **Sekme 2** → Aynı oturum ID ile katıl (Katılımcı 2)
3. Her sekmede farklı özel anahtar üret
4. Adımları sırayla uygula

Özel anahtarlar `localStorage`'da saklanır; sekmeler arası izolasyon tarayıcı politikasına bağlıdır.
Güvenli test için farklı tarayıcı profilleri kullan (örn. Firefox + Chrome).

---

## Sorun Giderme

### `bin/activate` bulunamadı
```bash
python3 -m venv .   # sanal ortamı yeniden oluştur
```

### Port 8000 meşgul
```bash
./start.sh testnet4 9001   # farklı port kullan
```

### Makine B backend'e bağlanamıyor
```bash
# Makine A'da:
sudo ufw allow 8000/tcp
# veya geçici olarak:
python3 -c "import socket; print(socket.gethostbyname(socket.gethostname()))"
```

### `Onaylanmış UTXO yok` hatası
MuSig2 adresine gönderilen coin henüz onaylanmamış. 1 blok (~10 dk) bekle.

### Nonce bulunamadı — `localStorage` temizlenmiş
Sayfayı yenilemeden önce nonce gönderimini tamamla. `localStorage` temizlenirse oturumu sıfırlayıp yeni nonce üretmen gerekir.

---

## Proje Yapısı

```
taproot/
├── backend/
│   └── app.py              # FastAPI REST API
├── frontend/
│   ├── index.html          # Tek sayfa uygulama
│   ├── app.js              # UI mantığı
│   ├── musig2d.js          # Tarayıcı-taraflı BIP-327 kripto
│   └── style.css
├── btc_examples/
│   ├── raw_tx.py           # BIP-341 transaction imzalama
│   └── musig2.py           # MuSig2 / BIP-327 referans impl.
├── src/
│   └── taproot_signer.py
├── start.sh                # Başlatıcı
└── backend/.env.testnet    # Testnet4 config
```

---

## API Uç Noktaları (Dağıtık MuSig2)

| Metod | Yol | Açıklama |
|-------|-----|---------|
| POST | `/api/musig2d/new` | Yeni oturum oluştur |
| GET | `/api/musig2d/list` | Tüm oturumları listele |
| GET | `/api/musig2d/{sid}` | Oturum detayı |
| DELETE | `/api/musig2d/{sid}` | Oturumu sil |
| POST | `/api/musig2d/{sid}/register` | Pubkey kaydet |
| POST | `/api/musig2d/{sid}/build-tx` | TX oluştur / sighash hesapla |
| POST | `/api/musig2d/{sid}/submit-nonce` | Pubnonce gönder |
| POST | `/api/musig2d/{sid}/submit-partial-sig` | Kısmi imza gönder |
| POST | `/api/musig2d/{sid}/broadcast` | TX yayınla |

Tam Swagger dokümantasyonu: **http://localhost:8000/docs**
