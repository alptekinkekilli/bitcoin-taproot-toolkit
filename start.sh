#!/usr/bin/env bash
# =============================================================================
# start.sh — Taproot Wallet Backend Başlatıcı
# =============================================================================
# Kullanım:
#   ./start.sh                                    → testnet4, port 8000, semi log
#   ./start.sh testnet4                           → testnet4
#   ./start.sh mainnet                            → mainnet (Bitcoin Core gerekli)
#   ./start.sh mainnet 9000                       → mainnet, port 9000
#   ./start.sh testnet4 8000 semi                 → log seviyesi: min|semi|full
#
# Bitcoin Core Parametreleri (.env dosyasından okunur veya aşağıdaki env ile):
#   USE_CORE_RPC=true
#   BITCOIN_RPCHOST=127.0.0.1
#   BITCOIN_RPCPORT=48332          # testnet4 default: 48332, mainnet: 8332
#   BITCOIN_RPCUSER=kullanici
#   BITCOIN_RPCPASSWORD=sifre
#   BITCOIN_WALLET=taproot-wallet
#
# Port Yapısı (Bitcoin Core):
#   Mainnet   : 8332  (RPC) / 8333  (P2P)
#   Testnet3  : 18332 (RPC) / 18333 (P2P)
#   Testnet4  : 48332 (RPC) / 48333 (P2P)  ← BIP-94, v26+
#   Regtest   : 18443 (RPC) / 18444 (P2P)
#
# Log Seviyeleri:
#   min   → yalnızca WARNING/ERROR (Esplora HTTP istekleri gizli)
#   semi  → INFO + WARNING/ERROR (default, Esplora istekleri gizli)
#   full  → tüm loglar DEBUG dahil (Esplora her GET isteği görünür)
# =============================================================================

set -e

NETWORK="${1:-testnet4}"
# testnet ve testnet4 aynı config dosyasını kullanır
[[ "$NETWORK" == "testnet4" ]] && NETWORK_ENV="testnet" || NETWORK_ENV="$NETWORK"
PORT="${2:-8000}"
LOG_LEVEL="${3:-}"    # boşsa .env'den okunur
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$SCRIPT_DIR"

# Renk kodları
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Ağ Kontrolü ───────────────────────────────────────────────────────────────
if [[ "$NETWORK" != "testnet4" && "$NETWORK" != "testnet" && "$NETWORK" != "mainnet" ]]; then
    echo -e "${RED}Hata:${NC} Geçersiz ağ: '$NETWORK'"
    echo "  Kullanım: ./start.sh [testnet4|mainnet] [port] [min|semi|full]"
    exit 1
fi

# ── .env Dosyası Seç ──────────────────────────────────────────────────────────
ENV_FILE="$BACKEND_DIR/.env.$NETWORK_ENV"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Hata:${NC} $ENV_FILE bulunamadı"
    exit 1
fi

# ── Mainnet Uyarısı ───────────────────────────────────────────────────────────
if [[ "$NETWORK" == "mainnet" ]]; then
    echo -e "${YELLOW}⚠  MAINNET MODU — GERÇEK BİTCOIN${NC}"
    echo ""
fi

# ── Sanal Ortam Aktivasyonu ───────────────────────────────────────────────────
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo -e "${RED}Hata:${NC} Python sanal ortamı bulunamadı: $VENV_DIR/bin/activate"
    echo "  Çözüm: python3 -m venv ~/taproot"
    exit 1
fi
source "$VENV_DIR/bin/activate"

# ── Bağımlılık Kontrolü ───────────────────────────────────────────────────────
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
    echo -e "${YELLOW}Bağımlılıklar yükleniyor...${NC}"
    pip install fastapi uvicorn --quiet
}

# ── Ortam Değişkenleri ────────────────────────────────────────────────────────
set -a
source "$ENV_FILE"
# Komut satırı argümanları .env'i ezer
[[ -n "$LOG_LEVEL" ]] && export LOG_LEVEL="$LOG_LEVEL"
set +a

# ── Başlatma Bilgisi ──────────────────────────────────────────────────────────
echo -e "${GREEN}Taproot Wallet Backend${NC}"
echo -e "  Ağ       : ${BLUE}$NETWORK${NC}"
echo -e "  Port     : ${BLUE}$PORT${NC}"
echo -e "  Log      : ${CYAN}${LOG_LEVEL:-semi}${NC}  (min|semi|full)"
echo -e "  Config   : $ENV_FILE"
echo ""

if [[ "${USE_CORE_RPC:-false}" == "true" ]]; then
    _HOST="${BITCOIN_RPCHOST:-127.0.0.1}"
    _PORT="${BITCOIN_RPCPORT:-8332}"
    _WALLET="${BITCOIN_WALLET:-taproot-wallet}"
    echo -e "  ${GREEN}Bitcoin Core RPC${NC}"
    echo -e "    Host   : $_HOST:$_PORT"
    echo -e "    User   : ${BITCOIN_RPCUSER:-(not set)}"
    echo -e "    Wallet : $_WALLET"
