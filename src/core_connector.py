"""
core_connector.py — Bitcoin Core v26+ JSON-RPC Arayüzü
=======================================================

Mimari Karar: Neden Doğrudan RPC?
----------------------------------
Esplora (mempool.space) üçüncü taraf bir servis olup şu sınırlılıkları taşır:
  - Ağ kesintisinde kullanılamaz
  - UTXO seti taraması sınırlı (scantxoutset desteksiz)
  - Ham TX yayını üzerinde kontrol yok
  - Rate limiting / API değişiklikleri riski

Bitcoin Core v26+ RPC ile:
  ✓ Tam UTXO set erişimi (scantxoutset, listunspent)
  ✓ Mempool durumu gerçek zamanlı (getrawmempool)
  ✓ Descriptor wallet zorunlu — importprivkey/importpubkey devre dışı
  ✓ Testnet4 desteği (v26+, port 48332)

Port Yapısı:
    Mainnet   : 8332  (RPC) / 8333  (P2P)
    Testnet3  : 18332 (RPC) / 18333 (P2P)   ← v25 ve öncesi
    Testnet4  : 48332 (RPC) / 48333 (P2P)   ← v26+ (BIP-94)
    Regtest   : 18443 (RPC) / 18444 (P2P)

Bitcoin Core v26 Kırıcı Değişiklikler:
    - Legacy wallet (importprivkey, importpubkey, addmultisigaddress) → DEPRECATED
    - Yeni cüzdan oluşturmak için: createwallet(..., descriptors=True) ZORUNLU
    - importdescriptors ile tr(KEY) formatı kullanılmalı

Kimlik Doğrulama:
    Öncelik sırası:
    1. rpcauth (bitcoin.conf içinde hashlenmiş kimlik)
    2. rpcuser/rpcpassword
    3. .cookie dosyası (~/.bitcoin/testnet3/.cookie)

Kullanım:
    rpc = CoreConnector(network="testnet", rpcuser="user", rpcpassword="pass")
    info = rpc.get_blockchain_info()
    utxos = rpc.list_unspent(addresses=["tb1p..."])
"""

import json
import base64
import hashlib
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field


# ── Port ve Dizin Sabitleri ────────────────────────────────────────────────────

NETWORK_CONFIG = {
    "mainnet": {
        "rpc_port": 8332,
        "p2p_port": 8333,
        "cookie_path": os.path.expanduser("~/.bitcoin/.cookie"),
        "chain": "main",
        "hrp": "bc",
    },
    "testnet": {
        "rpc_port": 18332,
        "p2p_port": 18333,
        "cookie_path": os.path.expanduser("~/.bitcoin/testnet3/.cookie"),
        "chain": "test",
        "hrp": "tb",
    },
    "testnet4": {
        # BIP-94 — v26.0+ ile gelen yeni testnet
        # Testnet3'teki "block storm" saldırısını önlemek için tasarlandı
        "rpc_port": 48332,
        "p2p_port": 48333,
        "cookie_path": os.path.expanduser("~/.bitcoin/testnet4/.cookie"),
        "chain": "testnet4",
        "hrp": "tb",
    },
    "regtest": {
        "rpc_port": 18443,
        "p2p_port": 18444,
        "cookie_path": os.path.expanduser("~/.bitcoin/regtest/.cookie"),
        "chain": "regtest",
        "hrp": "bcrt",
    },
}


# ── Hata Sınıfları ─────────────────────────────────────────────────────────────

