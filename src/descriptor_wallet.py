"""
descriptor_wallet.py — Bitcoin Core v26+ Descriptor Yönetimi
=============================================================

Neden Descriptor Wallet Zorunlu?
----------------------------------
Bitcoin Core v22'den itibaren descriptor wallet'lar varsayılan oldu.
v26+'da legacy importprivkey / importpubkey metodları artık
"Method not found" ya da "-4 wallet error" döndürür.

Descriptor Formatları:
    pkh(KEY)              P2PKH  — Legacy (1...)
    sh(wpkh(KEY))         P2SH-P2WPKH — Wrapped Segwit (3...)
    wpkh(KEY)             P2WPKH — Native Segwit (bc1q...)
    tr(KEY)               P2TR key-path only — Taproot (bc1p...)
    tr(KEY,{pk(LEAF)})    P2TR key+script path — Taproot script tree
    tr(KEY,{             P2TR multileaf tree
        {pk(A),pk(B)},
        pk(C)
    })

Taproot Descriptor Seçimi:
    tr(xonly_hex)          → Tek imzacı, key-path spend
    tr(xonly_hex,{         → Schnorr ile MuSig2 key + FROST fallback
        pk(musig(A,B)),
        {pk(A),pk(B)}
    })

Checksum Zorunluluğu:
    importdescriptors çağrısında her descriptor bir #checksum ile bitmelidir.
    Bitcoin Core, yanlış checksum'ı reddeder.
    Algoritma: 8 karakterlik BCH benzeri polynomial checksum.

Güvenlik:
    Descriptor'lar açık anahtar içerir, özel anahtar içermez.
    Özel anahtar içeren descriptor (tr(private_key_wif)) asla paylaşılmamalı.

Kaynak:
    https://github.com/bitcoin/bitcoin/blob/master/doc/descriptors.md
    BIP-380: https://github.com/bitcoin/bips/blob/master/bip-0380.mediawiki
    BIP-386: https://github.com/bitcoin/bips/blob/master/bip-0386.mediawiki (tr())
"""

import hashlib
import struct
import time
from typing import Any, Dict, List, Optional, Tuple


# ── Descriptor Checksum Algoritması ───────────────────────────────────────────
# Kaynak: Bitcoin Core src/script/descriptor.cpp

# Descriptor karakterlerinin kabul edildiği küme
_INPUT_CHARSET = (
    "0123456789()[],'/*abcdefgh@:$%{}"
    "IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~"
    "ijklmnopqrstuvwxyz"
)

# 8 karakterlik checksum çıktısı için karakter kümesi (bech32 ile aynı)
_CHECKSUM_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _descriptor_poly_mod(c: int, val: int) -> int:
    """
    BCH polynomial modulus — descriptor checksum için.

    Bitcoin Core'un descriptor.cpp'den doğrudan alınmış polynomial.
    GF(2^5) üzerinde çalışır, 8 × 5 = 40 bit checksum üretir.
    """
    c0 = c >> 35
    c = ((c & 0x7FFFFFFFF) << 5) ^ val
    if c0 & 1:  c ^= 0xF5DEE51989
    if c0 & 2:  c ^= 0xA9FDCA3312
    if c0 & 4:  c ^= 0x1BAB10E32D
    if c0 & 8:  c ^= 0x3706B1677A
    if c0 & 16: c ^= 0x644D626FFD
    return c