else
    echo -e "  ${CYAN}Esplora API${NC} (mempool.space — Bitcoin Core bağlı değil)"
fi
echo ""
echo -e "  Arayüz : ${BLUE}http://localhost:$PORT${NC}"
echo -e "  API    : ${BLUE}http://localhost:$PORT/docs${NC}"
echo ""

# ── Bitcoin Core Ön Kontroller (USE_CORE_RPC=true ise) ───────────────────────
if [[ "${USE_CORE_RPC:-false}" == "true" ]]; then
    _HOST="${BITCOIN_RPCHOST:-127.0.0.1}"
    _PORT="${BITCOIN_RPCPORT:-48332}"
    _USER="${BITCOIN_RPCUSER:-}"
    _PASS="${BITCOIN_RPCPASSWORD:-}"
    _WALLET="${BITCOIN_WALLET:-}"

    # 1. Port erişilebilirlik kontrolü
    if ! nc -z -w2 "$_HOST" "$_PORT" 2>/dev/null; then
        echo -e "${YELLOW}⚠  UYARI:${NC} Bitcoin Core $_HOST:$_PORT portunda erişilemiyor."
        echo -e "          Esplora fallback (mempool.space) kullanılacak."
    else
        # 2. RPC kimlik doğrulama + ağ uyumu kontrolü
        # Bash dizisi — özel karakter içeren şifrelerde word-splitting/history expansion sorununu önler
        _CURL_CREDS=()
        [[ -n "$_USER" ]] && _CURL_CREDS=(--user "${_USER}:${_PASS}")

        _RPC_RESULT=$(curl -s --max-time 5 "${_CURL_CREDS[@]}" \
            -X POST -H "Content-Type: application/json" \
            --data '{"jsonrpc":"2.0","id":1,"method":"getblockchaininfo","params":[]}' \
            "http://${_HOST}:${_PORT}/" 2>/dev/null)

        # result null değil VE error yok → başarılı (jsonrpc 1.1 null result ile hata ayrımı)
        if echo "$_RPC_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('result') is not None and not d.get('error') else 1)" 2>/dev/null; then
            _CHAIN=$(echo "$_RPC_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['chain'])" 2>/dev/null)
            _PRUNED=$(echo "$_RPC_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'].get('pruned','false'))" 2>/dev/null)

            # 4. Ağ uyumu kontrolü
            _EXPECTED_CHAIN=""
            case "$NETWORK" in
                mainnet)  _EXPECTED_CHAIN="main" ;;
                testnet4) _EXPECTED_CHAIN="testnet4" ;;
                testnet)  _EXPECTED_CHAIN="test" ;;
            esac
            if [[ -n "$_EXPECTED_CHAIN" && "$_CHAIN" != "$_EXPECTED_CHAIN" ]]; then
                echo -e "${YELLOW}⚠  UYARI:${NC} Ağ uyumsuzluğu — Core '$_CHAIN' zincirinde çalışıyor, config '$NETWORK' diyor."
                echo -e "          Doğru node'a bağlandığınızı kontrol edin."
            fi

            # 3. Wallet varlık ve yüklenme kontrolü
            if [[ -n "$_WALLET" ]]; then
                _WALLETS_RESULT=$(curl -s --max-time 5 "${_CURL_CREDS[@]}" \
                    -X POST -H "Content-Type: application/json" \
                    --data '{"jsonrpc":"2.0","id":2,"method":"listwallets","params":[]}' \
                    "http://${_HOST}:${_PORT}/" 2>/dev/null)
                _WALLET_LOADED=$(echo "$_WALLETS_RESULT" | python3 -c \
                    "import sys,json; wl=json.load(sys.stdin).get('result',[]); print('yes' if '$_WALLET' in wl else 'no')" 2>/dev/null)
                if [[ "$_WALLET_LOADED" != "yes" ]]; then
                    echo -e "${YELLOW}⚠  UYARI:${NC} '$_WALLET' wallet Bitcoin Core'da yüklü değil."
                    echo -e "          listunspent başarısız olacak — scantxoutset/Esplora fallback devreye girecek."
                    echo -e "          Çözüm: bitcoin-cli -rpcport=$_PORT createwallet '$_WALLET' false true '' false true"
                fi
            fi

            # 5. Prune modu uyarısı
            if [[ "$_PRUNED" == "True" || "$_PRUNED" == "true" ]]; then
                echo -e "${CYAN}ℹ  Bilgi:${NC} Node pruned modda çalışıyor. scantxoutset desteklenir ancak yavaş olabilir."
            fi

        else
            echo -e "${YELLOW}⚠  UYARI:${NC} Bitcoin Core RPC kimlik doğrulaması başarısız."
            echo -e "          BITCOIN_RPCUSER / BITCOIN_RPCPASSWORD değerlerini kontrol edin."
            echo -e "          Esplora fallback kullanılacak."
        fi
    fi
    echo ""
fi

# ── Sunucu Başlat ─────────────────────────────────────────────────────────────
cd "$BACKEND_DIR"
exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload \
    --reload-dir "$BACKEND_DIR" \
    --reload-dir "$SCRIPT_DIR/src"
