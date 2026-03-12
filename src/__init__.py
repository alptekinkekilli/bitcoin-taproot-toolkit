"""
src/ — Bitcoin Core v26+ Entegrasyon Katmanı
=============================================

Taproot Toolkit'in Bitcoin Core RPC arayüzü.
Esplora (mempool.space) yerine yerel tam düğümden veri çeker.

Modüller:
    core_connector   — JSON-RPC bağlantısı, testnet/mainnet port yönetimi
    descriptor_wallet — tr() descriptor oluşturma, importdescriptors sarmalayıcısı
    utxo_manager     — P2TR çıktı seçimi, scantxoutset entegrasyonu
    taproot_signer   — SIGHASH_DEFAULT (0x00) köprüsü, imza doğrulama

Hızlı Başlangıç:
    from src.core_connector import CoreConnector
    from src.descriptor_wallet import DescriptorWallet
    from src.utxo_manager import UTXOManager
    from src.taproot_signer import TaprootSigner

    rpc = CoreConnector(network="testnet")
    rpc.health_check()   # getblockchaininfo → bağlantıyı doğrular
"""

from .core_connector import CoreConnector
from .descriptor_wallet import DescriptorWallet, DescriptorChecksum
from .utxo_manager import UTXOManager, CoreUTXO, CoinSelector
from .taproot_signer import TaprootSigner

__all__ = [
    "CoreConnector",
    "DescriptorWallet",
    "DescriptorChecksum",
    "UTXOManager",
    "CoreUTXO",
    "CoinSelector",
    "TaprootSigner",
]

__version__ = "1.0.0"
