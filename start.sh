#!/usr/bin/env bash
# =============================================================================
# start.sh — Taproot Wallet Backend Başlatıcı
# =============================================================================
# Kullanım:
#   ./start.sh                → testnet4 (default, Bitcoin Core gerekmez)
#   ./start.sh testnet4       → testnet4 (Esplora API)
#   ./start.sh testnet        → testnet4 (testnet alias)
#   ./start.sh mainnet        → mainnet (Bitcoin Core v26+ zorunlu)
#   ./start.sh mainnet 9000   → mainnet, port 9000
#
# Mainnet Gereksinimleri:
#   - Bitcoin Core v26+ çalışıyor olmalı (localhost:8332)
#   - bitcoin.conf: rpcuser, rpcpassword tanımlı
#   - 'taproot-wallet' descriptor cüzdanı oluşturulmuş olmalı
#     (bitcoin-cli createwallet "taproot-wallet" false true "" false true)
# =============================================================================

set -e

NETWORK="${1:-testnet4}"
# testnet ve testnet4 aynı config dosyasını kullanır
[[ "$NETWORK" == "testnet4" ]] && NETWORK="testnet"
PORT="${2:-8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$SCRIPT_DIR"

# Renk kodları
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ── Ağ Kontrolü ───────────────────────────────────────────────────────────────
if [[ "$NETWORK" != "testnet" && "$NETWORK" != "mainnet" ]]; then
    echo -e "${RED}Hata:${NC} Geçersiz ağ: '$NETWORK'"
    echo "  Kullanım: ./start.sh [testnet|mainnet] [port]"
    exit 1
fi

# ── .env Dosyası Seç ──────────────────────────────────────────────────────────
ENV_FILE="$BACKEND_DIR/.env.$NETWORK"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Hata:${NC} $ENV_FILE bulunamadı"
    exit 1
fi

# ── Mainnet Uyarısı ───────────────────────────────────────────────────────────
if [[ "$NETWORK" == "mainnet" ]]; then
    echo -e "${YELLOW}⚠  MAINNET MODU — GERÇEK BİTCOIN${NC}"
    echo -e "   Bitcoin Core v26+ bağlantısı gerekli (localhost:8332)"
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
set +a

# ── Başlatma Bilgisi ──────────────────────────────────────────────────────────
echo -e "${GREEN}Taproot Wallet Backend${NC}"
echo -e "  Ağ     : ${BLUE}$NETWORK${NC}"
echo -e "  Port   : ${BLUE}$PORT${NC}"
echo -e "  Config : $ENV_FILE"
echo -e "  Core   : ${USE_CORE_RPC:-false}"
if [[ "${USE_CORE_RPC:-false}" == "true" ]]; then
    echo -e "  Wallet : ${BITCOIN_WALLET:-taproot-wallet}"
fi
echo ""
echo -e "  Arayüz : ${BLUE}http://localhost:$PORT${NC}"
echo -e "  API    : ${BLUE}http://localhost:$PORT/docs${NC}"
echo ""

# ── Sunucu Başlat ─────────────────────────────────────────────────────────────
cd "$BACKEND_DIR"
exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload \
    --reload-dir "$BACKEND_DIR" \
    --reload-dir "$SCRIPT_DIR/src"
