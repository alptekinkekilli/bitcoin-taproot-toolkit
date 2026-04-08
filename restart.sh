#!/usr/bin/env bash
# =============================================================================
# restart.sh — Bitcoin Core + Taproot Wallet yeniden başlatma
# =============================================================================
# Kullanım:
#   ./restart.sh           → her ikisini yeniden başlat
#   ./restart.sh bitcoin   → yalnızca Bitcoin Core
#   ./restart.sh wallet    → yalnızca Taproot Wallet backend
#   ./restart.sh stop      → her ikisini durdur
#   ./restart.sh status    → durum göster
# =============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

CMD="${1:-both}"

status_line() {
    local svc="$1"
    local st
    st=$(systemctl --user is-active "$svc" 2>/dev/null || echo "inactive")
    case "$st" in
        active)   echo -e "  $svc: ${GREEN}● çalışıyor${NC}" ;;
        inactive) echo -e "  $svc: ${RED}○ durdu${NC}" ;;
        failed)   echo -e "  $svc: ${RED}✗ hata${NC}" ;;
        *)        echo -e "  $svc: ${YELLOW}? $st${NC}" ;;
    esac
}

show_status() {
    echo -e "\n${CYAN}Servis Durumu:${NC}"
    status_line "bitcoind-testnet4"
    status_line "taproot-wallet"

    echo ""
    # Bitcoin Core blok sayısı
    if systemctl --user is-active --quiet bitcoind-testnet4 2>/dev/null; then
        BLOCKS=$(bitcoin-core.cli -testnet4 getblockcount 2>/dev/null || echo "?")
        echo -e "  Bitcoin Core blok: ${GREEN}$BLOCKS${NC}"
    fi
    # Backend sağlık kontrolü
    if systemctl --user is-active --quiet taproot-wallet 2>/dev/null; then
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/wallet/list 2>/dev/null || echo "000")
        if [[ "$HTTP" == "200" ]]; then
            echo -e "  Taproot Backend: ${GREEN}HTTP $HTTP ✓${NC}"
        else
            echo -e "  Taproot Backend: ${RED}HTTP $HTTP${NC}"
        fi
    fi
    echo ""
}

restart_bitcoin() {
    echo -e "${YELLOW}Bitcoin Core yeniden başlatılıyor...${NC}"
    systemctl --user restart bitcoind-testnet4
    sleep 3
    status_line "bitcoind-testnet4"
}

restart_wallet() {
    echo -e "${YELLOW}Taproot Wallet backend yeniden başlatılıyor...${NC}"
    systemctl --user restart taproot-wallet
    sleep 2
    status_line "taproot-wallet"
}

stop_all() {
    echo -e "${YELLOW}Servisler durduruluyor...${NC}"
    systemctl --user stop taproot-wallet 2>/dev/null || true
    systemctl --user stop bitcoind-testnet4 2>/dev/null || true
    echo -e "${GREEN}Durduruldu.${NC}"
}

case "$CMD" in
    bitcoin|core|btc)
        restart_bitcoin
        ;;
    wallet|taproot|backend)
        restart_wallet
        ;;
    stop|dur)
        stop_all
        ;;
    status|durum)
        show_status
        ;;
    both|*)
        restart_bitcoin
        echo -e "${YELLOW}Bitcoin Core RPC hazır olana bekleniyor (5s)...${NC}"
        sleep 5
        restart_wallet
        show_status
        ;;
esac
