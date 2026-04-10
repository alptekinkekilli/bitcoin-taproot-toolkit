#!/usr/bin/env bash
# =============================================================================
# install.sh — Taproot Wallet Kurulum Scripti
# =============================================================================
# Desteklenen OS: Ubuntu 22.04+, Debian 12+
#
# Kullanım:
#   curl -sSL https://raw.githubusercontent.com/alptekinkekilli/bitcoin-taproot-toolkit/main/install.sh | bash
#   veya:
#   git clone https://github.com/alptekinkekilli/bitcoin-taproot-toolkit.git ~/taproot
#   cd ~/taproot && bash install.sh
#
# Ne kurar:
#   1. Sistem bağımlılıkları (python3, git, netcat)
#   2. Python sanal ortamı + FastAPI/uvicorn
#   3. Taproot Wallet systemd servisi
#   4. (İsteğe bağlı) Bitcoin Core via snap + testnet4 servisi
# =============================================================================

set -e

# ── Renkler ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${GREEN}✓${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
error()   { echo -e "${RED}✗${NC}  $*"; }
section() { echo -e "\n${BOLD}${BLUE}── $* ──${NC}"; }
ask()     { echo -e "${CYAN}?${NC}  $*"; }

# ── Kurulum Dizini ────────────────────────────────────────────────────────────
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$INSTALL_DIR/backend"
VENV_DIR="$INSTALL_DIR"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║      Taproot Wallet — Kurulum Scripti   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Kurulum dizini: ${CYAN}$INSTALL_DIR${NC}"
echo -e "  Kullanıcı     : ${CYAN}$(whoami)${NC}"
echo ""

# ── Root kontrolü ─────────────────────────────────────────────────────────────
if [[ "$EUID" -eq 0 ]]; then
    error "Bu script root olarak çalıştırılmamalı."
    echo "  Normal kullanıcı ile çalıştırın: bash install.sh"
    exit 1
fi

# ── OS Kontrolü ───────────────────────────────────────────────────────────────
section "Sistem Kontrolü"

if ! command -v apt-get &>/dev/null; then
    warn "apt-get bulunamadı. Bu script Ubuntu/Debian için tasarlandı."
    warn "Devam etmek için manuel bağımlılık kurulumu gerekebilir."
fi

OS_NAME=$(lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2 || echo "Bilinmiyor")
info "İşletim sistemi: $OS_NAME"

# ── Kullanıcı Kararları ───────────────────────────────────────────────────────
section "Kurulum Seçenekleri"

ask "Bitcoin Core (bitcoind) da kurulsun mu? (testnet4 için tam node)"
echo "    [y] Evet — Bitcoin Core snap ile kurulur + testnet4 servisi oluşturulur"
echo "    [n] Hayır — Yalnızca Taproot Wallet backend (Esplora API kullanır)"
echo ""
read -rp "    Seçim [y/N]: " INSTALL_BITCOIN
INSTALL_BITCOIN="${INSTALL_BITCOIN,,}"  # küçük harfe çevir
[[ "$INSTALL_BITCOIN" == "y" || "$INSTALL_BITCOIN" == "yes" ]] && INSTALL_BITCOIN=true || INSTALL_BITCOIN=false

echo ""
ask "Ağ seçin:"
echo "    [1] testnet4 (önerilen — test ağı, gerçek BTC harcamaz)"
echo "    [2] mainnet  (gerçek Bitcoin — dikkatli kullanın)"
echo ""
read -rp "    Seçim [1/2]: " NET_CHOICE
if [[ "$NET_CHOICE" == "2" ]]; then
    NETWORK="mainnet"
    echo -e "  ${YELLOW}⚠  MAINNET seçildi — gerçek Bitcoin kullanacaksınız!${NC}"
else
    NETWORK="testnet4"
fi

echo ""
ask "API portu: [8000]"
read -rp "    Port [8000]: " API_PORT
API_PORT="${API_PORT:-8000}"

echo ""
info "Seçimler:"
echo "    Ağ            : $NETWORK"
echo "    Port          : $API_PORT"
echo "    Bitcoin Core  : $INSTALL_BITCOIN"
echo ""
read -rp "  Devam edilsin mi? [Y/n]: " CONFIRM
[[ "${CONFIRM,,}" == "n" ]] && { echo "İptal edildi."; exit 0; }