class DescriptorChecksum:
    """
    BIP-380 uyumlu descriptor checksum hesaplayıcısı.

    Kullanım:
        raw   = "tr(68494d7af4c3424d3c5751a47b781c062f6aa67c50d92cb946720d8908e9d300)"
        full  = DescriptorChecksum.append(raw)
        # → "tr(68494d...)#ab12cd34"

        valid = DescriptorChecksum.verify(full)
        # → True / False
    """

    @staticmethod
    def compute(descriptor: str) -> str:
        """
        Descriptor için 8 karakterlik checksum hesaplar.

        Algoritma (Bitcoin Core descriptor.cpp §DescriptorChecksum):
            1. Her karakter INPUT_CHARSET'teki pozisyonuna göre 5-bit gruba ayrılır
            2. Her 3 karakterde bir "class" byte polynomial'a eklenir
            3. 8 × dummy byte ile finalize edilir
            4. Sonuç CHECKSUM_CHARSET'ten 8 karakter olarak kodlanır

        Argümanlar:
            descriptor : Checksum olmadan ham descriptor string

        Döner:
            8 karakterlik checksum string

        Fırlatır:
            ValueError : Geçersiz karakter içeriyorsa
        """
        # # ile başlayan checksum'ı at (varsa)
        desc = descriptor.split("#")[0]

        c = 1
        cls = 0
        clscount = 0

        for ch in desc:
            pos = _INPUT_CHARSET.find(ch)
            if pos < 0:
                raise ValueError(
                    f"Descriptor'da geçersiz karakter: {ch!r}\n"
                    f"  Geçerli karakterler: {_INPUT_CHARSET}"
                )
            c = _descriptor_poly_mod(c, pos & 31)
            cls = cls * 3 + (pos >> 5)
            clscount += 1
            if clscount == 3:
                c = _descriptor_poly_mod(c, cls)
                cls = 0
                clscount = 0

        if clscount > 0:
            c = _descriptor_poly_mod(c, cls)

        for _ in range(8):
            c = _descriptor_poly_mod(c, 0)

        c ^= 1

        return "".join(
            _CHECKSUM_CHARSET[(c >> (5 * (7 - j))) & 31]
            for j in range(8)
        )

    @staticmethod
    def append(descriptor: str) -> str:
        """
        Checksum eklenmiş descriptor döner.

        Girdi  : "tr(68494d...)"
        Çıktı  : "tr(68494d...)#ab12cd34"
        """
        desc = descriptor.split("#")[0]
        return f"{desc}#{DescriptorChecksum.compute(desc)}"

    @staticmethod
    def verify(descriptor: str) -> bool:
        """
        Descriptor'daki checksum'ı doğrular.

        Döner:
            True  → checksum geçerli
            False → checksum yanlış veya eksik
        """
        if "#" not in descriptor:
            return False
        desc, chk = descriptor.rsplit("#", 1)
        return DescriptorChecksum.compute(desc) == chk


# ── Descriptor Oluşturucular ──────────────────────────────────────────────────