class RPCError(Exception):
    """Bitcoin Core RPC hata kodu ile birlikte fırlatılır."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"RPC Hata {code}: {message}")

class RPCConnectionError(Exception):
    """Bağlantı kurulamadığında fırlatılır (düğüm kapalı, port yanlış)."""
    pass

class LegacyMethodError(RPCError):
    """
    v26+ ortamında legacy metod (importprivkey vb.) çağrıldığında fırlatılır.

    Neden bu ayrı sınıf?
    --------------------
    Bitcoin Core v26, descriptor olmayan cüzdanlarda bu metodları devre dışı bıraktı.
    Uygulamanın legacy çağrıları tespit edip importdescriptors'a yönlendirmesi için
    bu özel hata tipi gerekli.

    Hata kodu: -32601 (Method not found) veya -4 (Wallet error)
    """
    LEGACY_METHODS = frozenset({
        "importprivkey",
        "importpubkey",
        "importaddress",
        "addmultisigaddress",
        "createmultisig",        # v26+'da sadece descriptor ile
        "importwallet",
        "dumpwallet",
        "dumpprivkey",
    })

    @classmethod
    def is_legacy(cls, method: str) -> bool:
        return method in cls.LEGACY_METHODS


# ── CoreConnector ─────────────────────────────────────────────────────────────

class CoreConnector:
    """
    Bitcoin Core v26+ ile tam uyumlu JSON-RPC istemcisi.

    Girdi → Mekanizma → Çıktı Zinciri:
    ────────────────────────────────────
    Kullanıcı çağrısı (örn. list_unspent)
        │
        ▼
    Kimlik doğrulama (cookie / user+pass)
        │
        ▼
    HTTP POST → http://127.0.0.1:{port}/
    Content-Type: application/json
    Authorization: Basic base64(user:pass)
        │
        ▼
    Bitcoin Core (bitcoin-cli -rpcport=18332)
        │
        ▼
    JSON yanıt {"result": ..., "error": null}
        │
        ▼
    Python dict/list — çağırana döner

    Descriptor Wallet Uyumu:
    ─────────────────────────
    v26+ ortamında Core, yeni cüzdanları varsayılan olarak descriptor tabanlı
    oluşturur. importprivkey gibi eski metodlar artık -32601 hatası döner.
    Bu sınıf legacy metod çağrısı algılandığında LegacyMethodError fırlatarak
    uygulamayı importdescriptors'a yönlendirir.

    Argümanlar:
        network     : "mainnet" | "testnet" | "testnet4" | "regtest"
        rpcuser     : bitcoin.conf'daki rpcuser (cookie yoksa)
        rpcpassword : bitcoin.conf'daki rpcpassword
        rpchost     : Düğüm adresi (varsayılan: 127.0.0.1)
        rpcport     : Özel port (None → network'e göre otomatik)
        wallet_name : Aktif cüzdan adı (multi-wallet setup için)
        timeout_sec : HTTP zaman aşımı
    """

    def __init__(
        self,
        network: str = "testnet",
        rpcuser: Optional[str] = None,
        rpcpassword: Optional[str] = None,
        rpchost: str = "127.0.0.1",
        rpcport: Optional[int] = None,
        wallet_name: Optional[str] = None,
        timeout_sec: int = 15,
    ):
        if network not in NETWORK_CONFIG:
            raise ValueError(f"Geçersiz ağ: {network}. Seçenekler: {list(NETWORK_CONFIG)}")

        self.network = network
        self.config = NETWORK_CONFIG[network]
        self.rpchost = rpchost
        self.rpcport = rpcport or self.config["rpc_port"]
        self.wallet_name = wallet_name
        self.timeout_sec = timeout_sec
        self._request_id = 0

        # Kimlik doğrulama — cookie dosyası öncelikli
        if rpcuser and rpcpassword:
            self._auth = (rpcuser, rpcpassword)
        else:
            self._auth = self._read_cookie()

    # ── Kimlik Doğrulama ──────────────────────────────────────────────────────

    def _read_cookie(self) -> Tuple[str, str]:
        """
        Bitcoin Core'un otomatik oluşturduğu .cookie dosyasını okur.

        .cookie formatı: "__cookie__:<random_hex_password>"
        Her düğüm başlangıcında yenilenir.
        Güvenlik: Dosya izinleri sadece bitcoin kullanıcısına açık olmalı.

        Döner:
            (username, password) — tipik olarak ("__cookie__", "<hex>")
        """
        cookie_path = self.config["cookie_path"]
        if os.path.exists(cookie_path):
            with open(cookie_path, "r") as f:
                content = f.read().strip()
            user, pw = content.split(":", 1)
            return (user, pw)

        # Fallback: ortam değişkenleri
        user = os.environ.get("BITCOIN_RPCUSER", "bitcoin")
        pw   = os.environ.get("BITCOIN_RPCPASSWORD", "")
        if pw:
            return (user, pw)

        raise RPCConnectionError(
            f"RPC kimlik bilgisi bulunamadı.\n"
            f"  Cookie: {cookie_path}\n"
            f"  Seçenekler:\n"
            f"    1. Bitcoin Core'u başlat (cookie otomatik oluşur)\n"
            f"    2. CoreConnector(rpcuser='...', rpcpassword='...')\n"
            f"    3. BITCOIN_RPCUSER / BITCOIN_RPCPASSWORD ortam değişkeni"
        )

    # ── HTTP JSON-RPC Çekirdeği ───────────────────────────────────────────────

    def _build_url(self) -> str:
        """Aktif cüzdana özgü RPC URL'i döner."""
        base = f"http://{self.rpchost}:{self.rpcport}"
        if self.wallet_name:
            return f"{base}/wallet/{self.wallet_name}"
        return base

    def call(self, method: str, *params) -> Any:
        """
        Ham JSON-RPC çağrısı.

        Girdi → Mekanizma → Çıktı:
            method, *params
                │
                ▼
            {"jsonrpc":"2.0","id":N,"method":method,"params":[...]}
                │
                ▼
            HTTP POST (Basic Auth)
                │
                ▼
            {"result": ..., "error": null}  veya  {"result": null, "error": {...}}
                │
                ├─ error != null  → RPCError / LegacyMethodError
                └─ result         → döner

        Legacy Metod Koruması:
            importprivkey, importpubkey gibi v26+'da desteklenmeyen metodlar
            çağrıldığında LegacyMethodError fırlatılır. Bu, uygulamanın
            otomatik olarak importdescriptors'a yönlenmesini sağlar.

        Argümanlar:
            method : RPC metod adı (ör: "listunspent")
            params : Pozisyonel parametreler

        Döner:
            JSON "result" alanı (dict, list, str, int, bool, None)

        Fırlatır:
            LegacyMethodError : Artık desteklenmeyen legacy metod çağrısı
            RPCError          : Core'dan gelen hata (kod + mesaj)
            RPCConnectionError: Bağlantı kurulamadı
        """
        # Legacy metod erken tespiti
        if LegacyMethodError.is_legacy(method):
            raise LegacyMethodError(
                code=-32601,
                message=(
                    f"'{method}' Bitcoin Core v26+ descriptor cüzdanlarında desteklenmez.\n"
                    f"  Modernize: DescriptorWallet.import_taproot_key() kullanın.\n"
                    f"  Referans: https://github.com/bitcoin/bitcoin/blob/master/doc/descriptors.md"
                )
            )

        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": list(params),
        }).encode("utf-8")

        user, pw = self._auth
        auth_header = base64.b64encode(f"{user}:{pw}".encode()).decode()

        req = urllib.request.Request(
            self._build_url(),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth_header}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 500 genellikle RPC hatasını taşır
            try:
                data = json.loads(e.read())
            except Exception:
                raise RPCConnectionError(f"HTTP {e.code}: {e.reason}")
        except OSError as e:
            raise RPCConnectionError(
                f"Bitcoin Core'a bağlanılamadı ({self.rpchost}:{self.rpcport})\n"
                f"  Hata: {e}\n"
                f"  Düğüm çalışıyor mu? bitcoin-cli -testnet getblockchaininfo"
            )

        if data.get("error"):
            err = data["error"]
            code = err.get("code", -1)
            msg  = err.get("message", str(err))
            if code == -32601:
                raise LegacyMethodError(code, msg)
            raise RPCError(code, msg)

        return data["result"]

    # ── Sağlık ve Durum ───────────────────────────────────────────────────────

    def health_check(self) -> Dict:
        """
        getblockchaininfo — bağlantı ve senkronizasyon durumu.

        Döner:
            {
                "chain": "test",
                "blocks": 2850000,
                "headers": 2850000,
                "bestblockhash": "...",
                "verificationprogress": 0.9999...,
                "pruned": false,
                ...
            }

        Entegrasyon Notu:
            "chain" alanı beklenen ağla eşleşmeli.
            verificationprogress < 0.999 ise düğüm henüz senkronize olmamış.
        """
        info = self.call("getblockchaininfo")
        expected_chain = self.config["chain"]
        if info["chain"] != expected_chain:
            raise RPCError(
                -1,
                f"Yanlış ağ! Beklenen: {expected_chain}, Gelen: {info['chain']}"
            )
        return info

    def get_network_info(self) -> Dict:
        """getnetworkinfo — peer bağlantıları ve ağ durumu."""
        return self.call("getnetworkinfo")

    def get_mempool_info(self) -> Dict:
        """getmempoolinfo — mempool boyutu, min fee rate."""
        return self.call("getmempoolinfo")

    # ── UTXO Metodları ────────────────────────────────────────────────────────

    def list_unspent(
        self,
        addresses: Optional[List[str]] = None,
        minconf: int = 1,
        maxconf: int = 9999999,
        query_options: Optional[Dict] = None,
    ) -> List[Dict]:
        """
        listunspent — adrese ait harcanmamış çıktıları listeler.

        Bu metod, cüzdan izleme listesindeki (watch-only) adreslerin UTXO'larını
        döndürür. Descriptor wallet ile import edilmiş adresler için çalışır.

        Taproot UTXO Filtresi:
            scriptPubKey.type == "witness_v1_taproot"
            scriptPubKey.hex başlangıcı == "5120" (OP_1 OP_PUSH32)

        Argümanlar:
            addresses     : Filtre adresleri (None → tüm cüzdan UTXO'ları)
            minconf       : Minimum onay sayısı (0 → unconfirmed dahil)
            maxconf       : Maksimum onay sayısı
            query_options : {"maximumAmount": sat, "minimumSumAmount": sat, ...}

        Döner:
            [
                {
                    "txid": "abc...",
                    "vout": 0,
                    "address": "tb1p...",
                    "amount": 0.0001,          # BTC cinsinden
                    "amountSat": 10000,        # sat (hesaplanmış)
                    "confirmations": 6,
                    "scriptPubKey": "5120...", # P2TR: 5120 + 32B xonly
                    "spendable": true,
                    "solvable": true,
                    "desc": "tr(...)#checksum"
                },
                ...
            ]
        """
        params: List[Any] = [minconf, maxconf]
        if addresses:
            params.append(addresses)
        if query_options:
            params.append([])  # addresses boşsa bile gerekli
            params.append(query_options)

        raw = self.call("listunspent", *params)

        # BTC → satoshi dönüşümü (Core BTC döner, biz sat istiyoruz)
        for u in raw:
            u["amountSat"] = round(u["amount"] * 1e8)

        return raw

    def scan_tx_out_set(
        self,
        descriptors: List[str],
        action: str = "start",
    ) -> Dict:
        """
        scantxoutset — UTXO setini descriptor ile tara (cüzdan gerektirmez).

        scantxoutset, listunspent'ten farklı olarak aktif cüzdan olmadan
        tüm UTXO setini tarar. Büyük izleme listelerinde çok daha verimlidir.

        Girdi → Mekanizma → Çıktı:
            descriptors (örn. ["tr(xonly_hex)"])
                │
                ▼
            Bitcoin Core UTXO set (chainstate/ dizini)
                │  — düğümün tüm UTXO setini scan eder
                ▼
            {
                "success": true,
                "txouts": 90000000,        # UTXO set büyüklüğü
                "height": 2850000,
                "bestblock": "...",
                "unspents": [
                    {
                        "txid": "...",
                        "vout": 0,
                        "scriptPubKey": "5120...",
                        "desc": "tr(...)#checksum",
                        "amount": 0.0001,
                        "height": 2849995
                    }
                ],
                "total_amount": 0.0001
            }

        Performans Notu:
            İlk tarama birkaç dakika sürebilir (UTXO set ~5GB).
            "abort" action ile durdurulabilir.
            Tekrar çalıştırıldığında cache'den hızlı yanıt verir.

        Argümanlar:
            descriptors : tr(xonly_hex) formatında descriptor listesi
            action      : "start" | "abort"

        Döner:
            scantxoutset sonuç dict'i
        """
        scan_objects = [{"desc": d} for d in descriptors]
        result = self.call("scantxoutset", action, scan_objects)

        if result and "unspents" in result:
            for u in result["unspents"]:
                u["amountSat"] = round(u["amount"] * 1e8)

        return result

    # ── Transaction Metodları ─────────────────────────────────────────────────

    def send_raw_transaction(self, tx_hex: str, max_fee_rate: float = 0.10) -> str:
        """
        sendrawtransaction — imzalanmış ham TX'i ağa yayınla.

        Argümanlar:
            tx_hex       : build_tx().hex() çıktısı
            max_fee_rate : BTC/kB cinsinden max ücret (0 → sınırsız, riskli)

        Döner:
            txid (64 karakter hex)

        Yaygın Hatalar:
            -25: Girdi bulunamadı (txid yanlış veya zaten harcanmış)
            -26: Dust çıktısı (<546 sat)
            -27: Zaten mempool'da
        """
        return self.call("sendrawtransaction", tx_hex, max_fee_rate)

    def decode_raw_transaction(self, tx_hex: str) -> Dict:
        """
        decoderawtransaction — ham TX'i insan okunur forma çevir.

        Hata ayıklamada kritik: imza ve scriptpubkey doğrulama için.
        """
        return self.call("decoderawtransaction", tx_hex)

    def get_raw_transaction(self, txid: str, verbose: bool = True) -> Any:
        """
        getrawtransaction — txid'den ham TX al.

        verbose=True → dict (tüm alanlar, onay bilgisi)
        verbose=False → hex string
        """
        return self.call("getrawtransaction", txid, verbose)

    def get_raw_mempool(self, verbose: bool = False) -> Any:
        """getrawmempool — bekleyen işlemler."""
        return self.call("getrawmempool", verbose)

    # ── Ücret Tahmini ─────────────────────────────────────────────────────────

    def estimate_smart_fee(
        self,
        conf_target: int = 6,
        estimate_mode: str = "CONSERVATIVE",
    ) -> Dict:
        """
        estimatesmartfee — hedef blok için sat/vByte ücret tahmini.

        Taproot TX vBytes:
            Input  : ~41 vBytes (vs Legacy P2PKH ~148 vBytes — %72 tasarruf)
            Output : ~43 vBytes
            Header : 10.5 vBytes (segwit marker/flag: 0.5 vByte)

        Argümanlar:
            conf_target  : Hedef onay bloğu sayısı (1=acil, 6=normal, 144=ucuz)
            estimate_mode: "CONSERVATIVE" | "ECONOMICAL"

        Döner:
            {"feerate": 0.00001234, "blocks": 6}
            feerate BTC/kB cinsinden — sat/vByte için × 100000 (1e5)
        """
        result = self.call("estimatesmartfee", conf_target, estimate_mode)
        if "feerate" in result:
            # sat/vByte dönüşümü: BTC/kB × 1e8 / 1000 = sat/byte
            result["sat_per_vbyte"] = round(result["feerate"] * 1e8 / 1000, 2)
        return result

    # ── Cüzdan Metodları ──────────────────────────────────────────────────────

    def create_descriptor_wallet(
        self,
        wallet_name: str,
        disable_private_keys: bool = False,
        blank: bool = True,
    ) -> Dict:
        """
        createwallet — descriptor tabanlı yeni cüzdan.

        v26+ Not: descriptors=True ZORUNLU. False ile legacy cüzdan oluşturma
        artık "deprecated" uyarısı verir, v27+'da tamamen kalkacak.

        Argümanlar:
            wallet_name          : Cüzdan dosya adı
            disable_private_keys : True → watch-only (özel anahtar yok)
            blank                : True → boş başla (descriptor elle eklenir)

        Döner:
            {"name": "...", "warning": ""}
        """
        return self.call(
            "createwallet",
            wallet_name,          # wallet_name
            disable_private_keys, # disable_private_keys
            blank,                # blank
            "",                   # passphrase (boş)
            False,                # avoid_reuse
            True,                 # descriptors=True ← ZORUNLU
        )

    def import_descriptors(self, requests: List[Dict]) -> List[Dict]:
        """
        importdescriptors — descriptor toplu içe aktarma.

        importprivkey / importpubkey'nin v26+ karşılığı.

        Her request şu alanları içerir:
            {
                "desc":      "tr(xonly_hex)#checksum",
                "timestamp": 0,          # 0 = genesis'ten tara
                "label":     "etiket",
                "watchonly": true,       # özel anahtar yoksa
                "active":    false,      # HD türetme zinciri değil
            }

        Döner:
            [{"success": true, "warnings": []}, ...]

        Hata:
            {"success": false, "error": {"code": ..., "message": "..."}}
        """
        return self.call("importdescriptors", requests)

    def load_wallet(self, wallet_name: str) -> Dict:
        """loadwallet — var olan cüzdanı yükle."""
        return self.call("loadwallet", wallet_name)

    def list_wallets(self) -> List[str]:
        """listwallets — yüklü cüzdan adları."""
        return self.call("listwallets")

    # ── Sihirli Metod ─────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"CoreConnector(network={self.network!r}, "
            f"host={self.rpchost}:{self.rpcport}, "
            f"wallet={self.wallet_name!r})"
        )