# ── Sistem Bağımlılıkları ─────────────────────────────────────────────────────
section "Sistem Bağımlılıkları"

sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl netcat-openbsd \
    2>/dev/null && info "Sistem paketleri kuruldu" || warn "Bazı paketler kurulamadı, devam ediliyor..."

# ── Python Sanal Ortamı ───────────────────────────────────────────────────────
section "Python Sanal Ortamı"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    python3 -m venv "$VENV_DIR"
    info "Sanal ortam oluşturuldu: $VENV_DIR"
else
    info "Sanal ortam zaten mevcut: $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install --quiet --upgrade pip
pip install --quiet -r "$BACKEND_DIR/requirements.txt"
info "Python bağımlılıkları kuruldu (fastapi, uvicorn, pydantic)"

# ── .env Dosyası Yapılandır ───────────────────────────────────────────────────
section "Yapılandırma"

if [[ "$NETWORK" == "testnet4" ]]; then
    ENV_FILE="$BACKEND_DIR/.env.testnet"
else
    ENV_FILE="$BACKEND_DIR/.env.mainnet"
fi

if [ -f "$ENV_FILE" ]; then
    warn ".env dosyası zaten mevcut: $ENV_FILE"
    read -rp "  Üzerine yazılsın mı? [y/N]: " OVERWRITE_ENV
    [[ "${OVERWRITE_ENV,,}" != "y" ]] && { info "Mevcut .env korundu."; SKIP_ENV=true; }
fi

if [[ "${SKIP_ENV:-false}" != "true" ]]; then
    echo ""
    ask "Bitcoin Core RPC kullanılsın mı? (USE_CORE_RPC)"
    echo "    [y] Evet — yerel Bitcoin Core node gerekir"
    echo "    [n] Hayır — Esplora API (mempool.space) kullanılır, node gerekmez"
    read -rp "    Seçim [y/N]: " USE_CORE
    USE_CORE="${USE_CORE,,}"

    if [[ "$USE_CORE" == "y" || "$USE_CORE" == "yes" ]]; then
        USE_CORE_RPC=true
        echo ""
        ask "RPC bağlantı bilgileri:"
        read -rp "    RPC Host [127.0.0.1]: " RPC_HOST
        RPC_HOST="${RPC_HOST:-127.0.0.1}"

        if [[ "$NETWORK" == "mainnet" ]]; then
            DEFAULT_PORT=8332
        else
            DEFAULT_PORT=18332
        fi
        read -rp "    RPC Port [$DEFAULT_PORT]: " RPC_PORT
        RPC_PORT="${RPC_PORT:-$DEFAULT_PORT}"

        read -rp "    RPC Kullanıcı adı: " RPC_USER
        read -srp "    RPC Şifresi: " RPC_PASS
        echo ""
        read -rp "    Wallet adı [taproot-wallet]: " RPC_WALLET
        RPC_WALLET="${RPC_WALLET:-taproot-wallet}"
    else
        USE_CORE_RPC=false
        RPC_HOST="127.0.0.1"
        RPC_PORT=""
        RPC_USER=""
        RPC_PASS=""
        RPC_WALLET=""
    fi

    # .env dosyasını yaz
    cat > "$ENV_FILE" <<ENVEOF
# Taproot Wallet — $NETWORK yapılandırması
# Otomatik oluşturuldu: $(date)
BITCOIN_NETWORK=$NETWORK
USE_CORE_RPC=$USE_CORE_RPC
BITCOIN_RPCHOST=${RPC_HOST:-127.0.0.1}
BITCOIN_RPCPORT=${RPC_PORT}
BITCOIN_RPCUSER=${RPC_USER}
BITCOIN_RPCPASSWORD=${RPC_PASS}
BITCOIN_WALLET=${RPC_WALLET}

# Log seviyesi: min (yalnızca hata), semi (normal), full (tüm HTTP istekleri)
LOG_LEVEL=semi
ENVEOF

    chmod 600 "$ENV_FILE"
    info ".env dosyası oluşturuldu: $ENV_FILE"
fi

# ── Servis Dosyaları ──────────────────────────────────────────────────────────
section "Systemd Servisleri"

mkdir -p "$SYSTEMD_USER_DIR"

# Taproot Wallet servisi
if [[ "$NETWORK" == "testnet4" ]]; then
    NETWORK_ENV="testnet"