class DescriptorWallet:
    """
    Bitcoin Core v26+ için Taproot descriptor üretici ve ithalat yöneticisi.

    Temel İş Akışı (importprivkey → importdescriptors migrasyonu):

    ESKI (Legacy — v26+'da çalışmaz):
        bitcoin-cli importprivkey "cPrivateKeyWIF" "label" false
        bitcoin-cli importpubkey "02xpubkey..." "label" false
        bitcoin-cli addmultisigaddress 2 '["pub1","pub2"]'

    YENİ (Descriptor — v26+ zorunlu):
        descriptor = DescriptorWallet.taproot_key_path("xonly_hex")
        rpc.import_descriptors([DescriptorWallet.make_import_request(descriptor)])

    Taproot Gizlilik Avantajı:
        tr(KEY) kullanıldığında zincir üzerinde yalnızca 32 byte x-only pubkey
        görünür. Script tree (varsa) hiçbir zaman açıklanmaz. Bu nedenle:
          - 2-of-2 MuSig2 ve tek imzacı P2TR adresleri AYIRT EDİLEMEZ
          - Script-path fallback (timelock, multisig) gizli kalır
          - UTXO kümesi analizi imkânsızlaşır
    """

    @staticmethod
    def taproot_key_path(xonly_hex: str) -> str:
        """
        P2TR key-path descriptor (BIP-386 §tr()).

        tr(KEY) — en basit Taproot formu.
        Zincir üzerinde:
            scriptPubKey = OP_1 OP_PUSH32 <tweaked_xonly>
        Witness'da:
            [Schnorr imzası] (tek eleman, 64 veya 65 byte)

        Tweak (BIP-341):
            Q = P + H("TapTweak", P.x ‖ ε)·G   (ε = boş merkle root)
        Bu implementasyonda tweaksız anahtar kullanılıyor (merkle_root=None).
        Bitcoin Core tr() descriptor'ı kendisi tweak hesaplar.

        Argümanlar:
            xonly_hex : 64 karakter hex, x-only 32-byte public key

        Döner:
            Checksum'lı descriptor: "tr(<xonly_hex>)#xxxxxxxx"

        Örnek:
            "tr(68494d7af4c3424d3c5751a47b781c062f6aa67c50d92cb946720d8908e9d300)#ab12ef34"
        """
        if len(xonly_hex) != 64:
            raise ValueError(
                f"xonly_hex 64 karakter olmalı (32 byte), alınan: {len(xonly_hex)}"
            )
        desc = f"tr({xonly_hex})"
        return DescriptorChecksum.append(desc)

    @staticmethod
    def taproot_script_path(
        internal_key_hex: str,
        leaf_scripts: List[str],
    ) -> str:
        """
        P2TR script-path descriptor — dahili anahtar + script ağacı.

        Taproot'un en güçlü özelliği: script-path harcama görünmez kalar
        ta ki harcanana kadar. Zincir üzerinde yalnızca tweaked public key görünür.

        tr(INTERNAL_KEY, {TREE}) Formatı:
            {A}           → tek yaprak
            {A,B}         → iki yapraklı ağaç (eşit ağırlık)
            {{A,B},C}     → A ve B daha derin (yüksek olasılıklı path üstte)

        Gizlilik İpucu:
            En sık kullanılan script path ağacın üstüne yerleştirilmeli.
            Daha az witness veri = daha düşük ücret + daha az bilgi sızıntısı.

        Argümanlar:
            internal_key_hex : 64 karakter x-only internal key
            leaf_scripts     : BIP-342 script descriptor'ları
                               Örn: ["pk(xonly_hex)", "pk(xonly_hex_2)"]

        Döner:
            Checksum'lı descriptor

        Örnek (2 yaprak):
            tr(internal_key,{pk(leaf1),pk(leaf2)})#checksum
        """
        if len(internal_key_hex) != 64:
            raise ValueError("internal_key_hex 64 karakter olmalı")

        if not leaf_scripts:
            raise ValueError("En az bir leaf script gerekli")

        # Yaprak listesini { } içine al
        if len(leaf_scripts) == 1:
            tree = leaf_scripts[0]
        else:
            tree = "{" + ",".join(leaf_scripts) + "}"

        desc = f"tr({internal_key_hex},{{{tree}}})"
        return DescriptorChecksum.append(desc)

    @staticmethod
    def taproot_musig2(
        participant_xonly_keys: List[str],
        network: str = "testnet",
    ) -> str:
        """
        MuSig2 n-of-n Taproot descriptor — BIP-327 + BIP-386 kombinasyonu.

        musig(key1, key2, ...) ifadesi, Bitcoin Core'un descriptor dilinde
        henüz tam desteklenmez (v26'da experimental). Alternatif yaklaşım:
        Birleşik (aggregate) x-only anahtar hesaplanır ve tr() ile kullanılır.

        Bu fonksiyon BIP-327 key aggregation'dan elde edilen aggregate anahtarı
        alarak standart tr() descriptor üretir. MuSig2 koordinasyonu uygulama
        katmanında gerçekleşir; Core yalnızca aggregate P2TR adresini görür.

        Gizlilik Avantajı:
            N katılımcıdan oluşan MuSig2, zincirde tek-imzacıdan AYIRT EDİLEMEZ.
            Katılımcı sayısı, kimlik bilgisi veya eşik değer zincirde görünmez.

        Argümanlar:
            participant_xonly_keys : Her katılımcının x-only pubkey hex listesi
            network                : "testnet" | "mainnet"

        Döner:
            tr(aggregate_key)#checksum — tek descriptor

        Not:
            Aggregate anahtar hesabı için btc_examples/musig2.py::key_aggregation
            fonksiyonu kullanılmalıdır.
        """
        # Bu fonksiyon aggregate pubkey'i dışarıdan alır
        # Gerçek MuSig2 key aggregation btc_examples/musig2.py'de
        raise NotImplementedError(
            "MuSig2 aggregate anahtar gerekli.\n"
            "  from btc_examples.musig2 import key_aggregation\n"
            "  agg_xonly = key_aggregation(pk_list)\n"
            "  descriptor = DescriptorWallet.taproot_key_path(agg_xonly.hex())"
        )

    @staticmethod
    def make_import_request(
        descriptor: str,
        label: str = "",
        timestamp: int = 0,
        watch_only: bool = True,
        active: bool = False,
        internal: bool = False,
    ) -> Dict:
        """
        importdescriptors için istek nesnesi oluşturur.

        importdescriptors API imzası:
            bitcoin-cli importdescriptors '[
                {
                    "desc":      "tr(...)#checksum",
                    "timestamp": 0,
                    "label":     "my_wallet",
                    "watchonly": true,
                    "active":    false,
                    "internal":  false
                }
            ]'

        Alan Açıklamaları:
            timestamp  : Tarama başlangıcı (UNIX timestamp).
                         0 = genesis'ten tara (yavaş ama güvenli)
                         "now" yerine current time kullanmak için int(time.time())
                         Bilinen block height için ilgili blok timestamp'ı kullanın
            watchonly  : True → özel anahtar yok, yalnızca izle
            active     : True → HD wallet türetme zinciri (gap limit dahil)
                         False → tek adres ithalatı
            internal   : True → değişim (change) adresi

        Argümanlar:
            descriptor : Checksum'lı descriptor string
            label      : Adres etiketi (wallet listesinde görünür)
            timestamp  : Tarama başlangıcı (0 = genesis)
            watch_only : Özel anahtar yoksa True
            active     : HD türetme aktif mi
            internal   : Change adresi mi

        Döner:
            importdescriptors'a geçilecek dict

        Fırlatır:
            ValueError : Checksum geçersizse
        """
        if not DescriptorChecksum.verify(descriptor):
            raise ValueError(
                f"Geçersiz descriptor checksum: {descriptor!r}\n"
                f"  Düzeltme: DescriptorChecksum.append(desc) kullanın"
            )

        req: Dict[str, Any] = {
            "desc": descriptor,
            "timestamp": timestamp,
            "watchonly": watch_only,
            "active": active,
        }

        if label:
            req["label"] = label
        if internal:
            req["internal"] = internal

        return req

    @staticmethod
    def import_taproot_key(
        rpc,
        xonly_hex: str,
        label: str = "",
        timestamp: int = 0,
        rescan: bool = False,
    ) -> Dict:
        """
        P2TR adresini Bitcoin Core cüzdanına tek adımda aktar.

        importprivkey / importpubkey → importdescriptors migrasyonu.

        Girdi → Mekanizma → Çıktı:
            xonly_hex (32-byte x-only public key)
                │
                ▼
            tr(xonly_hex)#checksum  descriptor oluştur
                │
                ▼
            make_import_request(...)
                │
                ▼
            rpc.import_descriptors([request])
                │
                ▼
            Bitcoin Core: cüzdana izleme adresi ekle
                │
                ▼
            {"success": True, "warnings": []}

        Argümanlar:
            rpc       : CoreConnector örneği
            xonly_hex : 64 karakter x-only pubkey
            label     : Adres etiketi
            timestamp : Blockchain tarama başlangıcı
            rescan    : timestamp=0 ise tüm zinciri yeniden tara (yavaş)

        Döner:
            importdescriptors yanıt listesi

        Örnek:
            result = DescriptorWallet.import_taproot_key(
                rpc, "68494d7a...", label="taproot-wallet-1"
            )
        """
        descriptor = DescriptorWallet.taproot_key_path(xonly_hex)
        request = DescriptorWallet.make_import_request(
            descriptor=descriptor,
            label=label,
            timestamp=timestamp if not rescan else 0,
            watch_only=True,
        )
        results = rpc.import_descriptors([request])

        # Sonuç kontrolü
        for r in results:
            if not r.get("success"):
                err = r.get("error", {})
                raise RuntimeError(
                    f"importdescriptors başarısız:\n"
                    f"  Kod   : {err.get('code')}\n"
                    f"  Mesaj : {err.get('message')}\n"
                    f"  Descriptor: {descriptor}"
                )

        return results

    @staticmethod
    def bulk_import(
        rpc,
        wallet_entries: List[Dict],
    ) -> List[Dict]:
        """
        Çok sayıda Taproot adresini tek importdescriptors çağrısıyla aktar.

        Bitcoin Core importdescriptors, birden fazla descriptor'ı tek seferde
        işleyebilir. Toplu ithalat, her adres için ayrı rescan yapmaktan
        çok daha verimlidir.

        Argümanlar:
            rpc            : CoreConnector örneği
            wallet_entries : [{"xonly_hex": "...", "label": "...", "timestamp": 0}, ...]

        Döner:
            importdescriptors yanıt listesi

        Örnek:
            entries = [
                {"xonly_hex": "68494d...", "label": "wallet-1"},
                {"xonly_hex": "85f307...", "label": "musig2-session-1"},
            ]
            results = DescriptorWallet.bulk_import(rpc, entries)
        """
        requests = []
        for entry in wallet_entries:
            desc = DescriptorWallet.taproot_key_path(entry["xonly_hex"])
            req  = DescriptorWallet.make_import_request(
                descriptor=desc,
                label=entry.get("label", ""),
                timestamp=entry.get("timestamp", 0),
            )
            requests.append(req)

        return rpc.import_descriptors(requests)