else
    NETWORK_ENV="$NETWORK"
fi

cat > "$SYSTEMD_USER_DIR/taproot-wallet.service" <<SVCEOF
[Unit]
Description=Taproot Wallet Backend ($NETWORK)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BACKEND_DIR
ExecStart=$VENV_DIR/bin/uvicorn app:app --host 0.0.0.0 --port $API_PORT
EnvironmentFile=$ENV_FILE
Environment=LOG_LEVEL=semi
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF

info "taproot-wallet.service oluşturuldu"

# ── Bitcoin Core Kurulumu (isteğe bağlı) ──────────────────────────────────────
if [[ "$INSTALL_BITCOIN" == "true" ]]; then
    section "Bitcoin Core Kurulumu"

    # Snap kontrolü
    if ! command -v snap &>/dev/null; then
        sudo apt-get install -y -qq snapd
        sudo systemctl enable snapd --now 2>/dev/null || true
        sleep 3
    fi

    if snap list bitcoin-core &>/dev/null 2>&1; then
        info "Bitcoin Core zaten kurulu: $(snap list bitcoin-core | tail -1 | awk '{print $2}')"
    else
        info "Bitcoin Core snap ile kuruluyor..."
        sudo snap install bitcoin-core
        info "Bitcoin Core kuruldu"
    fi

    # Bitcoin Core veri dizini
    BTC_DATADIR="$HOME/snap/bitcoin-core/common/.bitcoin"
    mkdir -p "$BTC_DATADIR"

    # bitcoin.conf
    BTC_CONF="$BTC_DATADIR/bitcoin.conf"
    if [ ! -f "$BTC_CONF" ]; then
        echo ""
        ask "Bitcoin Core RPC kimlik bilgileri (bitcoin.conf için):"
        read -rp "    RPC Kullanıcı adı [taproot]: " BTC_USER
        BTC_USER="${BTC_USER:-taproot}"
        read -srp "    RPC Şifresi: " BTC_PASS
        echo ""

        cat > "$BTC_CONF" <<CONFEOF
# Bitcoin Core yapılandırması — Taproot Wallet
# Otomatik oluşturuldu: $(date)
[testnet4]
rpcuser=$BTC_USER
rpcpassword=$BTC_PASS
rpcport=18332
server=1
txindex=0
daemon=1

[main]
rpcuser=$BTC_USER
rpcpassword=$BTC_PASS
rpcport=8332
server=1
daemon=1
CONFEOF
        chmod 600 "$BTC_CONF"
        info "bitcoin.conf oluşturuldu: $BTC_CONF"
    else
        warn "bitcoin.conf zaten mevcut, korundu: $BTC_CONF"
    fi

    # bitcoind wrapper scripti
    BITCOIND_START="$INSTALL_DIR/bitcoind-start.sh"
    if [ ! -f "$BITCOIND_START" ]; then
        cat > "$BITCOIND_START" <<'WRAPEOF'
#!/usr/bin/env bash
# bitcoind systemd wrapper — ön planda çalışır (Type=simple)
DATADIR="$HOME/snap/bitcoin-core/common/.bitcoin"
CONF="$DATADIR/bitcoin.conf"
PIDFILE="$DATADIR/testnet4/bitcoind.pid"

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && ! kill -0 "$OLD_PID" 2>/dev/null; then
        rm -f "$PIDFILE"
    fi
fi

exec /snap/bin/bitcoin-core.daemon \
    -testnet4 \
    -nodaemon \
    -conf="$CONF" \
    -datadir="$DATADIR"
WRAPEOF
        chmod +x "$BITCOIND_START"
        info "bitcoind-start.sh oluşturuldu"
    fi

    # bitcoind systemd servisi
    cat > "$SYSTEMD_USER_DIR/bitcoind-testnet4.service" <<BTCSVCEOF
[Unit]
Description=Bitcoin Core Daemon (testnet4)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$BITCOIND_START
ExecStop=/snap/bin/bitcoin-core.cli -testnet4 stop
Restart=on-failure
RestartSec=15
TimeoutStartSec=120
TimeoutStopSec=90
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
BTCSVCEOF

    info "bitcoind-testnet4.service oluşturuldu"

    # taproot-wallet servisini bitcoind'a bağla
    cat > "$SYSTEMD_USER_DIR/taproot-wallet.service" <<SVCEOF2
[Unit]
Description=Taproot Wallet Backend ($NETWORK)
After=network-online.target bitcoind-testnet4.service
Wants=network-online.target
Requires=bitcoind-testnet4.service

[Service]
Type=simple
WorkingDirectory=$BACKEND_DIR
ExecStartPre=/bin/sleep 5
ExecStart=$VENV_DIR/bin/uvicorn app:app --host 0.0.0.0 --port $API_PORT
EnvironmentFile=$ENV_FILE
Environment=LOG_LEVEL=semi
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF2

    info "taproot-wallet.service (bitcoind bağımlılıklı) güncellendi"
fi

# ── Linger + Servis Etkinleştirme ────────────────────────────────────────────
section "Servis Etkinleştirme"

# Linger: kullanıcı logout sonrası servisler çalışmaya devam eder
loginctl enable-linger "$(whoami)" 2>/dev/null && info "Linger etkinleştirildi (logout sonrası servisler çalışır)" || warn "Linger etkinleştirilemedi (sudo gerekebilir)"

systemctl --user daemon-reload

if [[ "$INSTALL_BITCOIN" == "true" ]]; then
    systemctl --user enable bitcoind-testnet4.service
    info "bitcoind-testnet4 servisi etkinleştirildi (başlangıçta otomatik başlar)"
fi

systemctl --user enable taproot-wallet.service
info "taproot-wallet servisi etkinleştirildi (başlangıçta otomatik başlar)"

# ── Başlatma ──────────────────────────────────────────────────────────────────
section "Servisler Başlatılıyor"

if [[ "$INSTALL_BITCOIN" == "true" ]]; then
    echo ""
    ask "Bitcoin Core şimdi başlatılsın mı? (testnet4 blokları indirilecek — saatler alabilir)"
    read -rp "    Başlat [Y/n]: " START_BITCOIN
    if [[ "${START_BITCOIN,,}" != "n" ]]; then
        systemctl --user start bitcoind-testnet4.service
        info "Bitcoin Core başlatıldı (arka planda senkronize oluyor)"
        echo "    İlerleme: journalctl --user -u bitcoind-testnet4 -f"
        echo "    Blok sayısı: /snap/bin/bitcoin-core.cli -testnet4 getblockcount"
        sleep 5
    fi
fi

echo ""
ask "Taproot Wallet şimdi başlatılsın mı?"
read -rp "    Başlat [Y/n]: " START_WALLET
if [[ "${START_WALLET,,}" != "n" ]]; then
    systemctl --user start taproot-wallet.service
    sleep 2
    STATUS=$(systemctl --user is-active taproot-wallet.service 2>/dev/null || echo "bilinmiyor")
    if [[ "$STATUS" == "active" ]]; then
        info "Taproot Wallet çalışıyor!"
    else
        warn "Servis durumu: $STATUS"
        echo "    Log: journalctl --user -u taproot-wallet -n 30"
    fi
fi

# ── Özet ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║           Kurulum Tamamlandı!            ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Arayüz   : ${BLUE}http://localhost:$API_PORT${NC}"
echo -e "  API Docs : ${BLUE}http://localhost:$API_PORT/docs${NC}"
echo -e "  Ağ       : ${CYAN}$NETWORK${NC}"
echo ""
echo -e "  ${BOLD}Yönetim Komutları:${NC}"
echo -e "  Durum    : ${CYAN}systemctl --user status taproot-wallet${NC}"
echo -e "  Log      : ${CYAN}journalctl --user -u taproot-wallet -f${NC}"
echo -e "  Yeniden  : ${CYAN}systemctl --user restart taproot-wallet${NC}"
echo -e "  Durdur   : ${CYAN}systemctl --user stop taproot-wallet${NC}"
if [[ "$INSTALL_BITCOIN" == "true" ]]; then
    echo ""
    echo -e "  Bitcoin Core:"
    echo -e "  Blok     : ${CYAN}/snap/bin/bitcoin-core.cli -testnet4 getblockcount${NC}"
    echo -e "  Log      : ${CYAN}journalctl --user -u bitcoind-testnet4 -f${NC}"
fi
echo ""
echo -e "  Manuel başlatma: ${CYAN}./start.sh $NETWORK $API_PORT${NC}"
echo ""
